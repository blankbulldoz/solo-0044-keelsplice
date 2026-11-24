"""接收方兼容验收：用可复跑 SQLite 输入区分三类交付事实。

场景（源定义见 examples/handoff_compat.sql）：
  1. 没有入选行：staging_area 在源库有 2 行，闭包选不中任何一行，子库 0 行；
  2. 结构仍在：staging_area 虽为 0 行，表 DDL、约束和部分索引仍随子库交付；
  3. 功能对象根本不交付：源库的 recent_events 视图与 event_log_insert_note
     触发器在认证子库中不存在（工作台数据契约明确视图和触发器不属交付范围）。

两个下游的放行口径：
  - 初始化型应用：只依赖“空表仍有 DDL/约束”，本交付可放行；
  - 行为延续型应用：依赖源库视图/触发器行为，本交付不可放行，
    需请求一份另行交付的视图/触发器 DDL（认证子库只保证表、显式索引与入选行）。

复跑（使用临时目录，不产生仓库文件）：
    python3 -m examples.acceptance_compat
退出码 0 表示全部断言通过并已打印放行依据；任何违例以 AssertionError 失败。
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import RequestHandler, create_server

# 访问日志只含时间戳和随机捕获 ID；静音后验收标准输出保持确定，请求失败仍由断言暴露。
RequestHandler.log_message = lambda self, format, *args: None

FIXTURE_SQL = Path(__file__).with_name('handoff_compat.sql')
REQUIRED_TABLES = ('registry', 'event_log', 'staging_area')
DELIVERED_INDEXES = ('event_log_tenant_lookup', 'staging_tenant_lookup')
SOURCE_VIEW = 'recent_events'
SOURCE_TRIGGER = 'event_log_insert_note'


def seed(file):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(FIXTURE_SQL.read_text())


def call(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, data, headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:300])
        if response.headers.get_content_type() == 'application/vnd.sqlite3':
            return payload, response.headers['ETag'].strip('"')
        return json.loads(payload)


def objects(conn, kind):
    return {row[0] for row in conn.execute(
        'SELECT name FROM main.sqlite_schema WHERE type=? AND name NOT LIKE "sqlite_%"', (kind,))}


def stored_sql(conn, kind, name):
    return conn.execute('SELECT sql FROM main.sqlite_schema WHERE type=? AND name=?', (kind, name)).fetchone()[0]


def class_report(conn):
    """三类事实的独立只读判别，返回每类的具体证据。"""
    tables = objects(conn, 'table')
    indexes = objects(conn, 'index')
    views = objects(conn, 'view')
    triggers = objects(conn, 'trigger')

    # 情况一：没有入选行。
    counts = {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in REQUIRED_TABLES}

    # 情况二：结构仍在（0 行表也保留 DDL、约束和显式索引）。
    structure = {
        'tables_present': sorted(tables),
        'indexes_present': sorted(indexes),
        'staging_ddl': stored_sql(conn, 'table', 'staging_area'),
        'staging_partial_index_sql': stored_sql(conn, 'index', 'staging_tenant_lookup'),
    }

    # 情况三：功能对象根本不交付。
    missing_function_objects = sorted(({SOURCE_VIEW} - views) | ({SOURCE_TRIGGER} - triggers))

    return counts, structure, missing_function_objects, sorted(views), sorted(triggers)


def downstream_initializer(conn):
    """下游一（初始化型）：空表也能按 DDL 建约束并插入自己的数据。"""
    conn.execute('PRAGMA foreign_keys=ON')
    # DDL 仍在：0 行表可正常读取列定义，违反 NOT NULL 的插入必须被拒。
    columns = [row[1] for row in conn.execute('PRAGMA table_info(staging_area)')]
    assert columns == ['slot_id', 'tenant', 'payload'], columns
    rejected = False
    try:
        conn.execute("INSERT INTO staging_area(slot_id, tenant) VALUES (99, 'core')")
    except sqlite3.IntegrityError:
        rejected = True
    conn.rollback()
    assert rejected, '空表的 NOT NULL 约束必须仍然生效'
    # 正常初始化插入成功；灌入足够页数后，交付的部分索引仍可被查询计划选用。
    conn.executemany('INSERT INTO staging_area VALUES (?, ?, ?)',
                     [(i, 'bulk', f'payload-{i:04d}') for i in range(100, 300)])
    conn.execute("INSERT INTO staging_area VALUES (99, 'core', 'downstream-seed')")
    plan = conn.execute('EXPLAIN QUERY PLAN SELECT * FROM staging_area WHERE tenant=? AND payload IS NOT NULL',
                        ('core',)).fetchall()
    assert any('staging_tenant_lookup' in line[-1] for line in plan), plan


def downstream_behavior(conn):
    """下游二（行为延续型）：索引行为可用，但视图/触发器不在，必须暴露缺口。"""
    # 灌量后验证交付索引的查询计划行为与源库一致（父键引用真实存在的 registry 行）。
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executemany('INSERT INTO event_log VALUES (?, ?, ?, ?)',
                     [(3000 + i, 'core', 7, f'probe-{i:04d}') for i in range(400)])
    plan = conn.execute('EXPLAIN QUERY PLAN SELECT * FROM event_log WHERE tenant=? AND asset_id=?',
                        ('core', 7)).fetchall()
    assert any('event_log_tenant_lookup' in line[-1] for line in plan), plan
    # 视图缺失会在使用点报错；触发器只能经 schema 表判别，二者均确认不在交付内。
    try:
        conn.execute(f'SELECT * FROM {SOURCE_VIEW}')
    except sqlite3.Error as exc:
        assert 'no such' in str(exc).lower()
    else:  # pragma: no cover - 断言失败路径
        raise AssertionError(f'view {SOURCE_VIEW} 不应出现在认证子库中')
    assert objects(conn, 'view') == set() and objects(conn, 'trigger') == set()


def main():
    with TemporaryDirectory(prefix='keelsplice-acceptance-') as directory:
        root = Path(directory)
        source = root / 'handoff_compat.sqlite'
        seed(source)

        # 先证明视图和触发器在源库确实可用，缺口是交付边界而不是源库缺失。
        with closing(sqlite3.connect(source)) as conn:
            source_views = objects(conn, 'view')
            source_triggers = objects(conn, 'trigger')
            assert SOURCE_VIEW in source_views and SOURCE_TRIGGER in source_triggers
            assert conn.execute(f'SELECT count(*) FROM {SOURCE_VIEW}').fetchone()[0] == 3
            assert conn.execute('SELECT count(*) FROM staging_area').fetchone()[0] == 2

        with create_server(('127.0.0.1', 0), root / 'repo.sqlite', {'compat': source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                catalog = call(base, '/sources/compat/catalog')
                assert set(catalog['schema']['tables']) == set(REQUIRED_TABLES)
                assert [index['name'] for index in catalog['schema']['indexes']] == sorted(DELIVERED_INDEXES)

                edge = next(edge for edge in catalog['schema']['edges']
                            if edge['child'] == 'event_log' and edge['parent'] == 'registry')
                recipe = {'source': 'compat',
                          'roots': [{'table': 'registry', 'key': ['core', 7]}],
                          'follow_children': [{'child': 'event_log', 'id': edge['id']}],
                          'row_limit': 50}
                version = call(base, '/recipes/compat-swatch/versions', recipe, 201)
                assert version['version'] == 1
                preview = call(base, '/preview', {'name': 'compat-swatch', 'version': 1})
                assert preview['summary']['counts'] == {
                    'registry': 1, 'event_log': 2, 'staging_area': 0}, preview['summary']

                capture = call(base, '/captures', {'name': 'compat-swatch', 'version': 1}, 201)
                cap_id = capture['id']
                call(base, f'/captures/{cap_id}/build', {})
                certified = call(base, f'/captures/{cap_id}/certify', {})
                certificate = certified['certificate']
                data, etag = call(base, f'/captures/{cap_id}/database')
            finally:
                server.shutdown()
                thread.join()

        # 证书与字节是后续一切判别的入场券：摘要不符直接拒收，不再讨论兼容。
        actual_sha = hashlib.sha256(data).hexdigest()
        assert actual_sha == etag == certificate['sha256']
        assert certificate['counts'] == {'registry': 1, 'event_log': 2, 'staging_area': 0}

        fixture = root / 'accepted.sqlite'
        fixture.write_bytes(data)

        # 任一比特被改动后摘要即失效，模拟非本次认证字节必须被挡在门外。
        tampered = root / 'tampered.sqlite'
        tampered.write_bytes(data[:-1] + bytes([data[-1] ^ 0x01]))
        assert hashlib.sha256(tampered.read_bytes()).hexdigest() != certificate['sha256']

        with closing(sqlite3.connect(fixture.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA trusted_schema=OFF')
            assert [row[0] for row in conn.execute('PRAGMA main.integrity_check')] == ['ok']
            assert conn.execute('PRAGMA main.foreign_key_check').fetchall() == []

            counts, structure, missing_objects, views, triggers = class_report(conn)

            print('== 情况一：没有入选行（零行不等于缺表） ==')
            print('  证书计数 certificate.counts =', certificate['counts'])
            print('  独立连接实读 counts        =', counts)
            assert counts == certificate['counts']
            assert counts['staging_area'] == 0

            print('== 情况二：结构仍在（DDL、约束、显式索引全部交付） ==')
            print('  子库表   =', structure['tables_present'])
            print('  子库索引 =', structure['indexes_present'])
            print('  空表 DDL =', ' '.join(structure['staging_ddl'].split()))
            print('  部分索引 =', ' '.join(structure['staging_partial_index_sql'].split()))
            assert set(REQUIRED_TABLES) <= set(structure['tables_present'])
            assert set(DELIVERED_INDEXES) <= set(structure['indexes_present'])
            assert 'NOT NULL' in structure['staging_ddl']
            assert structure['staging_partial_index_sql'].endswith('WHERE payload IS NOT NULL')

            print('== 情况三：功能对象根本不交付 ==')
            print('  源库对象   : view =', sorted(source_views), ' trigger =', sorted(source_triggers))
            print('  子库对象   : view =', views, ' trigger =', triggers)
            print('  缺失对象   =', missing_objects)
            assert views == [] and triggers == []
            assert set(missing_objects) == {SOURCE_VIEW, SOURCE_TRIGGER}

        # 两个下游各自在一份可写副本上验证自己的兼容口径。
        for name, check in (('初始化型下游', downstream_initializer), ('行为延续型下游', downstream_behavior)):
            copy = root / f'{name}.sqlite'
            copy.write_bytes(data)
            with closing(sqlite3.connect(copy)) as conn:
                check(conn)
            print(f'== {name}兼容演练通过 ==')

    print()
    print('放行依据（一线维护者按此执行）：')
    print('  1. 先核对 SHA-256 与证书一致；不一致或文件无法通过 integrity_check，一律拒收。')
    print('  2. 初始化型下游：0 行表仍有 DDL/约束、显式索引在、外键检查通过 -> 可放行。')
    print('  3. 行为延续型下游：索引行为延续可放行；若依赖源库视图/触发器，本认证文件')
    print('     不交付这些对象，应请求另一份交付（随附的视图/触发器 DDL 及在子库上的')
    print('     安装与复核记录），不得把“能打开数据库”当作功能等价。')


if __name__ == '__main__':
    main()
