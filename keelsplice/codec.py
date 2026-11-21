"""Stable wire representation of native SQLite values and row identities."""

import base64
import binascii
import hashlib
import json


class Fault(Exception):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code = code
        self.status = status


def dumps(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def loads(text):
    return json.loads(text)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(dumps(value).encode('ascii'))


def encode(value):
    if value is None or type(value) in (int, str):
        return value
    if isinstance(value, bytes):
        return {'blob': base64.b64encode(value).decode('ascii')}
    return {'real': value.hex()}


def decode(value):
    if value is None:
        return value
    if type(value) is str:
        try:
            value.encode('utf-8')
        except UnicodeError as exc:
            raise Fault('invalid_value', 'Text must be valid Unicode') from exc
        return value
    if type(value) is int and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, dict) and len(value) == 1:
        try:
            if 'blob' in value and isinstance(value['blob'], str):
                data = base64.b64decode(value['blob'], validate=True)
                if encode(data) == value:
                    return data
            if 'real' in value and isinstance(value['real'], str):
                data = float.fromhex(value['real'])
                if data == data and encode(data) == value:
                    return data
        except (ValueError, OverflowError, binascii.Error):
            pass
    raise Fault('invalid_value', 'Use NULL, signed int64, text, or a canonical blob/real object')


def key(values):
    return dumps(values)


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def row_values(row, columns):
    return [encode(row[column]) for column in columns]
