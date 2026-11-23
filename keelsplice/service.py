"""Use-case coordinator linking API requests to source and repository state."""

import re
import sqlite3
import threading

from . import bundle, capture, catalog, jobs, recipe
from .codec import Fault

NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')


class Workbench:
    def __init__(self, repository):
        self.repository = repository
        # Live preview tokens keyed by job id. Each owns one independent read transaction.
        self._controls = {}
        self._controls_lock = threading.Lock()
        # Optional test seam fired inside the read transaction before closure work starts.
        self.preview_gate = None

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

    def execute(self, body, *, cancel=None):
        recipe.fields(body, ('name', 'version'))
        name, number = body['name'], body['version']
        if not isinstance(name, str) or not NAME.fullmatch(name) or type(number) is not int or not 1 <= number < 2**63:
            raise Fault('invalid_version', 'Supply a recipe name and positive version')
        version = self.repository.version(name, number)
        if not version['active']:
            raise Fault('retired_recipe', 'Recipe version is retired', 409)
        normalized = version['recipe']
        with self.repository.source(normalized['source'], cancel=cancel) as conn:
            if cancel is not None and self.preview_gate is not None:
                # Runs while this job's read transaction (and snapshot) is already open.
                self.preview_gate(cancel.job_id, conn, cancel)
            disclosed = catalog.snapshot(conn)
            if disclosed['schema_digest'] != version['schema_digest']:
                raise Fault('schema_conflict', 'Source schema differs from the recipe binding', 409)
            result = capture.collect(conn, disclosed['schema'], normalized)
        return name, number, disclosed | {'recipe': normalized, 'result': result}

    def preview(self, body):
        name, number, sealed = self.execute(body)
        return {'name': name, 'version': number, 'schema_digest': sealed['schema_digest'],
                'summary': capture.summarize(sealed['result']), 'roots': sealed['result']['roots']}

    def start_preview(self, body):
        """Validate synchronously, persist a running job, then run its own read transaction."""
        recipe.fields(body, ('name', 'version'))
        name, number = body['name'], body['version']
        if not isinstance(name, str) or not NAME.fullmatch(name) or type(number) is not int \
                or not 1 <= number < 2**63:
            raise Fault('invalid_version', 'Supply a recipe name and positive version')
        version = self.repository.version(name, number)
        if not version['active']:
            raise Fault('retired_recipe', 'Recipe version is retired', 409)
        job_id = self.repository.add_preview(name, number)
        control = jobs.PreviewControl(job_id)
        with self._controls_lock:
            self._controls[job_id] = control
        worker = threading.Thread(target=self._run_preview, args=(job_id, body, control),
                                  name=f'preview-{job_id}', daemon=True)
        worker.start()
        return self.describe_preview(self.repository.load_preview(job_id))

    def _run_preview(self, job_id, body, control):
        try:
            name, number, sealed = self.execute(body, cancel=control)
            detail = {'name': name, 'version': number,
                      'schema_digest': sealed['schema_digest'],
                      'summary': capture.summarize(sealed['result']),
                      'roots': sealed['result']['roots']}
            # Conditional commit: a cancel already recorded in the repository wins over this result.
            self.repository.finish_preview(job_id, 'completed', detail)
        except Fault as exc:
            if exc.code == 'preview_cancelled':
                self.repository.finish_preview(job_id, 'cancelled')
            else:
                self.repository.finish_preview(job_id, 'failed',
                                               {'error': exc.code, 'message': str(exc), 'status': exc.status})
        except sqlite3.Error as exc:
            if jobs.interrupted(exc, control):
                self.repository.finish_preview(job_id, 'cancelled')
            else:
                self.repository.finish_preview(job_id, 'failed', {'error': 'database_input',
                                                                   'message': 'SQLite could not process the source'})
        except Exception as exc:  # a worker must never leave a job stuck in 'running'
            self.repository.finish_preview(job_id, 'failed', {'error': 'preview_failed', 'message': str(exc)})
        finally:
            with self._controls_lock:
                self._controls.pop(job_id, None)

    def preview_status(self, job_id):
        return self.describe_preview(self.repository.load_preview(job_id))

    def cancel_preview(self, job_id):
        """Release only this job's read transaction; settled jobs keep their existing outcome."""
        record = self.repository.load_preview(job_id)
        if record['state'] != 'running':
            return self.describe_preview(record)
        # Persist the cancellation first so a concurrently finishing worker cannot overwrite it.
        record = self.repository.request_preview_cancel(job_id)
        if record['state'] == 'cancelled':
            with self._controls_lock:
                control = self._controls.get(job_id)
            if control is not None:
                control.trigger()
        return self.describe_preview(record)

    def describe_preview(self, record):
        described = {k: record[k] for k in ('id', 'name', 'version', 'state')}
        described['detail'] = record['detail']
        return described

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
