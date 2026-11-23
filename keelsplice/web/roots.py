"""Typed per-root editing state for the browser recipe form.

Roots stay an ordered list.  Each root names a table and carries one cell per
real primary-key column, in the catalog's key order.  Cells remember the
SQLite value type the operator chose, so long integers, empty strings, BLOBs
and reals travel through the form without losing their type or spelling.
"""

import base64
import binascii
import math
import re

from .errors import message
from .forms import posted
from .render import options, text
from ..codec import Fault, decode, dumps, loads

KINDS = (('text', '文本'), ('integer', '整数'), ('blob', '字节（Base64）'), ('real', '实数（十六进制浮点）'))
MAX_ROOTS = 256
JSON_BYTES = 64 * 1024
INTEGER = re.compile(r'[+-]?[0-9]{1,19}\Z')
INDEX = re.compile(r'[0-9]{1,3}\Z')
ACTIONS = {'register', 'register_json', 'import', 'add', 'remove', 'up', 'down', 'retable'}


def match(schema, name):
    """Resolve a table name the way the catalog does, without raising on misses."""
    if not isinstance(name, str):
        return None
    for table in schema['tables']:
        if table.lower() == name.lower():
            return table
    return None


def columns(schema, table):
    """Primary-key columns of ``table`` in declared key order."""
    if table is None:
        return []
    by_name = {column['name']: column for column in schema['tables'][table]['columns']}
    return [by_name[name] for name in schema['tables'][table]['pk']]


def _kind(column):
    declared = (column['type'] or '').upper()
    if 'INT' in declared:
        return 'integer'
    if 'REAL' in declared or 'FLOA' in declared or 'DOUB' in declared:
        return 'real'
    return 'text'


def blank(schema, table):
    fixed = match(schema, table)
    return {'table': fixed if fixed is not None else table, 'cells': [
        {'kind': _kind(column), 'value': ''} for column in columns(schema, fixed)]}


def default_state(schema):
    first = next(iter(schema['tables']), '')
    return [blank(schema, first)]


def parse_state(values):
    """Read the numbered card fields; ``None`` when the form did not use cards.

    Each card records the table its cells were rendered for.  When the posted
    table differs, the cells keep their typed values but adopt the new table's
    column types, so switching tables takes effect on the same submit.
    """
    indices = []
    for key in values:
        found = re.fullmatch(r'root_table_([0-9]{1,3})', key)
        if found:
            indices.append(int(found[1]))
    if not indices:
        return None
    indices.sort()
    if indices != list(range(len(indices))):
        raise Fault('browser_form', '根记录编号不连续，请重新打开编辑页。', 400)
    state = []
    for index in indices:
        tables = values[f'root_table_{index}']
        kinds = values.get(f'root_kind_{index}', [])
        texts = values.get(f'root_value_{index}', [])
        if len(tables) != 1:
            raise Fault('browser_form', f'根 {index + 1} 的表只能提交一次。', 400)
        if len(kinds) != len(texts):
            raise Fault('browser_form', f'根 {index + 1} 的主键格数量不一致，请重新打开编辑页。', 400)
        card = {'table': tables[0],
                'cells': [{'kind': kind, 'value': value} for kind, value in zip(kinds, texts)]}
        if f'root_was_{index}' in values:
            card['was'] = values[f'root_was_{index}'][0] if values[f'root_was_{index}'] else ''
        state.append(card)
    return state


def adapt(schema, state):
    """Re-fit cards whose table changed so their cells follow the new key order."""
    fitted = []
    for root in state:
        if 'was' not in root:
            # No rendered table to compare with; the posted kinds are authoritative.
            fitted.append(root)
            continue
        was = root.pop('was')
        table = match(schema, root['table'])
        if table is None or match(schema, was) == table:
            fitted.append(root)
            continue
        defaults = blank(schema, table)['cells']
        cells = root['cells']
        merged = []
        for position, default in enumerate(defaults):
            if position < len(cells):
                # Keep what was typed, but the real column decides how it is read.
                merged.append({'kind': default['kind'], 'value': cells[position]['value']})
            else:
                merged.append(default)
        fitted.append({'table': table, 'cells': merged})
    return fitted


def parse_json(raw):
    if len(raw.encode('utf-8', 'surrogatepass')) > JSON_BYTES:
        raise Fault('invalid_json', '根记录 JSON 超过 64 KiB。', 413)
    try:
        return loads(raw)
    except (ValueError, RecursionError) as exc:
        raise Fault('invalid_json', '请输入有效的 JSON。', 400) from exc


def _from_wire(value, index):
    if value is None:
        raise Fault('invalid_key', f'根 {index + 1} 的主键分量不能为空值。', 400)
    checked = decode(value)
    if type(checked) is str:
        return {'kind': 'text', 'value': checked}
    if type(checked) is int:
        return {'kind': 'integer', 'value': str(checked)}
    if isinstance(checked, bytes):
        return {'kind': 'blob', 'value': value['blob']}
    return {'kind': 'real', 'value': value['real']}


