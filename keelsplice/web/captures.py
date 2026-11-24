"""Browse persistent capture records without reading artifact BLOBs."""

from . import handoff, preview
from .render import code, page, segment, text, typed
from ..codec import dumps

STAGES = {'sealed': '已封存', 'built': '已构建', 'certified': '已认证'}


def stage(state):
    style = {'sealed': 'muted', 'built': 'pending', 'certified': 'ready'}[state]
    return f'<span class="status {style}">{STAGES[state]}</span>'


def provenance(bench):
    context = bench.context()
    if context['authoritative']:
        return ''
    return (f'<p class="notice info">备用实例视图：配方投用状态取自 {text(context["as_of"])} 的快照，'
            f'不代表主实例当前状态。来源 <code>{text(context["snapshot_source_repository"])}</code>。</p>')


def index(bench):
    records = bench.repository.recent_captures()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/captures/{segment(record["id"])}">'
        f'{code(record["id"])}</a></th><td>{text(record["name"])}</td>'
        f'<td>{record["version"]}</td><td>{stage(record["state"])}</td></tr>'
        for record in records
    )
    return 200, page('封存捕获', 'capture', 'captures', provenance=provenance(bench),
                     rows=rows or '<tr><td colspan="4" class="empty">尚无封存捕获。</td></tr>')


def detail(bench, capture_id, *, reverified=False):
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
                     provenance=provenance(bench),
                     recipe=f'/workbench/recipes/{segment(record["name"])}/{record["version"]}',
                     name=text(record['name']), number=record['version'], stage=stage(record['state']),
                     digest=text(record['schema_digest']),
                     certificate=handoff.certificate(record, reverified=reverified),
                     actions=handoff.actions(base, record['state'], read_only=bench.read_only),
                     roots=roots, counts=preview.counts(record['summary']),
                     rows=record['summary']['rows'], witnesses=record['summary']['witnesses'])
