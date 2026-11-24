"""Re-run the WAL retention archive end to end in a disposable directory.

Builds the archive, copies it somewhere else, runs the bundled ``verify.py``
there as a separate process, and confirms the verifier rejects a tampered copy.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from keelsplice import retention_verify as evidence

from .retention_archive import build


def run_verifier(directory):
    process = subprocess.run(
        [sys.executable, '-I', 'verify.py', '.'],
        cwd=str(directory), capture_output=True, text=True, timeout=120)
    return process


def main():
    with tempfile.TemporaryDirectory(prefix='keelsplice-retention-check-') as directory:
        root = Path(directory)
        archive = root / 'archive'
        result = build(archive)
        assert result['points'] == ['P0', 'P1', 'P2', 'P3'], result

        offsite = root / 'offsite'
        shutil.copytree(archive, offsite)
        process = run_verifier(offsite)
        assert process.returncode == 0, process.stdout + process.stderr
        summary = json.loads(process.stdout.strip().splitlines()[-1])
        assert summary['ok'] and summary['points'] == ['P0', 'P1', 'P2', 'P3'], summary

        # The bundled verifier must work with no Keelsplice import on the path.
        source = (offsite / 'verify.py').read_text(encoding='utf-8')
        assert 'from keelsplice' not in source and 'import keelsplice' not in source

        tampered = root / 'tampered'
        shutil.copytree(archive, tampered)
        manifest = json.loads((tampered / 'manifest.json').read_text(encoding='utf-8'))
        for relation in manifest['relations']:
            if relation['left'] == 'P2' and relation['right'] == 'P3':
                relation['relation'] = 'same_visible'
        (tampered / 'manifest.json').write_bytes(evidence.dumps(manifest).encode('ascii'))
        rejected = run_verifier(tampered)
        assert rejected.returncode != 0 and 'FAIL' in rejected.stdout, rejected.stdout

        print(json.dumps({'archive_checks': result['checks'],
                          'offsite_checks': summary['checks'],
                          'schema_digests': summary['schema_digests'],
                          'content_digests': summary['content_digests'],
                          'tampered_rejected': True}, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
