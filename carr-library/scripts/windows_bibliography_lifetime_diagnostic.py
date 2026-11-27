"""Run the unchanged native timer-shutdown probe against one pinned product."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import sysconfig
import traceback


SOURCE_COMMIT = 'a6aa369cb7454102642071abf115f1f1664cee09'
SOURCE_TREE = '9543f5509fe9c9bdf2e7dcd8687244acb00ab634'
PROBE_BLOB = '0b181a2bff9b54d7001ac64dd2ec057010693cae'
PROBE_SHA256 = 'b9825f794658300767d683e1240f40c0d3f45038ba9470045b8e6d33c16cbf63'
EXPECTED_CHECKS = 47
CHILD_TIMEOUT = 180
MODULES = (
    'carr_library.ui.academic_library_dialog',
    'carr_library.research.academic_library',
    'carr_library.research.bibtex_import',
    'carr_library.research.export_file',
    'carr_library.ui.desktop_app',
    'carr_library.ui.save_actions',
    'carr_library.ui.workspace_shell',
    'carr_library.academic_integration.formatting',
)
REQUIRED_PASS = (
    'native_keyboard', 'native_bibtex_import', 'native_export_options',
    'native_overwrite_confirmation', 'native_startup_citation_attribution',
    'native_startup_draft_timer_shutdown',
)
REQUIRED_NOT_RUN = (
    'native_overwrite_accessible_focus', 'manual_keyboard',
    'native_file_pickers', 'nvda', 'installer',
)


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


def run_probe(script, interpreter, project, output, expected_modules):
    stdout_path, stderr_path = output / 'native.stdout.txt', output / 'native.stderr.txt'
    command = [interpreter, '-I', '-B', str(script)]
    timed_out, code, launch_error, pid = False, None, None, None
    try:
        with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
            process = subprocess.Popen(command, cwd=project, stdout=stdout, stderr=stderr)
            pid = process.pid
            try:
                code = process.wait(timeout=CHILD_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                code = process.wait(timeout=10)
    except Exception:
        launch_error = traceback.format_exc()
    text = stdout_path.read_text(encoding='utf-8', errors='replace') if stdout_path.exists() else ''
    passed = [line[6:] for line in text.splitlines() if line.startswith('PASS: ')]
    records = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and ('result' in value or 'native_process_exit' in value):
            records.append(value)
    results = [value for value in records if 'native_bibtex_import' in value]
    final = results[0] if len(results) == 1 else {}
    complete = (not timed_out and launch_error is None and code == 0
                and len(passed) == EXPECTED_CHECKS and final.get('result') == 'PASS'
                and final.get('checks') == EXPECTED_CHECKS
                and final.get('installed_modules') == expected_modules
                and all(final.get(key) == 'PASS' for key in REQUIRED_PASS)
                and all(final.get(key) == 'NOT_RUN' for key in REQUIRED_NOT_RUN))
    result = dict(classification='DIAGNOSTIC_ONLY', status='PASS' if complete else 'FAIL',
                  command=command, pid=pid, returncode=code,
                  returncode_hex=None if code is None else f'0x{code & 0xffffffff:08X}',
                  timed_out=timed_out, launch_error=launch_error, completed_47_checks=complete,
                  checks_completed=len(passed), checks=passed, result_records=records,
                  stdout=stdout_path.name, stderr=stderr_path.name)
    (output / 'native-result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-project', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'classification': 'DIAGNOSTIC_ONLY', 'status': 'NOT_RUN',
                          'reason': 'Actual Windows is required.'}), flush=True)
        return 77
    if not (sys.flags.isolated and sys.dont_write_bytecode and sys.version_info[:2] == (3, 11)
            and struct.calcsize('P') == 8):
        raise RuntimeError('Use isolated native x64 CPython 3.11 with -I -B.')
    project, output = args.source_project.resolve(strict=True), args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    def revision(expression):
        return subprocess.run(['git', '-C', str(project.parent), 'rev-parse', expression],
                              check=True, capture_output=True, text=True, timeout=15).stdout.strip()

    if revision('HEAD') != SOURCE_COMMIT or revision('HEAD^{tree}') != SOURCE_TREE:
        raise ValueError('The source checkout must be the exact pinned timer correction.')
    script = project / 'scripts' / 'windows_bibliography_interchange_smoke.py'
    probe = script.read_bytes()
    if git_blob(probe) != PROBE_BLOB or hashlib.sha256(probe).hexdigest() != PROBE_SHA256:
        raise ValueError('The native 47-check probe differs from its unchanged source identity.')
    (output / 'tested-native-probe.py').write_bytes(probe)

    import carr_library
    installed = Path(carr_library.__file__).resolve()
    purelib = Path(sysconfig.get_path('purelib')).resolve()
    if (not installed.is_relative_to(purelib)
            or installed.read_bytes() != (project / 'src/carr_library/__init__.py').read_bytes()):
        raise ValueError('The application must come from the installed pinned source package.')
    expected_modules, installed_origins = {}, {}
    for name in MODULES:
        relative = Path(*name.split('.')[1:]).with_suffix('.py')
        source = project / 'src/carr_library' / relative
        actual = (installed.parent / relative).resolve(strict=True)
        if not actual.is_relative_to(purelib) or actual.read_bytes() != source.read_bytes():
            raise ValueError('An installed native probe module differs from the pinned source: ' + name)
        expected_modules[name] = hashlib.sha256(actual.read_bytes()).hexdigest()
        installed_origins[name] = str(actual)
    provenance = dict(classification='DIAGNOSTIC_ONLY', source_commit=SOURCE_COMMIT,
        source_tree=SOURCE_TREE, probe_blob=git_blob(probe),
        probe_sha256=hashlib.sha256(probe).hexdigest(), probe_unchanged=True,
        interpreter=sys.executable, python=sys.version, wx=version('wxPython'),
        installed_package=str(installed), installed_origins=installed_origins,
        expected_installed_modules=expected_modules, expected_checks=EXPECTED_CHECKS,
        child_timeout=CHILD_TIMEOUT, prior_ab_diagnostic_commit='606107f349707d329334098669828021e0b90bf5')
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')
    result = run_probe(script, sys.executable, project, output, expected_modules)
    summary = dict(**provenance, native_result=result,
                   production_release_acceptance='NOT_CLAIMED',
                   full_bundle='NOT_RUN', nvda='NOT_RUN', native_file_pickers='NOT_RUN')
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary), flush=True)
    return 0 if result['completed_47_checks'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
