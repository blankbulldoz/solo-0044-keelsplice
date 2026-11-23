"""Use-case coordinator linking API requests to source and repository state."""

import re
import sqlite3

from . import bundle, capture, catalog, recipe, watch as watch_domain
from .codec import Fault, loads

NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')


class Workbench:
    def __init__(self, repository):
        self.repository = repository

    def catalog(self, alias):
        with self.repository.source(alias) as conn:
            disclosed = catalog.snapshot(conn)
        self._record_observed(alias, disclosed)
        return disclosed

    def _record_observed(self, alias, disclosed):
        """Every successful catalog read with an active watch becomes evidence."""
        if not self.repository.list_watches(source=alias):
            return None
        try:
            return self.repository.add_catalog_check(
                alias, observed_at=watch_domain.now(), outcome='observed',
                schema_digest=disclosed['schema_digest'], schema=disclosed['schema'])
        except sqlite3.Error:
            return None

    def check_source(self, alias):
        """Explicit watcher check; the only path that records unreadable intervals."""
        if alias not in self.repository.sources:
            raise Fault('unknown_source', 'Source alias is not registered', 404)
        if not self.repository.list_watches(source=alias):
            raise Fault('no_watch', '这个源库还没有关注项，先登记关注后再核对。', 409)
        try:
            with self.repository.source(alias) as conn:
                disclosed = catalog.snapshot(conn)
        except Fault as exc:
            return self._record_unreachable(alias, f'{exc.code}: {exc}')
        except (sqlite3.Error, OSError) as exc:
            kind = 'sqlite_error' if isinstance(exc, sqlite3.Error) else 'os_error'
            name = getattr(exc, 'sqlite_errorname', exc.__class__.__name__)
            return self._record_unreachable(alias, f'{kind}: {name}')
        check = self.repository.add_catalog_check(
            alias, observed_at=watch_domain.now(), outcome='observed',
            schema_digest=disclosed['schema_digest'], schema=disclosed['schema'])
        notifications = self._evaluate_watches(alias, check)
        return self.check_result(check), notifications

    def _record_unreachable(self, alias, reason):
        check = self.repository.add_catalog_check(
            alias, observed_at=watch_domain.now(), outcome='unreachable', reason=reason)
        notifications = self._evaluate_watches(alias, check)
        return self.check_result(check), notifications

    def _evaluate_watches(self, alias, check):
        checks = self.repository.checks_for_source(alias)
        earlier = [item for item in checks if item['id'] != check['id']]
        previous = earlier[-1] if earlier else None
        recipes = self.repository.all_recipes()
        notifications = []
        for item in self.repository.list_watches(source=alias):
            if check['outcome'] == 'unreachable':
                if previous is not None and previous['outcome'] == 'unreachable':
                    continue
                notifications.append(self._unreachable_notification(item, check, checks))
            else:
                made = self._change_notification(item, check, checks, recipes)
                if made is not None:
                    notifications.append(made)
        return [item for item in notifications if item]

    def _unreachable_notification(self, item, check, checks):
        return self.repository.add_notification(
            watch_id=item['id'], kind='unreachable', check_id=check['id'],
            created_at=watch_domain.now(),
            summary=f"{watch_domain.label(item)}的源库暂时读不到：{check['reason']}",
            evidence_entries=watch_domain.evidence(item, checks),
            recipients=watch_domain.recipients(item, self.repository.all_recipes()))

    def _change_notification(self, item, check, checks, recipes):
        timeline = watch_domain.build_timeline(item, checks)
        entry = next((value for value in timeline if value['check_id'] == check['id']), None)
        if entry is None or entry['state'] not in ('changed', 'missing'):
            return None
        prior = next((value for value in reversed(timeline[:-1])
                      if value['check_id'] != check['id']
                      and value['fingerprint'] is not None), None)
        summary = watch_domain.change_summary(
            item, prior['structure'] if prior else None, entry['structure'])
        return self.repository.add_notification(
            watch_id=item['id'], kind='changed', check_id=check['id'],
            created_at=watch_domain.now(), summary=summary,
            evidence_entries=watch_domain.evidence(item, checks),
            recipients=watch_domain.recipients(item, recipes))

    def create_watch(self, body):
        recipe.fields(body, ('source', 'kind'),
                      ('child', 'id', 'table', 'continuity_seconds', 'note'))
        if body['source'] not in self.repository.sources:
            raise Fault('unknown_source', 'Source alias is not registered', 404)
        target = watch_domain.validate_target(body) | {'source': body['source']}
        existing = self.repository.active_watch(body['source'], target)
        if existing is not None:
            raise Fault('watch_exists', '这个结构已经在关注列表中。', 409)
        with self.repository.source(body['source']) as conn:
            disclosed = catalog.snapshot(conn)
        current = watch_domain.subject(disclosed['schema'], target)
        if current is None:
            raise Fault('watch_subject_missing', '关注对象不在当前目录中，无法建立基线。', 422)
        baseline = watch_domain.subject_fingerprint(disclosed['schema'], target)
        item = self.repository.add_watch(
            target, continuity_seconds=watch_domain.continuity(body.get('continuity_seconds')),
            note_text=watch_domain.note(body.get('note')),
            baseline_fingerprint=baseline, created_at=watch_domain.now())
        self._record_observed(body['source'], disclosed)
        return self.watch_detail(item['id'])

    def watches(self, source=None):
        return [self._watch_view(item) for item in self.repository.list_watches(source=source)]

    def _watch_view(self, item):
        checks = self.repository.checks_for_source(item['source'])
        timeline = watch_domain.build_timeline(item, checks)
        counts = self.repository.unacknowledged_watch_counts()
        latest = timeline[-1] if timeline else None
        return {
            'watch': item,
            'latest_state': latest['state'] if latest else 'baseline',
            'latest_at': latest['observed_at'] if latest else None,
            'unacknowledged': counts.get(item['id'], 0),
            'segments': watch_domain.build_segments(item, timeline),
            'checks': len(checks),
        }

    def watch_detail(self, watch_id):
        item = self.repository.get_watch(watch_id)
        checks = self.repository.checks_for_source(item['source'])
        timeline = watch_domain.build_timeline(item, checks)
        view = self._watch_view(item)
        return view | {
            'timeline': timeline,
            'notifications': self.repository.list_notifications(watch_id=item['id']),
            'baseline_structure': watch_domain.subject(loads(checks[0]['schema']), item)
            if checks and checks[0]['schema'] else None,
        }

    def retire_watch(self, watch_id):
        item = self.repository.retire_watch(watch_id)
        return self._watch_view(item)

    def notifications(self, only_unacknowledged=False):
        rows = self.repository.list_notifications(only_unacknowledged=only_unacknowledged)
        watches = {item['id']: item for item in self.repository.list_watches(include_inactive=True)}
        for row in rows:
            row['watch'] = watches.get(row['watch_id'])
        return rows

    def notification_detail(self, notification_id):
        row = self.repository.get_notification(notification_id)
        row['watch'] = self.repository.get_watch(row['watch_id'])
        check = None
        for candidate in self.repository.checks_for_source(row['watch']['source']):
            if candidate['id'] == row['check_id']:
                check = candidate
        row['check'] = check
        return row

    def acknowledge(self, notification_id):
        return self.repository.acknowledge_notification(notification_id)

    def check_result(self, check):
        return {key: check[key] for key in
                ('id', 'source', 'observed_at', 'outcome', 'schema_digest', 'reason')}

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
