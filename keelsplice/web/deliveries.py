"""Run weekly per-group hand-over batches and review their closed records."""

from . import captures
from .errors import message
from .forms import field
from .render import code, options, page, segment, text
from ..codec import Fault

DECISIONS = {'pending': ('待判断', 'muted'), 'continued': ('继续跟进', 'pending'),
             'held': ('暂缓', 'pending'), 'settled': ('已销账', 'ready')}
OUTCOMES = {'delivered': ('已交付', 'ready'), 'reissue': ('待补发', 'pending'), 'held': ('暂缓', 'muted')}
STATES = {'open': ('交付中', 'pending'), 'closed': ('已关闭', 'muted')}
KINDS = {'initial': '首发', 'reissue': '补发'}


def badge(labels, key):
    label, style = labels[key]
    return f'<span class="status {style}">{label}</span>'


def notice(error):
    return f'<p role="alert" class="notice">{text(error)}</p>' if error else ''


def index(bench, error='', status=200):
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/batches/{segment(batch["id"])}">{text(batch["label"])}</a></th>'
        f'<td>{badge(STATES, batch["state"])}</td><td>{batch["entries"]}</td>'
        f'<td>{batch["delivered"] if batch["state"] == "closed" else "—"}</td></tr>'
        for batch in bench.repository.recent_batches()
    )
    return status, page('交付批次', 'delivery', 'batches', error=notice(error),
                        rows=rows or '<tr><td colspan="4" class="empty">尚无交付批次。</td></tr>')


def create(bench, values):
    try:
        batch = bench.create_batch(dict(label=field(values, 'label')))
    except Fault as exc:
        return index(bench, message(exc), exc.status)
    return f'/workbench/batches/{segment(batch["id"])}'


def capture_options(bench):
    recent = bench.repository.recent_captures()
    choices = [('', '请选择捕获')] + [
        (row['id'], f'{row["name"]} 版本 {row["version"]} · {row["id"][:12]} · {captures.STAGES[row["state"]]}')
        for row in recent
    ]
    return options(choices, '')


def recipient_list(bench):
    known = bench.repository.known_recipients()
    if not known:
        return ''
    return '<datalist id="recipient-list">' + ''.join(
        f'<option value="{text(name)}"></option>' for name in known) + '</datalist>'


def capture_cell(entry):
    href = f'/workbench/captures/{segment(entry["capture"])}'
    return (f'<a href="{href}">{code(entry["capture"][:12])}</a>'
            f'<br><span class="muted">{text(entry["name"])} 版本 {entry["version"]}</span>')


def open_row(base, entry):
    claim = ''
    if entry['capture_state'] == 'certified':
        claim = f' <a href="/workbench/captures/{segment(entry["capture"])}/database">领用</a>'
    return (
        f'<tr><th scope="row">{capture_cell(entry)}</th>'
        f'<td>{text(entry["recipient"])}</td><td>{KINDS[entry["kind"]]}</td>'
        f'<td>{captures.stage(entry["capture_state"])}{claim}</td>'
        f'<td>{badge(DECISIONS, entry["decision"])}</td>'
        f'<td><form class="decide-form" method="post" action="{base}/decide">'
        f'<input type="hidden" name="delivery" value="{text(entry["id"])}">'
        f'<input name="note" maxlength="200" placeholder="回执或未发原因" value="{text(entry["note"])}" aria-label="备注">'
        f'<button class="quiet" name="action" value="settle">销账</button>'
        f'<button class="quiet" name="action" value="continue">继续</button>'
        f'<button class="quiet" name="action" value="hold">暂缓</button></form></td></tr>'
    )


def closed_row(entry):
    if entry['outcome'] == 'delivered':
        sent = f'{code(entry["sent"]["sha256"])}<br><span class="muted">{entry["sent"]["bytes"]} 字节</span>'
    else:
        sent = '—'
    reason = entry['outcome_reason'] or entry['note']
    return (f'<tr><th scope="row">{capture_cell(entry)}</th>'
            f'<td>{text(entry["recipient"])}</td><td>{KINDS[entry["kind"]]}</td>'
            f'<td>{badge(OUTCOMES, entry["outcome"])}</td><td>{sent}</td>'
            f'<td>{text(reason) if reason else "—"}</td></tr>')


