"""Group equivalent observations across captures into per-check coverage entities.

Each sealed capture contributes three kinds of observations: resolved root
occurrences, relation targets of required-parent arcs, and generated column
values. Observations that agree on their entity token are equivalent for the
check in question, so the audit counts them as one independent entity and
keeps every sighting as the basis reviewers can trace back to a capture.
"""

from collections import defaultdict

from .codec import dumps, loads

ROOT_CHECK = 'root'
KIND_ORDER = {'root': 0, 'relation': 1, 'generated': 2}


def generated_columns(meta):
    return [(offset, column['name']) for offset, column in enumerate(meta['columns'])
            if column['hidden'] in (2, 3)]


def analyze(records):
    """Audit every sealed capture and report independent entities per check."""
    records = list(records)
    checks = {}
    shared = defaultdict(set)

    def see(check_id, kind, subject, token, entity, index, note=None):
        check = checks.setdefault(check_id, {'kind': kind, 'subject': subject, 'entities': {}})
        entry = check['entities'].setdefault(token, {'entity': entity, 'sightings': []})
        entry['sightings'].append((index, note))

    for index, record in enumerate(records):
        sealed = record['sealed']
        schema, result = sealed['schema'], sealed['result']
        edges = {(edge['child'], edge['id']): edge for edge in schema['edges']}
        for position, (table, values) in enumerate(result['roots']):
            see(ROOT_CHECK, 'root', None, dumps([table, values]),
                {'table': table, 'key': values}, index, position)
            shared[(index, table, dumps(values))].add(ROOT_CHECK)
        for arc in result['arcs']:
            if arc['direction'] != 'parent':
                continue
            edge = edges[tuple(arc['edge'])]
            check_id = ('relation', edge['child'], edge['id'], edge['parent'],
                        tuple(edge['child_cols']), tuple(edge['parent_cols']))
            subject = {'child': edge['child'], 'id': edge['id'], 'parent': edge['parent'],
                       'child_cols': edge['child_cols'], 'parent_cols': edge['parent_cols']}
            origin, target = arc['origin'], arc['target']
            see(check_id, 'relation', subject, dumps([origin, target]),
                {'origin': origin, 'target': target}, index)
            shared[(index, origin[0], dumps(origin[1]))].add(check_id)
        for table, meta in schema['tables'].items():
            generated = generated_columns(meta)
            if not generated:
                continue
            for token, stored in result['rows'][table].items():
                values = loads(token)
                for offset, column in generated:
                    check_id = ('generated', table, column)
                    see(check_id, 'generated', {'table': table, 'column': column},
                        dumps([table, values, column, stored[offset]]),
                        {'table': table, 'key': values, 'column': column,
                         'value': stored[offset]}, index)
                    shared[(index, table, token)].add(check_id)

    ordered = []
    for check_id, check in checks.items():
        entities = []
        for token, entry in sorted(check['entities'].items()):
            sightings = entry['sightings']
            entities.append({'token': token, 'entity': entry['entity'], 'sightings': sightings,
                             'captures': sorted({index for index, _ in sightings}),
                             'representative': sightings[-1][0]})
        ordered.append({'kind': check['kind'], 'subject': check['subject'], 'entities': entities,
                        'sightings': sum(len(entity['sightings']) for entity in entities),
                        'captures': len({i for entity in entities for i in entity['captures']})})
    ordered.sort(key=lambda check: (KIND_ORDER[check['kind']], dumps(check['subject'])))
    refs = {check_id: {'kind': check['kind'], 'subject': check['subject']}
            for check_id, check in checks.items()}

    duplicates = []
    retargeted = []
    for check in ordered:
        if check['kind'] == 'root':
            duplicates = [entity for entity in check['entities'] if len(entity['sightings']) > 1]
            continue
        if check['kind'] != 'relation':
            continue
        by_origin = defaultdict(list)
        for entity in check['entities']:
            by_origin[dumps(entity['entity']['origin'])].append(entity)
        for origin_token in sorted(by_origin):
            targets = by_origin[origin_token]
            if len(targets) > 1:
                retargeted.append({'check': {'kind': check['kind'], 'subject': check['subject']},
                                   'origin': targets[0]['entity']['origin'], 'targets': targets})

    shared_rows = []
    for (index, table, token), check_ids in sorted(shared.items()):
        if len(check_ids) < 2:
            continue
        shared_rows.append({'capture': index, 'table': table, 'key': loads(token),
                            'checks': sorted((refs[check_id] for check_id in check_ids), key=dumps)})

    entities_total = sum(len(check['entities']) for check in ordered)
    sightings_total = sum(check['sightings'] for check in ordered)
    return {'captures': [{k: record[k] for k in ('id', 'name', 'version', 'state')}
                         for record in records],
            'checks': ordered, 'duplicates': duplicates, 'shared': shared_rows,
            'retargeted': retargeted,
            'summary': {'captures': len(records), 'checks': len(ordered),
                        'entities': entities_total, 'sightings': sightings_total,
                        'merged': sightings_total - entities_total}}
