"""Browser pages for the source-schema watch list and change notifications."""

from ..codec import Fault
from ..watch import label, describe
from .forms import field
from .render import code, location, options, page, segment, text

STATE_LABELS = {
    'verified': ('连续核对一致', 'ready'),
    'changed': ('观测到结构变化', 'danger'),
    'missing': ('关注对象缺失', 'danger'),
    'reopened': ('结构相同但存在未观测区间', 'pending'),
    'unreachable': ('源库读不到', 'pending'),
    'baseline': ('已建基线，尚未核对', 'muted'),
}
OUTCOME_LABELS = {'observed': '成功读取目录', 'unreachable': '目录不可读'}
SEGMENT_LABELS = {
    'verified': '连续核对的范围',
    'unobserved': '结构相同但存在未观测区间',
    'change': '观测到的变化',
}


def state_badge(value):
    label_text, style = STATE_LABELS[value]
    return f'<span class="status {style}">{text(label_text)}</span>'


def source_choices(bench, active):
    aliases = sorted(bench.repository.sources)
    return options(((alias, alias) for alias in aliases), active)


def index(bench, query):
    selected = field(query, 'source')
    watches = bench.watches(selected or None)
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/watches/{segment(item["watch"]["id"])}">'
        f'{text(label(item["watch"]))}</a></th>'
        f'<td>{code(item["watch"]["source"])}</td><td>{state_badge(item["latest_state"])}</td>'
        f'<td>{text(item["latest_at"] or "—")}</td><td>{item["checks"]}</td>'
        f'<td>{unack_badge(item["unacknowledged"])}</td></tr>'
        for item in watches
    )
    return 200, page('源模式关注', 'watch', 'watches',
                     options=source_choices(bench, selected),
                     rows=rows or '<tr><td colspan="6" class="empty">尚无关注项。</td></tr>',
                     notifications=notification_links(bench))


def unack_badge(total):
    if not total:
        return '<span class="muted">无未确认</span>'
    return f'<span class="status danger">{total} 条待确认</span>'


def notification_links(bench):
    items = bench.notifications(only_unacknowledged=True)[:10]
    if not items:
        return '<p class="empty muted">没有待确认的通知。</p>'
    return '<ul class="data-list">' + ''.join(
        f'<li><a href="/workbench/notifications/{segment(item["id"])}">{text(item["summary"])}</a>'
        f' <span class="muted">{text(item["created_at"])}</span></li>' for item in items
    ) + '</ul>'


def new_form(bench, values=None, error='', status=200):
    values = values or {}
    alias = field(values, 'source', next(iter(sorted(bench.repository.sources)), ''))
    if alias not in bench.repository.sources:
        from .errors import message
        exc = Fault('unknown_source', '所选数据源未注册。', 404)
        return exc.status, page('登记关注', 'watch', 'watch_new',
                                options=source_choices(bench, alias), source=alias,
                                edge_choices='', pk_choices='',
                                continuity='3600', note='',
                                error=f'<p role="alert" class="notice">{text(message(exc))}</p>')
    try:
        catalog = bench.catalog(alias)
    except Fault as exc:
        from .errors import message
        return exc.status, page('登记关注', 'watch', 'watch_new',
                                options=source_choices(bench, alias), source=alias,
                                edge_choices='', pk_choices='',
                                continuity='3600', note='',
                                error=f'<p role="alert" class="notice">{text(message(exc))}</p>')
    schema = catalog['schema']
    edge_labels = [
        (f'edge:{edge["child"]}:{edge["id"]}',
         f'{edge["parent"]} → {edge["child"]}（{", ".join(edge["child_cols"])} → '
         f'{", ".join(edge["parent_cols"])}）外键 {edge["id"]}')
        for edge in schema['edges']
    ]
    pk_labels = [(f'pk:{name}', f'{name}（{" → ".join(meta["pk"])}）')
                 for name, meta in sorted(schema['tables'].items())]
    picked = field(values, 'subject')
    edge_choices = ''.join(
        f'<label class="edge-choice"><input type="radio" name="subject" value="{text(value)}"'
        f'{" checked" if value == picked else ""}> {text(caption)}</label>' for value, caption in edge_labels
    ) or '<p class="empty">此源库没有外键关系。</p>'
    pk_choices = ''.join(
        f'<label class="edge-choice"><input type="radio" name="subject" value="{text(value)}"'
        f'{" checked" if value == picked else ""}> {text(caption)}</label>' for value, caption in pk_labels
    )
    return status, page('登记关注', 'watch', 'watch_new',
                        options=source_choices(bench, alias), source=alias,
                        edge_choices=edge_choices, pk_choices=pk_choices,
                        continuity=text(field(values, 'continuity_seconds', '3600')),
                        note=text(field(values, 'note')),
                        error=f'<p role="alert" class="notice">{text(error)}</p>' if error else '')


def create(bench, values):
    alias = field(values, 'source')
    token = field(values, 'subject')
    parts = token.split(':')
    if len(parts) < 2 or parts[0] not in ('edge', 'pk'):
        return new_form(bench, values, '请选择要关注的外键关系或主键定义。', 400)
    body = {'source': alias, 'note': field(values, 'note') or None}
    raw_window = field(values, 'continuity_seconds', '3600')
    if not raw_window.isascii() or not raw_window.isdecimal():
        return new_form(bench, values, '连续核对窗口须为整数秒。', 400)
    body['continuity_seconds'] = int(raw_window)
    if parts[0] == 'edge':
        if len(parts) != 3 or not parts[2].isdecimal():
            return new_form(bench, values, '外键关注的选择无效。', 400)
        body.update(kind='edge', child=parts[1], id=int(parts[2]))
    else:
        body.update(kind='pk', table=parts[1])
    try:
        item = bench.create_watch(body)
    except Fault as exc:
        from .errors import message
        return new_form(bench, values, message(exc), exc.status)
    return location(f'/workbench/watches/{segment(item["watch"]["id"])}')


