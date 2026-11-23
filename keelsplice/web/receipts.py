"""Record and review recipient acknowledgements of a certified fixture."""

from .errors import message
from .forms import receipt_fields
from .render import code, page, segment, template, text
from ..codec import Fault, dumps

STATUSES = {'confirmed': ('已签收', 'ready'), 'mismatch': ('未签收', 'muted'), 'ambiguous': ('待确认', 'pending')}
REASONS = {'sha256': '摘要不符', 'counts': '表计数不符', 'ambiguous': '同一摘要对应多个认证捕获',
           'uncertified': '捕获已不再处于认证状态'}


def status(row):
    label, style = STATUSES[row['status']]
    detail = '、'.join(REASONS[reason] for reason in row['reasons'])
    return f'<span class="status {style}">{label}</span>' + (f' <span class="muted">{text(detail)}</span>' if detail else '')


def counts_text(counts):
    return '' if counts is None else dumps(dict(sorted(counts.items())))


def receipt_rows(base, rows, capture_id=None):
    table = []
    for row in rows:
        superseded = row['superseded_by']
        follow = ''
        if superseded:
            follow = f'<span class="muted">更正为 <a href="{base}/receipts/{segment(superseded[0])}">' \
                     f'{code(superseded[0][:12])}</a>（旧记录保留）</span>'
        elif capture_id is not None:
            follow = f'<a class="button quiet" href="{base}/receipts/{segment(row["id"])}">登记更正</a>'
        matched = ''
        if row['status'] == 'ambiguous' and row['matched']:
            matched = '<br><span class="muted">同一证据还匹配：' + ', '.join(
                f'<a href="/workbench/captures/{segment(item)}">{code(item[:12])}</a>' for item in row['matched']
                if item != capture_id) + '</span>'
        recipient = text(row['recipient']) or '<span class="muted">未填写</span>'
        table.append(
            f'<tr><th scope="row"><a href="{base}/receipts/{segment(row["id"])}">{code(row["id"][:12])}</a></th>'
            f'<td>{status(row)}{matched}</td><td>{code(row["sha256"])}</td>'
            f'<td>{recipient}</td>'
            f'<td>{text(row["created"])}</td><td>{follow}</td></tr>'
        )
    return ''.join(table) or '<tr><td colspan="6" class="empty">尚无签收记录。</td></tr>'


def summary_line(summary):
    if not summary['current']:
        return '<p class="notice info">此捕获尚无签收记录，不能视为已交接。</p>'
    if summary['signed']:
        return (f'<p class="notice info">已签收：当前 {summary["current"]} 条记录中有 '
                f'{summary["confirmed"]} 条能唯一确认本捕获。</p>')
    return (f'<p class="notice">未签收：当前 {summary["current"]} 条记录均不能唯一确认本捕获'
            f'（另有 {summary["corrected"]} 条已被更正，保留在下方可复查）。</p>')


def echoed(values, name):
    """Read a form value for redisplay, accepting both parsed forms and plain text."""
    value = values.get(name, '')
    if isinstance(value, list):
        return value[0] if value else ''
    return value


def show(bench, capture_id, values=None, error='', status_code=200):
    record = bench.status(capture_id)
    base = f'/workbench/captures/{segment(capture_id)}'
    listing = bench.receipts(capture_id)
    corrects = echoed(values or {}, 'corrects')
    certified = record['state'] == 'certified'
    form = template('receipt_form', base=base, corrects=text(corrects),
                    sha256=text(echoed(values or {}, 'sha256')),
                    recipient=text(echoed(values or {}, 'recipient')),
                    note=text(echoed(values or {}, 'note')),
                    counts=text(echoed(values or {}, 'counts')))
    return status_code, page('签收交接', 'capture', 'receipts',
                             base=base, id=text(capture_id),
                             digest=text((record['certificate'] or {}).get('sha256', '')),
                             summary=summary_line(listing['summary']),
                             rows=receipt_rows(base, listing['receipts'], capture_id),
                             form=(form if certified else
                                   '<p class="notice">子库通过认证后才能登记签收。</p>'),
                             correcting=(f'<p class="notice info">正在更正记录 '
                                         f'<code>{text(corrects[:12])}</code>；保存后会新增一条记录，'
                                         f'旧记录保留可复查。</p>' if corrects else ''),
                             error=f'<p class="notice" role="alert">{text(error)}</p>' if error else '')


def record(bench, capture_id, values):
    """Register a receipt; on invalid evidence redisplay the form with all inputs."""
    try:
        result = bench.acknowledge(capture_id, receipt_fields(values))
    except Fault as exc:
        return show(bench, capture_id, values, message(exc), exc.status)
    return 303, None, '/workbench/captures/' + segment(capture_id) + '/receipts/' + segment(result['id'])


def detail(bench, capture_id, receipt_id):
    base = f'/workbench/captures/{segment(capture_id)}'
    record = bench.receipt(receipt_id)
    if record['capture_id'] != capture_id:
        raise Fault('unknown_receipt', 'Receipt belongs to another capture', 404)
    listing = bench.receipts(capture_id)
    counts = code(counts_text(record['counts'])) if record['counts'] is not None else '<span class="muted">仅摘要</span>'
    matched = ', '.join(f'<a href="/workbench/captures/{segment(item)}">{code(item[:12])}</a>'
                        for item in record['matched']) or '<span class="muted">无</span>'
    return 200, page('签收记录', 'capture', 'receipt',
                     base=base, id=text(record['id']), capture_status=status(record),
                     sha256=code(record['sha256']), counts=counts,
                     certificate_sha256=code(record['certificate_sha256']),
                     capture_digest=code(record['capture_digest']), matched=matched,
                     registered=f'{status({"status": record["registered_status"], "reasons": record["registered_reasons"]})}'
                                f' <span class="muted">{text(record["created"])} 登记时</span>',
                     recipient=text(record['recipient']) or '未填写', note=text(record['note']) or '未填写',
                     corrects=code(record['corrects']) if record['corrects'] else '无',
                     superseded='、'.join(f'<a href="{base}/receipts/{segment(item)}">{code(item[:12])}</a>'
                                          for item in record['superseded_by']) or '当前有效记录',
                     created=text(record['created']),
                     capture_id=text(record['capture_id']),
                     summary=summary_line(listing['summary']),
                     rows=receipt_rows(base, listing['receipts'], record['capture_id']))


def correct(bench, capture_id, receipt_id):
    """Prefill the handoff form from the receipt being corrected."""
    record = bench.receipt(receipt_id)
    base = f'/workbench/captures/{segment(capture_id)}'
    if record['capture_id'] != capture_id:
        raise Fault('unknown_receipt', 'Receipt belongs to another capture', 404)
    if record['superseded_by']:
        raise Fault('invalid_correction', '这条记录已被更正，请从最新记录继续。', 409)
    values = {'sha256': record['sha256'], 'recipient': record['recipient'], 'note': record['note'],
              'counts': counts_text(record['counts']), 'corrects': record['id']}
    return show(bench, capture_id, values)
