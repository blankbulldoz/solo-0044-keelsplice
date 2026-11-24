"""Create a local root and server certificate for the HTTPS front proxy."""

import argparse
import subprocess
import sys
from pathlib import Path

TEMPLATE = '''[req]
distinguished_name = subject
x509_extensions = extensions
prompt = no
[subject]
{subject}
[extensions]
basicConstraints = critical,CA:{ca}
keyUsage = critical,{usage}
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
{extra}
'''


def openssl(*args):
    subprocess.run(('openssl', *args), check=True, stdout=subprocess.DEVNULL)


def write(path, text):
    path.write_text(text, encoding='ascii')


def authority(directory):
    key, cert = directory / 'keelsplice-ca.key', directory / 'keelsplice-ca.pem'
    if key.exists() != cert.exists():
        raise SystemExit('Remove the partial CA pair before regenerating it')
    if not cert.exists():
        write(directory / 'ca.cnf', TEMPLATE.format(
            subject='CN = Keelsplice local browser root', ca='TRUE',
            usage='keyCertSign,cRLSign', extra=''))
        openssl('genrsa', '-out', str(key), '3072')
        openssl('req', '-x509', '-new', '-sha256', '-days', '3650', '-key', str(key),
                '-out', str(cert), '-config', str(directory / 'ca.cnf'))
        (directory / 'ca.cnf').unlink()
    return key, cert


def server(directory, ca_key, ca_cert, hosts, days):
    key, cert = directory / 'keelsplice-server.key', directory / 'keelsplice-server.pem'
    names = ', '.join(f'DNS:{name}' if not _is_ip(name) else f'IP:{name}' for name in hosts)
    write(directory / 'server.cnf', TEMPLATE.format(
        subject='CN = ' + hosts[0], ca='FALSE', usage='digitalSignature,keyEncipherment',
        extra=f'subjectAltName = {names}'))
    openssl('genrsa', '-out', str(key), '2048')
    openssl('req', '-new', '-sha256', '-key', str(key), '-out', str(directory / 'server.csr'),
            '-config', str(directory / 'server.cnf'))
    openssl('x509', '-req', '-sha256', '-days', str(days), '-in', str(directory / 'server.csr'),
            '-CA', str(ca_cert), '-CAkey', str(ca_key), '-CAcreateserial',
            '-out', str(cert), '-extfile', str(directory / 'server.cnf'), '-extensions', 'extensions')
    (directory / 'server.cnf').unlink()
    (directory / 'server.csr').unlink()
    (directory / 'keelsplice-ca.srl').unlink(missing_ok=True)
    return key, cert


def _is_ip(name):
    return all(part.isdigit() for part in name.split('.')) or ':' in name


def fingerprint(cert):
    result = subprocess.run(('openssl', 'x509', '-in', str(cert), '-noout', '-fingerprint', '-sha256'),
                            check=True, capture_output=True, text=True)
    return result.stdout.strip().split('=', 1)[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, help='writable directory for key material')
    parser.add_argument('--host', action='append', default=[], metavar='NAME',
                        help='extra DNS name for the browser address (default localhost, 127.0.0.1)')
    parser.add_argument('--days', default=397, type=int)
    args = parser.parse_args()
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    hosts = ['localhost', '127.0.0.1'] + [name for name in args.host if name not in ('localhost', '127.0.0.1')]
    ca_key, ca_cert = authority(directory)
    server_key, server_cert = server(directory, ca_key, ca_cert, hosts, args.days)
    print(f'root   {ca_cert}')
    print(f'sha256 {fingerprint(ca_cert)}')
    print(f'cert   {server_cert}')
    print(f'key    {server_key}')
    print('将 keelsplice-ca.pem 导入浏览器或系统信任库后，再打开 HTTPS 工作台。')


if __name__ == '__main__':
    sys.exit(main())
