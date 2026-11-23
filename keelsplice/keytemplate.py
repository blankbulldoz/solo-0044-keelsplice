"""Reconcile two tool-generated column cross-reference lists into a registrable key template.

Both exports describe one table. One lists ordinal positions only, which on their own admit
more than one column order. The other lists older column names and may cover only part of
the columns. Neither export decides the template; the declared SQLite definition does: key
ordinals from ``PRAGMA table_xinfo`` and the ``seq`` order of composite foreign-key target
columns. What the lists decide is how their own entries map onto the real columns, and that
mapping is what can be ambiguous.

The rules:

1. The registrable key template is the declared primary-key order. The service requires root
   tuples in catalog key order, so a table with a declared key always has exactly one
   registrable template. The definition confirms it; the lists cannot add a second.
2. A position list is read under two orders: physical column order (``cid``) and declared key
   order first followed by the remaining columns. A name that still matches a current column
   in either export is an anchor that votes for the reading under which its position lands on
   that column. If the votes leave a tie, the position reading is not unique and is recorded
   as unresolved rather than guessed.
3. Legacy entries that do not match a current column are judged alone first. Once the
   template and the position reading are settled, they are re-judged by position: inside the
   key width it is a rename of that key column, inside the table it is a renamed payload
   column, beyond it the entry is excluded. A resolved name that contradicts the confirmed
   template is a conflict, recorded with its ground, and does not change the template.
"""

import json
import sqlite3
from contextlib import closing

from .catalog import inspect, resolve, sql_name
from .codec import Fault, dumps

MAX_ENTRIES = 256


def _entries(value, kind):
    if isinstance(value, dict):
        value = value.get('entries')
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        raise Fault('invalid_request', f'{kind} must be a list of up to {MAX_ENTRIES} entries', 400)
    return value


def _match(columns, name):
    if not isinstance(name, str) or not name:
        return None
    for column in columns:
        if sql_name(column['name']) == sql_name(name):
            return column['name']
    return None


def _base(positions, kind):
    if len(set(positions)) != len(positions):
        raise Fault('invalid_request', f'{kind} positions must be unique', 400)
    low = min(positions)
    if low not in (0, 1):
        raise Fault('invalid_request', f'{kind} positions must start at 0 or 1', 400)
    expected = list(range(low, low + len(positions)))
    if sorted(positions) != expected:
        raise Fault('invalid_request', f'{kind} positions must be contiguous from {low}')
    return low


def _ordinal(values):
    parsed = []
    for index, value in enumerate(_entries(values, 'ordinal')):
        name = None
        if type(value) is int:
            position = value
        elif isinstance(value, dict):
            position = value.get('position')
            name = value.get('name')
        else:
            raise Fault('invalid_request', 'Each ordinal entry must be an integer or object', 400)
        if type(position) is not int or not 0 <= position < MAX_ENTRIES:
            raise Fault('invalid_request', 'position must be a small non-negative integer', 400)
        if name is not None and not isinstance(name, str):
            raise Fault('invalid_request', 'ordinal name must be text', 400)
        parsed.append({'order': index, 'position': position, 'name': name})
    if not parsed:
        raise Fault('invalid_request', 'The ordinal list is empty', 400)
    base = _base([item['position'] for item in parsed], 'ordinal')
    return base, sorted(parsed, key=lambda item: item['position'])


def _legacy(values, base):
    """Normalize the possibly partial legacy list.

    The ordinal list fixes the table's numbering origin; both exports describe the same table,
    so the legacy list is read with that same origin even though it may skip columns.
    """
    parsed = []
    for index, value in enumerate(_entries(values, 'legacy')):
        if not isinstance(value, dict):
            raise Fault('invalid_request', 'Each legacy entry must be an object', 400)
        name = value.get('name')
        if not isinstance(name, str) or not name:
            raise Fault('invalid_request', 'legacy name must be non-empty text', 400)
        position = value.get('position', index + base)
        if type(position) is not int or not 0 <= position < MAX_ENTRIES:
            raise Fault('invalid_request', 'legacy position must be a non-negative integer', 400)
        parsed.append({'order': index, 'position': position, 'name': name})
    if not parsed:
        return []
    if len({item['position'] for item in parsed}) != len(parsed):
        raise Fault('invalid_request', 'legacy positions must be unique', 400)
    return sorted(parsed, key=lambda item: item['position'])


def _readings(columns, pk):
    """Whole-table orders a position can index into, with the columns each slot names."""
    physical = [column['name'] for column in sorted(columns, key=lambda column: column['cid'])]
    rest = [column['name'] for column in sorted(columns, key=lambda column: column['cid'])
            if column['name'] not in pk]
    declared = list(pk) + rest
    readings = [{'reading': 'physical', 'order': physical,
                 'reason': '位次按物理列序（PRAGMA table_xinfo cid）读取'},
                {'reading': 'declared-key', 'order': declared,
                 'reason': '位次先按声明主键序、其后按其余列读取'}]
    if physical == declared:
        return [{'reading': 'physical', 'order': physical,
                 'reason': '该表物理列序与声明主键序、其余列顺序一致，两种读法给出同一结果'}]
    return readings


