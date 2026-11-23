"""Source-schema watch subjects, change fingerprints and verification timelines.

A watch follows either a foreign-key edge (its real column mapping) or a table's
primary-key definition. Indexes are deliberately excluded from the subject
fingerprint, because adding unrelated indexes must not raise a change notice.

Catalog checks are immutable observations. Per-check conclusions fall into three
operational categories that callers must keep distinct:

* ``verified``  - consecutive observable checks agree within the continuity window
* ``changed``/``missing`` - an observed structural change against the prior check
* ``reopened``  - the structure is identical, but an unreadable interval or a gap
  longer than the continuity window separates the observations
"""

from datetime import datetime, timezone

from .codec import Fault, fingerprint, loads

DEFAULT_CONTINUITY = 3600
MIN_CONTINUITY = 1
MAX_CONTINUITY = 7 * 24 * 3600

# Conclusion codes stored on timeline entries and notification evidence.
STATES = ('verified', 'changed', 'missing', 'reopened', 'unreachable')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def parse(value):
    return datetime.fromisoformat(value)


def validate_target(body):
    kind = body.get('kind')
    if kind == 'edge':
        child = body.get('child')
        edge_id = body.get('id')
        if not isinstance(child, str) or not child or type(edge_id) is not int:
            raise Fault('invalid_watch', '外键关注需要 child 表名和整数外键编号。')
        return {'kind': 'edge', 'child_table': child, 'edge_id': edge_id, 'target_table': child}
    if kind == 'pk':
        table = body.get('table')
        if not isinstance(table, str) or not table:
            raise Fault('invalid_watch', '主键关注需要 table 表名。')
        return {'kind': 'pk', 'child_table': None, 'edge_id': None, 'target_table': table}
    raise Fault('invalid_watch', 'kind 必须是 edge（外键关系）或 pk（主键定义）。')


def continuity(value):
    if value is None:
        return DEFAULT_CONTINUITY
    if type(value) is not int or not MIN_CONTINUITY <= value <= MAX_CONTINUITY:
        raise Fault('invalid_continuity', f'continuity_seconds 须为 {MIN_CONTINUITY} 至 {MAX_CONTINUITY} 的整数。')
    return value


def note(value):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > 500:
        raise Fault('invalid_watch', '备注须为不超过 500 字符的字符串。')
    return value


def find_edge(schema, child, edge_id):
    for edge in schema['edges']:
        if edge['child'] == child and edge['id'] == edge_id:
            return edge
    return None


def subject(schema, watch):
    """Canonical watched structure, or None when the subject is absent."""
    if watch['kind'] == 'edge':
        edge = find_edge(schema, watch['child_table'], watch['edge_id'])
        if edge is None:
            return None
        return {key: edge[key] for key in
                ('child', 'id', 'parent', 'child_cols', 'parent_cols', 'on_update', 'on_delete')}
    table = watch['target_table']
    if table not in schema['tables']:
        return None
    meta = schema['tables'][table]
    return {
        'table': table,
        'pk': meta['pk'],
        'columns': {
            col['name']: {'type': col['type'], 'notnull': bool(col['notnull']), 'hidden': col['hidden']}
            for col in meta['columns'] if col['name'] in meta['pk']
        },
    }


def subject_fingerprint(schema, watch):
    structure = subject(schema, watch)
    return None if structure is None else fingerprint(structure)


def label(watch):
    if watch['kind'] == 'edge':
        return f"外键 {watch['child_table']} #{watch['edge_id']}"
    return f"主键 {watch['target_table']}"


def describe(structure, kind):
    if structure is None:
        return '关注对象已不在当前目录中。'
    if kind == 'edge':
        return (f"{structure['child']} → {structure['parent']}（"
                f"{', '.join(structure['child_cols'])} → {', '.join(structure['parent_cols'])}）")
    return f"{structure['table']}（{' → '.join(structure['pk'])}）"


