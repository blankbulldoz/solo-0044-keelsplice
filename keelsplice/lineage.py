"""Cross-reference a current relationship against retained recipe and capture history.

Foreign-key numbers are local to one schema snapshot.  SQLite may hand the
same number to a different constraint after a table is rebuilt, and the same
constraint may move to another number.  Matching therefore uses the normalized
definition -- child table, parent table and ordered column mapping -- and never
the bare number.  The number is still compared so that a slot which changed
meaning is reported separately from a genuine correspondence.

Capture history is folded into compact items one document at a time: row maps
and arc lists are counted and dropped, so a large sealed history cannot inflate
the page.  Only counts, roots and a few sample witness addresses survive.
"""

from .catalog import sql_name

CORRESPOND = 'correspond'
DIVERGENT = 'divergent'
INCOMPARABLE = 'incomparable'

WITNESS_SAMPLE = 3


def _core(edge):
    return (sql_name(edge['child']), sql_name(edge['parent']),
            tuple(sql_name(name) for name in edge['child_cols']),
            tuple(sql_name(name) for name in edge['parent_cols']))


def _endpoints(edge):
    return _core(edge)[:2]


def _actions(edge):
    return (str(edge.get('on_update')).upper(), str(edge.get('on_delete')).upper())


def _display(edge):
    if edge is None:
        return None
    return {'id': edge['id'], 'child': edge['child'], 'parent': edge['parent'],
            'child_cols': list(edge['child_cols']), 'parent_cols': list(edge['parent_cols']),
            'on_update': edge['on_update'], 'on_delete': edge['on_delete']}


def _find_core(schema, core):
    return next((edge for edge in schema['edges'] if _core(edge) == core), None)


def _find_number(schema, child, number):
    name = sql_name(child)
    return next((edge for edge in schema['edges']
                 if edge['id'] == number and sql_name(edge['child']) == name), None)


def _has_table(schema, name):
    key = sql_name(name)
    return any(sql_name(table) == key for table in schema['tables'])


def _involving(schema, references, table):
    found = []
    for child, number in references:
        edge = _find_number(schema, child, number)
        if edge is not None and sql_name(table) in _endpoints(edge) and edge not in found:
            found.append(edge)
    return found


def assess(current, item, table):
    """Classify one compact history item against the current relationship.

    The child table must exist historically for any comparison; a missing
    parent table does not stop a same-number reassignment from being detected.
    ``None`` means the item is related to ``table`` through some other
    relationship, so the caller lists it apart from the three groups.
    """
    schema = item['schema']
    if schema is None:
        return {'status': INCOMPARABLE, 'reason': 'schema_unavailable',
                'edge': None, 'number_edge': None, 'notes': ()}
    if not _has_table(schema, current['child']):
        return {'status': INCOMPARABLE, 'reason': 'table_absent',
                'edge': None, 'number_edge': None, 'notes': ()}
    used = _involving(schema, item['references'], table)
    semantic = next((edge for edge in used if _core(edge) == current['core']), None)
    if semantic is not None:
        notes = []
        if semantic['id'] != current['id']:
            notes.append(('renumbered', semantic['id']))
        reused = _find_number(schema, current['child'], current['id'])
        if reused is not None and _core(reused) != current['core']:
            notes.append(('number_reused', current['id']))
        if _actions(semantic) != current['actions']:
            notes.append(('actions_changed', _actions(semantic)))
        return {'status': CORRESPOND, 'reason': 'used', 'edge': semantic,
                'number_edge': reused, 'notes': tuple(notes)}
    slot = _find_number(schema, current['child'], current['id'])
    same_endpoints = next((edge for edge in used if _endpoints(edge) == _endpoints(current)
                           and _core(edge) != current['core']), None)
    if same_endpoints is not None:
        return {'status': DIVERGENT, 'reason': 'definition_changed',
                'edge': same_endpoints, 'number_edge': slot, 'notes': ()}
    same_number = next((edge for edge in used if edge['id'] == current['id']
                        and _core(edge) != current['core']), None)
    if same_number is not None:
        return {'status': DIVERGENT, 'reason': 'number_definition',
                'edge': same_number, 'number_edge': slot, 'notes': ()}
    if not _has_table(schema, current['parent']):
        return {'status': INCOMPARABLE, 'reason': 'table_absent',
                'edge': None, 'number_edge': slot, 'notes': ()}
    return {'status': None, 'reason': 'table_used', 'edge': None,
            'number_edge': slot, 'notes': ()}


