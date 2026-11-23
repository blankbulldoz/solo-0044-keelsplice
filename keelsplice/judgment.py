"""Transferability conclusions for recorded hand-off reviews.

A review judgment is bound to the certificate it saw and to the digest of the
bytes it actually read. Browsing replays neither the artifact BLOB nor a fresh
verification: conclusions are derived from the persisted judgment, the current
certificate identity, and the archived certificate history.
"""


def status(review, current_certificate_id):
    """Classify one judgment against the certificate in force right now."""
    if current_certificate_id is not None and review['certificate_id'] == current_certificate_id:
        return 'current'
    return 'expired'


def describe(review, current_certificate_id):
    return review | {'status': status(review, current_certificate_id)}


def conclusion(state, current_certificate_id, reviews, history):
    """Give the transferability verdict for the capture's present situation.

    ``reviews`` must be newest first. The conclusion always names a code so the
    reviewer sees a definitive answer: ``transferable`` when the latest judgment
    verified the current bytes, ``expired`` when it only saw a historical
    certificate, ``historical_only`` when no certificate is in force but
    archived ones exist, ``blocked`` when the latest check found wrong bytes,
    and ``unreviewed``/``uncertified`` when no judgment or certificate exists.
    """
    if state != 'certified':
        code = 'historical_only' if history else 'uncertified'
        return {'code': code, 'transferable': False, 'review': None}
    if not reviews:
        return {'code': 'unreviewed', 'transferable': False, 'review': None}
    latest = reviews[0]
    if status(latest, current_certificate_id) == 'expired':
        return {'code': 'expired', 'transferable': False, 'review': latest['id']}
    return {'code': latest['verdict'], 'transferable': latest['verdict'] == 'transferable',
            'review': latest['id']}