def _anchors(ordinal, ordinal_base, legacy, legacy_base, columns):
    """Entries whose name still matches a current column; each votes on the reading.

    Either export may carry names. The ordinal list usually carries only positions, but when
    it does include a name that still exists, that name pins its slot just as a legacy name
    does. A partial legacy list therefore contributes anchors for the columns it still names.
    """
    found = []
    for source, items, base in (('ordinal', ordinal, ordinal_base), ('legacy', legacy, legacy_base)):
        for item in items:
            matched = _match(columns, item['name'])
            if matched is not None:
                found.append({'source': source, 'name': item['name'], 'column': matched,
                              'position': item['position'], 'slot': item['position'] - base})
    return found


def _reading_votes(readings, anchors):
    """Count how many anchors each reading satisfies."""
    votes = []
    for reading in readings:
        support, against = 0, 0
        for anchor in anchors:
            slot = anchor['slot']
            if 0 <= slot < len(reading['order']) and reading['order'][slot] == anchor['column']:
                support += 1
            else:
                against += 1
        votes.append({'reading': reading['reading'], 'reason': reading['reason'],
                      'order': reading['order'], 'support': support, 'against': against})
    best = max(vote['support'] - vote['against'] for vote in votes)
    winners = [vote for vote in votes if vote['support'] - vote['against'] == best]
    return votes, winners


def _judge_legacy(legacy, columns, base, key_width, template, reading):
    """Judge each entry on its own, then re-judge unmatched ones once the template is known."""
    judged = []
    for item in legacy:
        slot = item['position'] - base
        matched = _match(columns, item['name'])
        if matched is not None:
            if slot >= key_width:
                initial = {'verdict': 'excluded', 'column': matched,
                           'how': '旧名直接命中非主键列，只用于定位载荷列，不进入主键模板'}
            elif matched != template[slot]:
                initial = {'verdict': 'conflict', 'column': matched,
                           'how': f'旧名直接命中第 {slot + 1} 位 {matched!r}，与已确认主键 {template[slot]!r} 冲突，'
                                  f'按声明主键为准'}
            else:
                initial = {'verdict': 'confirmed', 'column': matched,
                           'how': '旧名直接命中当前列，且与已确认主键位次一致'}
        else:
            initial = {'verdict': 'pending', 'column': None,
                       'how': '旧名不在当前目录，待按位次重判'}
        judged.append({'entry': item, 'slot': slot, 'initial': initial, 'final': None})
    for record in judged:
        slot = record['slot']
        if record['initial']['verdict'] != 'pending':
            record['final'] = record['initial']
        elif reading is None:
            record['final'] = {'verdict': 'unresolved', 'column': None,
                               'how': '旧名不在当前目录，且位次读法未唯一；保留 physical / declared-key 两种解释'}
        elif 0 <= slot < key_width:
            record['final'] = {'verdict': 'rename', 'column': template[slot],
                               'how': f'模板第 {slot + 1} 位已由声明主键确认，旧名按 {reading["reading"]} 位次对应'}
        elif 0 <= slot < len(reading['order']):
            record['final'] = {'verdict': 'payload', 'column': reading['order'][slot],
                               'how': '位次落在非主键列，旧名对应载荷列，不进入主键模板'}
        else:
            record['final'] = {'verdict': 'excluded', 'column': None,
                               'how': '位次超出表宽度，不能对应任何列'}
    return judged