def _capture_item(record, alias, digest):
    """Fold one sealed capture into the small facts the page needs."""
    sealed = record['sealed']
    if (sealed.get('recipe') or {}).get('source') != alias:
        return None
    result = sealed['result']
    counts = {}
    for name, rows in result['rows'].items():
        if rows:
            counts[sql_name(name)] = len(rows)
    references = set()
    seen = {}
    for arc in result['arcs']:
        token = (sql_name(arc['edge'][0]), arc['edge'][1])
        references.add(token)
        count, sample = seen.get(token, (0, ()))
        if len(sample) < WITNESS_SAMPLE:
            sample = sample + ({'origin': arc['origin'], 'target': arc['target'],
                                'direction': arc['direction']},)
        seen[token] = (count + 1, sample)
    follow = (sealed.get('recipe') or {}).get('follow_children') or []
    for edge in follow:
        references.add((sql_name(edge['child']), edge['id']))
    return {'kind': 'capture', 'id': record['id'], 'name': record['name'],
            'version': record['version'], 'state': record['state'],
            'digest': sealed['schema_digest'],
            'schema_state': 'current' if sealed['schema_digest'] == digest else 'retained',
            'schema': sealed['schema'], 'counts': counts,
            'roots': [sql_name(table) for table, _ in result['roots']],
            'root_tables': [table for table, _ in result['roots']],
            'references': references, 'witnesses': seen, 'follow': []}


def _recipe_item(record, alias, schemas, digest):
    recipe = record['recipe']
    if recipe.get('source') != alias:
        return None
    bound = record['schema_digest']
    state = 'current' if bound == digest else ('retained' if bound in schemas else 'unavailable')
    return {'kind': 'recipe', 'name': record['name'], 'version': record['version'],
            'active': bool(record['active']), 'digest': bound, 'schema_state': state,
            'schema': schemas.get(bound), 'counts': {},
            'roots': [sql_name(root['table']) for root in recipe['roots']],
            'root_tables': [root['table'] for root in recipe['roots']],
            'references': {(sql_name(edge['child']), edge['id']) for edge in recipe['follow_children']},
            'witnesses': {}, 'follow': list(recipe['follow_children'])}


def _table_used(item, table):
    key = sql_name(table)
    if key in item['roots'] or item['counts'].get(key):
        return True
    if item['schema'] is not None:
        return bool(_involving(item['schema'], item['references'], table))
    return any(sql_name(edge['child']) == key for edge in item['follow'])


def _table_rows(item, table):
    return item['counts'].get(sql_name(table))


def _witnesses(item, edge):
    if edge is None:
        return 0, []
    return item['witnesses'].get((sql_name(edge['child']), edge['id']), (0, []))


def _base(item):
    if item['kind'] == 'capture':
        return {'kind': 'capture', 'id': item['id'], 'name': item['name'],
                'version': item['version'], 'state': item['state'], 'digest': item['digest'],
                'schema_state': item['schema_state'], 'table_rows': None,
                'roots': [], 'follow': []}
    return {'kind': 'recipe', 'name': item['name'], 'version': item['version'],
            'active': item['active'], 'digest': item['digest'], 'schema_state': item['schema_state'],
            'table_rows': None, 'roots': list(item['root_tables']),
            'follow': [(edge['child'], edge['id']) for edge in item['follow']]}


