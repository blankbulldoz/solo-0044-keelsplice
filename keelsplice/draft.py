"""Parallel-source draft payloads, root compatibility, and merge rules."""

import json

from .catalog import sql_name
from .codec import Fault, decode, dumps

MAX_ROOTS = 256
MAX_NAME = 128
MAX_LIMIT = 16
MAX_ROOTS_TEXT = 64 * 1024
MAX_EDGE_TOKEN = 128
MAX_EDGES = 256

STATES = ('pending', 'copied')


def blank_payload(name='', row_limit='1000'):
    return dict(name=name, row_limit=row_limit, roots='', edges=[], marks=[])


def check_payload(payload):
    name, limit, roots, edges = (payload.get(key) for key in ('name', 'row_limit', 'roots', 'edges'))
    if not isinstance(name, str) or len(name) > MAX_NAME:
        raise Fault('invalid_draft', 'Draft name is missing or too long')
    if not isinstance(limit, str) or len(limit) > MAX_LIMIT:
        raise Fault('invalid_draft', 'Draft row limit is missing or too long')
    if not isinstance(roots, str) or len(roots) > MAX_ROOTS_TEXT:
        raise Fault('invalid_draft', 'Draft roots text is missing or too large')
    if (not isinstance(edges, list) or len(edges) > MAX_EDGES
            or any(not isinstance(token, str) or len(token) > MAX_EDGE_TOKEN for token in edges)):
        raise Fault('invalid_draft', 'Draft edge selection is missing or too large')
    return payload


def well_formed(entry):
    return (isinstance(entry, dict) and set(entry) == {'table', 'key'}
            and isinstance(entry['table'], str) and isinstance(entry['key'], list))


def identity(entry):
    return dumps({'table': entry['table'], 'key': entry['key']})


def parse_roots(text):
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise Fault('invalid_json', '请输入有效的 JSON。', 400) from exc
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ROOTS:
        raise Fault('invalid_roots', 'Specify 1 to 256 root tuples')
    return value


def pk_order(schema, table):
    if isinstance(table, str):
        for name, info in schema['tables'].items():
            if sql_name(name) == sql_name(table):
                return [sql_name(column) for column in info['pk']]
    return None


def incompatible(source_schema, target_schema, entry):
    """Return None when the root tuple names the same key columns in both sources."""
    if not well_formed(entry):
        return '条目形状须为 {"table":…,"key":…}'
    source_pk = pk_order(source_schema, entry['table'])
    if source_pk is None:
        return '本源目录中没有同名表'
    target_pk = pk_order(target_schema, entry['table'])
    if target_pk is None:
        return '目标源没有同名表'
    if source_pk != target_pk:
        return '两源主键列或其次序不同'
    key = entry['key']
    if len(key) != len(source_pk):
        return '主键分量数与主键列数不符'
    if None in key:
        return '主键分量不能为空'
    try:
        for component in key:
            decode(component)
    except Fault:
        return '主键值编码无效'
    return None


def well_formed_mark(mark):
    return (isinstance(mark, dict) and set(mark) == {'entry', 'from', 'state'}
            and isinstance(mark['entry'], str) and isinstance(mark['from'], str)
            and mark['state'] in STATES)


def prune_marks(marks, entries):
    present = {identity(entry) for entry in entries if well_formed(entry)}
    return [mark for mark in marks if well_formed_mark(mark) and mark['entry'] in present]


def retained_marks(marks, roots_text):
    try:
        entries = parse_roots(roots_text)
    except Fault:
        return [mark for mark in marks if well_formed_mark(mark)]
    return prune_marks(marks, entries)


def confirm_marks(payload):
    pending = sum(1 for mark in payload['marks'] if well_formed_mark(mark) and mark['state'] == 'pending')
    if not pending:
        return None, 0
    marks = [dict(mark, state='copied') if well_formed_mark(mark) and mark['state'] == 'pending' else mark
             for mark in payload['marks']]
    return dict(payload, marks=marks), pending


def merge(stored, entries, origin, defaults):
    """Append compatible roots to the stored payload, keeping the input order."""
    if stored is None:
        stored, existing = blank_payload(**defaults), []
    else:
        try:
            existing = parse_roots(stored['roots']) if stored['roots'].strip() else []
        except Fault:
            raise Fault('draft_roots_invalid', 'Stored parallel draft roots are not parseable') from None
    seen = {identity(entry) for entry in existing if well_formed(entry)}
    added, duplicates = [], 0
    for entry in entries:
        marker = identity(entry)
        if marker in seen:
            duplicates += 1
            continue
        if len(existing) >= MAX_ROOTS:
            raise Fault('invalid_roots', 'Merged draft would exceed 256 root tuples')
        seen.add(marker)
        existing.append(entry)
        added.append(entry)
    if not added:
        return stored, added, duplicates
    marks = prune_marks(stored['marks'], existing)
    marks.extend({'entry': identity(entry), 'from': origin, 'state': 'pending'} for entry in added)
    payload = dict(stored, roots=json.dumps(existing, ensure_ascii=False, indent=2), marks=marks)
    return check_payload(payload), added, duplicates
