"""Start the local Keelsplice backend."""

import argparse
import os
import re
import sqlite3
from pathlib import Path

from .api import create_server
from .codec import Fault


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--source', action='append', required=True, metavar='ALIAS=FILE')
    parser.add_argument('--port', default=8794, type=int)
    parser.add_argument('--role', choices=('primary', 'standby'), default='primary')
    parser.add_argument('--switchover', metavar='MANIFEST',
                        help='primary only: verify this handover manifest before accepting writes')
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < (3, 37):
        parser.error('SQLite 3.37 or newer is required for table metadata')
    sources = {}
    for item in args.source:
        alias, _, file = item.partition('=')
        if not re.fullmatch('[a-z][a-z0-9_-]{0,63}', alias) or not file or alias in sources:
            parser.error('Provide a unique source alias and filename as ALIAS=FILE')
        sources[alias] = file
    if args.role == 'standby' and args.switchover:
        parser.error('A standby instance cannot consume a switchover manifest')
    try:
        server = create_server(('127.0.0.1', args.port), args.repository, sources,
                               role=args.role, switchover=args.switchover)
    except Fault as exc:
        parser.error(f'{exc.code}: {exc}')
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    port_file = os.environ.get('KEELSPLICE_PORT_FILE')
    if port_file:
        Path(port_file).write_text(str(server.server_port), encoding='ascii')
    with server:
        print(f'Keelsplice listening on http://127.0.0.1:{server.server_port} ({args.role})', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
