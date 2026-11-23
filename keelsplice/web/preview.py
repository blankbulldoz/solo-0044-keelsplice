"""Show explicitly live closure results, canonical roots, and retention choices."""

from .observations import label
from .render import code, page, segment, text, typed
from ..codec import dumps


def counts(summary):
    rows = ''.join(f'<tr><th scope="row">{code(name)}</th><td>{count}</td></tr>'
                   for name, count in summary['counts'].items())
    return rows


def canonical_roots(roots):
    return ''.join(f'<li>{code(table)} {typed(key)}</li>' for table, key in roots)


def check_choices(checks):
    """Checkbox per check item, valued by its canonical JSON descriptor."""
    items = []
    for check in checks:
        token = dumps(check)
        chose = check['kind'] in ('counts', 'roots')
        items.append('<label class="edge-choice"><input type="checkbox" name="check"'
                     f' value="{text(token)}"{" checked" if chose else ""}> {code(label(check))}</label>')
    return ''.join(items) or '<p class="empty">此源库没有可复查的检查项。</p>'


def show(bench, name, number):
    result = bench.preview(dict(name=name, version=number))
    summary = result['summary']
    return 200, page('闭包试算', 'recipe', 'preview', name=text(name), number=number,
                     recipe=f'/workbench/recipes/{segment(name)}/{number}',
                     digest=text(result['schema_digest']), rows=summary['rows'],
                     witnesses=summary['witnesses'], roots=canonical_roots(result['roots']),
                     counts=counts(summary), check_choices=check_choices(result['checks']))
