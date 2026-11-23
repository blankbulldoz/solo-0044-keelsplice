"""Acceptance clauses, certified citations, and same-file co-presence verdicts.

A session freezes its receiving groups, clause set, recipe version and schema
digest when it opens.  Ordinary (``single``) clauses may be evidenced by
different certified fixtures, one per clause, including different snapshots of
the same active version.  Joint (``joint``) clauses name two relationships
that must be present in one actual fixture file, so two separately passing
certificates cannot be combined into a joint verdict.

The verdict comes from stored capture evidence only: a requirement is met by a
row identity in the sealed row set or by one exact directed arc.  A certificate
counts only when it is certified, bound to this session's recipe version and
schema digest, and its recorded digest still matches both the sealed content
and the stored file bytes.
"""

import re
from datetime import datetime, timezone

from .catalog import resolve
from .codec import Fault, dumps
from .recipe import fields, identity

MAX_CLAUSES = 64
MAX_GROUPS = 16
CLAUSE_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,31}\Z')
DIRECTIONS = ('parent', 'child')

REASON_TEXT = {
    'ok': '已满足',
    'separate_files': '不能并用：两项分别由不同的认证文件举证，没有一个文件同时包含两者',
    'awaiting_citation': '已有覆盖两项的认证文件，尚未由接收组引用',
    'needs_certification': '缺口：覆盖所需的捕获已经封存但尚未认证',
    'version_mismatch': '缺口：只有同名配方其他版本的证书，不能用于本会话',
    'schema_mismatch': '缺口：覆盖所需的捕获与本会话模式摘要不一致',
    'evidence_integrity': '缺口：候选证书与封存内容或实际文件不一致',
    'incomplete': '缺口：没有覆盖所需的已认证样本',
}

COMBINATION_TEXT = {
    'same_file': '这些认证文件中存在同一个文件（同一 SHA-256）同时包含全部要求，可以并用。',
    'separate_files': '两项单独由不同认证文件举证；没有同一文件同时包含两者，不能并用。',
    'awaiting_citation': '已有同一配方版本的认证文件同时覆盖两项，只需由接收组引用。',
    'needs_certification': '覆盖两项的同一文件捕获已经封存但尚未认证，认证后即可引用。',
    'version_mismatch': '覆盖所需的证书来自同名配方的其他版本，版本绑定不同，不能使用。',
    'schema_mismatch': '覆盖所需的捕获与本会话模式摘要不一致，不能使用。',
    'evidence_integrity': '候选证书与实际文件或封存内容不一致，不能使用。',
    'incomplete': '没有覆盖所需的已认证样本，需要补封存并认证。',
}

ACTION_TEXT = {'cite': '引用已有认证捕获', 'certify': '认证已封存捕获', 'seal': '封存新样本并认证'}

CLASSIFICATION_TEXT = {
    'eligible': '可用于本会话',
    'needs_certification': '已封存但尚未认证',
    'version_mismatch': '同名配方的其他版本',
    'schema_mismatch': '模式摘要不一致',
    'evidence_integrity': '证书与文件不一致',
}

MISSING_PRIORITY = ('evidence_integrity', 'needs_certification',
                    'version_mismatch', 'schema_mismatch', 'incomplete')


