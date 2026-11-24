"""Report which declared source indexes can serve the reads a recipe actually makes.

A recipe reads its source through exactly two statement shapes:

* a keyed equality lookup that resolves one row (``capture.find``), used for
  roots and for required parents;
* a parent-to-child join that fans one edge out (``capture.children``).

For each shape the report asks SQLite itself which plan it chooses, which plan it
chooses when each declared index is forced, and how much statement work the
statement costs.  It opens a read-only ``query_only`` snapshot and only explains
and runs SELECTs.  The check therefore never installs an index and cannot alter
the source, so it asks nothing of whoever administers the source.

Three facts drive the verdicts and are easy to get wrong:

* The child join compares ``p.col = +c.col``.  The unary plus is deliberate: it
  strips child affinity so the comparison follows the parent key.  It also makes
  the right-hand side an expression, so no child index can give that leg a keyed
  seek; the most a child index can do is change a full table scan into an index
  scan, or skip rows a partial predicate excludes.
* ``EXPLAIN QUERY PLAN`` demands bound placeholders, but the chosen plan does
  not depend on their values.  Work does: a join whose sample key matches no
  parent never fans out, so measurements use real keys sampled from the same
  snapshot.
* A partial index is usable only when the statement's context implies its
  predicate; forcing a non-implied partial index fails with ``no query
  solution`` rather than silently dropping rows.  An index is never reported as
  helping unless forcing it produced a valid plan.

The report keeps apart three things operators conflate at their peril: the plan
shape, the measured work delta, and the relationship closure.  The closure is
computed by the real capture engine on the same snapshot and reported on its
own, because installing or removing an index never changes which rows a capture
selects.
"""

import re
import sqlite3

from . import capture
from .codec import Fault, decode, fingerprint, quote
from .storage import SOURCE_CALLBACKS, SOURCE_INTERVAL, SOURCE_WORK

MEASURE_BATCH = 512
MEASURE_LADDER = ((1, 100_000), (1000, SOURCE_CALLBACKS))
MEASURE_STEPS = 40_000_000
STEP = re.compile(r'^(SEARCH|SCAN) (\S+)(?:\s+USING (.+))?$')


class Meter:
    """A shared, finite budget of VM steps for probing, so a large source cannot run away.

    Plan probes are cheap (``EXPLAIN`` never executes the statement), so only
    work measurement draws on the meter.  Each run is capped by what remains; if
    the budget runs out mid-run the reading is reported as skipped rather than as
    a capped measurement, because the cap then reflects the probe budget and not
    the statement's own cost.
    """

    def __init__(self, steps=MEASURE_STEPS):
        self.total = steps
        self.left = steps

    def charge(self, steps):
        self.left = max(0, self.left - steps)

    @property
    def spent(self):
        return self.total - self.left


def layer(detail):
    """Split one plan line into verb, alias, structure, constraint and flags."""
    match = STEP.fullmatch(detail)
    if match is None:
        return None
    verb, alias, tail = match.groups()
    constraint = None
    if tail is not None:
        opening = tail.rfind('(')
        if opening != -1 and tail.endswith(')'):
            constraint = tail[opening + 1:-1]
            tail = tail[:opening].strip() or None
    structure = tail or 'TABLE'
    index = None
    if 'PRIMARY KEY' in structure:
        kind = 'rowid' if 'INTEGER PRIMARY KEY' in structure else 'primary'
    elif 'INDEX' in structure:
        kind = 'automatic' if 'AUTOMATIC' in structure else 'index'
        index = structure.split('INDEX', 1)[1].strip() or None
    else:
        kind = 'table'
    return dict(verb=verb, alias=alias, kind=kind, index=index,
                keyed=constraint is not None, covering='COVERING' in structure,
                detail=detail)


def access(plan, alias):
    """The plan node that opens one table alias, or ``None`` if the plan is absent."""
    if plan is None:
        return None
    for detail in plan:
        node = layer(detail)
        if node is not None and node['alias'] == alias:
            return node
    return None


def _probe(conn, sql, value):
    """Plan for a statement, or ``None`` when SQLite finds no valid solution."""
    try:
        return [row['detail'] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, value)]
    except sqlite3.OperationalError as exc:
        if 'no query solution' in str(exc):
            return None
        raise


