"""Stage decisions for the capture handoff: build, certify, and download.

A row in ``captures`` carries every fact of a handoff: ``state`` walks
``sealed`` → ``built`` → ``certified`` while ``artifact`` and ``certificate``
accumulate the rebuilt SQLite bytes and their attestation. This module owns
when those columns may change; entry points (the JSON API, the HTML workbench,
or future local callers) receive the outcomes and only translate them into
their own transport shapes. ``keelsplice.web.handoff`` renders these decisions
as HTML; ``keelsplice.api`` maps them to JSON and download headers.
"""

import sqlite3

from . import bundle
from .codec import Fault


class Handoff:
    """Apply the capture stage gates against one repository."""

    def __init__(self, repository):
        self.repository = repository

    def build(self, capture_id):
        """Persist rebuilt fixture bytes for a sealed capture; retries are no-ops."""
        record = self.repository.load_capture(capture_id)
        if record['state'] == 'sealed':
            try:
                data = bundle.build(record['sealed'])
            except sqlite3.Error as exc:
                raise Fault('artifact_build', 'SQLite rejected the sealed schema or values') from exc
            record = self.repository.promote(capture_id, artifact=data)
        return record

    def certify(self, capture_id):
        """Persist the certificate for a built capture; retries return it."""
        record = self.repository.load_capture(capture_id, with_artifact=True)
        if record['state'] == 'sealed':
            raise Fault('fixture_not_built', 'Build the fixture before certifying', 409)
        if record['state'] == 'built':
            try:
                certificate = bundle.certify(record['artifact'], record['sealed'])
            except sqlite3.Error as exc:
                raise Fault('artifact_unreadable', 'SQLite could not read the resulting fixture') from exc
            record = self.repository.promote(capture_id, certificate=certificate)
        return record

    def download(self, capture_id):
        """Hand over certified bytes after re-checking them against the certificate."""
        record = self.repository.load_capture(capture_id, with_artifact=True)
        if record['state'] != 'certified':
            raise Fault('fixture_not_certified', 'Certify the fixture before handing it off', 409)
        data, sha = record['artifact'], record['certificate']['sha256']
        if bundle.digest_bytes(data) != sha:
            raise Fault('artifact_integrity', 'Stored fixture bytes do not match the certificate', 409)
        return data, sha
