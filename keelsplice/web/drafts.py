"""Save, confirm, and copy parallel-source drafts from the recipe editor."""

from ..codec import Fault
from . import draftview, selection
from .errors import message
from .forms import field, revision
from .render import location


def payload_from(values):
    return dict(name=field(values, 'name'), row_limit=field(values, 'row_limit'),
                roots=field(values, 'roots'), edges=list(values.get('edge', [])), marks=[])


def save(bench, values):
    alias = field(values, 'source')
    intent = field(values, 'intent', 'save')
    if intent not in ('save', 'switch'):
        raise Fault('invalid_request', 'Unknown draft action', 400)
    target = field(values, 'switch') or alias
    if intent == 'switch':
        bench.draft(target)
    expected = revision(field(values, 'revision'))
    stored = bench.draft(alias)
    if (stored['revision'] if stored else 0) != expected:
        return draftview.conflict(bench, alias, stored, values, intent)
    try:
        bench.save_draft(alias, expected, payload_from(values))
    except Fault as exc:
        if exc.code == 'draft_conflict':
            return draftview.conflict(bench, alias, bench.draft(alias), values, intent)
        return selection.editor(bench, alias, values, message(exc), exc.status)
    return location('/workbench/recipes/new', source=target if intent == 'switch' else alias,
                    notice='saved')


def confirm(bench, values):
    alias = field(values, 'source')
    expected = revision(field(values, 'revision'))
    try:
        bench.confirm_draft(alias, expected)
    except Fault as exc:
        if exc.code == 'draft_conflict':
            return draftview.conflict(bench, alias, bench.draft(alias), None, 'confirm')
        raise
    return location('/workbench/recipes/new', source=alias, notice='confirmed')


def copy(bench, values):
    alias = field(values, 'source')
    try:
        result = bench.copy_roots(alias, field(values, 'switch'), field(values, 'roots'),
                                  dict(name=field(values, 'name'), row_limit=field(values, 'row_limit')))
    except Fault as exc:
        if exc.code in {'unknown_source', 'unsupported_schema'}:
            raise
        return selection.editor(bench, alias, values, message(exc), exc.status)
    return selection.editor(bench, alias, values, report=draftview.report(result))
