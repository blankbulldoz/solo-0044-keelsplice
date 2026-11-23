"""Demonstrate audit marks on direct relationships across successive captures.

The sample source has two same-endpoint arc pairs (a self-referencing cycle and
an intentionally duplicated foreign key) and the recipe uses duplicate roots.
Marks survive unrelated column updates and foreign-key renumbering, stay put
when endpoints or traversal direction change, and wait for human review when
two old relationships cannot be matched uniquely.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from keelsplice.api import create_server

from .browser_walkthrough import html
from .walkthrough import call

LOT1 = ['lot', ['L1']]
U1 = ['unit', [1]]
U2 = ['unit', [2]]
L10 = ['link', [10]]


def seed(file):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(Path(__file__).with_name('audit.sql').read_text())


def entries(view, origin, target, direction, child):
    return [entry for entry in view['arcs']
            if entry['arc']['origin'] == origin and entry['arc']['target'] == target
            and entry['arc']['direction'] == direction and entry['arc']['edge'][0] == child]


def only(view, origin, target, direction, child):
    matches = entries(view, origin, target, direction, child)
    assert len(matches) == 1, (origin, target, direction, child, len(matches))
    return matches[0]


def update(source, *statements):
    with closing(sqlite3.connect(source)) as conn, conn:
        for statement in statements:
            conn.execute(statement)


def run(base, source):
    def request(route, body=None, status=200):
        return call(base, route, body, status)

    def mark(capture_id, arc, verdict, auditor, note=''):
        return request(f'/captures/{capture_id}/marks',
                       {'arc': arc, 'verdict': verdict, 'auditor': auditor, 'note': note}, 201)

    catalog = request('/sources/sample/catalog')
    follow = [{'child': edge['child'], 'id': edge['id']} for edge in catalog['schema']['edges']]
    assert sorted((edge['child'], edge['id']) for edge in catalog['schema']['edges']) == [
        ('link', 0), ('link', 1), ('unit', 0), ('unit', 1)]
    roots = [{'table': 'lot', 'key': ['L1']}, {'table': 'LOT', 'key': ['l1']}]
    body = {'source': 'sample', 'roots': roots, 'follow_children': follow, 'row_limit': 50}
    request('/recipes/audit-demo/versions', body, 201)

    first = request('/captures', {'name': 'audit-demo', 'version': 1}, 201)
    cap1 = first['id']
    assert first['roots'] == [LOT1, LOT1] and first['summary']['root_occurrences'] == 2
    print('重复根：两种拼写命中同一实存根，捕获保留两次出现', first['roots'])
    view1 = request(f'/captures/{cap1}/audit')
    assert len(view1['arcs']) == 18

    arc_lot = only(view1, U1, LOT1, 'parent', 'unit')['arc']
    arc_prev_p = only(view1, U2, U1, 'parent', 'unit')['arc']
    arc_prev_c = only(view1, U1, U2, 'child', 'unit')['arc']
    twins = entries(view1, L10, U1, 'parent', 'link')
    assert len(twins) == 2 and arc_lot['edge'] == ['unit', 1] and arc_prev_p['edge'] == ['unit', 0]
    arc_link = next(entry['arc'] for entry in twins if entry['arc']['edge'] == ['link', 0])

    m1 = mark(cap1, arc_lot, 'verified', '甲')
    m2 = mark(cap1, arc_prev_p, 'verified', '甲')
    m3 = mark(cap1, arc_prev_c, 'pending', '乙', '反向纳入需说明')
    m4 = mark(cap1, arc_link, 'verified', '甲', '重复外键之一')
    assert request(f'/captures/{cap1}/marks', {'arc': arc_lot, 'verdict': 'unsure', 'auditor': '甲'},
                   400)['error'] == 'invalid_verdict'
    assert request(f'/captures/{cap1}/marks', {'arc': arc_lot, 'verdict': 'verified', 'auditor': ' '},
                   400)['error'] == 'invalid_auditor'
    lost = dict(arc_lot, target=['lot', ['L9']])
    assert request(f'/captures/{cap1}/marks', {'arc': lost, 'verdict': 'verified', 'auditor': '甲'},
                   404)['error'] == 'unknown_arc'
    view1 = request(f'/captures/{cap1}/audit')
    assert only(view1, U1, LOT1, 'parent', 'unit')['state'] == 'verified'
    twin_states = {tuple(entry['arc']['edge']): entry['state']
                   for entry in entries(view1, L10, U1, 'parent', 'link')}
    assert twin_states == {('link', 0): 'verified', ('link', 1): 'unmarked'}
    print('同端点双边：重复外键只有被标记的那条显示核对过，另一条仍未标记', twin_states)

    update(source, "UPDATE lot SET note='recheck' WHERE code='L1'",
           "UPDATE unit SET remark='beta-2' WHERE serial=2")
    second = request('/captures', {'name': 'audit-demo', 'version': 1}, 201)
    cap2 = second['id']
    view2 = request(f'/captures/{cap2}/audit')
    carried = only(view2, U1, LOT1, 'parent', 'unit')
    assert carried['state'] == 'verified' and carried['carried'] and carried['mark']['id'] == m1['id']
    assert only(view2, U2, U1, 'parent', 'unit')['mark']['id'] == m2['id']
    assert only(view2, U1, U2, 'child', 'unit')['state'] == 'pending'
    review = entries(view2, L10, U1, 'parent', 'link')
    assert len(review) == 2 and all(entry['state'] == 'review' for entry in review)
    assert all(entry['mark']['id'] == m4['id'] for entry in review)
    print('无关列更新后：核对过沿用；同端点重复外键无法唯一对应 →',
          [entry['state'] for entry in review])

    m5 = mark(cap2, only(view2, U1, LOT1, 'parent', 'unit')['arc'], 'pending', '乙', '换拼写后需复核')
    view2 = request(f'/captures/{cap2}/audit')
    current = only(view2, U1, LOT1, 'parent', 'unit')
    assert current['state'] == 'pending' and not current['carried']
    assert [mark['id'] for mark in current['history']] == [m1['id'], m5['id']]
    view1 = request(f'/captures/{cap1}/audit')
    earlier = only(view1, U1, LOT1, 'parent', 'unit')
    assert earlier['state'] == 'verified' and earlier['mark']['id'] == m1['id']
    assert [mark['id'] for mark in earlier['history']] == [m1['id'], m5['id']]
    kept = request(f'/captures/{cap1}/marks')['marks']
    assert [mark['id'] for mark in kept] == [m1['id'], m2['id'], m3['id'], m4['id']]
    assert next(mark for mark in kept if mark['id'] == m1['id'])['arc']['edge'] == ['unit', 1]
    print('历史标记回到当时的捕获及关系：新判断不覆盖旧捕获的核对过，标记历史',
          [mark['id'] for mark in earlier['history']])

    update(source, 'ALTER TABLE unit ADD COLUMN mentor INTEGER REFERENCES unit',
           'UPDATE unit SET mentor=2 WHERE serial=1')
    renumbered = request('/sources/sample/catalog')['schema']['edges']
    assert sorted((edge['child'], edge['id']) for edge in renumbered) == [
        ('link', 0), ('link', 1), ('unit', 0), ('unit', 1), ('unit', 2)]
    follow = [{'child': edge['child'], 'id': edge['id']} for edge in renumbered]
    request('/recipes/audit-demo/versions', body | {'follow_children': follow}, 201)
    third = request('/captures', {'name': 'audit-demo', 'version': 2}, 201)
    cap3 = third['id']
    view3 = request(f'/captures/{cap3}/audit')
    moved = only(view3, U1, LOT1, 'parent', 'unit')
    assert moved['arc']['edge'] == ['unit', 2] and moved['state'] == 'pending'
    assert moved['carried'] and moved['mark']['id'] == m5['id']
    assert only(view3, U2, U1, 'parent', 'unit')['mark']['id'] == m2['id']
    assert only(view3, U1, U2, 'child', 'unit')['state'] == 'pending'
    mentor = only(view3, U1, U2, 'parent', 'unit')
    assert mentor['arc']['edge'] == ['unit', 0] and mentor['state'] == 'unmarked'
    print('另一条外键插入造成原边改号：unit→lot 外键 1→2，最新判断仍沿用；新关系未标记')

    update(source, 'UPDATE link SET unit=2 WHERE link_id=10')
    fourth = request('/captures', {'name': 'audit-demo', 'version': 2}, 201)
    cap4 = fourth['id']
    view4 = request(f'/captures/{cap4}/audit')
    assert not entries(view4, L10, U1, 'parent', 'link')
    assert [mark['id'] for mark in view4['inactive']] == [m4['id']]
    fresh = entries(view4, L10, U2, 'parent', 'link')
    assert len(fresh) == 2 and all(entry['state'] == 'unmarked' for entry in fresh)
    assert only(view4, U2, U1, 'parent', 'unit')['state'] == 'verified'
    print('实际端点换了：旧标记留在原捕获（未出现', view4['inactive'][0]['id'],
          '），新端点关系等待人工核对')

    follow = [edge for edge in follow if not (edge['child'] == 'unit' and edge['id'] == 1)]
    request('/recipes/audit-demo/versions', body | {'follow_children': follow}, 201)
    fifth = request('/captures', {'name': 'audit-demo', 'version': 3}, 201)
    cap5 = fifth['id']
    view5 = request(f'/captures/{cap5}/audit')
    assert not entries(view5, U1, U2, 'child', 'unit')
    assert m3['id'] in [mark['id'] for mark in view5['inactive']]
    forward = only(view5, U2, U1, 'parent', 'unit')
    assert forward['state'] == 'verified' and forward['mark']['id'] == m2['id']
    print('遍历方向变了：待说明不贴到反向边上，原方向仍只沿用核对过')

    _, page3 = html(base, f'/workbench/captures/{cap3}')
    assert '直接关系审计' in page3 and '待复核' in page3 and '沿用标记' in page3
    index = next(i for i, entry in enumerate(view3['arcs']) if entry['arc'] == moved['arc'])
    _, arc_page = html(base, f'/workbench/captures/{cap3}/arcs/{index}')
    assert '标记历史' in arc_page and '甲' in arc_page and '乙' in arc_page
    _, arc_page = html(base, f'/workbench/captures/{cap3}/mark',
                       {'arc': json.dumps(moved['arc']), 'index': str(index),
                        'auditor': '丙', 'note': '', 'verdict': 'verified'})
    assert '核对过' in arc_page and '丙' in arc_page and '本捕获标记' in arc_page
    view3 = request(f'/captures/{cap3}/audit')
    assert only(view3, U1, LOT1, 'parent', 'unit')['state'] == 'verified'
    _, page1 = html(base, f'/workbench/captures/{cap1}')
    assert '本捕获标记' in page1
    print('工作台：捕获页列出核对状态与依据，关系页登记结论并展示完整标记历史')
    print('审计演练通过：重复根、同端点双边、改号沿用、端点与方向变化、歧义待复核、历史不可覆盖')


def main():
    with TemporaryDirectory(prefix='keelsplice-audit-') as directory:
        root = Path(directory)
        source = root / 'audit.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'sample': source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', source)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()
