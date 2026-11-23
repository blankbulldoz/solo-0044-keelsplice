"""Use-case coordinator linking API requests to source and repository state."""

import re
import sqlite3

from . import bundle, capture, catalog, keys, recipe
from .codec import Fault, decode, encode

NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')


class Workbench:
    def __init__(self, repository):
        self.repository = repository

    def catalog(self, alias):
        with self.repository.source(alias) as conn:
            return catalog.snapshot(conn)

    def register(self, name, body, *, expected_schema=None):
        if not NAME.fullmatch(name):
            raise Fault('invalid_name', 'Expected an ASCII recipe name')
        recipe.fields(body, ('source', 'roots'), ('follow_children', 'row_limit'))
        disclosed = self.catalog(body['source'])
        if expected_schema is not None and expected_schema != disclosed['schema_digest']:
            raise Fault('browser_schema', '目录已变化，请重新打开配方编辑页核对主键和外键。', 409)
        normalized = recipe.validate(disclosed['schema'], body)
        return recipe.describe(self.repository.add_recipe(name, disclosed['schema_digest'], normalized))

    def version(self, name, number):
        return recipe.describe(self.repository.version(name, number))

    def retire(self, name, number):
        return recipe.describe(self.repository.retire(name, number))

    def execute(self, body):
        recipe.fields(body, ('name', 'version'))
        name, number = body['name'], body['version']
        if not isinstance(name, str) or not NAME.fullmatch(name) or type(number) is not int or not 1 <= number < 2**63:
            raise Fault('invalid_version', 'Supply a recipe name and positive version')
        version = self.repository.version(name, number)
        if not version['active']:
            raise Fault('retired_recipe', 'Recipe version is retired', 409)
        normalized = version['recipe']
        with self.repository.source(normalized['source']) as conn:
            disclosed = catalog.snapshot(conn)
            if disclosed['schema_digest'] != version['schema_digest']:
                raise Fault('schema_conflict', 'Source schema differs from the recipe binding', 409)
            result = capture.collect(conn, disclosed['schema'], normalized)
        return name, number, disclosed | {'recipe': normalized, 'result': result}

    def preview(self, body):
        name, number, sealed = self.execute(body)
        return {'name': name, 'version': number, 'schema_digest': sealed['schema_digest'],
                'summary': capture.summarize(sealed['result']), 'roots': sealed['result']['roots']}

    def capture(self, body):
        name, number, sealed = self.execute(body)
        return self.describe(self.repository.add_capture(name, number, sealed))

    def describe(self, record):
        sealed = record['sealed']
        return {k: record[k] for k in ('id', 'name', 'version', 'state', 'certificate')} | {
            'schema_digest': sealed['schema_digest'], 'summary': capture.summarize(sealed['result']), 'roots': sealed['result']['roots']}

    def status(self, capture_id):
        return self.describe(self.repository.load_capture(capture_id))

    def trace(self, capture_id, body):
        return capture.trace(self.repository.load_capture(capture_id)['sealed'], body)

    def compare(self, body):
        recipe.fields(body, ('left', 'right'))
        if any(not isinstance(body[k], str) for k in ('left', 'right')):
            raise Fault('invalid_capture', 'Supply two capture identifiers')
        left, right = (self.repository.load_capture(body[k])['sealed'] for k in ('left', 'right'))
        return {'left': body['left'], 'right': body['right'], 'changes': capture.compare(left, right)}

    def build(self, capture_id):
        record = self.repository.load_capture(capture_id)
        if record['state'] == 'sealed':
            try:
                data = bundle.build(record['sealed'])
            except sqlite3.Error as exc:
                raise Fault('artifact_build', 'SQLite rejected the sealed schema or values') from exc
            record = self.repository.promote(capture_id, artifact=data)
        return self.describe(record)

    def certify(self, capture_id):
        record = self.repository.load_capture(capture_id, with_artifact=True)
        if record['state'] == 'sealed':
            raise Fault('fixture_not_built', 'Build the fixture before certifying', 409)
        if record['state'] == 'built':
            try:
                certificate = bundle.certify(record['artifact'], record['sealed'])
            except sqlite3.Error as exc:
                raise Fault('artifact_unreadable', 'SQLite could not read the resulting fixture') from exc
            record = self.repository.promote(capture_id, certificate=certificate)
        return self.describe(record)

    def download(self, capture_id):
        record = self.repository.load_capture(capture_id, with_artifact=True)
        if record['state'] != 'certified':
            raise Fault('fixture_not_certified', 'Certify the fixture before handing it off', 409)
        data, sha = record['artifact'], record['certificate']['sha256']
        if bundle.digest_bytes(data) != sha:
            raise Fault('artifact_integrity', 'Stored fixture bytes do not match the certificate', 409)
        return data, sha

    def scan(self, alias, table, *, after=None, before=None, limit=None, session=None):
        """Return one page of stored keys plus the evidence that frames it.

        ``session`` is the marker a previous page returned. Comparing its
        witness with this page's is what lets the workbench flag a key set that
        moved between pages, instead of presenting unrelated windows as one
        stable listing.
        """
        limit = keys.page_limit(limit)
        marker = keys.parse_session(session)
        with self.repository.source(alias) as conn:
            disclosed = catalog.snapshot(conn)
            schema = disclosed['schema']
            table = catalog.resolve(schema['tables'], table)
            order = keys.layout(conn, schema, table)
            evidence = keys.witness(conn, table, order, disclosed['schema_digest'])
            after_key = keys.validate_key(schema, table, after, 'after') if after is not None else None
            before_key = keys.validate_key(schema, table, before, 'before') if before is not None else None
            window = keys.page(conn, schema, table, order,
                               after=after_key, before=before_key, limit=limit)
            cursor = before_key if before_key is not None else after_key
            if cursor is not None:
                stored = [encode(value) for value in cursor]
                found = keys.resolve(conn, schema, table, cursor)
                window['cursor_stored'] = stored in found
                window['cursor_present'] = bool(found)
            else:
                window['cursor_stored'] = True
                window['cursor_present'] = True
        witness = keys.stamp(evidence)
        drifted = marker is not None and (
            marker['witness'] != witness or marker['schema'] != disclosed['schema_digest']
            or marker['layout'] != keys.layout_digest(order))
        if marker is None:
            state = 'start' if after_key is None and before_key is None else 'jump'
        else:
            state = 'drift' if drifted else 'same'
        options = dict(witness=witness, proved=keys.exact(evidence),
                       schema_digest=disclosed['schema_digest'], order_digest=keys.layout_digest(order))
        if marker is None or drifted:
            marker = keys.anchor(window, after_key, before_key, **options)
        else:
            marker = keys.carry(marker, window, after_key, before_key, proved=options['proved'])
        return {
            'source': alias, 'table': table, 'schema_digest': disclosed['schema_digest'],
            'layout': order, 'layout_text': keys.layout_text(order), 'witness': evidence,
            'witness_stamp': witness, 'state': state, 'session': marker, 'window': window,
        }

    def scan_page(self, alias, table, body):
        """Page through stored keys from a JSON body.

        Accepts ``after``/``before`` as typed key lists, an optional ``limit``
        and the ``session`` marker a previous page returned.
        """
        recipe.fields(body, (), ('after', 'before', 'limit', 'session'))
        return self.scan(alias, table, after=body.get('after'), before=body.get('before'),
                         limit=body.get('limit'), session=body.get('session'))

    def select(self, body):
        """Record a chosen root with the exact listing evidence behind it.

        The check is a fresh read: the stored evidence reflects the source when
        the operator picked and names the session it belongs to. It is not a
        promise about what a later preview or capture will observe.
        """
        recipe.fields(body, ('source', 'table', 'key'), ('session', 'note'))
        alias, table = body['source'], body['table']
        marker = keys.parse_session(body.get('session'))
        note = body.get('note', '')
        if not isinstance(note, str) or len(note) > 200:
            raise Fault('invalid_note', 'note must be a string of at most 200 characters', 400)
        with self.repository.source(alias) as conn:
            disclosed = catalog.snapshot(conn)
            schema = disclosed['schema']
            table = catalog.resolve(schema['tables'], table)
            order = keys.layout(conn, schema, table)
            typed = keys.validate_key(schema, table, body['key'])
            matches = keys.resolve(conn, schema, table, typed)
            if len(matches) != 1:
                raise Fault('missing_key', 'The selected key does not resolve to exactly one stored row', 404)
            stored = matches[0]
            evidence = keys.witness(conn, table, order, disclosed['schema_digest'])
        witness = keys.stamp(evidence)
        found = {'schema': disclosed['schema_digest'], 'layout': keys.layout_digest(order),
                 'witness': witness}
        state = 'unverified' if marker is None else (
            'same' if all(marker[key] == found[key] for key in found) else 'drift')
        return self.repository.add_selection({
            'source': alias, 'table': table, 'key': stored,
            'schema_digest': disclosed['schema_digest'], 'layout': order, 'witness': evidence,
            'evidence': {
                'requested': [encode(value) for value in typed], 'stored': stored,
                'same_spelling': [encode(value) for value in typed] == stored,
                'state': state, 'note': note,
                'session': {key: marker[key] for key in ('witness', 'schema', 'layout')} if marker else None}})

    def selection(self, selection_id):
        """Recheck stored selection evidence against the current source.

        The stored record is immutable. This adds what the source looks like
        now: whether the selected key still resolves, and whether the key set
        and schema still match what the operator was looking at.
        """
        record = self.repository.selection(selection_id)
        present, found = None, None
        try:
            with self.repository.source(record['source']) as conn:
                disclosed = catalog.snapshot(conn)
                schema = disclosed['schema']
                if record['table'] in schema['tables']:
                    matches = keys.resolve(conn, schema, record['table'],
                                           [decode(value) for value in record['key']])
                    present = record['key'] in matches
                    order = keys.layout(conn, schema, record['table'])
                    evidence = keys.witness(conn, record['table'], order, disclosed['schema_digest'])
                    found = {'schema': disclosed['schema_digest'],
                             'layout': keys.layout_digest(order),
                             'witness': keys.stamp(evidence),
                             'count': evidence['count']}
        except Fault:
            present, found = None, None
        if present is None:
            recheck = 'unknown'
        elif (found['schema'] != record['schema_digest']
              or found['layout'] != keys.layout_digest(record['layout'])):
            recheck = 'schema'
        elif found['witness'] != keys.stamp(record['witness']):
            recheck = 'drift' if present else 'absent'
        else:
            recheck = 'present' if present else 'absent'
        return record | {'present': present, 'current': found, 'recheck': recheck}
