"""Delivery-batch operator decisions and recipient-facing text checks."""

from .codec import Fault

MAX_LABEL = 64
MAX_RECIPIENT = 64
MAX_NOTE = 200
MAX_ENTRIES = 256

ACTIONS = {'continue': 'continued', 'hold': 'held', 'settle': 'settled'}


def clean(value, code, limit, allow_blank=False):
    if not isinstance(value, str):
        raise Fault(code, 'Expected printable text')
    value = value.strip()
    if not value:
        if allow_blank:
            return ''
        raise Fault(code, 'Expected non-empty printable text')
    if len(value) > limit or not value.isprintable():
        raise Fault(code, f'Expected up to {limit} printable characters')
    return value


def label(value):
    return clean(value, 'invalid_label', MAX_LABEL)


def recipient(value):
    return clean(value, 'invalid_recipient', MAX_RECIPIENT)


def note(value):
    return clean(value, 'invalid_note', MAX_NOTE, allow_blank=True)


def decision(action):
    if not isinstance(action, str) or action not in ACTIONS:
        raise Fault('invalid_action', 'Action must be continue, hold, or settle')
    return ACTIONS[action]