def _consume(conn, sql, value, interval, limit):
    """Run a statement, stopping once ``limit`` callback ticks have elapsed.

    Returns ``(ticks, capped)`` or ``None`` when SQLite refuses the statement --
    which is how a forced partial index whose predicate is not implied reports
    itself, at execution as well as at ``EXPLAIN``.
    """
    ticks = 0

    def tick():
        nonlocal ticks
        ticks += 1
        return ticks > limit

    conn.set_progress_handler(tick, interval)
    try:
        cursor = conn.execute(sql, value)
        try:
            while cursor.fetchmany(MEASURE_BATCH):
                pass
        finally:
            cursor.close()
    except sqlite3.OperationalError as exc:
        refused = ('no query solution', 'no such index', 'no such table',
                   'no such column', 'unable to use function')
        if any(marker in str(exc) for marker in refused) and ticks <= limit:
            return None
        if ticks <= limit:
            raise
        return ticks, True
    finally:
        conn.set_progress_handler(None, 0)
    return ticks, False


def _measure(conn, sql, value, meter):
    """Estimate the VM steps a statement runs, or mark it skipped if unaffordable.

    ``units`` are approximate VM steps, so readings taken at different rungs are
    directly comparable; the rung only bounds the resolution.  A run that hits its
    tick limit is retried at the next coarser rung.  ``skipped`` means the shared
    probe budget ran out or SQLite refused the statement, in which case the caller
    reports the plan alone rather than a fabricated number.
    """
    for interval, limit in MEASURE_LADDER:
        if meter.left < interval * limit:
            return dict(units=None, capped=None, skipped=True, interval=None)
        outcome = _consume(conn, sql, value, interval, limit)
        if outcome is None:
            return dict(units=None, capped=None, skipped=True, interval=None)
        ticks, capped = outcome
        meter.charge(ticks * interval)
        if not capped:
            return dict(units=ticks * interval, capped=False, skipped=False, interval=interval)
    return dict(units=None, capped=None, skipped=True, interval=None)


def _declared(conn, table, schema):
    """Every index SQLite reports on the table, with key columns and predicate."""
    sources = {item['name']: item['sql'] for item in schema['indexes']}
    result = []
    for entry in conn.execute(f'PRAGMA main.index_list({quote(table)})'):
        keys = [dict(row) for row in conn.execute(f'PRAGMA main.index_xinfo({quote(entry["name"])})')
                if row['key']]
        result.append(dict(name=entry['name'], origin=entry['origin'], unique=bool(entry['unique']),
                           partial=bool(entry['partial']), columns=[key['name'] for key in keys],
                           collations=[key['coll'] for key in keys], sql=sources.get(entry['name'])))
    return result


def _describe(node, work):
    if node is None:
        return dict(shape='unavailable', keyed=False, covering=False, index=None,
                    kind=None, detail=None, work=work)
    return dict(shape=('seek' if node['keyed'] else node['verb'].lower()), keyed=node['keyed'],
                covering=node['covering'], index=node['index'], kind=node['kind'],
                detail=node['detail'], work=work)


def _classify(baseline, forced, delta):
    """Give one index a single label for this read.

    The label answers the operator's question -- does this index actually help
    the read this recipe performs? -- and is derived from both the plan and the
    measured work, always against a no-index baseline rather than against
    whatever the planner happened to pick:

    ``seeks``      the read gains a keyed seek the baseline lacks;
    ``less_work``  the shape is unchanged but measured work really drops;
    ``neutral``    neither the plan nor the measured work changes;
    ``costs``      measured work rises, or forcing it loses a seek the baseline
                   had -- a namesake index with the wrong collation or column
                   order does exactly this;
    ``unusable``   SQLite refuses it, typically a partial index whose predicate
                   this read does not imply.
    """
    if forced is None:
        return 'unusable'
    if forced['keyed'] and not baseline['keyed']:
        return 'seeks'
    if baseline['keyed'] and not forced['keyed']:
        return 'costs'
    if delta == 'less':
        return 'less_work'
    if delta == 'more':
        return 'costs'
    return 'neutral'


