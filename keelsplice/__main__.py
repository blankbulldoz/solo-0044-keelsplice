"""Start the local Keelsplice backend."""

import argparse
import re
import sqlite3

from .api import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--source', action='append', required=True, metavar='ALIAS=FILE')
    parser.add_argument('--port', default=8794, type=int)
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < (3, 37):
        parser.error('SQLite 3.37 or newer is required for table metadata')
    sources = {}
    for item in args.source:
        alias, _, file = item.partition('=')
        if not re.fullmatch('[a-z][a-z0-9_-]{0,63}', alias) or not file or alias in sources:
            parser.error('Provide a unique source alias and filename as ALIAS=FILE')
        sources[alias] = file
    with create_server(('127.0.0.1', args.port), args.repository, sources) as server:
        print(f'Keelsplice listening on http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
