"""Check shipped CMD launchers against incomplete and completed Windows installs.

Run after installing the real bundle and enforcing its outbound firewall rules.
Only the two small CMD files are copied; no runtime or research data is copied.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


LAUNCHERS = ('ResearchLibrary.cmd', 'CheckInstallation.cmd')
INCOMPLETE = ('Installation is incomplete. Extract a fresh package into a new '
              'folder and run Install.cmd.')


def _run_launcher(cmd, folder, name, *arguments, timeout=120):
    # Fixed relative names avoid embedding a user-selected path in CMD syntax.
    # CreateProcess receives cwd separately, including spaces in the bundle path.
    return subprocess.run([str(cmd), '/d', '/c', '.\\' + name, *arguments],
                          cwd=folder, input='\n', text=True, encoding='utf-8',
                          errors='replace', capture_output=True, timeout=timeout)


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _reports(output):
    reports = []
    for line in output.splitlines():
        try:
            report = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict):
            reports.append(report)
    return reports


def exercise(bundle):
    system_root = os.environ.get('SystemRoot')
    _require(system_root, 'Windows did not provide SystemRoot.')
    cmd = Path(system_root) / 'System32' / 'cmd.exe'
    _require(cmd.is_file(), 'The native Windows command interpreter is missing.')
    bundle = bundle.resolve(strict=True)
    marker = bundle / '.venv' / '.installation-complete'
    _require(marker.is_file(), 'The real bundle has no installation completion marker.')
    _require((bundle / '.venv' / 'Scripts' / 'python.exe').is_file(),
             'The installed bundle interpreter is missing.')
    for name in LAUNCHERS:
        _require((bundle / name).is_file(), 'The shipped launcher is missing: ' + name)

    checks = []
    with TemporaryDirectory(prefix='carr launcher smoke ') as temporary:
        root = Path(temporary)
        incomplete = root / 'Program with spaces'
        python = incomplete / '.venv' / 'Scripts' / 'python.exe'
        python.parent.mkdir(parents=True)
        python.write_bytes(b'Synthetic interpreter sentinel; must not be executed.\n')
        data = root / 'Separate research data' / 'keep.bin'
        data.parent.mkdir()
        data.write_bytes(b'Synthetic research data must remain unchanged.\x00')
        sentinels = {python: python.read_bytes(), data: data.read_bytes()}
        for name in LAUNCHERS:
            copied = incomplete / name
            copied.write_bytes((bundle / name).read_bytes())
            sentinels[copied] = copied.read_bytes()
        absent_marker = incomplete / '.venv' / '.installation-complete'
        for name in LAUNCHERS:
            result = _run_launcher(cmd, incomplete, name, timeout=20)
            output = result.stdout + result.stderr
            _require(result.returncode == 1 and INCOMPLETE in output,
                     f'{name} did not reject an incomplete installation: '
                     f'exit={result.returncode}; {output[-4000:]}')
            _require(not absent_marker.exists(), 'A launcher created a completion marker.')
            for path, contents in sentinels.items():
                _require(path.read_bytes() == contents, 'An incomplete-install sentinel changed.')
            checks.append(name + ': incomplete install rejected')

    for name in LAUNCHERS:
        arguments = ('--self-test',) if name == 'ResearchLibrary.cmd' else ()
        result = _run_launcher(cmd, bundle, name, *arguments)
        _require(result.returncode == 0,
                 f'{name} failed on the completed bundle: exit={result.returncode}; '
                 f'{(result.stdout + result.stderr)[-4000:]}')
        reports = _reports(result.stdout)
        _require(any(report.get('status') == 'PASS' and report.get('checks')
                     for report in reports), name + ' did not run the application self-test.')
        if name == 'CheckInstallation.cmd':
            _require(any(report.get('status') == 'PASS'
                         and report.get('windows_execution') == 'PASS' for report in reports),
                     'CheckInstallation.cmd did not complete its native dependency checks.')
        _require(marker.is_file(), 'The real installation completion marker disappeared.')
        checks.append(name + ': completed install passed')
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    arguments = parser.parse_args(argv)
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'nvda': 'NOT_RUN'}, separators=(',', ':')), flush=True)
        return 77
    try:
        checks = exercise(arguments.bundle)
    except Exception as error:
        print(json.dumps({'status': 'FAIL', 'error': str(error), 'nvda': 'NOT_RUN'},
                         separators=(',', ':')), flush=True)
        return 1
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'cases': checks,
                      'nvda': 'NOT_RUN'}, separators=(',', ':')), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
