"""Browse persistent capture records and operation receipts without reading artifact BLOBs."""

from . import handoff, preview
from .render import code, page, segment, text, typed
from ..codec import dumps

STAGES = {'sealed': '已封存', 'built': '已构建', 'certified': '已认证'}


def stage(state):
    style = {'sealed': 'muted', 'built': 'pending', 'certified': 'ready'}[state]
    return f'<span class="status {style}">{STAGES[state]}</span>'


def index(bench):
    records = bench.repository.recent_captures()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/captures/{segment(record["id"])}">'
        f'{code(record["id"])}</a></th><td>{text(record["name"])}</td>'
        f'<td>{record["version"]}</td><td>{stage(record["state"])}</td></tr>'
        for record in records
    )
    return 200, page('封存捕获', 'capture', 'captures',
                     rows=rows or '<tr><td colspan="4" class="empty">尚无封存捕获。</td></tr>')


def operation_rows(base, operations):
    rows = []
    for operation in operations:
        detail = operation['detail']
        if operation['outcome'] == 'observed':
            related = (f'由{handoff.receipt_link(base, detail["by"])}推进' if detail.get('by')
                       else '<span class="muted">推进回执无记录</span>')
        elif operation['action'] == 'build':
            related = f'子库 {code(detail["sha256"][:12])}…'
        else:
            build = detail.get('build')
            related = f'证书 {code(detail["sha256"][:12])}…'
            related += f' · 对应{handoff.receipt_link(base, build)}' if build else ' · 构建回执无记录'
        rows.append(
            f'<tr><th scope="row">{handoff.receipt_link(base, operation["id"])}</th>'
            f'<td>{handoff.ACTIONS[operation["action"]]}</td><td>{handoff.OUTCOMES[operation["outcome"]]}</td>'
            f'<td>{STAGES[operation["before"]]} → {STAGES[operation["after"]]}</td><td>{related}</td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="5" class="empty">尚无操作回执。</td></tr>'


def detail(bench, capture_id):
    record = bench.status(capture_id)
    base = f'/workbench/captures/{segment(capture_id)}'
    operations = bench.repository.operations(capture_id)
    roots = ''.join(
        f'<li><form class="inline" method="post" action="{base}/trace">'
        f'<input type="hidden" name="table" value="{text(table)}">'
        f'<input type="hidden" name="key" value="{text(dumps(key))}">'
        f'<span>{code(table)} {typed(key)}</span><button class="quiet">追踪此根</button></form></li>'
        for table, key in record['roots']
    )
    return 200, page('捕获详情', 'capture', 'capture', id=text(capture_id), base=base,
                     recipe=f'/workbench/recipes/{segment(record["name"])}/{record["version"]}',
                     name=text(record['name']), number=record['version'], stage=stage(record['state']),
                     stage_line=handoff.stage_line(record['state']),
                     digest=text(record['schema_digest']),
                     certificate=handoff.certificate(record, base, handoff.performed_build(operations)),
                     actions=handoff.actions(base, record['state']), roots=roots,
                     operations=operation_rows(base, operations),
                     counts=preview.counts(record['summary']), rows=record['summary']['rows'],
                     witnesses=record['summary']['witnesses'])


def receipt(bench, capture_id, operation_id):
    record = bench.status(capture_id)
    operation = bench.operation(capture_id, operation_id)
    base = f'/workbench/captures/{segment(capture_id)}'
    detail = operation['detail']
    action = handoff.ACTIONS[operation['action']]
    if operation['outcome'] == 'performed':
        verdict = (f'本次请求把捕获从「{STAGES[operation["before"]]}」推进到「{STAGES[operation["after"]]}」。'
                   '断线重连或重复提交都可凭此回执核对。')
        if operation['action'] == 'build':
            related = (f'<dl class="facts"><div class="wide"><dt>子库 SHA-256</dt><dd>{code(detail["sha256"])}</dd></div>'
                       f'<div><dt>子库字节</dt><dd>{detail["bytes"]}</dd></div></dl>')
        else:
            build = detail.get('build')
            linked = (handoff.receipt_link(base, build) if build
                      else '<span class="muted">无回执记录（早于操作回执）</span>')
            related = (f'<dl class="facts"><div class="wide"><dt>证书 SHA-256</dt><dd>{code(detail["sha256"])}</dd></div>'
                       f'<div class="wide"><dt>对应构建回执</dt><dd>{linked}</dd></div></dl>')
    else:
        verdict = '本次请求没有改变状态；该阶段在此之前已由其他回执推进。'
        related = (f'<p>实际推进的回执：{handoff.receipt_link(base, detail["by"])}</p>' if detail.get('by')
                   else '<p class="muted">实际推进的回执无记录（早于操作回执）。</p>')
    outcome = f'<span class="status {"ready" if operation["outcome"] == "performed" else "muted"}">' \
              f'{handoff.OUTCOMES[operation["outcome"]]}</span>'
    return 200, page('操作回执', 'capture', 'operation', base=base, id=code(operation['id']),
                     action=text(action), outcome=outcome, verdict=text(verdict), related=related,
                     transition=f'{STAGES[operation["before"]]} → {STAGES[operation["after"]]}',
                     stage_line=handoff.stage_line(record['state']),
                     actions=handoff.actions(base, record['state']))
