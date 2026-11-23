"""Show explicitly live closure results and canonical root occurrences."""

from .render import code, page, segment, text, typed


def counts(summary):
    rows = ''.join(f'<tr><th scope="row">{code(name)}</th><td>{count}</td></tr>'
                   for name, count in summary['counts'].items())
    return rows


def canonical_roots(roots):
    return ''.join(f'<li>{code(table)} {typed(key)}</li>' for table, key in roots)


def show(bench, name, number):
    result = bench.preview(dict(name=name, version=number))
    summary = result['summary']
    return 200, page('闭包试算', 'recipe', 'preview', name=text(name), number=number,
                     recipe=f'/workbench/recipes/{segment(name)}/{number}',
                     digest=text(result['schema_digest']), rows=summary['rows'],
                     witnesses=summary['witnesses'], roots=canonical_roots(result['roots']),
                     counts=counts(summary))