def _entry(item, table, current=None):
    entry = _base(item)
    entry['table_rows'] = _table_rows(item, table)
    if current is None:
        if item['schema'] is not None and not _has_table(item['schema'], table):
            entry.update(status=INCOMPARABLE, reason='table_absent')
        elif item['schema'] is None:
            entry.update(status=INCOMPARABLE, reason='schema_unavailable')
        else:
            entry.update(status=None, reason='table_used')
        entry.update(edge=None, number_edge=None, notes=(), witness_count=0, witnesses=[])
        return entry
    assessment = assess(current, item, table)
    entry.update(assessment)
    entry['witness_count'], entry['witnesses'] = _witnesses(item, assessment['edge'])
    if item['kind'] == 'recipe':
        entry['selected'] = bool(
            assessment['edge'] is not None
            and (sql_name(assessment['edge']['child']), assessment['edge']['id']) in item['references'])
    return entry


def _table_row_entry(item, table):
    entry = _entry(item, table)
    involved = []
    if item['schema'] is not None and item['kind'] == 'capture':
        involved = _involving(item['schema'], item['references'], table)
    elif item['schema'] is not None:
        involved = [edge for edge in item['schema']['edges']
                    if (sql_name(edge['child']), edge['id']) in item['references']
                    and sql_name(table) in _endpoints(edge)]
    entry['edges'] = [_display(edge) for edge in involved]
    return entry


def cross_reference(catalog, alias, table, edge, captures, recipes, totals):
    """Build the cross-reference result for one table and optional live edge.

    ``captures`` must be an iterable of sealed documents newest-first and is
    consumed once; ``recipes`` is the loaded recipe-version list; ``totals``
    reports the repository-wide counts so truncation can be disclosed.
    """
    schema = catalog['schema']
    digest = catalog['schema_digest']
    schemas = {digest: schema}

    items = []
    seen_captures = 0
    for record in captures:
        seen_captures += 1
        item = _capture_item(record, alias, digest)
        if item is None:
            continue
        schemas.setdefault(item['digest'], item['schema'])
        items.append(item)
    for record in recipes:
        item = _recipe_item(record, alias, schemas, digest)
        if item is not None:
            items.append(item)

    current = None
    if edge is not None:
        current = {'id': edge['id'], 'child': edge['child'], 'parent': edge['parent'],
                   'child_cols': list(edge['child_cols']), 'parent_cols': list(edge['parent_cols']),
                   'core': _core(edge), 'actions': _actions(edge)}

    truncated = (max(totals.get('recipes', 0) - len(recipes), 0)
                 + max(totals.get('captures', 0) - seen_captures, 0))
    result = {'alias': alias, 'table': table, 'digest': digest,
              'edge': _display(edge) if edge is not None else None,
              'groups': {CORRESPOND: [], DIVERGENT: [], INCOMPARABLE: []},
              'table_only': [], 'table_entries': [], 'edge_index': [],
              'stats': {'recipes': 0, 'captures': 0, 'truncated': truncated,
                        'examined': {'recipes': len(recipes), 'captures': seen_captures},
                        'totals': dict(totals)},
              'table_state': 'current' if _has_table(schema, table) else 'absent'}

    for item in items:
        if current is None:
            if _table_used(item, table):
                result['stats'][item['kind'] + 's'] += 1
                result['table_entries'].append(_table_row_entry(item, table))
            continue
        entry = _entry(item, table, current)
        status = entry['status']
        if status is None:
            if _table_used(item, table):
                result['table_only'].append(entry)
            continue
        if status == INCOMPARABLE and not (_table_used(item, table) or entry['witness_count']):
            continue
        result['groups'][status].append(entry)
        result['stats'][item['kind'] + 's'] += 1

    if current is None:
        for candidate in schema['edges']:
            core = _core(candidate)
            if sql_name(table) not in core[:2]:
                continue
            count = 0
            for item in items:
                if item['schema'] is None:
                    continue
                found = _find_core(item['schema'], core)
                if found is not None and (sql_name(found['child']), found['id']) in item['references']:
                    count += 1
            result['edge_index'].append({
                'edge': _display(candidate), 'count': count,
                'direction': 'outgoing' if sql_name(candidate['child']) == sql_name(table) else 'incoming'})
    return result
