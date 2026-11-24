"""Start the local Keelsplice backend."""

import argparse
import os
import re
import sqlite3

from .api import create_server
from .web.access import make_policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--source', action='append', required=True, metavar='ALIAS=FILE')
    parser.add_argument('--port', default=8794, type=int)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--public-origin', action='append', default=[], metavar='https://HOST[:PORT]',
                        help='public HTTPS workbench origin relayed by the local proxy')
    parser.add_argument('--proxy-token-env', default='KEELSPLICE_PROXY_TOKEN', metavar='NAME',
                        help='environment variable holding the shared proxy token')
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < (3, 37):
        parser.error('SQLite 3.37 or newer is required for table metadata')
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('Keep the backend on a loopback host; the TLS proxy is the only public edge')
    token = os.environ.get(args.proxy_token_env, '') if args.public_origin else ''
    try:
        policy = make_policy(args.public_origin, token)
    except ValueError as exc:
        parser.error(str(exc))
    sources = {}
    for item in args.source:
        alias, _, file = item.partition('=')
        if not re.fullmatch('[a-z][a-z0-9_-]{0,63}', alias) or not file or alias in sources:
            parser.error('Provide a unique source alias and filename as ALIAS=FILE')
        sources[alias] = file
    with create_server((args.host, args.port), args.repository, sources,
                       public_origins=policy.public_origins, proxy_token=policy.proxy_token) as server:
        print(f'Keelsplice listening on http://{args.host}:{server.server_port}', flush=True)
        if policy.proxied:
            print('Trusted HTTPS origins: ' + ', '.join(sorted(policy.public_origins)), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