def detail(bench, batch_id, error='', status=200):
    batch = bench.batch_detail(batch_id)
    base = f'/workbench/batches/{segment(batch_id)}'
    entries = batch['entries']
    toolbar, close = '', ''
    if batch['state'] == 'open':
        head = ('<tr><th scope="col">捕获</th><th scope="col">组别</th><th scope="col">类型</th>'
                '<th scope="col">认证状态</th><th scope="col">判断</th><th scope="col">判断操作与备注</th></tr>')
        rows = ''.join(open_row(base, entry) for entry in entries)
        empty = '<tr><td colspan="6" class="empty">尚无交付项。上一批的待补发项会在开立时自动列入，也可逐项登记。</td></tr>'
        pending = sum(1 for entry in entries if entry['decision'] == 'pending')
        summary = f'<div><dt>待判断</dt><dd>{pending}</dd></div>'
        caption = '按实际认证状态与回执逐项判断；关闭批次后结果不可更改'
        toolbar = (
            f'<form class="compare-form" method="post" action="{base}/add">'
            f'<label>封存捕获<select name="capture" required>{capture_options(bench)}</select></label>'
            f'<label>交付组别<input name="recipient" list="recipient-list" required maxlength="64"'
            f' placeholder="例如 甲组"></label>{recipient_list(bench)}'
            f'<button class="primary">登记交付项</button></form>'
        )
        close = (
            f'<div class="actions"><form method="post" action="{base}/close">'
            f'<button class="primary">关闭批次并正式交付</button></form>'
            f'<p class="muted">关闭时冻结每笔结果：已核销项记录实际发出的字节摘要，未发项记录未发原因；'
            f'待补发项自动列入下一批。</p></div>'
        )
    else:
        head = ('<tr><th scope="col">捕获</th><th scope="col">组别</th><th scope="col">类型</th>'
                '<th scope="col">结果</th><th scope="col">实际发出的字节摘要</th><th scope="col">未发原因 / 备注</th></tr>')
        rows = ''.join(closed_row(entry) for entry in entries)
        empty = '<tr><td colspan="6" class="empty">此批次没有交付项。</td></tr>'
        tally = {key: sum(1 for entry in entries if entry['outcome'] == key) for key in OUTCOMES}
        summary = (f'<div><dt>已交付</dt><dd>{tally["delivered"]}</dd></div>'
                   f'<div><dt>待补发</dt><dd>{tally["reissue"]}</dd></div>'
                   f'<div><dt>暂缓</dt><dd>{tally["held"]}</dd></div>')
        caption = '关闭时的正式交付记录，含当时实际发出的字节摘要与未发原因'
    return status, page(f'交付批次 {batch["label"]}', 'delivery', 'batch',
                        label=text(batch['label']), id=text(batch['id']),
                        stage=badge(STATES, batch['state']), total=len(entries), summary=summary,
                        error=notice(error), toolbar=toolbar, caption=caption, head=head,
                        rows=rows or empty, close=close)


def add(bench, batch_id, values):
    try:
        bench.add_delivery(batch_id, dict(capture=field(values, 'capture'),
                                          recipient=field(values, 'recipient')))
    except Fault as exc:
        if exc.status == 404:
            raise
        return detail(bench, batch_id, message(exc), exc.status)
    return f'/workbench/batches/{segment(batch_id)}'


def decide(bench, batch_id, values):
    try:
        bench.decide_delivery(batch_id, field(values, 'delivery'),
                              dict(action=field(values, 'action'), note=field(values, 'note')))
    except Fault as exc:
        if exc.status == 404:
            raise
        return detail(bench, batch_id, message(exc), exc.status)
    return f'/workbench/batches/{segment(batch_id)}'


def close(bench, batch_id):
    try:
        bench.close_batch(batch_id)
    except Fault as exc:
        if exc.status == 404:
            raise
        return detail(bench, batch_id, message(exc), exc.status)
    return f'/workbench/batches/{segment(batch_id)}'
