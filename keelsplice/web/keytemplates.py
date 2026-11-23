"""Turn the two tool column cross-reference lists into a registrable key template."""

from .directory import source_options
from .errors import message
from .forms import field, typed_json
from .render import code, page, text
from ..codec import Fault, dumps
from ..keytemplate import render

STYLES = {'confirmed': 'ready', 'rename': 'ready', 'payload': 'muted',
          'conflict': 'pending', 'excluded': 'muted', 'unresolved': 'pending', 'pending': 'pending'}


def table_options(tables, active):
    return ''.join(f'<option value="{text(name)}"{" selected" if name == active else ""}>{text(name)}</option>'
                   for name in tables)


def report_html(report):
    def row(label, detail):
        return f'<tr><th scope="row">{code(label)}</th><td>{detail}</td></tr>'

    readings = ''.join(
        row(f'{vote["reading"]} · 锚点支持 {vote["support"]} / 反对 {vote["against"]}',
            code(' → '.join(vote['order'])) + f'<div class="muted">{text(vote["reason"])}</div>')
        for vote in report['votes'])
    entries = ''.join(
        f'<tr><td>{code(record["entry"]["name"])}</td><td>{record["entry"]["position"]}</td>'
        f'<td><span class="status {STYLES[record["final"]["verdict"]]}">{text(record["final"]["verdict"])}</span></td>'
        f'<td>{code(record["final"]["column"]) if record["final"]["column"] else "—"}</td>'
        f'<td>{text(record["final"]["how"])}</td></tr>'
        for record in report['legacy']
    )
    grounds = ''.join(f'<li>{text(ground)}</li>' for ground in report['confirmed_by'])
    notes = ''.join(f'<li>{text(note)}</li>' for note in report['notes'])
    excluded = ''.join(
        f'<li>{code(item["entry"].get("name") or item["entry"]["position"])} · {text(item["reason"])}</li>'
        for item in report['excluded'])
    reading = ('位次读法唯一：' + code(report['reading']) if report['reading_unique']
               else '位次读法未唯一：保留全部候选，不据此断言未命中的旧名')
    return ''.join((
        '<section aria-labelledby="template"><h2 id="template">确认的主键模板</h2>',
        f'<dl class="facts"><div><dt>表</dt><dd>{code(report["table"])}</dd></div>',
        f'<div><dt>声明主键</dt><dd>{code(" → ".join(report["declared_pk"]))}</dd></div>',
        f'<div class="wide"><dt>可登记根模板</dt><dd>{code(dumps(report["template"]))}</dd></div></dl>',
        f'<ul class="data-list">{grounds or "<li class=empty>无</li>"}</ul>',
        f'<p>{reading}</p><ul class="data-list">{notes or ""}</ul></section>',
        '<section aria-labelledby="readings"><h2 id="readings">位次读法</h2>',
        f'<div class="scroll"><table><thead><tr><th scope="col">读法</th><th scope="col">列序</th></tr></thead>'
        f'<tbody>{readings}</tbody></table></div></section>',
        '<section aria-labelledby="legacy"><h2 id="legacy">旧列名判定（含确认模板后的重判）</h2>',
        '<div class="scroll"><table><thead><tr><th scope="col">旧名</th><th scope="col">位次</th>'
        '<th scope="col">结论</th><th scope="col">对应列</th><th scope="col">依据</th></tr></thead>'
        f'<tbody>{entries or "<tr><td colspan=5 class=empty>旧名名单为空。</td></tr>"}</tbody></table></div></section>',
        '<section aria-labelledby="excluded"><h2 id="excluded">排除项</h2>',
        f'<ul class="data-list">{excluded or "<li class=empty>没有排除项。</li>"}</ul></section>',
        f'<details><summary>解析文本</summary><pre>{text(chr(10).join(render(report)))}</pre></details>',
    ))


def board(bench, query, values=None, result=None, error='', status=200):
    alias = field(query or values or {}, 'source',
                  next(iter(sorted(bench.repository.sources)), ''))
    try:
        catalog = bench.catalog(alias)
    except Fault as exc:
        return exc.status, page('主键模板', 'template', 'catalog_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)))
    tables = list(catalog['schema']['tables'])
    errors = f'<p role="alert" class="notice">{text(error)}</p>' if error else ''
    return status, page('主键模板', 'template', 'keytemplate',
                        options=source_options(bench, alias), source=text(alias),
                        table_options=table_options(tables, field((values or {}), 'table')),
                        ordinal=text(field((values or {}), 'ordinal', '[1,2]')),
                        legacy=text(field((values or {}), 'legacy', '[]')),
                        error=errors, result=report_html(result) if result else '')


def show(bench, query):
    return board(bench, query)


def check(bench, values):
    alias = field(values, 'source')
    try:
        report = bench.key_template(dict(source=alias, table=field(values, 'table'),
                                         ordinal=typed_json(field(values, 'ordinal')),
                                         legacy=typed_json(field(values, 'legacy'))))
    except Fault as exc:
        if exc.code in {'unknown_source', 'unsupported_schema'}:
            raise
        return board(bench, None, values, error=message(exc), status=exc.status)
    return board(bench, None, values, result=report)
