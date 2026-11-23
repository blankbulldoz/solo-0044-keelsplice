"""Rehearse the A-review/B-pickup handoff, damaging bytes only on a standby copy.

The production repository keeps serving certified bytes while a copied
repository goes through storage corruption, blocked pickup, duty-officer
re-review, and recovery by rebuild plus re-certification.
"""

import sqlite3
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from keelsplice.api import create_server

from .walkthrough import call, seed


@contextmanager
def serve(server):
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join()


def copy_repository(repo, standby):
    with closing(sqlite3.connect(repo)) as source, closing(sqlite3.connect(standby)) as target:
        source.backup(target)


def damage_artifact(standby, capture_id):
    with closing(sqlite3.connect(standby)) as conn, conn:
        data = conn.execute('SELECT artifact FROM captures WHERE id=?', (capture_id,)).fetchone()[0]
        damaged = bytearray(data)
        damaged[len(damaged) // 2] ^= 0xFF
        conn.execute('UPDATE captures SET artifact=? WHERE id=?', (bytes(damaged), capture_id))


def prepare(base):
    catalog = call(base, '/sources/microscope/catalog')
    edge = next(edge for edge in catalog['schema']['edges'] if edge['child'] == 'assay')
    body = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
            'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}
    call(base, '/recipes/swatch/versions', body, 201)
    capture = call(base, '/captures', {'name': 'swatch', 'version': 1}, 201)
    endpoint = '/captures/' + capture['id']
    assert call(base, endpoint + '/review', {'station': 'A'}, 409)['error'] == 'fixture_not_certified'
    call(base, endpoint + '/build', {})
    certified = call(base, endpoint + '/certify', {})
    return capture['id'], endpoint, certified['certificate']


def rehearse():
    with TemporaryDirectory(prefix='keelsplice-handoff-') as directory:
        root = Path(directory)
        source = root / 'microscope.sqlite'
        seed(source)
        repo, standby = root / 'repo.sqlite', root / 'standby.sqlite'
        with create_server(('127.0.0.1', 0), repo, {'microscope': source}) as main_server:
            with serve(main_server) as main:
                capture_id, endpoint, certificate = prepare(main)

                # A 工位审核证书：核验通过，结论可转交。
                first = call(main, endpoint + '/review', {'station': 'A'}, 201)
                assert first['verdict'] == 'transferable' and first['status'] == 'current'
                assert first['artifact_sha256'] == certificate['sha256']
                assert call(main, endpoint + '/review', {'station': '  '}, 400)['error'] == 'invalid_station'
                panel = call(main, endpoint + '/reviews')
                assert panel['conclusion'] == {'code': 'transferable', 'transferable': True,
                                               'review': first['id']}

                # 备用副本：A 审核成功后、B 取文件前字节损坏。
                copy_repository(repo, standby)
                damage_artifact(standby, capture_id)
                with create_server(('127.0.0.1', 0), standby, {'microscope': source}) as standby_server:
                    with serve(standby_server) as copy:
                        # B 工位正式取文件仍被阻止。
                        assert call(copy, endpoint + '/database', status=409)['error'] == 'artifact_integrity'

                        # 值班人定位：第一次判断用过的字节与当前实存字节。
                        panel = call(copy, endpoint + '/reviews')
                        assert panel['reviews'][0]['artifact_sha256'] == certificate['sha256']
                        second = call(copy, endpoint + '/review', {'station': 'duty'}, 201)
                        assert second['verdict'] == 'blocked'
                        damaged_sha = second['artifact_sha256']
                        assert damaged_sha != certificate['sha256']
                        panel = call(copy, endpoint + '/reviews')
                        assert [row['artifact_sha256'] for row in panel['reviews'][:2]] == [
                            damaged_sha, certificate['sha256']]
                        assert panel['conclusion']['code'] == 'blocked'

                        # 恢复：重建后只看到历史证书，旧判断结果过期。
                        rebuilt = call(copy, endpoint + '/rebuild', {})
                        assert rebuilt['state'] == 'built' and rebuilt['certificate'] is None
                        panel = call(copy, endpoint + '/reviews')
                        assert panel['conclusion']['code'] == 'historical_only'
                        assert all(row['status'] == 'expired' for row in panel['reviews'])
                        assert call(copy, endpoint + '/database', status=409)['error'] == 'fixture_not_certified'
                        assert call(copy, endpoint + '/review', {'station': 'A'}, 409)['error'] == 'fixture_not_certified'

                        certified = call(copy, endpoint + '/certify', {})
                        assert certified['certificate']['sha256'] == certificate['sha256']
                        panel = call(copy, endpoint + '/reviews')
                        assert panel['conclusion'] == {'code': 'expired', 'transferable': False,
                                                       'review': second['id']}
                        assert [row['current'] for row in panel['certificates']] == [True, False]

                        # 复审通过，B 工位取到与证书一致的字节。
                        third = call(copy, endpoint + '/review', {'station': 'A'}, 201)
                        assert third['verdict'] == 'transferable' and third['status'] == 'current'
                        data, etag = call(copy, endpoint + '/database')
                        assert sha256(data).hexdigest() == etag == certificate['sha256']

                # 演练只动备用副本，生产库交接不受影响。
                data, etag = call(main, endpoint + '/database')
                assert sha256(data).hexdigest() == etag == certificate['sha256']
                panel = call(main, endpoint + '/reviews')
                assert panel['conclusion']['code'] == 'transferable' and len(panel['reviews']) == 1
        print('交接演练通过：A 审核可转交、副本字节损坏后 B 取件被阻止、值班人定位判断所用字节、'
              '重建后历史证书与过期结论、复审恢复交接，生产库未受影响')


if __name__ == '__main__':
    rehearse()
