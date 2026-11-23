"""Use-case coordinator linking API requests to source and repository state."""

import re
import sqlite3

from . import bundle, capture, catalog, recipe
from .codec import Fault, dumps

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
        return {'left': body['left'], 'right': body['right'], 'changes': capture.compare(left, right),
                'items': capture.differences(left, right)}

    def open_ledger(self, body):
        recipe.fields(body, ('name', 'baseline'))
        if not isinstance(body['name'], str) or not NAME.fullmatch(body['name']):
            raise Fault('invalid_name', 'Expected an ASCII ledger name')
        if not isinstance(body['baseline'], str):
            raise Fault('invalid_capture', 'Supply a capture identifier as the baseline')
        baseline = self.repository.load_capture(body['baseline'])['sealed']
        self.repository.add_ledger(body['name'], baseline['schema_digest'], body['baseline'])
        return self.ledger(body['name'])

    def ledger(self, name, comparison_id=None):
        record = self.repository.ledger(name)
        comparisons = self.repository.comparisons(name)
        labels = {row['baseline'] for row in comparisons} | {row['candidate'] for row in comparisons}
        labels.add(record['baseline'])
        reviews = {row['id']: self.repository.reviews(row['id']) for row in comparisons}
        # Reviews only follow a difference that reappears under the same
        # baseline. A rebase starts a fresh series, so earlier verdicts stay
        # attached to the comparison that actually judged them.
        carried = {}
        rendered = []
        for row in comparisons:
            previous = carried.setdefault(row['baseline'], {})
            items = []
            for item in row['items']:
                own = reviews[row['id']].get(item['token'])
                prior = list(previous.get(item['token'], []))
                # A verbatim repeat keeps the earlier verdict as the standing
                # review; a withdrawn or re-valued difference never does.
                state = 'reviewed' if own else ('carried' if prior else 'pending')
                items.append(dict(item, review=own, prior_reviews=prior, state=state))
            rendered.append(dict(row, items=items))
            for item in row['items']:
                review = reviews[row['id']].get(item['token'])
                if review:
                    previous.setdefault(item['token'], []).append(
                        dict(comparison=row['id'], ordinal=row['ordinal'],
                             verdict=review['verdict'], note=review['note']))
        series = [row for row in rendered if row['baseline'] == record['baseline']]
        current = series[-1] if series else None
        if comparison_id is None:
            chosen = current
        else:
            chosen = next((row for row in rendered if row['id'] == comparison_id), None)
            if chosen is None:
                raise Fault('unknown_comparison', 'Comparison does not exist', 404)
        withdrawn = []
        reopened = []
        if chosen:
            # Only a change under the same baseline can be a withdrawal or a
            # re-valuing of an earlier difference. An earlier series ended by
            # a rebase keeps its own verdicts and is never consulted here.
            target_series = [row for row in rendered if row['baseline'] == chosen['baseline']]
            present = {item['token'] for item in chosen['items']}
            current_keys = {(item['table'], tuple(item['key'])) for item in chosen['items']}
            seen = set()
            for row in target_series:
                if row['id'] == chosen['id']:
                    continue
                for item in row['items']:
                    identity = (item['table'], tuple(item['key']))
                    if item['token'] in present or item['token'] in seen or identity in current_keys:
                        continue
                    # Absent entirely: the row matches the baseline again or is
                    # gone, so the earlier change is genuinely withdrawn.
                    seen.add(item['token'])
                    withdrawn.append(dict(item, comparison=row['id'], ordinal=row['ordinal'],
                                          review=reviews[row['id']].get(item['token'])))
            # Same typed key, but a different change under the same baseline:
            # the earlier verdict belongs to that earlier comparison and must
            # not silently count as a check of this one. Keep the earlier
            # change even without a verdict so the reopen is still visible.
            keyed = {}
            for row in target_series:
                if row['id'] == chosen['id']:
                    continue
                for item in row['items']:
                    review = reviews[row['id']].get(item['token'])
                    keyed.setdefault((item['table'], tuple(item['key'])), []).append(
                        dict(comparison=row['id'], ordinal=row['ordinal'], token=item['token'],
                             kind=item['kind'], verdict=review['verdict'] if review else None,
                             note=review['note'] if review else ''))
            for item in chosen['items']:
                earlier = keyed.get((item['table'], tuple(item['key'])))
                if earlier and item['token'] not in {entry['token'] for entry in earlier}:
                    reopened.append(dict(table=item['table'], key=item['key'], token=item['token'],
                                         kind=item['kind'], earlier=earlier))
        return {'ledger': record, 'comparisons': rendered, 'current': current, 'chosen': chosen,
                'withdrawn': withdrawn, 'reopened': reopened,
                'captures': self._capture_labels(labels)}

    def _capture_labels(self, identifiers):
        labels = {}
        for capture_id in identifiers:
            try:
                record = self.repository.load_capture(capture_id)
            except Fault:
                labels[capture_id] = {'id': capture_id, 'name': None, 'version': None, 'state': None}
            else:
                labels[capture_id] = {key: record[key] for key in ('id', 'name', 'version', 'state')}
        return labels

    def record_comparison(self, name, body):
        recipe.fields(body, ('candidate',))
        if not isinstance(body['candidate'], str):
            raise Fault('invalid_capture', 'Supply a capture identifier to compare')
        record = self.repository.ledger(name)
        baseline = self.repository.load_capture(record['baseline'])['sealed']
        candidate = self.repository.load_capture(body['candidate'])['sealed']
        if candidate['schema_digest'] != record['schema_digest']:
            raise Fault('schema_conflict', 'Capture schema differs from the ledger binding', 409)
        items = capture.differences(baseline, candidate)
        row = self.repository.add_comparison(name, record['baseline'], body['candidate'], items)
        return {'id': row['id'], 'ledger': name, 'ordinal': row['ordinal'],
                'baseline': row['baseline'], 'candidate': row['candidate'], 'items': items}

    def rebase(self, name, body):
        recipe.fields(body, ('baseline',))
        if not isinstance(body['baseline'], str):
            raise Fault('invalid_capture', 'Supply a capture identifier as the new baseline')
        record = self.repository.ledger(name)
        baseline = self.repository.load_capture(body['baseline'])['sealed']
        if baseline['schema_digest'] != record['schema_digest']:
            raise Fault('schema_conflict', 'Capture schema differs from the ledger binding', 409)
        return self.repository.set_baseline(name, body['baseline'])

    def comparison(self, comparison_id):
        row = self.repository.review(comparison_id)
        evaluations = self.repository.reviews(comparison_id)
        return dict(row, items=[dict(item, review=evaluations.get(item['token'])) for item in row['items']])

    def review(self, comparison_id, body):
        recipe.fields(body, ('item', 'verdict'), ('note',))
        if not isinstance(body['item'], str):
            raise Fault('invalid_item', 'Supply the difference item identifier', 400)
        verdict, note = body['verdict'], body.get('note', '')
        if verdict not in ('confirmed', 'rejected'):
            raise Fault('invalid_verdict', 'Use confirmed or rejected', 400)
        if not isinstance(note, str) or len(note) > 2000:
            raise Fault('invalid_note', 'Notes must be text up to 2000 characters', 400)
        comparison = self.repository.review(comparison_id)
        item = next((entry for entry in comparison['items'] if entry['token'] == body['item']), None)
        if item is None:
            raise Fault('unknown_item', 'Difference item does not belong to this comparison', 404)
        self.repository.add_review(comparison_id, item['token'], item['table'],
                                   dumps(item['key']), item['kind'], verdict, note)
        return {'comparison': comparison_id, 'reviews': self.repository.reviews(comparison_id)}

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
