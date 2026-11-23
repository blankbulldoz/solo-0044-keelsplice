"""Decode browser form fields without losing SQLite integer precision."""

import json
import re
from urllib.parse import parse_qs

from ..codec import Fault

MAX_BYTES = 256 * 1024
VERSION = re.compile(r'[1-9][0-9]{0,18}\Z')


def decode_form(body):
    try:
        return parse_qs(body.decode('ascii'), keep_blank_values=True,
                        max_num_fields=1024, encoding='utf-8', errors='strict')
    except (ValueError, UnicodeError) as exc:
        raise Fault('browser_form', '表单编码无效。', 400) from exc


def field(values, name, default=''):
    entries = values.get(name, [default])
    if len(entries) != 1:
        raise Fault('browser_form', f'字段 {name} 只能提交一次。', 400)
    return entries[0]


def version(value):
    if not VERSION.fullmatch(value) or int(value) >= 2**63:
        raise Fault('invalid_version', '请输入合法的配方版本号。', 400)
    return int(value)


def typed_json(value):
    try:
        return json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise Fault('invalid_json', '请输入有效的 JSON。', 400) from exc


def receipt_fields(values):
    """Collect recipient evidence from the handoff form, keeping blanks absent."""
    body = {'sha256': field(values, 'sha256')}
    if field(values, 'recipient'):
        body['recipient'] = field(values, 'recipient')
    if field(values, 'note'):
        body['note'] = field(values, 'note')
    raw = field(values, 'counts')
    if raw.strip():
        counts = typed_json(raw)
        if not isinstance(counts, dict):
            raise Fault('invalid_counts', '请按「表名: 行数」的 JSON 对象填写计数。', 400)
        body['counts'] = counts
    if field(values, 'corrects'):
        body['corrects'] = field(values, 'corrects')
    return body
