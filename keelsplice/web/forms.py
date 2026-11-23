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


def checks(values):
    """Decode the check items a browser picked before reading the source."""
    picked = []
    for value in values.get('check', []):
        try:
            picked.append(json.loads(value))
        except (ValueError, RecursionError) as exc:
            raise Fault('invalid_check', '检查项编码无效。', 400) from exc
    return picked


def observation_body(values):
    """Recipe identity plus the chosen checks for a post-preview observation."""
    body = {}
    if 'name' in values:
        body['name'] = field(values, 'name')
    if 'version' in values:
        body['version'] = version(field(values, 'version'))
    picked = checks(values)
    if picked:
        body['checks'] = picked
    return body
