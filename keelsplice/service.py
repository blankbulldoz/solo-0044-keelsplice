"""Use-case coordinator linking API requests to source and repository state."""

import re
import sqlite3

from . import bundle, capture, catalog, delivery, recipe
from .codec import Fault, loads

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

    def describe_batch(self, record):
        entries = []
        for row in record['entries']:
            certificate = loads(row['certificate']) if row['certificate'] else None
            sent = None
            if row['outcome'] == 'delivered':
                sent = {'sha256': row['sent_sha256'], 'bytes': row['sent_bytes']}
            entries.append({
                'id': row['id'], 'capture': row['capture_id'], 'name': row['name'], 'version': row['version'],
                'recipient': row['recipient'], 'kind': row['kind'], 'decision': row['decision'],
                'note': row['note'], 'capture_state': row['capture_state'],
                'certificate': ({'sha256': certificate['sha256'], 'bytes': certificate['bytes']}
                                if certificate else None),
                'outcome': row['outcome'], 'outcome_reason': row['outcome_reason'], 'sent': sent})
        return {'id': record['id'], 'label': record['label'], 'state': record['state'], 'entries': entries}

    def create_batch(self, body):
        recipe.fields(body, ('label',))
        return self.describe_batch(self.repository.add_batch(delivery.label(body['label'])))

    def batches(self):
        return self.repository.recent_batches()

    def batch_detail(self, batch_id):
        return self.describe_batch(self.repository.load_batch(batch_id))

    def add_delivery(self, batch_id, body):
        recipe.fields(body, ('capture', 'recipient'))
        if not isinstance(body['capture'], str):
            raise Fault('unknown_capture', 'Capture does not exist', 404)
        record = self.repository.add_delivery(batch_id, body['capture'], delivery.recipient(body['recipient']))
        return self.describe_batch(record)

    def decide_delivery(self, batch_id, delivery_id, body):
        recipe.fields(body, ('action',), ('note',))
        decision = delivery.decision(body['action'])
        note = delivery.note(body.get('note', ''))
        if decision == 'held' and not note:
            raise Fault('hold_needs_reason', 'Holding a delivery needs the reason it is not sent')
        return self.describe_batch(self.repository.decide_delivery(batch_id, delivery_id, decision, note))

    def close_batch(self, batch_id):
        return self.describe_batch(self.repository.close_batch(batch_id))
