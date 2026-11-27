"""Compare one native-window lifetime variable; never qualify a release."""
import argparse
import ast
import copy
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


SOURCE_COMMIT = 'de4dd799c74d28afd0b13891484c44f3f4f245ac'
SOURCE_TREE = 'd6bd3e32124eb4f7cc60c08992b0b0b442badd61'
BASELINE_BLOB = 'b5844fbe29cd214d0b7554e7204fcc974ab4aa7f'
BASELINE_SHA256 = 'a1155c0d512c3c55d8557e18ab2cb4c350165f995081163be82a7f144d01d1f8'
EXPECTED_CHECKS = 44
CHILD_TIMEOUT = 180


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


def deferred_variant(data):
    """Relocate the identical five statements and prove the remaining AST equal."""
    if git_blob(data) != BASELINE_BLOB or hashlib.sha256(data).hexdigest() != BASELINE_SHA256:
        raise ValueError('The original native probe differs from the pinned baseline.')
    source = data.decode('utf-8')
    first = '                startup_executor, startup_cancel = startup.executor, startup.cancel_event\n'
    after = "                workspace = library.create_workspace('Synthetic native interchange')\n"
    destination = "                check('all interchange operations stayed offline', not network_attempts)\n"
    if any(source.count(marker) != 1 for marker in (first, after, destination)):
        raise ValueError('The expected single relocation boundary changed.')
    start, end = source.index(first), source.index(after)
    block = source[start:end]
    if not block.endswith('                startup = None\n'):
        raise ValueError('Unexpected startup-close block ending.')
    variant = source[:start] + source[end:]
    variant = variant.replace(destination, destination + block, 1)
    original_tree, variant_tree = ast.parse(source), ast.parse(variant)
    original = next(node for node in ast.walk(original_tree)
                    if isinstance(node, ast.FunctionDef) and node.name == 'scenarios')
    normalized = copy.deepcopy(variant_tree)
    changed = next(node for node in ast.walk(normalized)
                   if isinstance(node, ast.FunctionDef) and node.name == 'scenarios')
    dump = lambda node: ast.dump(node, include_attributes=False)
    first_line, end_line = source[:start].count('\n') + 1, source[:end].count('\n') + 1
    indices = [i for i, node in enumerate(original.body) if first_line <= node.lineno < end_line]
    if len(indices) != 5 or indices != list(range(indices[0], indices[0] + 5)):
        raise ValueError('Expected exactly five unchanged close/lifecycle statements.')
    statements = original.body[indices[0]:indices[0] + 5]
    matches = [i for i in range(len(changed.body) - 4)
               if [dump(node) for node in changed.body[i:i + 5]] == [dump(node) for node in statements]]
    if len(matches) != 1 or matches[0] + 5 != len(changed.body) - 1:
        raise ValueError('The close block must move immediately before the final result.')
    moved = changed.body[matches[0]:matches[0] + 5]
    del changed.body[matches[0]:matches[0] + 5]
    changed.body[indices[0]:indices[0]] = moved
    if dump(original_tree) != dump(normalized):
        raise ValueError('The variant changed more than startup-close ordering.')
    compile(variant, '<deferred-startup-close>', 'exec')
    return variant.encode('utf-8')


def run_arm(name, script, interpreter, project, output):
    stdout_path, stderr_path = output / (name + '.stdout.txt'), output / (name + '.stderr.txt')
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
    complete = (not timed_out and launch_error is None and code == 0
                and len(passed) == EXPECTED_CHECKS and len(results) == 1
                and results[0].get('result') == 'PASS' and results[0].get('checks') == EXPECTED_CHECKS)
    result = dict(arm=name, classification='DIAGNOSTIC_ONLY', command=command, pid=pid,
                  returncode=code, returncode_hex=None if code is None else f'0x{code & 0xffffffff:08X}',
                  timed_out=timed_out, launch_error=launch_error, completed_44_checks=complete,
                  checks_completed=len(passed), checks=passed, result_records=records,
                  stdout=stdout_path.name, stderr=stderr_path.name)
    (output / (name + '.json')).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
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
        raise ValueError('The installed source checkout must be exact de4, not the diagnostic branch.')
    original = project / 'scripts' / 'windows_bibliography_interchange_smoke.py'
    baseline = original.read_bytes()
    variant = deferred_variant(baseline)
    baseline_copy, variant_path = output / 'A-baseline.py', output / 'B-deferred-close.py'
    baseline_copy.write_bytes(baseline)
    variant_path.write_bytes(variant)
    import carr_library
    installed = Path(carr_library.__file__).resolve()
    purelib = Path(sysconfig.get_path('purelib')).resolve()
    if (not installed.is_relative_to(purelib)
            or installed.read_bytes() != (project / 'src/carr_library/__init__.py').read_bytes()):
        raise ValueError('The application must come from the installed pinned source package.')
    provenance = dict(classification='DIAGNOSTIC_ONLY', source_commit=SOURCE_COMMIT,
        source_tree=SOURCE_TREE, baseline_blob=git_blob(baseline),
        baseline_sha256=hashlib.sha256(baseline).hexdigest(), variant_blob=git_blob(variant),
        variant_sha256=hashlib.sha256(variant).hexdigest(), normalized_ast_equal=True,
        interpreter=sys.executable, python=sys.version, wx=version('wxPython'),
        installed_package=str(installed), expected_checks=EXPECTED_CHECKS, child_timeout=CHILD_TIMEOUT,
        only_variable='The identical startup-close block runs after all bibliography checks.')
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')
    first = run_arm('A-baseline', original, sys.executable, project, output)
    second = run_arm('B-deferred-close', variant_path, sys.executable, project, output)
    summary = dict(**provenance, arms=[first, second], production_acceptance='NOT_CLAIMED',
        causality='NOT_ESTABLISHED_BY_THIS_DIAGNOSTIC',
        observation=('both_completed_no_reproduced_failure' if first['completed_44_checks']
                     and second['completed_44_checks'] else 'deferred_close_completed_after_baseline_failure'
                     if second['completed_44_checks'] else 'deferred_close_did_not_complete'))
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary), flush=True)
    return 0 if second['completed_44_checks'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