def build_timeline(watch, checks):
    """Attach a per-check conclusion to every catalog check of the source.

    ``checks`` must be ordered by check id and carry the stored schema text.
    """
    entries = []
    previous_fingerprint = watch['baseline_fingerprint']
    previous_time = parse(watch['created_at'])
    gap_pending = False
    for check in checks:
        common = {
            'check_id': check['id'], 'observed_at': check['observed_at'], 'outcome': check['outcome'],
            'schema_digest': check['schema_digest'], 'reason': check['reason'],
        }
        if check['outcome'] == 'unreachable':
            entries.append(common | {'state': 'unreachable', 'fingerprint': None, 'structure': None})
            gap_pending = True
            continue
        schema = loads(check['schema'])
        current = subject(schema, watch)
        current_fingerprint = None if current is None else fingerprint(current)
        elapsed = (parse(check['observed_at']) - previous_time).total_seconds()
        if current_fingerprint is None:
            state = 'missing'
        elif current_fingerprint != previous_fingerprint:
            state = 'changed'
        elif gap_pending or elapsed > watch['continuity_seconds']:
            state = 'reopened'
        else:
            state = 'verified'
        entries.append(common | {'state': state, 'fingerprint': current_fingerprint, 'structure': current})
        previous_fingerprint = current_fingerprint
        previous_time = parse(check['observed_at'])
        gap_pending = False
    return entries


def build_segments(watch, entries):
    """Collapse entries into verified ranges, unobserved gaps and change points."""
    segments = []
    run = None
    last_observed = None
    unreachable_count = 0

    def close_run():
        nonlocal run
        if run is not None:
            segments.append(run)
            run = None

    for entry in entries:
        if entry['state'] == 'unreachable':
            close_run()
            unreachable_count += 1
            continue
        if entry['state'] == 'reopened':
            close_run()
            if last_observed is not None and last_observed['fingerprint'] is not None:
                segments.append({
                    'kind': 'unobserved',
                    'from_check_id': last_observed['check_id'], 'to_check_id': entry['check_id'],
                    'from': last_observed['observed_at'], 'to': entry['observed_at'],
                    'unreachable_checks': unreachable_count,
                    'duration_seconds': (parse(entry['observed_at'])
                                         - parse(last_observed['observed_at'])).total_seconds(),
                    'fingerprint': entry['fingerprint'],
                })
            else:
                segments.append({
                    'kind': 'unobserved', 'from_check_id': None, 'to_check_id': entry['check_id'],
                    'from': watch['created_at'], 'to': entry['observed_at'],
                    'unreachable_checks': unreachable_count, 'duration_seconds': None,
                    'fingerprint': entry['fingerprint'],
                })
        elif entry['state'] in ('changed', 'missing'):
            close_run()
            segments.append({
                'kind': 'change', 'check_id': entry['check_id'], 'at': entry['observed_at'],
                'state': entry['state'],
                'from_fingerprint': last_observed['fingerprint'] if last_observed else watch['baseline_fingerprint'],
                'to_fingerprint': entry['fingerprint'],
            })
        if entry['fingerprint'] is not None:
            if run is not None and entry['state'] == 'verified' and entry['fingerprint'] == run['fingerprint']:
                run['to_check_id'] = entry['check_id']
                run['to'] = entry['observed_at']
                run['checks'] += 1
            else:
                close_run()
                run = {
                    'kind': 'verified',
                    'from_check_id': entry['check_id'], 'to_check_id': entry['check_id'],
                    'from': entry['observed_at'], 'to': entry['observed_at'], 'checks': 1,
                    'fingerprint': entry['fingerprint'],
                    'matches_baseline': entry['fingerprint'] == watch['baseline_fingerprint'],
                }
        else:
            close_run()
        last_observed = entry
        unreachable_count = 0
    close_run()
    return segments


def evidence(watch, checks):
    """Lightweight, JSON-storable conclusions captured inside a notification."""
    return [
        {key: entry[key] for key in
         ('check_id', 'observed_at', 'outcome', 'state', 'fingerprint', 'schema_digest', 'reason')}
        for entry in build_timeline(watch, checks)
    ]


def recipients(watch, recipes):
    """Recipe versions that actually use the watched edge or root on the table."""
    found = []
    for row in recipes:
        normalized = row['recipe']
        if normalized['source'] != watch['source']:
            continue
        usage = []
        if watch['kind'] == 'edge':
            if any(edge['child'] == watch['child_table'] and edge['id'] == watch['edge_id']
                   for edge in normalized['follow_children']):
                usage.append('follow_children')
        elif any(root['table'] == watch['target_table'] for root in normalized['roots']):
            usage.append('root')
        if usage:
            found.append({'name': row['name'], 'version': row['version'],
                          'active': row['active'], 'usage': usage})
    return found


def change_summary(watch, previous, current):
    if current is None:
        return f"{label(watch)}已从源目录中移除。"
    if previous is None:
        return f"{label(watch)}重新出现在源目录中：{describe(current, watch['kind'])}"
    return f"{label(watch)}的结构已变化：{describe(current, watch['kind'])}"