def _delta(before, after):
    """Compare two work readings, accepting the granularity of the coarser rung."""
    if before is None or after is None:
        return None
    if before.get('skipped') or after.get('skipped'):
        return None
    if before['capped'] and after['capped']:
        return None
    if before['capped']:
        return 'less'
    if after['capped']:
        return 'more'
    low, high = sorted((before['units'], after['units']))
    granularity = max(2, before.get('interval') or 1, after.get('interval') or 1)
    if high - low <= max(granularity, low // 100):
        return 'same'
    return 'more' if after['units'] > before['units'] else 'less'


def _hint(sql, table, alias, hint):
    """Add ``NOT INDEXED``/``INDEXED BY`` to one alias, or fail loudly.

    Both query shapes place the alias right after the table name, so a missing
    match means the shapes drifted and a silently unhinted probe would fabricate
    a comparison; refuse instead.
    """
    needle = f'{quote(table)} {alias} '
    if needle not in sql:
        raise Fault('readiness_shape', f'The {alias} read no longer matches the capture SQL')
    return sql.replace(needle, f'{quote(table)} {alias} {hint} ', 1)


def _read(conn, sql, value, alias, table, schema, meter):
    """One read shaped by the engine, with every declared index classified against it."""
    natural_node = access(_probe(conn, sql, value), alias)
    baseline_sql = _hint(sql, table, alias, 'NOT INDEXED')
    baseline_node = access(_probe(conn, baseline_sql, value), alias)
    natural = _describe(natural_node, _measure(conn, sql, value, meter))
    baseline = _describe(baseline_node, _measure(conn, baseline_sql, value, meter))
    indexes = []
    for entry in _declared(conn, table, schema):
        hinted = _hint(sql, table, alias, f'INDEXED BY {quote(entry["name"])}')
        forced_node = access(_probe(conn, hinted, value), alias)
        work = _measure(conn, hinted, value, meter) if forced_node is not None else None
        delta = _delta(baseline['work'], work)
        indexes.append(dict(_describe(forced_node, work) | dict(
            name=entry['name'], origin=entry['origin'], unique=entry['unique'],
            partial=entry['partial'], columns=entry['columns'], collations=entry['collations'],
            sql=entry['sql'], work_delta=delta,
            chosen=bool(forced_node is not None and natural_node is not None
                        and forced_node['detail'] == natural_node['detail']),
            verdict=_classify(baseline_node, forced_node, delta))))
    return dict(table=table, natural=natural, baseline=baseline, indexes=indexes)


def _sample(conn, table, columns):
    """A real non-NULL tuple from the snapshot, so a lookup is not short-circuited.

    NULL key parts are skipped: a parent lookup backed by a NULL foreign key can
    never find a row, and measuring it would understate the read.  If nothing but
    NULLs exists, any tuple still gives a valid, if unmatched, probe.
    """
    columns = list(columns)
    projection = ", ".join(quote(column) for column in columns)
    not_null = ' AND '.join(f'{quote(column)} IS NOT NULL' for column in columns)
    for clause in (not_null, '1'):
        row = conn.execute(f'SELECT {projection} FROM {quote(table)} WHERE {clause} LIMIT 1').fetchone()
        if row is not None:
            return [row[column] for column in columns]
    return None


def _planned(conn, schema, normalized, meter):
    """Every distinct read the recipe drives, each with a real sample key.

    Reads are grouped so a table reached by several roots or edges is described
    once per column set, while every reason for the read stays listed in ``via``.
    A child read is sampled from the parent's key so the join really fans out; a
    parent read is sampled from the child's foreign-key values so the lookup
    finds a row; a root read uses the registered key itself.  Several roots that
    share a key therefore collapse into one row of the report.
    """
    specs = {}

    def add(kind, table, columns, via, **extra):
        key = (kind, table, tuple(columns), tuple(extra.get('edge') or ()))
        spec = specs.setdefault(key, dict(kind=kind, table=table, columns=list(columns),
                                          via=[], edge=extra.get('edge'),
                                          **{k: v for k, v in extra.items() if k != 'edge'}))
        spec['via'].append(via)

    selected = {(item['child'], item['id']) for item in normalized['follow_children']}
    for root in normalized['roots']:
        add('root', root['table'], schema['tables'][root['table']]['pk'], dict(root=root['key']),
            sample_table=root['table'], sample_columns=schema['tables'][root['table']]['pk'],
            value=[decode(part) for part in root['key']])

    # A table is reached if a root lands in it; from a reached child every parent
    # edge is always followed, and from a reached parent every selected child edge
    # is followed.  That is a fixed point, matching capture.collect.
    reached = {root['table'] for root in normalized['roots']}
    grown = True
    while grown:
        grown = False
        for edge in schema['edges']:
            if edge['child'] in reached and edge['parent'] not in reached:
                reached.add(edge['parent'])
                grown = True
            if (edge['child'], edge['id']) in selected and edge['parent'] in reached \
                    and edge['child'] not in reached:
                reached.add(edge['child'])
                grown = True

    for edge in schema['edges']:
        if edge['child'] in reached:
            add('parent', edge['parent'], edge['parent_cols'], dict(edge=[edge['child'], edge['id']]),
                edge=[edge['child'], edge['id']], child=edge['child'],
                sample_table=edge['child'], sample_columns=edge['child_cols'])
        if (edge['child'], edge['id']) in selected and edge['parent'] in reached:
            add('child', edge['child'], edge['child_cols'], dict(edge=[edge['child'], edge['id']]),
                edge=[edge['child'], edge['id']], parent=edge['parent'],
                parent_cols=edge['parent_cols'], parent_pk=schema['tables'][edge['parent']]['pk'],
                sample_table=edge['parent'], sample_columns=schema['tables'][edge['parent']]['pk'])

    reads = []
    for spec in specs.values():
        if spec['kind'] == 'child':
            sql = capture.children_sql(spec['parent'], spec['table'], spec['parent_cols'],
                                       spec['columns'], spec['parent_pk'])
            alias = 'c'
        else:
            sql = capture.find_sql(spec['table'], spec['columns'])
            alias = 't'
        value = spec.get('value') or _sample(conn, spec['sample_table'], spec['sample_columns'])
        if value is None:
            reads.append(dict(kind=spec['kind'], via=spec['via'], table=spec['table'],
                              edge=spec['edge'], sample=None, indexes=[],
                              natural=_describe(None, None), baseline=_describe(None, None),
                              note='no row to sample in this snapshot'))
            continue
        described = _read(conn, sql, value, alias, spec['table'], schema, meter)
        reads.append(dict(kind=spec['kind'], via=spec['via'], edge=spec['edge'],
                          sample=dict(table=spec['sample_table'], columns=spec['sample_columns']),
                          **described))
    return reads


def closure(conn, schema, normalized):
    """Run the real capture engine, so the closure is observed rather than inferred."""
    ticks = 0

    def tick():
        nonlocal ticks
        ticks += 1
        return ticks > SOURCE_CALLBACKS

    conn.set_progress_handler(tick, SOURCE_INTERVAL)
    try:
        result = capture.collect(conn, schema, normalized)
    except Fault as exc:
        return dict(error=exc.code, message=str(exc), work=ticks * SOURCE_INTERVAL)
    except sqlite3.OperationalError as exc:
        if ticks <= SOURCE_CALLBACKS:
            raise
        return dict(error='scan_budget', message='The closure exceeded the source work budget',
                    work=ticks * SOURCE_INTERVAL)
    finally:
        conn.set_progress_handler(None, 0)
    return dict(summary=capture.summarize(result), work=ticks * SOURCE_INTERVAL,
                work_capped=ticks > SOURCE_CALLBACKS,
                roots=fingerprint(result['roots']),
                rows=fingerprint({table: sorted(values) for table, values in result['rows'].items()}))


def readiness(conn, schema, normalized):
    """A source-index readiness report for one normalized recipe in one snapshot."""
    observed = closure(conn, schema, normalized)
    meter = Meter()
    reads = _planned(conn, schema, normalized, meter)
    verdicts = {}
    for read in reads:
        for item in read['indexes']:
            verdicts[item['verdict']] = verdicts.get(item['verdict'], 0) + 1
    return dict(schema_digest=fingerprint(schema),
                budget=dict(work=SOURCE_WORK, interval=SOURCE_INTERVAL, callbacks=SOURCE_CALLBACKS,
                            probe_steps=MEASURE_STEPS, probe_spent=meter.spent),
                closure=observed, reads=reads, summary=verdicts)
