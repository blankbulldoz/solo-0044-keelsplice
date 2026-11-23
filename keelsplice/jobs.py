"""Cancellation tokens scoped to a single preview read transaction.

Each preview owns its own token and its own source connection. Cancelling one token
interrupts only that connection, so concurrent previews and seal captures are untouched.
"""

import sqlite3
import threading


class PreviewControl:
    def __init__(self, job_id):
        self.job_id = job_id
        self._event = threading.Event()
        self._interrupt = None
        self._lock = threading.Lock()

    def bind(self, interrupt):
        with self._lock:
            self._interrupt = interrupt
            if self._event.is_set():
                interrupt()

    def release(self):
        with self._lock:
            self._interrupt = None

    def trigger(self):
        """Signal cancellation and abort any statement running on this connection only."""
        self._event.set()
        with self._lock:
            if self._interrupt is not None:
                self._interrupt()

    def is_set(self):
        return self._event.is_set()

    def progress(self, budget_check):
        """SQLite progress handler: keep the scan budget and add a cooperative cancel gate."""
        def handler():
            if self._event.is_set():
                return 1
            return budget_check()
        return handler


def interrupted(exc, control):
    """True when a SQLite operational error came from this token's cancellation."""
    return isinstance(exc, sqlite3.OperationalError) and control.is_set()