def import_records(records, schema):
    """Load canonical ``{table, key}`` records into editable per-cell state."""
    if not isinstance(records, list):
        raise Fault('invalid_roots', '根记录须为 JSON 数组。', 400)
    if not 1 <= len(records) <= MAX_ROOTS:
        raise Fault('invalid_roots', f'请登记 1 至 {MAX_ROOTS} 个根记录。', 400)
    state = []
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {'table', 'key'}:
            raise Fault('invalid_roots', '每个根记录须为 {"table": ..., "key": [...]}。', 400)
        table = match(schema, record['table'])
        if table is None:
            raise Fault('unknown_identifier', f'根 {index + 1} 的表名不在当前目录中。', 400)
        key = record['key']
        if not isinstance(key, list) or len(key) != len(schema['tables'][table]['pk']):
            raise Fault('invalid_key', f'根 {index + 1} 须按 {table} 的主键顺序给出完整元组。', 400)
        state.append({'table': table, 'cells': [_from_wire(value, index) for value in key]})
    return state


def _integer(value):
    if not INTEGER.fullmatch(value) or not -(2**63) <= int(value) < 2**63:
        raise Fault('invalid_value', f'须为有符号 64 位十进制整数，收到 {value!r}。', 400)
    return int(value)


def _blob(value):
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Fault('invalid_value', '须为规范的 Base64 文本（可留空表示零字节）。', 400) from exc
    if base64.b64encode(raw).decode('ascii') != value:
        raise Fault('invalid_value', '须为规范的 Base64 文本（补齐并去掉多余空白）。', 400)
    return {'blob': value}


def _real(value):
    try:
        number = float.fromhex(value)
    except ValueError:
        try:
            number = float(value)
        except ValueError as exc:
            raise Fault('invalid_value', f'须为十六进制浮点或十进制有限数，收到 {value!r}。', 400) from exc
    if not math.isfinite(number):
        raise Fault('invalid_value', '实数须为有限值。', 400)
    return {'real': number.hex()}


def _wire(cell):
    if cell['kind'] == 'text':
        return cell['value']
    if cell['kind'] == 'integer':
        return _integer(cell['value'])
    if cell['kind'] == 'blob':
        return _blob(cell['value'])
    if cell['kind'] == 'real':
        return _real(cell['value'])
    raise Fault('invalid_value', f'未知的主键格类型 {cell["kind"]!r}。', 400)


def wire_roots(schema, state):
    """Convert editable state to the canonical recipe ``roots`` shape."""
    roots = []
    for index, root in enumerate(state):
        table = match(schema, root['table'])
        if table is None:
            raise Fault('invalid_key', f'根 {index + 1} 尚未选择有效的表。', 400)
        pk = schema['tables'][table]['pk']
        if len(root['cells']) != len(pk):
            raise Fault('browser_form',
                        f'根 {index + 1} 有 {len(root["cells"])} 格，而 {table} 的主键有 {len(pk)} 列。'
                        '切换表后请先点「按此表刷新格」。', 400)
        try:
            key = [_wire(cell) for cell in root['cells']]
        except Fault as exc:
            raise Fault('root_cell', f'根 {index + 1}：{exc}', exc.status) from exc
        roots.append({'table': table, 'key': key})
    if not 1 <= len(roots) <= MAX_ROOTS:
        raise Fault('invalid_roots', f'请登记 1 至 {MAX_ROOTS} 个根记录。', 400)
    return roots


def export(schema, state):
    """Canonical JSON for the raw textarea, or ``(None, reason)`` if not buildable."""
    if not state:
        return '[]', None
    try:
        return dumps(wire_roots(schema, state)), None
    except Fault as exc:
        return None, message(exc)


def transition(schema, state, verb, index, *, table=''):
    """Apply a non-submitting control to the ordered root list."""
    state = [{'table': root['table'], 'cells': [dict(cell) for cell in root['cells']]} for root in state]
    if verb == 'add':
        wanted = match(schema, table) or (state[-1]['table'] if state else '')
        state.append(blank(schema, wanted))
        return state
    if index is None or index >= len(state):
        raise Fault('browser_form', '根序号已超出当前草稿。', 400)
    if verb == 'remove':
        del state[index]
        return state
    if verb == 'up' and index:
        state[index - 1], state[index] = state[index], state[index - 1]
    elif verb == 'down' and index + 1 < len(state):
        state[index + 1], state[index] = state[index], state[index + 1]
    elif verb == 'retable':
        state[index] = blank(schema, state[index]['table'])
    return state


def control(values):
    """Split the clicked button into an action verb and an optional root index."""
    raw = posted(values, 'action', 'register')
    verb, _, index = raw.partition(':')
    if verb not in ACTIONS:
        raise Fault('browser_form', '未知的表单操作。', 400)
    if verb in {'register', 'register_json', 'import', 'add'}:
        return verb, None
    if not INDEX.fullmatch(index):
        raise Fault('browser_form', '根序号无效。', 400)
    return verb, int(index)


