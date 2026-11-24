"""Separate direct loopback browser traffic from HTTPS relayed by the local proxy."""

import hmac
import re
from dataclasses import dataclass

from ..codec import Fault

LOOPBACK_HOST = re.compile(r'(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?\Z')
PUBLIC_ORIGIN = re.compile(
    r'https://(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?\Z'
)
FORWARD_HEADERS = ('Forwarded', 'X-Forwarded-For', 'X-Forwarded-Host',
                   'X-Forwarded-Port', 'X-Forwarded-Proto')
PROXY_HEADER = 'X-Keelsplice-Proxy'
MIN_TOKEN = 16

LABELS = {
    'loopback': '本机回环',
    'https-proxy': 'HTTPS · 前置代理',
    'untrusted': '未验证来源',
}


@dataclass(frozen=True)
class Policy:
    """Origins and shared secret the backend accepts from its local TLS proxy."""

    public_origins: frozenset
    proxy_token: str

    @property
    def proxied(self):
        return bool(self.public_origins and self.proxy_token)


def public_origin(value):
    """Normalize an https://host[:port] origin or reject it."""
    if not isinstance(value, str):
        raise ValueError('Public origin must be a string')
    normalized = value.strip().lower()
    if not PUBLIC_ORIGIN.fullmatch(normalized):
        raise ValueError('Public origin must look like https://host[:port]')
    return normalized


def split_origin(value):
    """Return (host, port) for a normalized https origin, defaulting to 443."""
    normalized = public_origin(value).removeprefix('https://')
    if normalized.startswith('['):
        host, _, rest = normalized.partition(']')
        port = rest.removeprefix(':') or '443'
        return host.removeprefix('['), port
    if ':' in normalized:
        host, _, port = normalized.rpartition(':')
        return host, port
    return normalized, '443'


def make_policy(public_origins=(), proxy_token=''):
    token = proxy_token or ''
    if not isinstance(token, str) or (token and (len(token) < MIN_TOKEN or not token.isascii())):
        raise ValueError(f'Proxy token must be at least {MIN_TOKEN} ASCII characters')
    origins = frozenset(public_origin(value) for value in public_origins)
    if bool(origins) != bool(token):
        raise ValueError('Configure public origins and the proxy token together')
    return Policy(origins, token)


def _forwarded(headers):
    return headers.get(PROXY_HEADER) is not None or any(
        headers.get(name) is not None for name in FORWARD_HEADERS
    )


def _trusted(headers, policy):
    supplied = headers.get(PROXY_HEADER) or ''
    if not policy.proxy_token or len(supplied) != len(policy.proxy_token):
        return False
    if not hmac.compare_digest(supplied, policy.proxy_token):
        return False
    if headers.get('X-Forwarded-Proto', '').lower() != 'https':
        return False
    forwarded_host = (headers.get('X-Forwarded-Host') or '').strip().lower()
    if not forwarded_host:
        return False
    try:
        origin = public_origin('https://' + forwarded_host)
    except ValueError:
        return False
    return origin in policy.public_origins


def classify(headers, policy):
    """Return loopback, https-proxy, or untrusted without raising."""
    if _forwarded(headers):
        return 'https-proxy' if _trusted(headers, policy) else 'untrusted'
    host = headers.get('Host') or ''
    return 'loopback' if LOOPBACK_HOST.fullmatch(host) else 'untrusted'


def expected_origin(headers, kind):
    if kind == 'https-proxy':
        return 'https://' + (headers.get('X-Forwarded-Host') or '').strip().lower()
    return 'http://' + (headers.get('Host') or '').strip().lower()


def require_form(headers, policy):
    """Require a same-origin form post from one of the accepted transports."""
    kind = classify(headers, policy)
    if kind == 'untrusted':
        raise Fault(
            'browser_origin',
            '请从本机工作台或已登记的 HTTPS 代理地址提交此表单。',
            403,
        )
    supplied = (headers.get('Origin') or '').strip().lower()
    if not supplied or supplied != expected_origin(headers, kind):
        raise Fault('browser_origin', '请从本机工作台提交此表单，且来源须与当前地址一致。', 403)
    return kind
