"""Workbench pages for handoff revisit packages and independent rechecks."""

import json

from .forms import field, typed_json
from .render import code, page, segment, template, text
from ..codec import Fault, dumps
from ..revisit import BUCKETS, MATERIAL_FILES

BUCKET_LABEL = dict(BUCKETS)
BUCKET_STYLE = {'equivalent': 'ready', 'change': 'pending', 'unverified': 'muted'}
MAX_FORM_BYTES = 4 * 1024 * 1024


def bucket_chip(bucket):
    return f'<span class="status {BUCKET_STYLE[bucket]}">{text(BUCKET_LABEL[bucket])}</span>'


def capture_choices(bench, selected):
    rows = bench.repository.recent_captures()
    ids = [(row['id'], f'{row["name"]} 版本 {row["version"]} · {row["id"][:12]} · {row["state"]}')
           for row in rows]
    options = ''.join(
        '<option value="{}"{}>{}</option>'.format(text(value), ' selected' if value == selected else '', text(label))
        for value, label in [('', '请选择已认证交付'), *ids])
    if selected and selected not in {value for value, _ in ids}:
        options += f'<option value="{text(selected)}" selected>{text(selected)}</option>'
    return options


def index(bench, values=None, error='', status=200):
    values = values or {}
    records = bench.list_revisits()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/revisits/{segment(record["id"])}">'
        f'{code(record["id"][:12])}…</a></th>'
        f'<td><a href="/workbench/captures/{segment(record["capture_id"])}">{code(record["capture_id"][:12])}…</a></td>'
        f'<td>{text(record["name"])} v{record["version"]}</td>'
        f'<td><code>{text(record["anchor_sha"][:12])}…</code></td>'
        f'<td>{text(record["note"] or "—")}</td></tr>'
        for record in records
    )
    return status, page('交接回访', 'revisit', 'revisits',
                        captures=capture_choices(bench, field(values, 'capture')),
                        bundle=text(field(values, 'bundle')),
                        note=text(field(values, 'note')),
                        rows=rows or '<tr><td colspan="5" class="empty">尚无回访记录。</td></tr>',
                        error=f'<p role="alert" class="notice">{text(error)}</p>' if error else '')


def submit(bench, values):
    capture_id = field(values, 'capture')
    if not capture_id:
        return index(bench, values, '请选择对应的已认证交付。', 400)
    try:
        body = typed_json(field(values, 'bundle'))
        if not isinstance(body, dict):
            raise Fault('invalid_revisit', '材料包须是 JSON 对象。')
        body['note'] = field(values, 'note')
        result = bench.submit_revisit(capture_id, body)
    except Fault as exc:
        from .errors import message
        return index(bench, values, message(exc), exc.status)
    return 200, page('交接回访', 'revisit', 'revisit_detail', **_detail_fields(bench, result))

def _report_sections(report):
    sections = []
    for bucket, label in BUCKETS:
        items = report['buckets'][bucket]
        rows = []
        for item in items:
            files = ''.join(f'<li>{code(name)}</li>' for name in item['material_files'])
            rows.append(
                '<tr><th scope="row">{}</th><td>{}</td><td>{}</td>'
                '<td><ul class="file-refs">{}</ul></td></tr>'.format(
                    code(item['subject']), text(item['detail']), text(item['recheck']), files))
        if not rows:
            rows.append('<tr><td colspan="4" class="empty">无此类结论。</td></tr>')
        sections.append(template('revisit_bucket', bucket=bucket_chip(bucket), label=text(label),
                                 count=len(items), rows=''.join(rows)))
    return ''.join(sections)


def _recheck_options(report, selected=''):
    parts = []
    for bucket, items in report['buckets'].items():
        for item in items:
            subject = item['subject']
            chosen = ' selected' if subject == selected else ''
            parts.append('<option value="{}"{}>{}</option>'.format(
                text(subject), chosen, text(f'[{BUCKET_LABEL[bucket]}] {subject}')))
    return ''.join(parts)


def _detail_fields(bench, result, selected_subject='', recheck_html=''):
    report = result['report']
    materials = ''.join(
        f'<li><a href="/workbench/revisits/{segment(result["id"])}/materials/{text(name)}">{code(name)}</a></li>'
        for name in MATERIAL_FILES)
    return dict(id=text(result['id']), capture=text(result['capture_id']),
                capture_href=f'/workbench/captures/{segment(result["capture_id"])}',
                anchor=text(result['anchor_sha256']), note=text(result['note'] or '—'),
                overall=bucket_chip(report['overall']),
                summary=text(json.dumps(report['summary'], ensure_ascii=False)),
                sections=_report_sections(report), materials=materials,
                recheck_options=_recheck_options(report, selected_subject), recheck=recheck_html)


def detail(bench, revisit_id):
    result = bench.revisit_status(revisit_id)
    return 200, page('回访详情', 'revisit', 'revisit_detail', **_detail_fields(bench, result))


def material_page(bench, revisit_id, filename):
    if filename not in MATERIAL_FILES:
        raise Fault('unknown_route', '找不到指定材料文件。', 404)
    name, material = bench.revisit_material(revisit_id, filename)
    body = dumps(material)
    return 200, page('回访材料', 'revisit', 'revisit_material',
                     revisit=text(revisit_id), filename=code(name),
                     material_text=text(body),
                     back=f'/workbench/revisits/{segment(revisit_id)}')


def recheck(bench, revisit_id, values):
    subject = field(values, 'subject')
    try:
        result = bench.recheck_revisit(revisit_id, dict(subject=subject))
    except Fault as exc:
        from .errors import message
        return _recheck_response(bench, revisit_id, subject, message(exc), None)
    holds_value = result['independent_evidence']['holds']
    holds = {True: '复查成立', False: '复查不成立', None: '服务端无法独立复算，需现场判断'}[holds_value]
    evidence = (
        f'<p>{bucket_chip(result["bucket"])} <code>{text(result["subject"])}</code></p>'
        f'<p>{text(result["independent_evidence"]["basis"])}</p>'
        f'<p>独立复查结论：<strong>{text(holds)}</strong></p>'
        f'<pre>{text(json.dumps(result["independent_evidence"], ensure_ascii=False, sort_keys=True))}</pre>'
        f'<p>原结论依据文件：{("".join(code(f) for f in result["material_files"]))}</p>')
    return _recheck_response(bench, revisit_id, subject, '', evidence)


def _recheck_response(bench, revisit_id, subject, error, evidence):
    result = bench.revisit_status(revisit_id)
    recheck_html = (f'<p role="alert" class="notice">{text(error)}</p>' if error else '') + (evidence or '')
    status = 400 if error else 200
    return status, page('回访详情', 'revisit', 'revisit_detail',
                        **_detail_fields(bench, result, subject, recheck_html))
