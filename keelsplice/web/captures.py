"""Browse persistent capture records without reading artifact BLOBs."""

from . import handoff, preview, receivers
from .render import code, options, page, segment, text, typed
from ..codec import dumps

STAGES = {'sealed': '已封存', 'built': '已构建', 'certified': '已认证'}


def stage(state):
    style = {'sealed': 'muted', 'built': 'pending', 'certified': 'ready'}[state]
    return f'<span class="status {style}">{STAGES[state]}</span>'


def trial_panel(bench, capture_id):
    devices = [(record['name'], f"{record['profile']['name']}（{record['profile']['relay']}）")
               for record in bench.list_receivers()]
    device_options = options([('', '请选择接收设备'), *devices], '')
    trials = bench.trials_for(capture_id)
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/trials/{segment(trial["id"])}">'
        f'{code(trial["id"][:12])}</a></th><td>{text(trial["device"])}</td>'
        f'<td>{receivers.verdict_badge(trial["verdict"])}</td></tr>'
        for trial in trials
    )
    if not rows:
        rows = '<tr><td colspan="3" class="empty">尚未试交付。</td></tr>'
    return (
        f'<section><h2>实物试交付</h2>'
        f'<form class="trace-form" method="post" action="/workbench/captures/{segment(capture_id)}/trials">'
        f'<label>接收设备<select name="device" required>{device_options}</select></label>'
        '<button class="primary">发起试交付并独立复核</button></form>'
        '<div class="scroll"><table><caption>本捕获最近的试交付</caption>'
        '<thead><tr><th scope="col">试交付编号</th><th scope="col">设备</th><th scope="col">结论</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div></section>'
    )


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


def detail(bench, capture_id):
    record = bench.status(capture_id)
    base = f'/workbench/captures/{segment(capture_id)}'
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
                     digest=text(record['schema_digest']), certificate=handoff.certificate(record),
                     actions=handoff.actions(base, record['state']), roots=roots,
                     counts=preview.counts(record['summary']), rows=record['summary']['rows'],
                     witnesses=record['summary']['witnesses'], trials=trial_panel(bench, capture_id))
