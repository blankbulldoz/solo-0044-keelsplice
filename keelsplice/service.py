"""Use-case coordinator linking API requests to source and repository state."""

import re
import sqlite3
from uuid import uuid4

from . import acceptance, bundle, capture, catalog, recipe
from .codec import Fault, decode, encode, fingerprint

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

    def accept(self, body):
        recipe.fields(body, ('name', 'version', 'groups', 'clauses'))
        name, number = body['name'], body['version']
        if not isinstance(name, str) or not NAME.fullmatch(name) or type(number) is not int or not 1 <= number < 2**63:
            raise Fault('invalid_version', 'Supply a recipe name and positive version')
        version = self.repository.version(name, number)
        receiving = acceptance.groups(body['groups'])
        with self.repository.source(version['recipe']['source']) as conn:
            schema = catalog.inspect(conn)
            if fingerprint(schema) != version['schema_digest']:
                raise Fault('schema_conflict', 'Source schema differs from the recipe binding', 409)
            declared = acceptance.clauses(schema, body['clauses'])

            def lookup(table, values):
                matches = capture.find(conn, table, schema['tables'][table]['pk'],
                                       [decode(value) for value in values])
                if len(matches) != 1:
                    return values  # keep the typed input; the clause will report a gap
                return [encode(matches[0][column]) for column in schema['tables'][table]['pk']]

            acceptance.canonicalize(declared, lookup)
        session_id = uuid4().hex
        record = self.repository.add_acceptance(
            session_id, receiving, name, number, version['schema_digest'], declared, acceptance.clock())
        return self.describe_acceptance(record)

    def acceptance(self, session_id):
        return self.describe_acceptance(self.repository.acceptance(session_id))

    def recent_acceptances(self):
        return self.repository.recent_acceptances()

    def cite(self, session_id, body):
        recipe.fields(body, ('clause', 'capture', 'group'))
        if any(not isinstance(body[key], str) for key in ('clause', 'capture', 'group')):
            raise Fault('invalid_request', 'Supply clause, capture and group strings', 400)
        session = self.repository.acceptance(session_id)
        clause = next((item for item in session['clauses'] if item['id'] == body['clause']), None)
        if clause is None:
            raise Fault('unknown_clause', 'This acceptance session has no such clause', 404)
        if body['group'] not in session['groups']:
            raise Fault('invalid_group', 'Citation group is not part of this acceptance session', 422)
        record = self.repository.load_capture(body['capture'], with_artifact=True)
        if record['state'] != 'certified':
            raise Fault('citation_uncertified', 'Only a certified capture can be cited', 409)
        if record['name'] != session['recipe_name'] or record['version'] != session['recipe_version']:
            raise Fault('citation_version', 'Citation comes from another recipe version', 409)
        sealed = record['sealed']
        if sealed['schema_digest'] != session['schema_digest']:
            raise Fault('citation_schema', 'Citation schema differs from this session', 409)
        if not fingerprint(sealed) == record['certificate']['capture_digest']:
            raise Fault('citation_integrity', 'Cited capture content differs from its certificate', 409)
        if not bundle.digest_bytes(record['artifact']) == record['certificate']['sha256']:
            raise Fault('citation_integrity', 'Cited certificate does not match the stored file bytes', 409)
        self.repository.add_citation(session_id, clause['id'], record['id'], body['group'],
                                     record['certificate'], acceptance.clock())
        return self.describe_acceptance(self.repository.acceptance(session_id))

    def _evaluate(self, session, evidence):
        candidates = []
        for record in self.repository.named_captures(session['recipe_name']):
            sealed = record['sealed']
            certificate = record['certificate']
            entry = dict(capture=record['id'], name=record['name'], version=record['version'],
                         state=record['state'], certificate=certificate, sealed=sealed,
                         classification='eligible')
            if sealed['schema_digest'] != session['schema_digest']:
                entry['classification'] = 'schema_mismatch'
            elif record['version'] != session['recipe_version']:
                entry['classification'] = 'version_mismatch'
            elif record['state'] != 'certified':
                entry['classification'] = 'needs_certification'
            elif (certificate is None or fingerprint(sealed) != certificate.get('capture_digest')
                  or not bundle.digest_bytes(record['artifact']) == certificate.get('sha256')):
                entry['classification'] = 'evidence_integrity'
            else:
                entry['classification'] = 'eligible'
            if entry['classification'] != 'eligible':
                entry['certificate'] = None
            candidates.append(entry)
        return acceptance.evaluate(session['clauses'], evidence, candidates)

    def describe_acceptance(self, record, evaluated=None):
        if evaluated is None:
            evaluated = self._evaluate(record, self._citations(record))
        return {'id': record['id'], 'groups': record['groups'], 'recipe': record['recipe_name'],
                'version': record['recipe_version'], 'schema_digest': record['schema_digest'],
                'created': record['created'], 'clauses': record['clauses'],
                **evaluated}

    def _citations(self, session):
        evidence = []
        for citation in self.repository.citations(session['id']):
            try:
                record = self.repository.load_capture(citation['capture_id'], with_artifact=True)
            except Fault:
                evidence.append(dict(clause_id=citation['clause_id'], capture=citation['capture_id'],
                                     name=None, version=None, group=citation['group_name'],
                                     certificate=None, usable=False, reason='incomplete',
                                     sealed={'result': {}}))
                continue
            certificate = citation['certificate']
            usable = (record['state'] == 'certified' and record['name'] == session['recipe_name']
                      and record['version'] == session['recipe_version']
                      and record['sealed']['schema_digest'] == session['schema_digest'])
            reason = 'ok'
            if not usable:
                reason = ('needs_certification' if record['state'] != 'certified'
                          else 'version_mismatch' if record['version'] != session['recipe_version']
                          else 'schema_mismatch')
            elif (record['certificate'] != certificate
                  or fingerprint(record['sealed']) != certificate['capture_digest']
                  or not bundle.digest_bytes(record['artifact']) == certificate['sha256']):
                usable, reason = False, 'evidence_integrity'
            evidence.append(dict(clause_id=citation['clause_id'], capture=record['id'],
                                 name=record['name'], version=record['version'],
                                 group=citation['group_name'], certificate=certificate,
                                 usable=usable, reason=reason, sealed=record['sealed']))
        return evidence

    def supplement(self, session_id, body):
        """Seal the sample a gap asks for, still under the session's fixed version."""
        recipe.fields(body, ('clause',))
        if not isinstance(body['clause'], str):
            raise Fault('invalid_request', 'Supply the clause identifier', 400)
        session = self.repository.acceptance(session_id)
        clause = next((item for item in session['clauses'] if item['id'] == body['clause']), None)
        if clause is None:
            raise Fault('unknown_clause', 'This acceptance session has no such clause', 404)
        version = self.repository.version(session['recipe_name'], session['recipe_version'])
        if not version['active']:
            raise Fault('retired_recipe', 'Recipe version is retired', 409)
        views = self._evaluate(session, self._citations(session))['clauses']
        clause_view = next(item for item in views if item['id'] == clause['id'])
        if clause_view['status'] == 'satisfied':
            raise Fault('clause_satisfied', 'This clause already holds in one certified file', 409)
        suggestion = clause_view['needed_sample']
        if suggestion['action'] == 'cite':
            raise Fault('clause_awaiting_citation',
                        'An eligible certified capture already covers this clause; cite it instead',
                        409)
        if suggestion['action'] == 'certify':
            capture_id = suggestion['captures'][0]
            self.build(capture_id)
            self.certify(capture_id)
            return {'session': self.describe_acceptance(self.repository.acceptance(session_id)),
                    'capture': self.status(capture_id)}
        normalized = dict(version['recipe'],
                          roots=suggestion['body']['roots'],
                          follow_children=suggestion['body'].get('follow_children', []),
                          row_limit=version['recipe']['row_limit'])
        with self.repository.source(normalized['source']) as conn:
            disclosed = catalog.snapshot(conn)
            if disclosed['schema_digest'] != version['schema_digest']:
                raise Fault('schema_conflict', 'Source schema differs from the acceptance binding', 409)
            checked = recipe.validate(disclosed['schema'], normalized)
            sealed = disclosed | {'recipe': checked,
                                  'result': capture.collect(conn, disclosed['schema'], checked)}
        record = self.repository.add_capture(session['recipe_name'], session['recipe_version'], sealed)
        return {'session': self.describe_acceptance(self.repository.acceptance(session_id)),
                'capture': self.describe(record)}
