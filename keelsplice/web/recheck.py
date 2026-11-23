"""Issue digest-only self-check manifests and judge returned material."""

from .forms import field, typed_json
from .render import code, page, segment, template, text, typed
from ..codec import Fault, dumps

VERDICTS = {'original': ('ready', '原文件'), 'appended': ('ready', '接收端合法追加'),
            'missing_root': ('pending', '缺指定根'), 'rejected': ('rejected', '无法确认')}
REASONS = {'integrity': '数据库完整性或外键自检未通过。',
           'schema': '模式摘要与证书不一致，表定义已变化。',
           'missing_root': '表定义仍在，但找不到指定根。',
           'root_key': '指定根的实存主键被改写。',
           'content': '原有行被修改或删除。',
           'appended': '原有内容完整，接收端追加了新行。',
           'original': '行内容与证书逐项一致。'}


def section(base, record):
    if record['state'] != 'certified':
        return ''
    options = ''.join(
        f'<option value="{text(dumps([table, key]))}">{text(table)} {text(dumps(key))}</option>'
        for table, key in record['roots']
    )
    return template('recheck_form', base=base, options=options)


def root_spec(values):
    spec = typed_json(field(values, 'root'))
    if (not isinstance(spec, list) or len(spec) != 2
            or not isinstance(spec[0], str) or not isinstance(spec[1], list)):
        raise Fault('browser_form', '请选择要核对的实存根。', 400)
    return {'table': spec[0], 'key': spec[1]}


def manifest(bench, capture_id, values):
    result = bench.recheck(capture_id, root_spec(values))
    base = f'/workbench/captures/{segment(capture_id)}'
    return 200, page('自查清单', 'capture', 'recheck', base=base,
                     table=code(result['root']['table']), key=typed(result['root']['key']),
                     digest=code(result['capture_digest']), sha=code(result['file']['sha256']),
                     bytes=result['file']['bytes'], manifest=text(dumps(result)))


def verdict(bench, capture_id, values):
    spec = root_spec(values)
    material = typed_json(field(values, 'material'))
    result = bench.verdict(capture_id, {'root': spec, 'material': material})
    base = f'/workbench/captures/{segment(capture_id)}'
    style, label = VERDICTS[result['verdict']]
    reasons = ''.join(f'<li>{text(REASONS[reason])}</li>' for reason in result['reasons'])
    rows = ''.join(
        f'<tr><th scope="row">{code(name)}</th><td>{counts["missing"]}</td><td>{counts["extra"]}</td></tr>'
        for name, counts in result['tables'].items()
    ) or '<tr><td colspan="3" class="empty">模式不一致，无法逐表核对。</td></tr>'
    found = '在库中' if result['root']['found'] else '缺失'
    bytes_note = '与证书一致' if result['bytes_match'] else '与证书不一致（内容判定不受影响）'
    return 200, page('自查判定', 'capture', 'recheck_verdict', base=base, style=style, label=label,
                     table=code(result['root']['table']), key=typed(result['root']['key']),
                     found=found, missing=result['missing_rows'], appended=result['appended_rows'],
                     bytes_note=bytes_note, reasons=reasons, rows=rows)
