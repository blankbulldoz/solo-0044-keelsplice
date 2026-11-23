"""HTML recipe forms bind typed per-root editing to a schema digest."""

from . import roots
from .directory import source_options
from .errors import message
from .forms import field, posted
from .render import location, page, segment, text
from ..codec import Fault


def editor(bench, alias, values=None, error='', status=200, state=None, raw=None, mode=None, info=False):
    values = values or {}
    catalog = bench.catalog(alias)
    schema = catalog['schema']
    if state is None:
        state, mode, raw, notice = roots.load(schema, values)
        error = error or notice
    mode = mode or 'cards'
    canonical, problem = roots.export(schema, state)
    if mode == 'cards' or raw is None:
        # The typed cards are authoritative; the JSON box mirrors them.
        raw = canonical if canonical is not None else (raw or '')
    picked = set(values.get('edge', []))
    edges = []
    for edge in schema['edges']:
        token = f'{edge["id"]}:{edge["child"]}'
        checked = ' checked' if token in picked else ''
        content = f'{edge["parent"]} → {edge["child"]} ({", ".join(edge["child_cols"])})'
        edges.append(f'<label class="edge-choice"><input type="checkbox" name="edge" value="{text(token)}"'
                     f'{checked}> <span class="mono">{text(content)}</span></label>')
    pk_items = ''.join(f'<li><span class="mono">{text(name)}</span>'
                       f' <span>{text(" → ".join(table["pk"]))}</span></li>'
                       for name, table in schema['tables'].items())
    add_options = ''.join(f'<option value="{text(name)}">{text(name)}</option>' for name in schema['tables'])
    notices = []
    if error:
        kind = 'notice info' if info else 'notice'
        alert = '' if info else ' role="alert"'
        notices.append(f'<p{alert} class="{kind}">{text(error)}</p>')
    elif problem:
        notices.append(f'<p class="notice info">逐根表单还未能形成可提交的 JSON：{text(problem)}</p>')
    return status, page('登记配方', 'recipe', 'recipe_editor',
                        options=source_options(bench, alias), source=text(alias),
                        digest=text(catalog['schema_digest']), keys=pk_items,
                        cards=roots.cards(schema, state), roots=text(raw),
                        rendered=text(raw),
                        summary=roots.summary(schema, state),
                        add_options=add_options,
                        edges=''.join(edges) or '<p class="empty">此源库没有可选关系。</p>',
                        name=text(posted(values, 'name')),
                        limit=text(posted(values, 'row_limit', '1000')),
                        notice=''.join(notices))


def register(bench, values):
    alias = field(values, 'source')
    state, mode, raw = None, 'cards', None
    try:
        schema = bench.catalog(alias)['schema']
        state, mode, raw, notice = roots.load(schema, values)
        verb, _ = roots.control(values)
        if verb not in {'register', 'register_json'}:
            adoption = mode == 'adopt'
            return editor(bench, alias, values, notice,
                          200 if adoption else (400 if notice else 200),
                          state=state, raw=raw, mode='cards' if adoption else mode,
                          info=adoption)
        if notice:
            return editor(bench, alias, values, notice, 400, state=state, raw=raw, mode=mode)
        if verb == 'register' and mode == 'cards' and roots.divergent(
                posted(values, 'roots'), posted(values, 'rendered')):
            return editor(bench, alias, values,
                          '原始 JSON 与逐根格不一致：点「导入 JSON 为逐根格」后用逐根格保存，'
                          '或点「按原始 JSON 保存」提交文本框内容。', 400,
                          state=state, raw=raw, mode=mode)
        payload = roots.wire_roots(schema, state)
        raw_limit = field(values, 'row_limit')
        if not raw_limit.isascii() or not raw_limit.isdecimal() or len(raw_limit) > 4:
            raise Fault('invalid_limit', 'Invalid row limit')
        chosen = []
        for item in values.get('edge', []):
            number, _, child = item.partition(':')
            if not number.isascii() or not number.isdecimal() or len(number) > 10 or not child:
                raise Fault('invalid_edge', 'Invalid edge')
            chosen.append(dict(child=child, id=int(number)))
        body = dict(source=alias, roots=payload, follow_children=chosen, row_limit=int(raw_limit))
        recipe = bench.register(field(values, 'name'), body, expected_schema=field(values, 'digest'))
    except Fault as exc:
        if exc.code in {'browser_schema', 'unknown_source', 'unsupported_schema'}:
            raise
        return editor(bench, alias, values, message(exc), exc.status, state=state, raw=raw, mode=mode)
    return location(f'/workbench/recipes/{segment(recipe["name"])}/{recipe["version"]}')
