"""Normalize handoff receipts and decide which capture a recipient's evidence names.

Different captures can rebuild to identical SQLite bytes, so a matching SHA-256 alone
does not always identify the handoff a recipient accepted. Evidence is confirmed only
when, among the certified captures at registration time, the supplied digest (and the
supplied table counts, when present) selects exactly one capture.
"""

import re

from .codec import Fault, dumps
from .recipe import fields

DIGEST = re.compile('[0-9a-f]{64}\Z')
IDENTIFIER = re.compile('[0-9a-f]{32}\Z')
MAX_TABLES = 256
MAX_RECIPIENT = 200
MAX_NOTE = 2000
MAX_COUNTS_BYTES = 64 * 1024


def evidence(body):
    """Validate recipient-supplied evidence without consulting a certificate."""
    fields(body, ('sha256',), ('recipient', 'note', 'counts', 'corrects'))
    sha = body['sha256']
    if not isinstance(sha, str):
        raise Fault('invalid_digest', 'Supply the SHA-256 as hexadecimal text', 400)
    sha = sha.strip().lower()
    if not DIGEST.fullmatch(sha):
        raise Fault('invalid_digest', 'A SHA-256 must be 64 hexadecimal characters', 400)

    recipient = body.get('recipient', '')
    if not isinstance(recipient, str) or len(recipient) > MAX_RECIPIENT:
        raise Fault('invalid_receipt', f'The recipient must be text of at most {MAX_RECIPIENT} characters', 400)
    note = body.get('note', '')
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise Fault('invalid_receipt', f'The note must be text of at most {MAX_NOTE} characters', 400)

    counts = body.get('counts')
    if counts is not None:
        if not isinstance(counts, dict) or len(counts) > MAX_TABLES:
            raise Fault('invalid_counts', 'Supply table counts as a JSON object', 400)
        normalized = {}
        for table, count in counts.items():
            if not isinstance(table, str) or not table or len(table) > 200:
                raise Fault('invalid_counts', 'Each table count needs a non-empty table name', 400)
            if type(count) is not int or not 0 <= count < 2**63:
                raise Fault('invalid_counts', 'Each table count must be a non-negative integer', 400)
            normalized[table] = count
        if len(dumps(normalized).encode('ascii')) > MAX_COUNTS_BYTES:
            raise Fault('invalid_counts', 'Table counts exceed the size budget', 413)
        counts = normalized

    corrects = body.get('corrects')
    if corrects is not None:
        if not isinstance(corrects, str) or not IDENTIFIER.fullmatch(corrects):
            raise Fault('invalid_correction', 'Supply the identifier of the receipt being corrected', 400)

    return {'sha256': sha, 'recipient': recipient.strip(), 'note': note,
            'counts': counts, 'corrects': corrects}


def counts_match(supplied, certificate):
    """Every supplied table count must equal the certificate's count for that table."""
    return supplied is None or all(
        table in certificate['counts'] and certificate['counts'][table] == count
        for table, count in supplied.items()
    )


def judge(ev, capture_id, certificates):
    """Weigh evidence against every certified capture's certificate.

    ``certificates`` maps capture identifiers to certificate records. The verdict is
    ``confirmed`` only when the evidence, judged against the current set of certified
    captures, names this capture and no other: distinct captures may rebuild to
    identical bytes, and a shared digest cannot tell the recipient's file apart.
    Counts may cover only some tables; those supplied must match exactly.
    """
    own = certificates.get(capture_id)
    matches = sorted(
        other for other, certificate in certificates.items()
        if certificate['sha256'] == ev['sha256'] and counts_match(ev['counts'], certificate)
    )
    if own is None:
        reasons = ['uncertified']
    elif ev['sha256'] != own['sha256']:
        reasons = ['sha256']
    elif not counts_match(ev['counts'], own):
        reasons = ['counts']
    elif matches != [capture_id]:
        reasons = ['ambiguous']
    else:
        reasons = []
    status = 'confirmed' if not reasons else ('ambiguous' if reasons == ['ambiguous'] else 'mismatch')
    return {'status': status, 'reasons': reasons, 'matched': matches}


def reassess(row, capture_id, certificates):
    """Rejudge a persisted receipt against the certificates that exist now."""
    verdict = judge({'sha256': row['sha256'], 'counts': row['counts']}, capture_id, certificates)
    return describe(row) | verdict | {'registered_status': row['status'],
                                      'registered_reasons': row['reasons']}


def describe(row):
    described = {key: row[key] for key in (
        'id', 'capture_id', 'recipient', 'note', 'sha256', 'counts', 'status', 'reasons',
        'matched', 'certificate_sha256', 'capture_digest', 'corrects', 'created')} | {
        'superseded_by': list(row.get('superseded_by', ()))}
    if 'registered_status' in row:
        described['registered_status'] = row['registered_status']
        described['registered_reasons'] = row.get('registered_reasons', [])
    return described


def summarize(receipts):
    current = [row for row in receipts if not row['superseded_by']]
    return {'total': len(receipts), 'current': len(current),
            'signed': any(row['status'] == 'confirmed' for row in current),
            'confirmed': sum(row['status'] == 'confirmed' for row in current),
            'unconfirmed': sum(row['status'] != 'confirmed' for row in current),
            'corrected': sum(bool(row['superseded_by']) for row in receipts),
            'mixed': len({row['status'] for row in current}) > 1}
