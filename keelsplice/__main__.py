"""Start the Keelsplice backend on the built-in server or a WSGI host."""

import argparse
import sqlite3

from .api import create_server
from .storage import ALIAS
from . import wsgi


def sources(items):
    """Parse repeated ``ALIAS=FILE`` arguments into a registration mapping."""
    found = {}
    for item in items:
        alias, _, file = item.partition('=')
        if not ALIAS.fullmatch(alias) or not file or alias in found:
            raise ValueError('Provide a unique source alias and filename as ALIAS=FILE')
        found[alias] = file
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--source', action='append', required=True, metavar='ALIAS=FILE')
    parser.add_argument('--port', default=8794, type=int)
    parser.add_argument('--wsgi', action='store_true',
                        help='serve through the bundled PEP 3333 host instead of http.server')
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < (3, 37):
        parser.error('SQLite 3.37 or newer is required for table metadata')
    try:
        registered = sources(args.source)
    except ValueError as exc:
        parser.error(str(exc))
    # Both entries stay on the loopback interface; the browser form check and
    # the trusted-operator model both rely on that.
    address = ('127.0.0.1', args.port)
    server = wsgi.serve(address, args.repository, registered) if args.wsgi else \
        create_server(address, args.repository, registered)
    with server:
        print(f'Keelsplice listening on http://127.0.0.1:{server.server_port} '
              f'({"wsgi" if args.wsgi else "http.server"})', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
