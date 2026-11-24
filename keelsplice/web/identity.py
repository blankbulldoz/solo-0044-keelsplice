"""The one vocabulary for row identity shown by the workbench pages.

Four fragments sit next to each other in the directory, recipe and evidence
pages, and they must never blur:

``key``
    The exact typed SQLite key produced by :func:`keelsplice.codec.dumps`. It
    is the only fragment that may be submitted back to the JSON API or carried
    in a trace form, so hidden inputs carry exactly this text.
``text``
    A human-readable gloss. Long text and binary values are shortened and the
    full magnitude is disclosed. A gloss is never an address.
``edge``
    A directed foreign-key reference addressed by the child table and the
    SQLite foreign-key number. The same pair reads in the opposite direction,
    so the fragment declares both directions.
``identifier``
    A table or column name exactly as the source catalog spells it.

Pages never assemble key JSON, glosses, edge labels or root positions
themselves; they call this module.
"""

import base64
import binascii

from ..codec import dumps
from .render import text

TEXT_LIMIT = 40
BLOB_PREFIX = 12

LABELS = {'text': '人读文本', 'key': '精确键', 'edge': '边引用'}
ARROWS = {'parent': ('子→父', '父→子'), 'child': ('父→子', '子→父'),
          'self': ('自引用', '自引用')}
OPPOSITE = {'parent': 'child', 'child': 'parent', 'self': 'self'}


def _badge(kind):
    return f'<span class="badge badge-{kind}">{LABELS[kind]}</span>'


def _quoted(value):
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def _component(value):
    """Render one typed value for humans; report any shortening."""
    if value is None:
        return 'NULL', None, False
    if isinstance(value, dict) and set(value) == {'blob'}:
        try:
            raw = base64.b64decode(value['blob'], validate=True)
        except (binascii.Error, ValueError):
            return '「无法解码的二进制」', None, True
        prefix = value['blob'][:BLOB_PREFIX]
        shortened = len(value['blob']) > BLOB_PREFIX
        return (f'「二进制 {len(raw)} 字节」', f'base64 前缀 {prefix}…' if shortened else None,
                shortened)
    if isinstance(value, dict) and set(value) == {'real'}:
        try:
            return repr(float.fromhex(value['real'])), None, False
        except (ValueError, OverflowError):
            return '「无法解码的实数」', None, True
    rendered = str(value)
    if len(rendered) > TEXT_LIMIT:
        shortened = ''.join(' ' if char in '\r\n\t' else char for char in rendered[:TEXT_LIMIT])
        return _quoted(shortened + '…'), f'共 {len(rendered)} 字符', True
    return _quoted(rendered), None, False


def _gloss(values):
    parts, notes, shortened = [], [], False
    for value in values:
        shown, note, cut = _component(value)
        parts.append(shown)
        shortened = shortened or cut
        if note:
            notes.append(note)
    return '[' + ', '.join(parts) + ']', '；'.join(notes), shortened


def exact_value(values):
    """Plain exact-key text, byte-for-byte what the JSON API accepts."""
    return dumps(values)


def identifier(name):
    """A table or column name exactly as the source catalog spells it."""
    return f'<code class="identifier" data-kind="identifier">{text(name)}</code>'


def identifiers(names, separator=', '):
    """An ordered column list: each name keeps its own identifier fragment."""
    joined = text(separator).join(identifier(name) for name in names)
    return f'<span class="identifier-list">{joined}</span>'


def key(values):
    """The exact typed key tuple, safe to submit back unchanged."""
    return (f'<span class="identity identity-key">{_badge("key")}'
            f'<code class="exact" data-kind="key">{text(exact_value(values))}</code></span>')


def readable(values):
    """A shortened human gloss of a key tuple, with the full size disclosed."""
    visible, note, shortened = _gloss(values)
    note_markup = f'<span class="gloss-note">（{text(note)}）</span>' if note else ''
    return (f'<span class="identity identity-text">{_badge("text")}'
            f'<code class="gloss" data-kind="text" data-truncated="'
            f'{"yes" if shortened else "no"}">{text(visible)}</code>{note_markup}</span>')


def pair(values):
    """The exact key first, then the gloss that must not be mistaken for it."""
    return key(values) + readable(values)


def fields(table, values):
    """Hidden inputs carrying the exact key so a round-trip cannot drift."""
    return (f'<input type="hidden" name="table" value="{text(table)}">'
            f'<input type="hidden" name="key" value="{text(exact_value(values))}">')


def edge(child, number, direction):
    """A directed foreign-key reference that reads the same in reverse."""
    if direction not in ARROWS:
        raise ValueError(f'Unknown edge direction: {direction!r}')
    forward, reverse = ARROWS[direction]
    return (f'<span class="identity identity-edge">{_badge("edge")}'
            f'<code class="edge" data-kind="edge" data-child="{text(child)}"'
            f' data-number="{number}" data-direction="{direction}"'
            f' data-reverse-direction="{OPPOSITE[direction]}">'
            f'{text(child)} · 外键 #{number} · {forward}'
            f'<span class="edge-reverse" data-reverse="yes">，反向 {reverse}</span></code></span>')


def root_positions(indices):
    """Root occurrences in input order; zero-based in the attribute, one-based in words."""
    indices = list(indices)
    attribute = ','.join(str(index) for index in indices)
    visible = '、'.join(f'第 {index + 1} 个' for index in indices) if indices else '无'
    return (f'<span class="root-positions" data-root-positions="{attribute}">'
            f'{text(visible)}</span>')


def legend():
    return ('<p class="legend">'
            f'{_badge("text")} 仅供辨认：长文本截断并注明原长度，二进制注明字节数与 base64 前缀，不能回传。 '
            f'{_badge("key")} SQLite 主键的精确类型化元组，可直接追踪或用于 JSON 接口。 '
            f'{_badge("edge")} 由子表与外键编号定位的有向引用，同一编号可反向读取。 '
            '表名与列名按目录原样显示。'
            '</p>')
