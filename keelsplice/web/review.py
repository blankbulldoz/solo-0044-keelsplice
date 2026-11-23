"""Difference ledger pages: per-side evidence, review carry-forward and rebasing."""

from .forms import field
from .render import code, options, page, segment, text, typed
from ..codec import dumps

KIND_LABELS = {'added': '新增', 'removed': '移除', 'modified': '修改值', 'retarget': '关系换目标'}
VERDICT_LABELS = {'confirmed': '确认', 'rejected': '退回'}


def label(ledger, capture_id):
    record = ledger['captures'].get(capture_id) or {}
    if not record.get('name'):
        return code(capture_id[:12])
    base = f'/workbench/captures/{segment(capture_id)}'
    return f'<a href="{base}">{text(record["name"])} v{record["version"]}</a> {code(capture_id[:12])}'


def evidence_form(capture_id, table, key, caption):
    if capture_id is None:
        return f'<span class="muted">{text(caption)}：此行不存在</span>'
    base = f'/workbench/captures/{segment(capture_id)}'
    return (f'<form class="inline" method="post" action="{base}/trace">'
            f'<input type="hidden" name="table" value="{text(table)}">'
            f'<input type="hidden" name="key" value="{text(dumps(key))}">'
            f'<button class="quiet">{text(caption)}</button></form>')


def side_evidence(item, comparison):
    table, key = item['table'], item['key']
    before = evidence_form(comparison['baseline'] if item['baseline_side'] else None, table, key, '基准侧证据')
    after = evidence_form(comparison['candidate'] if item['candidate_side'] else None, table, key, '对照侧证据')
    return before, after


