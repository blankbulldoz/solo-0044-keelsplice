"""Use-case coordinator linking API requests to source and repository state."""

import re
import sqlite3

from . import bundle, capture, catalog, recipe
from .codec import Fault

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
            'schema_digest': sealed['schema_digest'],
            'layouts': self.layouts(record), 'summary': capture.summarize(sealed['result']),
            'roots': sealed['result']['roots']}

    def layouts(self, record):
        rows = {item['layout']: item for item in record['layouts']}
        result = {}
        for name, profile in bundle.LAYOUTS.items():
            if name == 'default':
                state = 'pending' if record['state'] == 'sealed' else record['state']
                certificate = record['certificate']
            else:
                row = rows.get(name)
                state, certificate = (row['state'], row['certificate']) if row else ('pending', None)
            result[name] = {'label': profile['label'], 'description': profile['description'],
                            'page_size': profile['page_size'], 'free_pages': profile['free_pages'],
                            'state': state, 'certificate': certificate}
        return result

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

    def build(self, capture_id, layout='default'):
        name, _ = bundle.profile(layout)
        record = self.repository.load_capture(capture_id, with_artifact=True)
        if name != 'default' and record['state'] == 'sealed':
            raise Fault('fixture_not_built', 'Build the default layout before an alternate one', 409)
        built = next((item for item in record['layouts'] if item['layout'] == name), None)
        if name == 'default':
            todo = record['state'] == 'sealed'
        else:
            todo = built is None
        if todo:
            try:
                data = bundle.build(record['sealed'], name)
            except sqlite3.Error as exc:
                raise Fault('artifact_build', 'SQLite rejected the sealed schema or values') from exc
            record = self.repository.promote(capture_id, artifact=data, layout=name)
        return self.describe(record)

    def certify(self, capture_id, layout='default'):
        name, _ = bundle.profile(layout)
        record = self.repository.load_capture(capture_id, with_artifact=True)
        if name == 'default':
            state, data = record['state'], record['artifact']
        else:
            built = next((item for item in record['layouts'] if item['layout'] == name), None)
            if built is None:
                raise Fault('fixture_not_built', 'Build this layout before certifying it', 409)
            state = built['state']
            data = self.repository.load_layout(capture_id, name, with_artifact=True)['artifact']
        if state == 'sealed':
            raise Fault('fixture_not_built', 'Build the fixture before certifying it', 409)
        if state == 'built':
            try:
                certificate = bundle.certify(data, record['sealed'], name)
            except sqlite3.Error as exc:
                raise Fault('artifact_unreadable', 'SQLite could not read the resulting fixture') from exc
            record = self.repository.promote(capture_id, certificate=certificate, layout=name)
        return self.describe(record)

    def download(self, capture_id, layout='default'):
        name, _ = bundle.profile(layout)
        record = self.repository.load_capture(capture_id, with_artifact=True)
        if name == 'default':
            state, sha = record['state'], (record['certificate'] or {}).get('sha256')
            data = record['artifact']
        else:
            built = next((item for item in record['layouts'] if item['layout'] == name), None)
            if built is None or built['state'] != 'certified':
                raise Fault('fixture_not_certified', 'Certify this layout before handing it off', 409)
            state, sha = built['state'], built['certificate']['sha256']
            data = self.repository.load_layout(capture_id, name, with_artifact=True)['artifact']
        if state != 'certified':
            raise Fault('fixture_not_certified', 'Certify the fixture before handing it off', 409)
        if bundle.digest_bytes(data) != sha:
            raise Fault('artifact_integrity', 'Stored fixture bytes do not match the certificate', 409)
        return data, sha