def segment_rows(segments):
    rows = []
    for seg in segments:
        if seg['kind'] == 'verified':
            detail = (f'{seg["checks"]} 次连续核对，{text(seg["from"])} 至 {text(seg["to"])}'
                      + ('（与基线一致）' if seg['matches_baseline'] else '（对应变化后的结构）'))
        elif seg['kind'] == 'unobserved':
            detail = (f'{text(seg["from"])} 至 {text(seg["to"])}，期间 {seg["unreachable_checks"]} 次读不到源库，'
                      f'跨度 {seg["duration_seconds"]:.0f} 秒；两端结构摘要相同')
        else:
            detail = f'{text(seg["at"])} 触发，状态 {STATE_LABELS[seg["state"]][0]}'
        rows.append(f'<tr><td><span class="status {"danger" if seg["kind"] == "change" else "ready" if seg["kind"] == "verified" else "pending"}">'
                    f'{SEGMENT_LABELS[seg["kind"]]}</span></td><td>{detail}</td>'
                    f'<td>{code((seg.get("fingerprint") or seg.get("to_fingerprint") or "")[:16])}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="3" class="empty">还没有核对记录。</td></tr>'


def timeline_rows(entries):
    rows = []
    for entry in entries:
        structure = describe(entry['structure'], entry['kind']) if entry.get('structure') else '—'
        rows.append(
            f'<tr><td>{text(entry["observed_at"])}</td><td>{text(OUTCOME_LABELS[entry["outcome"]])}</td>'
            f'<td>{state_badge(entry["state"])}</td>'
            f'<td>{code((entry["fingerprint"] or "—")[:16])}</td><td>{text(entry["reason"] or structure)}</td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="5" class="empty">尚无目录核对。</td></tr>'


def detail(bench, watch_id):
    item = bench.watch_detail(watch_id)
    watch = item['watch']
    notifications = ''.join(
        f'<li><a href="/workbench/notifications/{segment(note["id"])}">{text(note["summary"])}</a>'
        f' {state_badge("changed" if note["kind"] == "changed" else "unreachable")}'
        f' <span class="muted">{"已确认" if note["acknowledged"] else "待确认"} · {text(note["created_at"])}</span></li>'
        for note in item['notifications']
    )
    return 200, page('关注详情', 'watch', 'watch_detail',
                     heading=text(label(watch)), source=code(watch['source']),
                     kind=text('外键关系' if watch['kind'] == 'edge' else '主键定义'),
                     baseline=code(watch['baseline_fingerprint']),
                     continuity=watch['continuity_seconds'], note=text(watch['note'] or '—'),
                     state=state_badge(item['latest_state']),
                     checks=item['checks'],
                     segments=segment_rows(item['segments']),
                     timeline=timeline_rows([entry | {'kind': watch['kind']} for entry in item['timeline']]),
                     notifications=notifications or '<li class="empty">暂无通知。</li>',
                     watch_id=segment(watch_id), source_alias=segment(watch['source']))


def notifications(bench):
    items = bench.notifications()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/notifications/{segment(item["id"])}">{text(item["summary"])}</a></th>'
        f'<td>{text(label(item["watch"]))}</td>'
        f'<td>{state_badge("changed" if item["kind"] == "changed" else "unreachable")}</td>'
        f'<td>{text(item["created_at"])}</td>'
        f'<td>{"已确认" if item["acknowledged"] else "待确认"}</td></tr>'
        for item in items
    )
    return 200, page('模式通知', 'watch', 'notifications',
                     rows=rows or '<tr><td colspan="5" class="empty">尚无通知。</td></tr>')


def notification_detail(bench, notification_id):
    item = bench.notification_detail(notification_id)
    watch = item['watch']
    evidence_rows = ''.join(
        f'<tr><td>{text(entry["observed_at"])}</td><td>{text(OUTCOME_LABELS[entry["outcome"]])}</td>'
        f'<td>{state_badge(entry["state"])}</td><td>{code((entry["fingerprint"] or "—")[:16])}</td>'
        f'<td>{text(entry["reason"] or "—")}</td></tr>'
        for entry in item['evidence']
    )
    recipients = ''.join(
        f'<li>{code(row["name"])} 版本 {row["version"]} '
        f'<span class="muted">{"投用中" if row["active"] else "已停用"} · {text("、".join(row["usage"]))}</span></li>'
        for row in item['recipients']
    ) or '<li class="empty">当时没有配方版本使用这条结构。</li>'
    check = item['check']
    check_line = ('—' if check is None else
                  f'{check["observed_at"]} · {OUTCOME_LABELS[check["outcome"]]} · {text(check["reason"] or check["schema_digest"])}')
    return 200, page('通知详情', 'watch', 'notification',
                     summary=text(item['summary']), created=text(item['created_at']),
                     acknowledged='已确认' if item['acknowledged'] else '待确认',
                     watch_label=text(label(watch)),
                     watch_href=f'/workbench/watches/{segment(watch["id"])}',
                     check_line=text(check_line),
                     evidence=evidence_rows or '<tr><td colspan="5" class="empty">无证据。</td></tr>',
                     recipients=recipients,
                     notification_id=segment(notification_id),
                     ack_form='' if item['acknowledged'] else
                     f'<form method="post" action="/workbench/notifications/{segment(notification_id)}/acknowledge">'
                     '<button class="primary">确认已知悉</button></form>')


def acknowledge(bench, notification_id):
    bench.acknowledge(notification_id)
    return location(f'/workbench/notifications/{segment(notification_id)}')


def run_check(bench, source):
    return bench.check_source(source)
