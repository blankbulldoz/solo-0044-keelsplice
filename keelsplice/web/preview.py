"""Show explicitly live closure results and canonical root occurrences."""

from . import identity
from .render import page, segment, text


def counts(summary):
    return ''.join(f'<tr><th scope="row">{identity.identifier(name)}</th><td>{count}</td></tr>'
                   for name, count in summary['counts'].items())


def canonical_roots(roots):
    return ''.join(f'<li>{identity.identifier(table)} {identity.pair(key)}</li>'
                   for table, key in roots)


def show(bench, name, number):
    result = bench.preview(dict(name=name, version=number))
    summary = result['summary']
    return 200, page('闭包试算', 'recipe', 'preview', name=text(name), number=number,
                     recipe=f'/workbench/recipes/{segment(name)}/{number}',
                     digest=text(result['schema_digest']), rows=summary['rows'],
                     witnesses=summary['witnesses'], legend=identity.legend(),
                     roots=canonical_roots(result['roots']), counts=counts(summary))
