"""Browser pages for receiving-device registration and physical trial delivery."""

from .errors import message
from .forms import field
from .render import code, page, segment, template, text, typed
from ..codec import Fault, dumps

VERDICT_LABEL = {'passed': ('通过', 'ready'), 'degraded': ('降级', 'pending'), 'failed': ('未通过', 'muted')}
CLASS_LABEL = {'integer': '整数', 'real': '实数', 'text': '文本', 'blob': 'BLOB', 'null': 'NULL'}
CHECK_LABEL = [
    ('null', 'NULL 保真'), ('negative_zero', '负零保真'), ('blob', 'BLOB 保真'),
    ('int64', 'int64 保真'), ('text', '文本/存储类型保真'), ('real', '实数保真'),
    ('platform_normalization', '平台亲和性归一（非中继失真）'),
]


def verdict_badge(verdict):
    label, style = VERDICT_LABEL[verdict]
    return f'<span class="status {style}">{label}</span>'


def device_rows(bench):
    rows = []
    for record in bench.list_receivers():
        profile = record['profile']
        classes = ', '.join(CLASS_LABEL[name] for name in profile['storage_classes'])
        origin = '内置' if record['builtin'] else '登记'
        rows.append(
            f'<tr><th scope="row">{code(profile["name"])}</th><td>{text(profile["relay"])}</td>'
            f'<td>{text(classes)}</td><td>{origin}</td><td>{text(profile["notes"])}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="5" class="empty">尚未登记接收设备。</td></tr>'


def index(bench):
    return 200, page('接收设备', 'receiver', 'receivers', rows=device_rows(bench))


def register(bench, values):
    if not values:
        return 200, page('登记接收设备', 'receiver', 'receiver_editor',
                         name='', relay_options=_relay_options('text'), classes='', notes='', error='')
    try:
        body = dict(
            name=field(values, 'name'), relay=field(values, 'relay'),
            storage_classes=[item for item in ('integer', 'real', 'text', 'blob', 'null')
                             if item in values.get('classes', [])],
            notes=field(values, 'notes'))
        record = bench.register_receiver(body)
    except Fault as exc:
        return exc.status, page('登记接收设备', 'receiver', 'receiver_editor',
                                name=text(field(values, 'name')),
                                relay_options=_relay_options(field(values, 'relay')),
                                classes=text(dumps(body.get('storage_classes', []))),
                                notes=text(field(values, 'notes')),
                                error=f'<p role="alert" class="notice">{text(message(exc))}</p>')
    return 200, page('接收设备', 'receiver', 'receiver_created',
                     name=text(record['profile']['name']))


def _relay_options(active):
    options = [('typed', '类型化中继（五类存储值各自独立）'), ('text', '纯文本中继（按列亲和性重新解释）')]
    return ''.join(f'<option value="{value}"{" selected" if value == active else ""}>{text(label)}</option>'
                   for value, label in options)


def _finding_rows(items, kind):
    rows = []
    for item in items:
        location = f'{item["table"]}.{item.get("column", "")}'.rstrip('.')
        rows.append(
            f'<tr><td>{code(kind)}</td><td>{code(location)}</td>'
            f'<td>{typed(item.get("primary_key", item.get("primary_key_requested", "")))}</td>'
            f'<td>{text(item.get("expected", ""))}</td><td>{text(item.get("actual_repr", ""))}</td>'
            f'<td>{text(item["detail"])}</td></tr>')
    return rows


def _value_section(report):
    vf = report['value_findings']
    rows = []
    for kind, label in CHECK_LABEL:
        rows.extend(_finding_rows(vf[kind], label))
    if not rows:
        rows.append('<tr><td colspan="6" class="empty">所有可比较单元格的值与存储类型均保真。</td></tr>')
    note = ''
    if vf.get('platform_normalization'):
        note = ('<p class="notice info">下列负零差异来自 SQLite 在 REAL/NUMERIC 亲和列的固有归一行为，'
                '源库与接收库表现一致，不计为中继设备失真，也不改变交付结论。</p>')
    return note + template('trial_value_rows', rows=''.join(rows))


def detail(bench, trial_id):
    record = bench.trial(trial_id)
    report = record['report']
    rejected = ''.join(
        f'<tr><td>{code(item["table"])}</td><td>{item["row_index"]}</td>'
        f'<td>{typed(item["primary_key_requested"])}</td><td>{text(item["reason"])}</td></tr>'
        for item in report['rejected_rows']
    ) or '<tr><td colspan="4" class="empty">没有行被接收库拒绝。</td></tr>'
    pk = ''.join(
        f'<tr><td>{code(item["table"])}</td><td>{typed(item["lost_primary_keys"])}</td>'
        f'<td>{typed(item["collapsed_identities"])}</td></tr>'
        for item in report['primary_key_findings']
    ) or '<tr><td colspan="3" class="empty">重新打开后主键仍可区分，没有身份坍缩。</td></tr>'
    rel = ''.join(
        f'<tr><td>{typed(item["edge"])}</td><td>{typed(item["child_primary_key"])}</td>'
        f'<td>{text(item["effect"])}</td><td>{text(item["detail"])}</td></tr>'
        for item in report['relationship_findings']
    ) or '<tr><td colspan="4" class="empty">复合外键重新打开后仍能定位唯一原父行。</td></tr>'
    fk = ''.join(
        f'<tr><td>{code(item["table"])}</td><td>{item["rowid"]}</td>'
        f'<td>{code(item["parent"])}</td><td>{item["foreign_key"]}</td></tr>'
        for item in report['foreign_key_violations']
    ) or '<tr><td colspan="4" class="empty">PRAGMA foreign_key_check 未报告违例。</td></tr>'
    unsupported = ''.join(
        f'<tr><td>{code(item["table"])}</td><td>{code(item["column"])}</td>'
        f'<td>{text(CLASS_LABEL[item["storage_class"]])}</td><td>{text(item["value_repr"])}</td></tr>'
        for item in report.get('unsupported_cells', [])
    ) or '<tr><td colspan="4" class="empty">设备载体覆盖了全部出现的存储类型。</td></tr>'
    impacted = ''.join(
        f'<tr><td>{typed(item["edge"])}</td><td>{typed(item["foreign_key_columns"])}</td>'
        f'<td>{text(item["detail"])}</td></tr>'
        for item in report['value_findings']['relationships']
    ) or '<tr><td colspan="3" class="empty">没有外键分量受到值表示变化影响。</td></tr>'
    counts = ''.join(
        f'<tr><th scope="row">{code(name)}</th><td>{report["expected_counts"][name]}</td>'
        f'<td>{report["reopened_counts"][name]}</td></tr>'
        for name in sorted(report['expected_counts'])
    )
    return 200, page('实物试交付报告', 'receiver', 'trial',
                     id=text(trial_id), device=text(report['device']), relay=text(report['relay']),
                     verdict=verdict_badge(report['verdict']),
                     integrity='是' if report['integrity_ok'] else '否',
                     schema='一致' if report['schema_match'] else '不一致',
                     count_match='一致' if report['row_counts_match'] else '不一致（不能仅凭行数判定成功）',
                     sha=text(report['sha256']), bytes=report['bytes'],
                     payload_sha=text(report['payload_sha256']),
                     counts=counts, rejected=rejected, pk=pk, rel=rel, fk=fk,
                     unsupported=unsupported, impacted=impacted, values=_value_section(report))


def start(bench, capture_id, values):
    device = field(values, 'device')
    trial_id, _ = bench.deliver_trial(capture_id, {'device': device})
    return f'/workbench/trials/{segment(trial_id)}'