def resolve_lists(schema, table, ordinal_values, legacy_values):
    """Return the confirmed template and the full grounds for every list entry."""
    target = resolve(schema['tables'], table)
    meta = schema['tables'][target]
    columns, pk = meta['columns'], meta['pk']
    ordinal_base, ordinal = _ordinal(ordinal_values)
    legacy = _legacy(legacy_values, ordinal_base)
    readings = _readings(columns, pk)
    anchors = _anchors(ordinal, ordinal_base, legacy, ordinal_base, columns)
    votes, winners = _reading_votes(readings, anchors)
    reading = winners[0] if len(winners) == 1 else None
    if reading is None:
        basis = 'unresolved'
    elif len(readings) == 1:
        basis = 'table-shape'
    elif reading['support']:
        basis = 'anchors'
    else:
        basis = 'table-shape'

    report = {
        'table': target,
        'declared_pk': list(pk),
        'column_order': [column['name'] for column in sorted(columns, key=lambda c: c['cid'])],
        'ordinal_base': ordinal_base,
        'legacy_base': ordinal_base,
        'votes': votes,
        'reading': reading['reading'] if reading else None,
        'reading_unique': len(winners) == 1,
        'reading_basis': basis,
        'anchors': [{'source': a['source'], 'name': a['name'], 'column': a['column'],
                     'slot': a['slot'] + 1} for a in anchors],
        'template': {'table': target, 'key': list(pk)},
        'confirmed_by': [f'声明主键（PRAGMA table_xinfo pk）：{" → ".join(pk)}'],
        'legacy': None,
        'excluded': [],
        'notes': [],
    }
    parents = [list(edge['parent_cols']) for edge in schema['edges'] if edge['parent'] == target]
    if list(pk) in parents:
        report['confirmed_by'].append(f'指向该表的复合外键目标列顺序 {" → ".join(pk)} 一致')
    if basis == 'anchors':
        report['confirmed_by'].append(f'位次读法 {reading["reading"]} 由 {len(anchors)} 个名称锚点唯一确定')
    elif basis == 'table-shape':
        report['confirmed_by'].append(f'位次读法 {reading["reading"]} 由表结构唯一确定（两种读法或唯一读法一致）')
    else:
        report['notes'].append('两份名单中的名称锚点不足或相互抵消，位次读法未唯一；'
                               '模板仍由声明主键确认，但位次对未命中条目的解释保留两种候选。')

    report['legacy'] = _judge_legacy(legacy, columns, ordinal_base, len(pk), list(pk), reading)
    for record in report['legacy']:
        final = record['final']
        if final['verdict'] in ('conflict', 'excluded', 'payload', 'unresolved'):
            report['excluded'].append({'entry': record['entry'], 'reason': final['how']})
    for item in ordinal[len(pk):]:
        report['excluded'].append({'entry': item, 'reason': '位次超出声明主键宽度，不作为主键位'})
    return report


def render(report):
    lines = [f'表 {report["table"]}：声明主键 {" → ".join(report["declared_pk"])}',
             f'物理列序：{" → ".join(report["column_order"])}',
             f'编号表起始位：{report["ordinal_base"]}（旧名表同用此起始位，可跳过未列出的列）']
    for vote in report['votes']:
        mark = '唯一' if report['reading_unique'] and vote['reading'] == report['reading'] else '候选'
        lines.append(f'  读法 {vote["reading"]}（{vote["reason"]}）：{" → ".join(vote["order"])}'
                     f' · 锚点支持 {vote["support"]} / 反对 {vote["against"]} · {mark}')
    for anchor in report['anchors']:
        lines.append(f'  锚点 来自{anchor["source"]}名单的 {anchor["name"]!r} 命中列 {anchor["column"]!r}，'
                     f'落在第 {anchor["slot"]} 位')
    if not report['reading_unique']:
        lines.append('  位次读法未唯一：保留 physical 与 declared-key 两种解释，不据此断言未命中的旧名')
    lines.append('结论：可登记模板主键序 ' + ' → '.join(report['template']['key']))
    for ground in report['confirmed_by']:
        lines.append(f'  入选依据 {ground}')
    for record in report['legacy']:
        item, initial, final = record['entry'], record['initial'], record['final']
        if initial['verdict'] != final['verdict'] or initial.get('how') != final.get('how'):
            verdict = f'{initial["verdict"]} → {final["verdict"]}'
        else:
            verdict = final['verdict']
        column = f' → {final["column"]!r}' if final['column'] else ''
        lines.append(f'  旧名 {item["name"]!r}（第 {item["position"]} 位）：{verdict}{column} · {final["how"]}')
    for excluded in report['excluded']:
        entry = excluded['entry']
        label = entry.get('name') or f'位次 {entry.get("position")}'
        lines.append(f'  排除 {label!r}：{excluded["reason"]}')
    for note in report['notes']:
        lines.append('  说明 ' + note)
    lines.append('可登记根模板：' + dumps(report['template']))
    return lines


def inspect_source(path):
    with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        return inspect(conn)


def _load(path):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except FileNotFoundError:
        return json.loads(path)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='把两家工具的列名对照表解析为可登记的主键模板。')
    parser.add_argument('--source', required=True, metavar='ALIAS=FILE')
    parser.add_argument('--table', required=True)
    parser.add_argument('--ordinal', required=True, help='编号位次名单 JSON（整数列表或 {"entries": [...]}）')
    parser.add_argument('--legacy', required=True, help='旧列名名单 JSON（列表或 {"entries": [...]}）')
    args = parser.parse_args(argv)
    alias, _, path = args.source.partition('=')
    if not alias or not path:
        parser.error('Provide ALIAS=FILE')
    try:
        report = resolve_lists(inspect_source(path), args.table,
                               _load(args.ordinal), _load(args.legacy))
    except Fault as exc:
        print(f'拒绝：{exc.code} · {exc}')
        return 2
    print('\n'.join(render(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
