"""Browse persistent capture records without reading artifact BLOBs."""

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
                     digest=text(record['schema_digest']), sealed_at=text(sealed_at(record)),
                     certificate=handoff.certificate(record),
                     actions=handoff.actions(base, record['state']), roots=roots,
                     counts=preview.counts(record['summary']), rows=record['summary']['rows'],
                     witnesses=record['summary']['witnesses'])


def sealed_at(record):
    sealed = record.get('sealed')
    return sealed.get('sealed_at') if isinstance(sealed, dict) else None
