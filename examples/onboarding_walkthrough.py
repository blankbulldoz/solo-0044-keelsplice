"""Demonstrate the new-source onboarding self-check against a running WAL database.

The scenario reproduces what an administrator actually holds during a migration:

* ``live``        - the running WAL database, registered under the ``microscope`` alias;
* ``alias``       - a symlink whose path follows the original file;
* ``copy-full``   - a directory copy that still carries the un-checkpointed WAL frame;
* ``copy-main``   - a second directory copy that kept only the main database file;
* ``standalone``  - a separately created database with the same schema but different rows.

A committed representative row stays in the WAL while a second connection keeps the
database open, so the difference between "same main file" and "same committed view"
is observable.  After the first audit the live source is updated and checkpointed;
the re-check states which earlier conclusions still apply.
"""

import json
import shutil
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import create_server
from keelsplice.onboarding import (
    DIVERGENT_DATABASE,
    MAIN_FILE_ONLY,
    SAME_DATABASE_COPY,
    SAME_OBJECT,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE substrate (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
INSERT INTO substrate VALUES ('East','Mica',x'0000ff'), ('West','Basalt',x'fe');
"""

# Committed while the database is open; this row only exists in the WAL frame.
WAL_ROW = ('Delta', 'Quartz', b'\x01\x02')
# Committed and checkpointed between the audit and the re-check.
UPDATE_ROW = ('Echo', 'Shale', b'\x03')
PROBE = {'table': 'substrate', 'key': ['Delta', 'Quartz']}


def write_database(path, with_wal_row):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA)
        if with_wal_row:
            conn.execute('INSERT INTO substrate VALUES (?,?,?)', WAL_ROW)


def materialize(root):
    live_dir = root / 'live'
    live_dir.mkdir()
    live = live_dir / 'microscope.sqlite'
    with closing(sqlite3.connect(live)) as conn:
        conn.executescript(SCHEMA)

    # A live connection keeps the writer from checkpointing at close time.
    holder = sqlite3.connect(live)
    holder.execute('PRAGMA journal_mode=WAL')
    holder.execute('PRAGMA wal_autocheckpoint=0')
    with closing(sqlite3.connect(live)) as writer:
        writer.execute('INSERT INTO substrate VALUES (?,?,?)', WAL_ROW)
        writer.commit()
    assert (Path(str(live) + '-wal')).is_file(), 'representative row must remain in the WAL'

    alias_dir = root / 'alias'
    alias_dir.mkdir()
    alias = alias_dir / 'microscope-link.sqlite'
    alias.symlink_to(live)

    full_dir = root / 'copy-full'
    shutil.copytree(live_dir, full_dir)

    main_dir = root / 'copy-main'
    main_dir.mkdir()
    shutil.copy2(live, main_dir / 'microscope.sqlite')

    standalone_dir = root / 'standalone'
    standalone_dir.mkdir()
    standalone = standalone_dir / 'microscope.sqlite'
    write_database(standalone, with_wal_row=False)
    with closing(sqlite3.connect(standalone)) as conn:
        conn.execute("INSERT INTO substrate VALUES ('North','Granite',x'ab')")
        conn.commit()

    return holder, live, {
        'alias-link': str(alias),
        'full-copy': str(full_dir / 'microscope.sqlite'),
        'main-only-copy': str(main_dir / 'microscope.sqlite'),
        'standalone': str(standalone),
    }


def by_label(result):
    return {item['label']: item for item in result['candidates']}


def run():
    def post(base, route, payload):
        request = Request(base + route, json.dumps(payload).encode('utf-8'),
                          {'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    with TemporaryDirectory(prefix='keelsplice-onboarding-') as directory:
        root = Path(directory)
        holder, live, candidates = materialize(root)
        try:
            server = create_server(('127.0.0.1', 0), root / 'state.sqlite',
                                   {'microscope': live})
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                status, baseline = post(base, '/sources/onboarding/check', {
                    'reference': 'microscope', 'candidates': candidates, 'probes': [PROBE]})
                assert status == 200, baseline
                findings = by_label(baseline)

                alias = findings['alias-link']
                assert alias['verdict'] == SAME_OBJECT and alias['same_object'] is True
                assert alias['identity'] == baseline['reference']['identity']
                assert alias['representative_rows_visible'] is True
                assert alias['recommendation'] == 'register_alias'

                full = findings['full-copy']
                assert full['verdict'] == SAME_DATABASE_COPY and full['same_object'] is False
                assert full['main_file_match'] is True and full['sidecars']['wal'] is True
                assert full['representative_rows_visible'] is True
                assert full['recommendation'] == 'register_copy'

                main_only = findings['main-only-copy']
                assert main_only['verdict'] == MAIN_FILE_ONLY
                assert main_only['main_file_match'] is True
                assert main_only['representative_rows_visible'] is False
                assert main_only['recommendation'] == 'do_not_register'

                separate = findings['standalone']
                assert separate['verdict'] == DIVERGENT_DATABASE
                assert separate['representative_rows_visible'] is False
                assert separate['recommendation'] == 'do_not_register'

                print('接入自查基线：')
                for item in baseline['candidates']:
                    print('  {label:<16} {verdict:<18} 主文件一致={main} WAL可见={wal} 建议={rec}'.format(
                        label=item['label'], verdict=item['verdict'],
                        main=item['main_file_match'],
                        wal=item['representative_rows_visible'],
                        rec=item['recommendation']))

                # The source moves on: another committed row, then a checkpoint rewrites the main file.
                with closing(sqlite3.connect(live)) as writer:
                    writer.execute('INSERT INTO substrate VALUES (?,?,?)', UPDATE_ROW)
                    writer.commit()
                    writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')

                status, rechecked = post(base, '/sources/onboarding/recheck',
                                         {'baseline': baseline})
                assert status == 200, rechecked
                again = by_label(rechecked)
                assert rechecked['reference']['changed_since_baseline'] is True
                # The alias follows the live object: identity survives, but the main
                # file bytes moved with the checkpoint, so the recorded evidence drifted.
                assert again['alias-link']['verdict'] == SAME_OBJECT
                assert again['alias-link']['verdict_unchanged'] is True
                assert again['alias-link']['still_applies'] is False
                assert 'main_file_sha256' in again['alias-link']['evidence_changes']
                # The frozen full copy keeps its own WAL frame: it still shows the
                # representative row, but its main file no longer matches the live one.
                assert again['full-copy']['verdict'] == SAME_DATABASE_COPY
                assert again['full-copy']['verdict_unchanged'] is True
                assert again['full-copy']['still_applies'] is False
                assert again['full-copy']['evidence_changes'].get('main_file_match') == {
                    'before': True, 'after': False}
                # With the live main file rewritten, the main-only copy can no longer
                # claim even main-file equality: it is now a divergent database.
                assert again['main-only-copy']['verdict'] == DIVERGENT_DATABASE
                assert again['main-only-copy']['verdict_unchanged'] is False
                assert again['main-only-copy']['still_applies'] is False
                # The standalone database stays divergent for the same reasons as before.
                assert again['standalone']['verdict'] == DIVERGENT_DATABASE
                assert again['standalone']['still_applies'] is True
                assert rechecked['all_conclusions_hold'] is False

                print('源更新后复核：')
                for item in rechecked['candidates']:
                    print('  {label:<16} 结论={verdict:<18} 先前结论仍适用={holds} 漂移={drift}'.format(
                        label=item['label'], verdict=item['verdict'],
                        holds=item['still_applies'],
                        drift=sorted(item['evidence_changes'])))
                print('reference changed_since_baseline =',
                      rechecked['reference']['changed_since_baseline'])
                print('all_conclusions_hold =', rechecked['all_conclusions_hold'])
            finally:
                server.shutdown()
                thread.join()
        finally:
            holder.close()


def main():
    argument = sys.argv[1] if len(sys.argv) == 2 else 'all'
    if argument not in ('check', 'all'):
        raise SystemExit('Choose: check or all')
    run()
    print('onboarding self-check walkthrough passed')


if __name__ == '__main__':
    main()
