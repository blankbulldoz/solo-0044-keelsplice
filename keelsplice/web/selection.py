"""HTML recipe forms bind the visible relationship choices to a schema digest."""

from .. import draft
from ..codec import Fault
from . import draftview
from .errors import message
from .forms import field, revision, typed_json
from .render import code, location, options, page, segment, text

NOTICES = {
    'saved': '草稿已保存。',
    'confirmed': '复制项已确认，标记为已复制。',
}


def editor(bench, alias, values=None, error='', status=200, notice='', report=''):
    catalog = bench.catalog(alias)
    schema = catalog['schema']
    stored = bench.repository.draft(alias)
    payload = stored['payload'] if stored else None
    if values is None:
        values = {}
        if stored:
            values = {'name': [payload['name']], 'roots': [payload['roots']],
                      'row_limit': [payload['row_limit']], 'edge': list(payload['edges']),
                      'revision': [str(stored['revision'])]}
    picked = set(values.get('edge', []))
    edges = []
    for edge in schema['edges']:
        token = f'{edge["id"]}:{edge["child"]}'
        checked = ' checked' if token in picked else ''
        content = f'{edge["parent"]} → {edge["child"]} ({", ".join(edge["child_cols"])})'
        edges.append(f'<label class="edge-choice"><input type="checkbox" name="edge" value="{text(token)}"'
                     f'{checked}> {code(content)}</label>')
    pk_items = ''.join(f'<li>{code(name)} <span>{code(", ".join(table["pk"]))}</span></li>'
                       for name, table in schema['tables'].items())
    others = [item for item in sorted(bench.repository.sources) if item != alias]
    parallel = ''
    if others:
        parallel = ('<div class="actions"><label for="switch">并行源</label>'
                    f'<select id="switch" name="switch">{options(((item, item) for item in others), "")}</select>'
                    '<button name="intent" value="switch" formnovalidate>保存草稿并切换</button>'
                    '<button formaction="/workbench/drafts/copy" formnovalidate>检查并复制兼容的根</button></div>')
    pending = sum(1 for mark in (payload['marks'] if payload else [])
                  if draft.well_formed_mark(mark) and mark['state'] == 'pending')
    confirm = (f'<button formaction="/workbench/drafts/confirm" formnovalidate>确认 {pending} 项复制根</button>'
               if pending else '')
    return status, page('登记配方', 'recipe', 'recipe_editor',
                        source=text(alias), digest=text(catalog['schema_digest']),
                        revision=text(field(values, 'revision')), keys=pk_items,
                        edges=''.join(edges) or '<p class="empty">此源库没有可选关系。</p>',
                        name=text(field(values, 'name')), roots=text(field(values, 'roots')),
                        limit=text(field(values, 'row_limit', '1000')),
                        badges=draftview.badges(payload, field(values, 'roots')),
                        panel=draftview.panel(bench.drafts(), alias),
                        parallel=parallel, confirm=confirm, report=report,
                        notice=(f'<p class="notice info" role="status">{text(NOTICES[notice])}</p>'
                                if notice in NOTICES else ''),
                        error=f'<p role="alert" class="notice">{text(error)}</p>' if error else '')


def register(bench, values):
    alias = field(values, 'source')
    try:
        roots = typed_json(field(values, 'roots'))
        raw_limit = field(values, 'row_limit')
        if not raw_limit.isascii() or not raw_limit.isdecimal() or len(raw_limit) > 4:
            raise Fault('invalid_limit', 'Invalid row limit')
        chosen = []
        for item in values.get('edge', []):
            number, _, child = item.partition(':')
            if not number.isascii() or not number.isdecimal() or len(number) > 10 or not child:
                raise Fault('invalid_edge', 'Invalid edge')
            chosen.append(dict(child=child, id=int(number)))
        stored = bench.repository.draft(alias)
        if stored and stored['revision'] != revision(field(values, 'revision')):
            return draftview.conflict(bench, alias, stored, values, 'register')
        body = dict(source=alias, roots=roots, follow_children=chosen, row_limit=int(raw_limit))
        recipe = bench.register(field(values, 'name'), body, expected_schema=field(values, 'digest'))
    except Fault as exc:
        if exc.code in {'browser_schema', 'unknown_source', 'unsupported_schema'}:
            raise
        return editor(bench, alias, values, message(exc), exc.status)
    return location(f'/workbench/recipes/{segment(recipe["name"])}/{recipe["version"]}')
