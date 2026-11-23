"""HTML recipe forms bind the visible relationship choices to a schema digest."""

from .directory import source_options
from .errors import message
from .forms import field, typed_json
from .render import code, location, page, segment, template, text
from ..codec import Fault, dumps
from .. import checks


def editor(bench, alias, values=None, error='', status=200, report=None):
    values = values or {}
    catalog = bench.catalog(alias)
    schema = catalog['schema']
    picked = set(values.get('edge', []))
    edges = []
    for edge in schema['edges']:
        # Tokens carry the parent visible when the form was rendered, so a
        # later reassignment of the same key number can be detected on submit.
        token = f'{edge["id"]}:{edge["child"]}:{edge["parent"]}'
        selected_numbers = {parse_edge_token(item)[:2] for item in picked}
        checked = ' checked' if (edge['id'], edge['child']) in selected_numbers else ''
        content = f'{edge["parent"]} → {edge["child"]} ({", ".join(edge["child_cols"])})'
        edges.append(f'<label class="edge-choice"><input type="checkbox" name="edge" value="{text(token)}"'
                     f'{checked}> {code(content)}</label>')
    pk_items = ''.join(f'<li>{code(name)} <span>{code(", ".join(table["pk"]))}</span></li>'
                       for name, table in schema['tables'].items())
    return status, page('登记配方', 'recipe', 'recipe_editor',
                        options=source_options(bench, alias), source=text(alias),
                        digest=text(catalog['schema_digest']), keys=pk_items,
                        edges=''.join(edges) or '<p class="empty">此源库没有可选关系。</p>',
                        name=text(field(values, 'name')), roots=text(field(values, 'roots')),
                        limit=text(field(values, 'row_limit', '1000')),
                        report=render_report(report),
                        error=f'<p role="alert" class="notice">{text(error)}</p>' if error else '')


def parse_edge_token(item):
    number, _, rest = item.partition(':')
    child, sep, parent = rest.partition(':')
    if not number.isascii() or not number.isdecimal() or not child:
        raise Fault('invalid_edge', 'Invalid edge')
    return int(number), child, parent if sep else None


def parse_edges(values):
    chosen = []
    for item in values.get('edge', []):
        number, _, rest = item.partition(':')
        child, sep, parent = rest.partition(':')
        if not number.isascii() or not number.isdecimal() or len(number) > 10 or not child:
            raise Fault('invalid_edge', 'Invalid edge')
        entry = dict(child=child, id=int(number))
        if sep:
            entry['parent'] = parent
        chosen.append(entry)
    return chosen


def draft_body(values):
    alias = field(values, 'source')
    roots = typed_json(field(values, 'roots'))
    raw_limit = field(values, 'row_limit')
    if not raw_limit.isascii() or not raw_limit.isdecimal() or len(raw_limit) > 4:
        raise Fault('invalid_limit', 'Invalid row limit')
    return dict(source=alias, roots=roots, follow_children=parse_edges(values),
                row_limit=int(raw_limit))


VERDICT_LABELS = {
    checks.KEEP: ('可保留', 'ready'),
    checks.CORRECT: ('需纠正', 'pending'),
    checks.DEFER: ('暂无法判', 'muted'),
}


def verdict_badge(status):
    label, style = VERDICT_LABELS[status]
    return f'<span class="status {style}">{label}</span>'


def safe_dumps(value):
    try:
        return dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def report_rows(items, kind):
    rows = []
    for item in items:
        if kind == 'root':
            target = f'{item.get("table", "")} {safe_dumps(item.get("key", ""))}'
        else:
            target = f'{item.get("child", "")} · 外键 {item.get("id", "")}'
        extra = ''
        if kind == 'root' and item['status'] == checks.KEEP and item.get('respelled'):
            extra = f' → 实存 {code(dumps(item["actual"]))}'
        if kind == 'edge' and item['status'] == checks.DEFER and 'parent' in item:
            extra = f' → 当前指向 {code(item["parent"])}'
        reason = item['message'] if item['status'] == checks.KEEP else text(
            message(Fault(item['code'], item['message'])))
        rows.append(f'<tr><td>{item["index"]}</td><td>{verdict_badge(item["status"])}</td>'
                    f'<td>{code(target)}{extra}</td><td>{reason}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="4" class="empty">无输入项。</td></tr>'


def render_report(report):
    if report is None:
        return ''
    headline = ('全部输入可按当前模式登记。' if report['ready']
                else '存在需纠正或暂无法判定的输入，未保存任何配方版本。')
    style = 'ready' if report['ready'] else 'pending'
    counts = report['statuses']
    badges = ' '.join(
        f'{verdict_badge(status)} <strong>{counts[status]}</strong>' for status in checks.STATUSES
    )
    problems = ''.join(f'<li>{text(item["message"])}</li>' for item in report['problems'])
    return template_report(headline, style, badges,
                           report_rows(report['roots'], 'root'),
                           report_rows(report['edges'], 'edge'),
                           f'<ul class="data-list">{problems}</ul>' if problems else '<p class="empty">无整体形状问题。</p>')


def template_report(headline, style, badges, root_rows, edge_rows, problems):
    return template('recipe_check', headline=headline, style=style, badges=badges,
                    root_rows=root_rows, edge_rows=edge_rows, problems=problems)


def check(bench, values):
    alias = field(values, 'source')
    try:
        body = draft_body(values)
        name = field(values, 'name')
        if name:
            body['name'] = name
        report = bench.check_recipe(body, expected_schema=field(values, 'digest'))
    except Fault as exc:
        if exc.code in {'browser_schema', 'unknown_source', 'unsupported_schema'}:
            raise
        return editor(bench, alias, values, message(exc), exc.status)
    return editor(bench, alias, values, report=report)


def register(bench, values):
    alias = field(values, 'source')
    try:
        body = draft_body(values)
        recipe = bench.register(field(values, 'name'), body, expected_schema=field(values, 'digest'))
    except Fault as exc:
        if exc.code in {'browser_schema', 'unknown_source', 'unsupported_schema'}:
            raise
        return editor(bench, alias, values, message(exc), exc.status)
    return location(f'/workbench/recipes/{segment(recipe["name"])}/{recipe["version"]}')
