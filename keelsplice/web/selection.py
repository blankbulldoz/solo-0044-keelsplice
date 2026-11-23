"""HTML recipe forms bind the visible relationship choices to a schema digest."""

from .directory import source_options
from .errors import message
from .forms import field, typed_json
from .render import code, location, page, segment, text
from ..codec import Fault


def editor(bench, alias, values=None, error='', status=200):
    values = values or {}
    catalog = bench.catalog(alias)
    schema = catalog['schema']
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
    return status, page('登记配方', 'recipe', 'recipe_editor',
                        options=source_options(bench, alias), source=text(alias),
                        digest=text(catalog['schema_digest']), keys=pk_items,
                        edges=''.join(edges) or '<p class="empty">此源库没有可选关系。</p>',
                        name=text(field(values, 'name')), roots=text(field(values, 'roots')),
                        limit=text(field(values, 'row_limit', '1000')),
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
        body = dict(source=alias, roots=roots, follow_children=chosen, row_limit=int(raw_limit))
        recipe = bench.register(field(values, 'name'), body, expected_schema=field(values, 'digest'))
    except Fault as exc:
        if exc.code in {'browser_schema', 'unknown_source', 'unsupported_schema'}:
            raise
        return editor(bench, alias, values, message(exc), exc.status)
    return location(f'/workbench/recipes/{segment(recipe["name"])}/{recipe["version"]}')
