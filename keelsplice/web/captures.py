"""Browse persistent capture records without reading artifact BLOBs."""

from . import handoff, identity, preview
from .render import code, page, segment, text

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


def roots(base, addresses):
    """Each root is a trace form carrying its own exact key and a readable gloss."""
    return ''.join(
        f'<li><form class="inline" method="post" action="{base}/trace">'
        f'{identity.fields(table, key)}'
        f'<span>{identity.identifier(table)} {identity.pair(key)}</span>'
        '<button class="quiet">追踪此根</button></form></li>'
        for table, key in addresses
    )


def detail(bench, capture_id):
    record = bench.status(capture_id)
    base = f'/workbench/captures/{segment(capture_id)}'
    return 200, page('捕获详情', 'capture', 'capture', id=text(capture_id), base=base,
                     recipe=f'/workbench/recipes/{segment(record["name"])}/{record["version"]}',
                     name=text(record['name']), number=record['version'], stage=stage(record['state']),
                     digest=text(record['schema_digest']), certificate=handoff.certificate(record),
                     actions=handoff.actions(base, record['state']),
                     roots=roots(base, record['roots']), legend=identity.legend(),
                     counts=preview.counts(record['summary']), rows=record['summary']['rows'],
                     witnesses=record['summary']['witnesses'])