def clock():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def groups(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_GROUPS:
        raise Fault('invalid_groups', f'Specify 1 to {MAX_GROUPS} receiving groups')
    result = []
    for item in value:
        if (not isinstance(item, str) or not 1 <= len(item.strip()) <= 64
                or any(ord(char) < 32 for char in item)):
            raise Fault('invalid_groups', 'Receiving group names must be 1 to 64 printable characters')
        name = item.strip()
        if name in result:
            raise Fault('invalid_groups', 'Receiving group names must be unique')
        result.append(name)
    return result


def requirement(schema, value):
    if not isinstance(value, dict):
        raise Fault('invalid_requirement', 'Each requirement must be an object')
    if value.get('kind') == 'row':
        fields(value, ('kind', 'table', 'key'))
        found = identity(schema, {'table': value['table'], 'key': value['key']})
        return {'kind': 'row', 'table': found['table'], 'key': found['key']}
    if value.get('kind') == 'link':
        fields(value, ('kind', 'edge', 'origin', 'target'), ('direction',))
        edge_value = value['edge']
        if (not isinstance(edge_value, list) or len(edge_value) != 2
                or not isinstance(edge_value[0], str) or type(edge_value[1]) is not int):
            raise Fault('invalid_requirement', 'A link requirement needs [child table, foreign key number]')
        child = resolve(schema['tables'], edge_value[0])
        edge = next((item for item in schema['edges']
                     if item['child'] == child and item['id'] == edge_value[1]), None)
        if edge is None:
            raise Fault('invalid_requirement', f'Unknown key number in {child}')
        origin = identity(schema, value['origin'])
        target = identity(schema, value['target'])
        choices = []
        if origin['table'] == edge['child'] and target['table'] == edge['parent']:
            choices.append('parent')
        if origin['table'] == edge['parent'] and target['table'] == edge['child']:
            choices.append('child')
        if not choices:
            raise Fault('invalid_requirement', 'Link endpoints do not match the foreign key direction')
        if 'direction' in value:
            direction = value['direction']
            if direction not in DIRECTIONS:
                raise Fault('invalid_requirement', 'Link direction must be parent or child')
            if direction not in choices:
                raise Fault('invalid_requirement', 'Link direction conflicts with the foreign key endpoints')
        elif len(choices) == 1:
            direction = choices[0]
        else:
            raise Fault('invalid_requirement', 'Self-referencing links need an explicit direction')
        return {'kind': 'link', 'edge': [edge['child'], edge['id']], 'direction': direction,
                'origin': {'table': origin['table'], 'key': origin['key']},
                'target': {'table': target['table'], 'key': target['key']}}
    raise Fault('invalid_requirement', 'Requirement kind must be row or link')


def clauses(schema, value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CLAUSES:
        raise Fault('invalid_clause', f'Specify 1 to {MAX_CLAUSES} acceptance clauses')
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise Fault('invalid_clause', 'Each clause must be an object')
        fields(item, ('id', 'kind', 'requirements'), ('label',))
        identifier = item['id']
        if not isinstance(identifier, str) or not CLAUSE_ID.fullmatch(identifier):
            raise Fault('invalid_clause',
                        'Clause identifiers must start with a letter and use ASCII letters, digits, _ or -')
        if any(existing['id'] == identifier for existing in result):
            raise Fault('invalid_clause', 'Clause identifiers must be unique')
        kind = item['kind']
        if kind not in ('single', 'joint'):
            raise Fault('invalid_clause', 'Clause kind must be single or joint')
        required = item['requirements']
        expected = 1 if kind == 'single' else 2
        if not isinstance(required, list) or len(required) != expected:
            raise Fault('invalid_clause', f'A {kind} clause needs exactly {expected} requirement(s)')
        label = item.get('label', identifier)
        if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 120
                or any(ord(char) < 32 for char in label)):
            raise Fault('invalid_clause', 'Clause labels must be 1 to 120 printable characters')
        normalized = [requirement(schema, entry) for entry in required]
        if len({dumps(entry) for entry in normalized}) != len(normalized):
            raise Fault('invalid_clause', 'A joint clause needs two distinct requirements')
        result.append({'id': identifier, 'label': label.strip(), 'kind': kind,
                       'requirements': normalized})
    return result


def canonicalize(clause_list, lookup):
    """Replace input spellings with the actual stored primary keys."""
    for clause in clause_list:
        for required in clause['requirements']:
            if required['kind'] == 'row':
                required['key'] = lookup(required['table'], required['key'])
            else:
                required['origin']['key'] = lookup(required['origin']['table'], required['origin']['key'])
                required['target']['key'] = lookup(required['target']['table'], required['target']['key'])
        if clause['kind'] == 'joint':
            tokens = {dumps(entry) for entry in clause['requirements']}
            if len(tokens) != len(clause['requirements']):
                raise Fault('invalid_clause',
                            'A joint clause needs two distinct requirements after resolving real keys')
    return clause_list


def _row_token(required):
    return ('row', required['table'], dumps(required['key']))


def _link_token(required):
    return ('link', tuple(required['edge']), required['direction'],
            required['origin']['table'], dumps(required['origin']['key']),
            required['target']['table'], dumps(required['target']['key']))


def token(required):
    return _row_token(required) if required['kind'] == 'row' else _link_token(required)


def index(sealed):
    """Project a sealed result into the exact identities a requirement can name."""
    result = sealed.get('result') or {}
    rows = {('row', table, stored) for table, stored in (result.get('rows') or {}).items()
            for stored in (stored if isinstance(stored, dict) else ())}
    arcs = {('link', tuple(arc['edge']), arc['direction'],
             arc['origin'][0], dumps(arc['origin'][1]),
             arc['target'][0], dumps(arc['target'][1]))
            for arc in result.get('arcs') or []}
    return rows | arcs


def coverage(sealed_index, clause):
    return [position for position, required in enumerate(clause['requirements'])
            if token(required) in sealed_index]


def _requirement_view(required):
    view = {'kind': required['kind']}
    if required['kind'] == 'row':
        view.update(table=required['table'], key=required['key'])
    else:
        view.update(edge=list(required['edge']), direction=required['direction'],
                    origin=dict(required['origin']), target=dict(required['target']))
    return view


def _requirement_text(required):
    if required['kind'] == 'row':
        return f'行 {required["table"]} {dumps(required["key"])}'
    origin = f'{required["origin"]["table"]} {dumps(required["origin"]["key"])}'
    target = f'{required["target"]["table"]} {dumps(required["target"]["key"])}'
    arrow = '子行→父行' if required['direction'] == 'parent' else '父行→子行'
    return f'关联 {required["edge"][0]} #{required["edge"][1]}（{arrow}）{origin} → {target}'


def _certificate(view):
    certificate = view.get('certificate') or {}
    return {'capture': view.get('capture'), 'name': view.get('name'),
            'version': view.get('version'), 'sha256': certificate.get('sha256'),
            'capture_digest': certificate.get('capture_digest'), 'bytes': certificate.get('bytes')}


def _piece(view, covers):
    return _certificate(view) | {'group': view.get('group'), 'covers': list(covers)}


def _evidence_view(view):
    return _piece(view, view['satisfies']) | {
        'usable': view['usable'], 'reason': view['reason'],
        'reason_text': REASON_TEXT.get(view['reason'], REASON_TEXT['ok'])}


def _rejected_view(view):
    return _evidence_view(view) | {'covers': list(view['satisfies'])}


def _candidate_view(candidate):
    return _certificate(candidate) | {
        'state': candidate.get('state'), 'classification': candidate['classification'],
        'classification_text': CLASSIFICATION_TEXT[candidate['classification']],
        'covers': list(candidate['covers'])}


def _suggestion(clause, action, captures):
    roots, edges = [], []

    def root(table, values):
        item = {'table': table, 'key': values}
        if item not in roots:
            roots.append(item)

    for required in clause['requirements']:
        if required['kind'] == 'row':
            root(required['table'], required['key'])
        else:
            root(required['origin']['table'], required['origin']['key'])
            if required['direction'] == 'child':
                edge = {'child': required['edge'][0], 'id': required['edge'][1]}
                if edge not in edges:
                    edges.append(edge)
    body = {'roots': roots}
    if edges:
        body['follow_children'] = edges
    return {'action': action, 'text': ACTION_TEXT[action], 'captures': list(captures),
            'body': body,
            'requirements': [_requirement_text(item) for item in clause['requirements']]}


def evaluate(clause_list, evidence, candidates):
    """Judge every clause from stored citations and the same-recipe candidate pool."""
    indexed_candidates = [dict(candidate, sealed_index=index(candidate['sealed']))
                          for candidate in candidates]
    indexed_evidence = [dict(item, sealed_index=index(item.get('sealed') or {}))
                        for item in evidence]
    results = [_clause(clause, indexed_evidence, indexed_candidates) for clause in clause_list]
    return {
        'clauses': results,
        'summary': {
            'clauses': len(results),
            'satisfied': sum(1 for item in results if item['status'] == 'satisfied'),
            'gaps': sum(1 for item in results if item['status'] != 'satisfied'),
            'citations': len(indexed_evidence),
            'groups': sorted({item['group'] for item in indexed_evidence}),
            'certificates': len({(item.get('certificate') or {}).get('sha256')
                                 for item in indexed_evidence if item.get('usable')}),
        },
    }


def _clause(clause, evidence, candidates):
    required = clause['requirements']
    count = len(required)
    views = []
    for item in evidence:
        if item.get('clause_id') != clause['id']:
            continue
        views.append(dict(item, satisfies=coverage(item['sealed_index'], clause)))
    assessed = []
    for candidate in candidates:
        covers = coverage(candidate['sealed_index'], clause)
        if covers:
            assessed.append(dict(candidate, covers=covers))

    result = {'id': clause['id'], 'label': clause['label'], 'kind': clause['kind'],
              'requirements': [_requirement_view(item) for item in required],
              'evidence': [_evidence_view(view) for view in views],
              'rejected': [_rejected_view(view) for view in views if not view.get('usable')],
              'gaps': [], 'needed_sample': None, 'combination': None}
    winners = [view for view in views if view.get('usable') and len(view['satisfies']) == count]
    if winners:
        result.update(status='satisfied', reason='ok', reason_text=REASON_TEXT['ok'])
        files, seen = [], set()
        for view in winners:
            digest = (view.get('certificate') or {}).get('sha256')
            if digest in seen:
                continue
            seen.add(digest)
            files.append(_piece(view, view['satisfies']))
        result['combination'] = {
            'usable': True, 'reason': 'same_file', 'reason_text': COMBINATION_TEXT['same_file'],
            'certificates': files}
        return result

    available = [candidate for candidate in assessed
                 if candidate['classification'] == 'eligible' and len(candidate['covers']) == count]
    pieces, missing = [], []
    for position, required_item in enumerate(required):
        cited = [view for view in views if view.get('usable') and position in view['satisfies']]
        eligible = [candidate for candidate in assessed
                    if candidate['classification'] == 'eligible' and position in candidate['covers']]
        if cited:
            pieces.append(_piece(cited[0], [position]))
        elif eligible:
            pieces.append(_piece(eligible[0], [position]))
        else:
            related = [candidate for candidate in assessed if position in candidate['covers']]
            reason = 'incomplete'
            if related:
                for code in MISSING_PRIORITY:
                    if any(candidate['classification'] == code for candidate in related):
                        reason = code
                        break
            missing.append({'requirement': position, 'text': _requirement_text(required_item),
                            'reason': reason, 'reason_text': REASON_TEXT[reason],
                            'candidates': [_candidate_view(candidate) for candidate in related]})

    if available:
        reason = 'awaiting_citation'
    elif not missing and count > 1:
        reason = 'separate_files'
    elif missing:
        codes = {item['reason'] for item in missing}
        reason = next(iter(codes)) if len(codes) == 1 else 'incomplete'
    else:
        reason = 'incomplete'

    merged = {}
    for piece in pieces:
        token = (piece['capture'], piece['group'], piece['sha256'])
        if token in merged:
            merged[token]['covers'] = sorted(set(merged[token]['covers']) | set(piece['covers']))
        else:
            merged[token] = dict(piece)

    result.update(status='gap', reason=reason, reason_text=REASON_TEXT[reason], gaps=missing)
    result['combination'] = {'usable': False, 'reason': reason,
                             'reason_text': COMBINATION_TEXT[reason],
                             'pieces': list(merged.values()),
                             'certificates': [_piece(candidate, candidate['covers'])
                                              for candidate in available]}
    if reason == 'awaiting_citation':
        action, captures = 'cite', [candidate['capture'] for candidate in available]
    elif reason == 'needs_certification':
        pending = [candidate['capture'] for candidate in assessed
                   if candidate['classification'] == 'needs_certification'
                   and len(candidate['covers']) == count]
        action, captures = ('certify', pending) if pending else ('seal', [])
    else:
        action, captures = 'seal', []
    result['needed_sample'] = _suggestion(clause, action, captures)
    return result