def load(schema, values):
    """Return ``(state, mode, raw, notice)`` for the editor and the submitter.

    ``mode`` is ``'cards'`` when the numbered fields drive the draft, ``'json'``
    when the operator chose to submit the pasted canonical recipe JSON, and
    ``'adopt'`` when a control adopted a newer JSON box before redrawing.
    ``notice`` explains why pasted JSON could not be adopted; the text is then
    kept as-is so the operator can correct it in place.
    """
    verb, index = control(values)
    uses_cards = any(re.fullmatch(r'root_table_[0-9]{1,3}', key) for key in values)
    raw = posted(values, 'roots')
    if verb in {'import', 'register_json'} or not uses_cards:
        if not raw:
            return default_state(schema), 'cards', None, None
        try:
            state = import_records(parse_json(raw), schema)
        except Fault as exc:
            # Keep the pasted text for correction, and any typed cards alongside it.
            fallback = parse_state(values) if uses_cards else None
            return (adapt(schema, fallback) if fallback else default_state(schema)), 'json', raw, message(exc)
        return state, 'json', raw, None
    state = adapt(schema, parse_state(values))
    if verb not in {'register', 'import'}:
        if divergent(raw, posted(values, 'rendered')):
            # The box holds newer text than the cards. Adopt it first instead of
            # letting an add/move/delete silently drop what was pasted.
            try:
                state = import_records(parse_json(raw), schema)
            except Fault as exc:
                return state, 'json', raw, message(exc)
            return state, 'adopt', raw, '已先按原始 JSON 更新逐根格；请再次点击刚才的操作。'
        state = transition(schema, state, verb, index, table=posted(values, 'add_table'))
    return state, 'cards', None, None


def divergent(raw, rendered):
    """True when the JSON box holds text the page did not put there.

    Browsers resubmit the box verbatim, including canonical spellings the page
    rendered that differ from a cell's input (a decimal real shown as hex, for
    example).  Only genuinely different text means the operator pasted or
    edited JSON without importing it.
    """
    return bool(raw) and raw.strip() != rendered.strip()


def summary(schema, state):
    rows = []
    for index, root in enumerate(state):
        table = match(schema, root['table'])
        names = schema['tables'][table]['pk'] if table else []
        cells = []
        for position, cell in enumerate(root['cells']):
            label = names[position] if position < len(names) else f'第 {position + 1} 列'
            cells.append(f'{text(label)} = {text(cell["value"])}（{_label(cell["kind"])}）')
        rows.append(f'<li><span class="root-position">根 {index + 1}</span> '
                    f'<code>{text(table or root["table"] or "未选择")}</code>'
                    f' <span class="muted">{text(" · ".join(cells) or "尚未选择表")}</span></li>')
    return ''.join(rows) or '<li class="empty">尚未添加根。</li>'


def _label(kind):
    return dict(KINDS).get(kind, kind)


def cards(schema, state):
    return ''.join(_card(schema, index, root) for index, root in enumerate(state))


def _card(schema, index, root):
    table = match(schema, root['table'])
    table_options = options([(name, name) for name in schema['tables']], table or '')
    known = columns(schema, table)
    cells = []
    for position, cell in enumerate(root['cells']):
        column = known[position] if position < len(known) else None
        label = f'第 {position + 1} 列 · {column["name"]}' if column else f'第 {position + 1} 列 · 超出主键'
        declared = f'（{column["type"] or "未声明类型"}）' if column else ''
        cells.append(
            f'<div class="root-cell"><span class="root-cell-label">{text(label)}'
            f'<span class="muted">{text(declared)}</span></span>'
            f'<select name="root_kind_{index}" aria-label="根 {index + 1} 第 {position + 1} 列的类型">'
            f'{options(KINDS, cell["kind"])}</select>'
            f'<input name="root_value_{index}" value="{text(cell["value"])}" spellcheck="false"'
            f' autocomplete="off" aria-label="根 {index + 1} 第 {position + 1} 列的值"></div>')
    if table is None:
        cells.append('<p class="empty">先选择表，再按该表的真实主键顺序填写每格。</p>')
    return (
        f'<fieldset class="root-card"><legend><span class="root-position">根 {index + 1}</span></legend>'
        f'<input type="hidden" name="root_was_{index}" value="{text(root["table"])}">'
        f'<div class="root-head"><label>表<select name="root_table_{index}"'
        f' aria-label="根 {index + 1} 的表">{table_options}</select></label>'
        '<div class="root-tools">'
        f'<button class="quiet" name="action" value="up:{index}">上移</button>'
        f'<button class="quiet" name="action" value="down:{index}">下移</button>'
        f'<button class="quiet" name="action" value="retable:{index}">按此表刷新格</button>'
        f'<button class="quiet" name="action" value="remove:{index}">删除</button>'
        '</div></div>'
        f'<div class="root-cells">{"".join(cells)}</div></fieldset>')
