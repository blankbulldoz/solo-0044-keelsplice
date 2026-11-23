"""List existing primary-key tuples in the order SQLite compares each key.

The listing never counts a whole table to find its place. Every page is one
bounded keyset read in the declared key order, so a page costs a single index
seek no matter how deep it sits. A caller passes back the first or last key of a
previous page and receives the neighbouring window plus an explicit flag for
"another window exists on this side".

Three details keep the listing faithful to SQLite rather than to Python:

* The order is read from the primary-key index, not from the column
  declarations. ``PRIMARY KEY(a COLLATE BINARY)`` over a NOCASE column sorts by
  BINARY, and an ``INTEGER PRIMARY KEY DESC`` is not the rowid alias.
* The keyset predicate repeats a bound on the leading component and compares
  with the index collations, so SQLite keeps seeking the key index even when a
  component collation differs from the column's declared one.
* Every page carries a cheap witness of the key set it read: the schema digest,
  the key layout, a bounded count and the two extreme keys. Comparing witnesses
  across a walk is what lets a caller warn that the source moved underneath it
  rather than implying the pages form one stable listing.
"""

from .codec import Fault, decode, encode, fingerprint, quote

PAGE_DEFAULT = 25
PAGE_MIN = 5
PAGE_MAX = 100
COUNT_CAP = 50_000


def _primary_index(conn, table):
    for row in conn.execute(f'PRAGMA main.index_list({quote(table)})'):
        if row['origin'] == 'pk':
            return row['name']
    return None


def layout(conn, schema, table):
    """Describe the key components in the order the primary-key index stores them."""
    meta = schema['tables'][table]
    pk = meta['pk']
    name = _primary_index(conn, table)
    if name is None:
        if meta['without_rowid'] or len(pk) != 1:
            raise Fault('unsupported_schema', f'Cannot describe the key order of {table}')
        return [dict(column=pk[0], collation='BINARY', descending=False)]
    components = sorted(
        (dict(row) for row in conn.execute(f'PRAGMA main.index_xinfo({quote(name)})') if row['key']),
        key=lambda row: row['seqno'])
    if [row['name'] for row in components] != pk:
        raise Fault('unsupported_schema', f'Primary key of {table} does not match its index')
    return [dict(column=row['name'], collation=row['coll'], descending=bool(row['desc']))
            for row in components]


def layout_digest(order):
    return fingerprint(order)


def layout_text(order):
    return ', '.join(
        f'{component["column"]} ({component["collation"]}'
        f'{" DESC" if component["descending"] else " ASC"})' for component in order)


def _terms(order, reverse=False):
    return [(f'{quote(component["column"])} COLLATE {quote(component["collation"])}',
             component['descending'] != reverse) for component in order]


def _columns(order):
    return [component['column'] for component in order]


def _not_null(order):
    return ' AND '.join(f'{quote(column)} IS NOT NULL' for column in _columns(order))


def _ordering(terms):
    return ', '.join(f'{term} {"DESC" if descending else "ASC"}' for term, descending in terms)


def _present(rows, order):
    return [[encode(row[column]) for column in _columns(order)] for row in rows]


def _predicate(terms, cursor):
    """Lexicographic "strictly past the cursor" bound, index seek preserved.

    The leading component gets a redundant non-strict bound; without it SQLite
    treats the OR chain as a full scan. The chain then reproduces the exact
    declared order. Key components are never NULL in a listed row, so the
    comparisons stay on the values.
    """
    bound = f'{terms[0][0]} {"<=" if terms[0][1] else ">="} ?'
    clauses, equals = [], []
    for term, descending in terms:
        clauses.append('(' + ' AND '.join(equals + [f'{term} {"<" if descending else ">"} ?']) + ')')
        equals = equals + [f'{term} = ?']
    values = [cursor[0]]
    for index in range(len(terms)):
        values.extend(cursor[:index])
        values.append(cursor[index])
    return bound, '(' + ' OR '.join(clauses) + ')', values


def validate_key(schema, table, values, label='key'):
    if not isinstance(values, list) or len(values) != len(schema['tables'][table]['pk']):
        raise Fault('invalid_key', f'{label} must list one value for each key column')
    if None in values:
        raise Fault('invalid_key', f'{label} must not contain NULL components')
    try:
        keys = [decode(value) for value in values]
    except Fault as exc:
        raise Fault('invalid_key', f'{label}: {exc}') from exc
    if None in keys:
        raise Fault('invalid_key', f'{label} must not contain NULL components')
    return keys


def page_limit(value):
    if value is None:
        return PAGE_DEFAULT
    if type(value) is not int or not PAGE_MIN <= value <= PAGE_MAX:
        raise Fault('invalid_page', f'limit must be an integer from {PAGE_MIN} to {PAGE_MAX}')
    return value


def witness(conn, table, order, schema_digest):
    """Cheap evidence about the key set a page was read from.

    The count is bounded and reported as a band, never a full tally, so the
    check stays O(cap) however large the table is. The two extreme keys catch a
    replacement at either end that a count alone would hide.
    """
    not_null = _not_null(order)
    capped = conn.execute(
        f'SELECT count(*) FROM (SELECT 1 FROM {quote(table)} WHERE {not_null} LIMIT ?)',
        (COUNT_CAP + 1,)).fetchone()[0]
    count = {'exact': capped} if capped <= COUNT_CAP else {'at_least': COUNT_CAP}
    selected = ', '.join(quote(column) for column in _columns(order))
    first, last = (conn.execute(
        f'SELECT {selected} FROM {quote(table)} WHERE {not_null} '
        f'ORDER BY {_ordering(_terms(order, reverse))} LIMIT 1').fetchone()
        for reverse in (False, True))
    return {'schema': schema_digest, 'layout': [dict(component) for component in order],
            'count': count, 'first': _present([first], order)[0] if first else None,
            'last': _present([last], order)[0] if last else None}