def relation_evidence(item, comparison):
    rows = []
    for relation in item.get('relations', []):
        pairs = []
        if relation['parent_before'] is not None:
            pairs.append(evidence_form(
                comparison['baseline'] if relation['parent_baseline'] else None,
                relation['parent'], relation['parent_before'], '旧父行证据'))
        if relation['parent_after'] is not None:
            pairs.append(evidence_form(
                comparison['candidate'] if relation['parent_candidate'] else None,
                relation['parent'], relation['parent_after'], '新父行证据'))
        missing = '<span class="muted">父行未入选</span>'
        cells = ' '.join(pairs) or missing
        rows.append(
            f'<tr><td>{code(relation["child"])} #{relation["id"]} → {code(relation["parent"])}</td>'
            f'<td>{typed(relation["before"])} → {typed(relation["after"])}</td>'
            f'<td>{cells}</td></tr>'
        )
    if not rows:
        return ''
    return ('<div class="scroll"><table><caption>关系换目标的两侧父行</caption><thead><tr>'
            '<th scope="col">外键</th><th scope="col">旧值 → 新值</th><th scope="col">父行证据</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def review_cell(item):
    state = item.get('state')
    review = item.get('review')
    if state == 'reviewed' and review:
        verdict = VERDICT_LABELS.get(review['verdict'], review['verdict'])
        note = f'：{text(review["note"])}' if review['note'] else ''
        return f'<span class="status ready">本次已{text(verdict)}</span>{note}'
    carried = item.get('prior_reviews') or []
    if state == 'carried' and carried:
        latest = carried[-1]
        verdict = VERDICT_LABELS.get(latest['verdict'], latest['verdict'])
        note = f'：{text(latest["note"])}' if latest['note'] else ''
        return ('<span class="status ready">沿用原判断</span> '
                f'<span class="muted">与第 {latest["ordinal"]} 次比较原样相同，'
                f'那次已{text(verdict)}{note}；本次不重复记为已核对</span>')
    return '<span class="status pending">待本次复核</span>'


def review_form(item, comparison):
    base = f'/workbench/ledgers/{segment(comparison["ledger"])}/review'
    return (f'<form class="inline" method="post" action="{base}">'
            f'<input type="hidden" name="comparison" value="{text(comparison["id"])}">'
            f'<input type="hidden" name="item" value="{text(item["token"])}">'
            '<select name="verdict"><option value="confirmed">确认</option>'
            '<option value="rejected">退回</option></select>'
            '<input name="note" maxlength="2000" placeholder="复核说明">'
            '<button class="quiet">记录复核</button></form>')


def item_rows(comparison, ledger):
    rows = []
    for item in comparison['items']:
        before, after = side_evidence(item, comparison)
        changed = f'（列 {text("、".join(item["changed"]))}）' if item.get('changed') else ''
        rows.append(
            f'<tr><th scope="row">{code(item["table"])}</th>'
            f'<td>{KIND_LABELS.get(item["kind"], item["kind"])}{changed}</td>'
            f'<td>{typed(item["key"])}</td><td>{before}</td><td>{after}</td>'
            f'<td>{review_cell(item)}</td><td>{review_form(item, comparison)}</td></tr>'
        )
        relation = relation_evidence(item, comparison)
        if relation:
            rows.append(f'<tr><td></td><td colspan="6">{relation}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="7" class="empty">本次比较没有行差异。</td></tr>')
    return ''.join(rows)


def withdrawn_rows(ledger):
    rows = []
    for item in ledger['withdrawn']:
        review = item.get('review') or {}
        verdict = VERDICT_LABELS.get(review.get('verdict'), review.get('verdict', '未复核'))
        rows.append(
            f'<tr><th scope="row">{code(item["table"])}</th>'
            f'<td>{KIND_LABELS.get(item["kind"], item["kind"])}</td>'
            f'<td>{typed(item["key"])}</td>'
            f'<td>第 {item["ordinal"]} 次比较 · {text(verdict)}</td>'
            f'<td><a href="/workbench/ledgers/{segment(ledger["ledger"]["name"])}'
            f'?comparison={segment(item["comparison"])}">原比较</a></td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="5" class="empty">没有已撤销的差异。</td></tr>'


def reopened_rows(ledger):
    rows = []
    for entry in ledger['reopened']:
        earlier = '；'.join(
            '第 {} 次比较{}（{}）'.format(
                item['ordinal'],
                VERDICT_LABELS.get(item['verdict'], '未复核'),
                KIND_LABELS.get(item.get('kind'), item.get('kind', '')))
            for item in entry['earlier']
        )
        rows.append(
            f'<tr><th scope="row">{code(entry["table"])}</th><td>{typed(entry["key"])}</td>'
            f'<td>{KIND_LABELS.get(entry.get("kind"), entry.get("kind", ""))}</td>'
            f'<td>{text(earlier)}</td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="4" class="empty">同一键没有出现另一种值。</td></tr>'


def capture_choices(bench, active=''):
    records = bench.repository.recent_captures()
    available = [('', '请选择捕获')] + [
        (row['id'], f'{row["name"]} v{row["version"]} · {row["state"]} · {row["id"][:12]}')
        for row in records
    ]
    known = {row['id'] for row in records}
    if active and active not in known:
        available.append((active, active))
    return options(available, active)


def history_rows(bench, ledger, base):
    rows = []
    current = ledger['chosen']
    baseline = ledger['ledger']['baseline']
    for row in ledger['comparisons']:
        active = ' aria-current="true"' if current and row['id'] == current['id'] else ''
        origin = '当前系列' if row['baseline'] == baseline else '旧基准系列'
        rebase = ''
        if row['candidate'] != baseline:
            rebase = (f'<form class="inline" method="post" action="{base}/rebase">'
                      f'<input type="hidden" name="baseline" value="{text(row["candidate"])}">'
                      '<button class="quiet">设为新基准</button></form>')
        rows.append(
            f'<tr{active}><th scope="row"><a href="{base}?comparison={segment(row["id"])}">'
            f'第 {row["ordinal"]} 次</a></th>'
            f'<td>{label(ledger, row["baseline"])}</td>'
            f'<td>{label(ledger, row["candidate"])}</td>'
            f'<td>{len(row["items"])}</td><td>{text(origin)}</td><td>{rebase}</td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="6" class="empty">尚未执行比较。</td></tr>'


def index(bench, values=None):
    values = values or {}
    records = bench.repository.recent_ledgers()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/ledgers/{segment(row["name"])}">'
        f'{text(row["name"])}</a></th><td>{code(row["baseline"][:12])}</td>'
        f'<td>{row["comparisons"]}</td></tr>'
        for row in records
    )
    return 200, page('差异清单', 'ledger', 'ledgers', rows=rows or
                     '<tr><td colspan="3" class="empty">还没有差异清单。</td></tr>',
                     captures=capture_choices(bench),
                     name=text(field(values, 'name')), error='')


def detail(bench, name, query=None):
    query = query or {}
    selected = field(query, 'comparison') or None
    ledger = bench.ledger(name, selected)
    record = ledger['ledger']
    chosen = ledger['chosen']
    base = f'/workbench/ledgers/{segment(name)}'
    items = item_rows(chosen, ledger) if chosen else (
        '<tr><td colspan="7" class="empty">先选择一个对照捕获执行比较。</td></tr>')
    return 200, page('差异清单', 'ledger', 'ledger', name=text(name), base=base,
                     baseline=label(ledger, record['baseline']),
                     digest=code(record['schema_digest'][:12]),
                     captures=capture_choices(bench),
                     history=history_rows(bench, ledger, base), items=items,
                     selected=f'第 {chosen["ordinal"]} 次比较' if chosen else '尚无比较',
                     withdrawn=withdrawn_rows(ledger), reopened=reopened_rows(ledger))


def create(bench, values):
    name = field(values, 'name')
    baseline = field(values, 'baseline')
    bench.open_ledger(dict(name=name, baseline=baseline))
    return f'/workbench/ledgers/{segment(name)}'


def run_comparison(bench, name, values):
    bench.record_comparison(name, dict(candidate=field(values, 'candidate')))
    return f'/workbench/ledgers/{segment(name)}'


def rebase(bench, name, values):
    bench.rebase(name, dict(baseline=field(values, 'baseline')))
    return f'/workbench/ledgers/{segment(name)}'


def review(bench, name, values):
    bench.review(field(values, 'comparison'), dict(
        item=field(values, 'item'), verdict=field(values, 'verdict'),
        note=field(values, 'note')))
    return f'/workbench/ledgers/{segment(name)}'
