"""End-to-end driver: legacy 3.11 repository, cutover, verification, rollback.

Each phase runs as an explicit subprocess under the interpreter that owns it,
so the upgrade is demonstrated with two real CPython builds rather than with
imports inside one process.  The driver itself only needs the standard library
and may run under either interpreter.

Phases
------
1. runtime probes on both interpreters, compared into a go/no-go verdict that
   keeps Python-runtime and SQLite-library changes in separate sections.
2. legacy repository built entirely by the 3.11 interpreter (recipes, retired
   versions, sealed captures, certified fixture and all 3.11 reports).
3. freeze: checkpointed backup plus an executable rollback recipe.
4. cutover under 3.14: preservation checks, new capture on the replica, fresh
   build/certify, independent re-reads and browser-form checks.
5. fresh-disposable form parity run on 3.14, compared byte-for-byte with the
   3.11 wire report.
6. rollback drill: restore the frozen bytes and re-verify on the repository.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from upgrade._subprocess import run_phase  # noqa: E402

SWITCH_RECORD = 'switch_record.json'
ROLLBACK_RECORD = 'rollback.json'


def _phase(python, module, arguments):
    completed = run_phase(python, module, arguments, cwd=PROJECT_ROOT)
    message = completed.stdout.strip().splitlines()
    for line in message:
        print('   ', line)
    return completed


def run(old_python, new_python, workdir, sqlite_cli):
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    reports = workdir / 'reports'
    reports.mkdir(parents=True, exist_ok=True)
    timeline = []

    def step(name, callable_):
        started = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        print(f'[phase] {name} ({started})')
        callable_()
        timeline.append({'phase': name, 'started_at_utc': started,
                         'finished_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})

    # 1. Runtime inventory and the separated go/no-go comparison.
    step('runtime-probe-3.11', lambda: _phase(
        old_python, 'upgrade.runtime_probe',
        ['probe', '--out', reports / 'runtime-3.11.json']))
    step('runtime-probe-3.14', lambda: _phase(
        new_python, 'upgrade.runtime_probe',
        ['probe', '--out', reports / 'runtime-3.14.json']))
    step('runtime-compare', lambda: _phase(
        new_python, 'upgrade.runtime_probe',
        ['compare', '--old', reports / 'runtime-3.11.json',
         '--new', reports / 'runtime-3.14.json', '--out', reports / 'runtime-compare.json']))
    verdict = json.loads((reports / 'runtime-compare.json').read_text(encoding='utf-8'))
    if verdict['verdict'] != 'go':
        raise SystemExit(f"runtime comparison verdict: {verdict['verdict']} {verdict['verdict_reasons']}")

    # 2. Legacy artifacts must come from the real 3.11 interpreter.
    step('build-legacy-repository-on-3.11', lambda: _phase(
        old_python, 'upgrade.legacy_repo',
        ['--output', workdir] + (['--sqlite-cli', sqlite_cli] if sqlite_cli else [])))

    # 3-4. Freeze and cutover (cutover re-freezes defensively).
    step('freeze', lambda: _phase(
        new_python, 'upgrade.switch',
        ['freeze', '--workdir', workdir, '--old-python', old_python]))
    step('cutover-to-3.14', lambda: _phase(
        new_python, 'upgrade.switch',
        ['cutover', '--workdir', workdir, '--new-python', new_python]
        + (['--sqlite-cli', sqlite_cli] if sqlite_cli else [])))

    # 5. Fresh disposable repository under 3.14 for exact form byte parity.
    step('fresh-form-probe-on-3.14', lambda: _phase(
        new_python, 'upgrade.http_form_probe',
        ['probe', '--role', 'fresh', '--prefix', 'parity314',
         '--out', reports / 'http-form-3.14-fresh.json']
        + (['--sqlite-cli', sqlite_cli] if sqlite_cli else [])))
    step('form-byte-compare', lambda: _phase(
        new_python, 'upgrade.http_form_probe',
        ['compare', '--old', reports / 'http-form-3.11.json',
         '--new', reports / 'http-form-3.14-fresh.json',
         '--out', reports / 'http-form-compare.json']))
    form_verdict = json.loads((reports / 'http-form-compare.json').read_text(encoding='utf-8'))
    if form_verdict['verdict'] != 'go':
        raise SystemExit('HTTP form byte comparison failed')

    # 6. Rollback drill against the frozen bytes.
    step('rollback-drill', lambda: _phase(
        new_python, 'upgrade.switch', ['rollback', '--workdir', workdir]))
    step('repository-reread-after-rollback', lambda: _phase(
        old_python, 'upgrade.fixture_probe',
        ['repository', '--repository', workdir / 'state.sqlite',
         '--out', reports / 'repository-reread-after-rollback.json']))

    # 7. Re-apply the cutover so the delivered workdir ends on 3.14.
    step('recutover-to-3.14-after-drill', lambda: _phase(
        new_python, 'upgrade.switch',
        ['cutover', '--workdir', workdir, '--new-python', new_python]
        + (['--sqlite-cli', sqlite_cli] if sqlite_cli else [])))

    summary = _summarize(workdir, reports, timeline, old_python, new_python)
    _write(summary, workdir / 'upgrade_summary.json')
    print('\n[upgrade] complete')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _summarize(workdir, reports, timeline, old_python, new_python):
    switch = json.loads((workdir / SWITCH_RECORD).read_text(encoding='utf-8'))
    manifest = json.loads((workdir / 'legacy_manifest.json').read_text(encoding='utf-8'))
    runtime_compare = json.loads((reports / 'runtime-compare.json').read_text(encoding='utf-8'))
    form_compare = json.loads((reports / 'http-form-compare.json').read_text(encoding='utf-8'))
    rollback_reports = sorted((workdir / 'backups').glob('rollback-*.json'))
    return {
        'generated_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'old_python': old_python,
        'new_python': new_python,
        'workdir': str(workdir),
        'runtime_verdict': {
            'verdict': runtime_compare['verdict'],
            'python_runtime_changes': len(runtime_compare['runtime_changes']),
            'sqlite_library_changes': len(runtime_compare['sqlite_library_changes']),
            'capability_changes': len(runtime_compare['capability_changes']),
        },
        'http_form_verdict': {
            'verdict': form_compare['verdict'],
            'byte_identical_responses': sum(
                1 for entry in form_compare['stable_byte_identical_responses']
                if entry['byte_identical']),
            'mismatches': len(form_compare['byte_mismatches']),
        },
        'preservation': switch['preserved'],
        'digest_checks': switch['digests'],
        'legacy_capture': manifest['captures'][0]['id'],
        'new_capture': switch['new_capture']['id'],
        'rollback_drill': {
            'recipe': str(workdir / ROLLBACK_RECORD),
            'last_report': str(rollback_reports[-1]) if rollback_reports else None,
        },
        'phases': timeline,
        'reports_directory': str(reports),
    }


def _write(payload, destination):
    Path(destination).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-python', default='/usr/bin/python3.11')
    parser.add_argument('--new-python',
                        default='/workspace/.python-runtimes/python-3.14.10/bin/python3.14')
    parser.add_argument('--workdir', default=str(PROJECT_ROOT / 'upgrade-workdir'))
    parser.add_argument('--sqlite-cli',
                        default='/workspace/.python-runtimes/sqlite-3.54.1/bin/sqlite3')
    args = parser.parse_args(argv)
    run(args.old_python, args.new_python, args.workdir, args.sqlite_cli)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