def stamp(witness):
    return fingerprint(witness)


def exact(witness):
    return 'exact' in witness['count']


def page(conn, schema, table, order, *, after=None, before=None, limit=PAGE_DEFAULT):
    """Read one window of stored keys, after or before a decoded cursor tuple."""
    if after is not None and before is not None:
        raise Fault('invalid_cursor', 'Ask for a window after one key or before one key')
    reverse = before is not None
    cursor = before if reverse else after
    terms = _terms(order, reverse)
    conditions = [_not_null(order)]
    args = []
    if cursor is not None:
        bound, chain, args = _predicate(terms, cursor)
        conditions.extend((bound, chain))
    selected = ', '.join(quote(column) for column in _columns(order))
    rows = conn.execute(
        f'SELECT {selected} FROM {quote(table)} WHERE {" AND ".join(conditions)} '
        f'ORDER BY {_ordering(terms)} LIMIT ?', args + [limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    if reverse:
        rows.reverse()
    keys = _present(rows, order)
    return {'rows': keys, 'first': keys[0] if keys else None, 'last': keys[-1] if keys else None,
            'has_more': more, 'limit': limit, 'direction': 'prev' if reverse else 'next'}


def resolve(conn, schema, table, values):
    """Return the stored key tuples the table matches for typed values.

    Uses the same equality as the recipe and capture paths, so the answer is
    what a root would resolve to right now. Because a NOCASE comparison can
    match another spelling, the caller compares the returned tuple with the
    requested one to tell "same value" from "same row, other spelling".
    """
    condition = ' AND '.join(f'{quote(column)} = ?' for column in schema['tables'][table]['pk'])
    selected = ', '.join(quote(column) for column in schema['tables'][table]['pk'])
    rows = conn.execute(
        f'SELECT {selected} FROM {quote(table)} WHERE {condition} LIMIT 2', values).fetchall()
    return [[encode(row[column]) for column in schema['tables'][table]['pk']] for row in rows]


# --- walk bookkeeping -------------------------------------------------------
#
# A marker travels with a page and comes back with the next request. It steers
# only the claim "this walk has covered the whole key set"; it never chooses a
# row. Shape validation is therefore enough for safety, but the coverage rule is
# strict and conservative: complete needs a forward chain that began at the
# first key, never skipped, reached the end, and saw an unchanged witness whose
# count was exact throughout. A backward step, a jump, a drift or a table too
# large to count exactly all leave the walk explicitly partial.

MARKER_FIELDS = frozenset({'schema', 'layout', 'witness', 'proved', 'forward',
                           'complete', 'reached', 'frontier', 'keys'})


def parse_session(marker):
    if marker is None:
        return None
    if not isinstance(marker, dict) or set(marker) != MARKER_FIELDS:
        raise Fault('invalid_session', 'The paging session marker is not valid', 400)
    if not all(isinstance(marker[key], str) for key in ('schema', 'layout', 'witness')):
        raise Fault('invalid_session', 'The paging session marker is not valid', 400)
    if not all(type(marker[key]) is bool for key in ('proved', 'forward', 'complete', 'reached')):
        raise Fault('invalid_session', 'The paging session marker is not valid', 400)
    if type(marker['keys']) is not int or marker['keys'] < 0:
        raise Fault('invalid_session', 'The paging session marker is not valid', 400)
    if marker['frontier'] is not None and not isinstance(marker['frontier'], list):
        raise Fault('invalid_session', 'The paging session marker is not valid', 400)
    return marker


def anchor(window, after, before, *, witness, proved, schema_digest, order_digest):
    """Start a marker at the page just served.

    Used for the first page of a walk and whenever the source witness moved, so
    a drift restarts coverage rather than inheriting an earlier claim.
    """
    origin = after is None and before is None
    reached = origin and not window['has_more']
    return {'schema': schema_digest, 'layout': order_digest, 'witness': witness,
            'proved': proved, 'forward': origin,
            'complete': reached and proved, 'reached': reached,
            'frontier': window['last'] if origin else None,
            'keys': len(window['rows'])}


def carry(marker, window, after, before, *, proved):
    """Advance a marker along a walk that has not drifted.

    Forward steps from the tracked frontier extend the walk; anything else is
    treated as a jump and drops the coverage claim to partial. A walk that
    already reached the end stays complete: no key can lie past its frontier.
    ``after``/``before`` are decoded tuples and are compared in encoded form so
    BLOB and integer keys match the stored frontier exactly.
    """
    marker = dict(marker)
    marker['proved'] = marker['proved'] and proved
    if before is not None:
        marker.update(forward=False, frontier=None, reached=False, complete=False)
        return marker
    if marker['complete']:
        return marker
    if after is None:
        marker.update(forward=True, frontier=window['last'], keys=len(window['rows']),
                      reached=not window['has_more'])
        marker['complete'] = marker['proved'] and marker['reached']
        return marker
    resumed = marker['forward'] and marker['frontier'] == [encode(value) for value in after]
    if resumed:
        marker['keys'] += len(window['rows'])
        marker['frontier'] = window['last']
        marker['reached'] = not window['has_more']
        marker['complete'] = marker['proved'] and marker['reached']
        return marker
    marker.update(forward=False, frontier=None, reached=False, complete=False)
    return marker
