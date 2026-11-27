"""Run exact retained-capacity contracts against source and a verified installed wheel.

CI-only orchestration. Corpora, wheels and installed runtimes stay in RUNNER_TEMP;
only bounded text/JSON/XML evidence is retained. No capacity verdict is inferred.
"""
import argparse
from hashlib import sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    'tests/packaging/test_library_scale_benchmark.py': 24,
    'tests/packaging/test_library_capacity_measurement.py': 24,
    'tests/storage/test_backup_capacity.py': 28,
    'tests/storage/test_backup_capacity_cli.py': 3,
    'tests/ui/test_backup_capacity.py': 17,
    'tests/storage/test_library_backup.py': 9,
    'tests/storage/test_crash_backup.py': 5,
    'tests/storage/test_complete_pending_backup.py': 4,
    'tests/storage/test_backup_pending_common4.py': 27,
    'tests/storage/test_backup_schema_identity.py': 36,
}
PINS = {
    'scripts/benchmark_library_scale.py': 'ec198e50daffd3d3687048e15882c3744c3c54c4',
    'scripts/benchmark_semantic_cache.py': '898b05e37b5a50c018567e35c44ef71f1393535d',
    'tests/packaging/test_library_scale_benchmark.py': 'd4bd24dafe5dbc4c0bcd856ab0ff416f9f1f5411',
    'tests/packaging/test_library_capacity_measurement.py': '5f2ab090a0baaad61d326c128dc6da453b1c4fc7',
    'tests/storage/test_backup_capacity.py': '895aec39b689aea7101da23db0801413dac324ef',
    'tests/storage/test_backup_capacity_cli.py': '741a005ca9f351579b9d51b464a88dda9afb52b7',
    'tests/ui/test_backup_capacity.py': '26f03b8f46f6425460bb248f94bd3f1347787dc6',
    'tests/storage/test_library_backup.py': 'd14b278b356bb47fe66e822125a257807f2cd895',
    'tests/storage/test_crash_backup.py': '5e84ede7eee0b7406c687b5a6020b0fcd535ec2c',
    'tests/storage/test_complete_pending_backup.py': '36fe4b61a3a61351fecc1a88016c1e1db32275d5',
    'tests/storage/test_backup_pending_common4.py': 'bbed36da78265d635317880b264fd90b59ed4235',
    'tests/storage/test_backup_schema_identity.py': 'ebed1d823de241c639877dd747de9aa4ce2085ed',
    'scripts/ci_check.py': '8a23ef36c2df93928b0aff3edd8b59a53c54684c',
    'packaging/source_wheel.py': '478de248451d59a44b35784eb808fb2974f0af67',
    'src/carr_library/backup_capacity.py': '1fd11ab8d3a2be5434e32bdcba65447c55730ad7',
    'src/carr_library/application.py': '4544453f62a96993a2f05ed1522a563649289715',
    'src/carr_library/__main__.py': '99b3c98d5f60cfa6ba1074d74c32acc14250fd16',
    'src/carr_library/library_backup.py': 'd36d2ab93043bff46048bb6146769a1a19f29db8',
    'src/carr_library/pending_backup.py': 'b2c3fc3a52e9d2fef5c019196ef239381c519fc7',
    'src/carr_library/backup_sidecars.py': '5c2ec87913e8ad252dce9aa8cff7ccc22dca3525',
    'src/carr_library/storage/revisions.py': '417a9920d4c25eacd36cc81f7441ea321c18d632',
    'src/carr_library/ui/backup_capacity.py': '66555889648ec8b76ac11e7ffe5bd54ece6c29e9',
}
COMPANIONS = ('scripts/benchmark_library_scale.py', 'scripts/benchmark_semantic_cache.py')


def require(value, message):
    if not value:
        raise ValueError(message)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def sources():
    result = {}
    for relative, expected in PINS.items():
        path = ROOT / relative
        require(path.is_file() and not path.is_symlink(), 'Missing ordinary source: ' + relative)
        raw = path.read_bytes()
        actual = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        require(actual == expected, 'Changed pinned source: ' + relative)
        result[relative] = dict(git_blob=actual, sha256=sha256(raw).hexdigest(), bytes=len(raw))
    return result


def git(*args):
    return subprocess.run(['git', '--no-replace-objects', '-C', str(ROOT.parent), *args],
        check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def child(command, output, *, cwd, ci, timeout=600):
    environment = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTEST_PLUGINS', 'PYTEST_ADDOPTS'):
        environment.pop(key, None)
    environment.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', HF_HUB_OFFLINE='1',
        TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
        ORT_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')
    isolation = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == 'nt' else {'start_new_session': True})
    with output.open('xb') as stream:
        process = subprocess.Popen(command, cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, **isolation)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            ci._terminate_process_tree(process)
            raise
    require(output.stat().st_size <= 8 * 1024**2, 'Required-suite log exceeds its finite bound.')
    print(output.read_text(encoding='utf-8', errors='replace'), flush=True)
    return code


INSTALLED_CHILD = r"""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

wheel, proof_path, checkout, receipt, contract_path = map(Path, sys.argv[1:6])
arguments = sys.argv[6:]
contract = json.loads(contract_path.read_text(encoding='utf-8'))
proof = json.loads(proof_path.read_text(encoding='utf-8'))
assert sys.flags.isolated and sys.flags.dont_write_bytecode and __debug__
assert sha256(wheel.read_bytes()).hexdigest() == proof['wheel_sha256']
import carr_library, bootstrap
package = Path(carr_library.__file__).resolve(strict=True).parent
helper = Path(bootstrap.__file__).resolve(strict=True).parent
assert package.parent == helper.parent
assert not package.is_relative_to(checkout) and not helper.is_relative_to(checkout)
assert all(not Path(p).resolve().is_relative_to(checkout) for p in sys.path if p)
payload = {}
with ZipFile(wheel) as archive:
    for name in archive.namelist():
        if name.startswith(('carr_library/', 'bootstrap/')) and not name.endswith('/'):
            payload[name] = sha256(archive.read(name)).hexdigest()
assert payload
def verify():
    rows = {}
    for relative, digest in payload.items():
        path = package.parent / relative
        assert path.is_file() and not path.is_symlink()
        assert sha256(path.read_bytes()).hexdigest() == digest, relative
    for name in contract['modules']:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        assert path.is_relative_to(package if name.startswith('carr_library') else helper)
        rows[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return rows
before = verify()
sys.argv = ['pytest', *arguments]
try:
    exec(compile(contract['pytest_child'], '<existing-ci-check-policy>', 'exec'),
         {'__name__': '__main__'})
except SystemExit as result:
    code = result.code
else:
    raise AssertionError('Existing strict pytest child did not exit explicitly.')
after = verify()
assert after == before
receipt.write_text(json.dumps({'status': 'PASS', 'origins': before,
    'wheel_sha256': proof['wheel_sha256'], 'wheel_payload_files_verified': len(payload),
    'isolated': True, 'source_injection': False, 'payload_unchanged': True},
    indent=2, sort_keys=True) + '\n', encoding='utf-8')
raise SystemExit(code)
"""


def main():
    # Isolated Python ignores PYTHONUTF8; display the retained UTF-8 child log.
    # This changes only this orchestrator's stream, not child/test encodings.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    args = parser.parse_args()
    require(__debug__ and re.fullmatch('[0-9a-f]{40}', args.expected_head), 'Exact commit and assertions required.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Checkout differs from exact event head.')
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, exist_ok=False)
    before = sources()
    own_relative = Path(__file__).resolve().relative_to(ROOT.parent).as_posix()
    require(git('show', args.expected_head + ':' + own_relative) == Path(__file__).read_text(encoding='utf-8').strip(),
            'Orchestrator differs from its committed source.')
    identity = dict(source_commit=args.expected_head, source_tree=git('rev-parse', 'HEAD^{tree}'),
        inputs=before, python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('capacity_existing_ci_check', ROOT / 'scripts/ci_check.py')
    benchmark = load('capacity_existing_harness', ROOT / COMPANIONS[0])
    proof_module = load('capacity_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    # Five two-suite waves retain 600 s plus the existing 30 s tree cleanup
    # and 0.2 s report retries; 3200 s also covers aggregate setup/reporting.
    code = child([sys.executable, '-B', str(ROOT / 'scripts/ci_check.py'),
                  '--jobs', '2', *SUITES], source_log, cwd=ROOT, ci=ci, timeout=3200)
    lines = source_log.read_text(encoding='utf-8').splitlines()
    expected = [f'PASS: {suite}: junit_passed={count}, failed=0, skipped=0, pytest_exit=0'
                for suite, count in SUITES.items()]
    require(code == 0 and all(lines.count(line) == 1 for line in expected)
            and lines[-1] == f'Required suites complete: {len(SUITES)}; passed={len(SUITES)}; failed=0',
            'Every source whole suite must pass without skips or expected failures.')
    write(evidence / 'source-summary.json', dict(status='PASS', suites=SUITES,
          passed=sum(SUITES.values()), failed=0, skipped=0, expected_failures=False,
          log_sha256=sha256(source_log.read_bytes()).hexdigest()))
    work_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='carr-capacity-contracts-', dir=work_parent) as tmp:
        work = Path(tmp)
        wheels = work / 'wheel'
        wheels.mkdir()
        build_code = child([sys.executable, '-B', '-m', 'pip', 'wheel', '--no-index',
            '--no-deps', '--no-build-isolation', '--wheel-dir', str(wheels), str(ROOT)],
            evidence / 'wheel-build.txt', cwd=work, ci=ci, timeout=300)
        require(build_code == 0, 'Current source wheel build failed.')
        found = list(wheels.glob('*.whl'))
        require(len(found) == 1, 'Exactly one current application wheel is required.')
        wheel = found[0]
        proof = proof_module.verify_application_wheel(wheel, ROOT.parent, args.expected_head)
        write(evidence / 'source-wheel-proof.json', proof)
        install_code = child([sys.executable, '-I', '-B', '-m', 'pip', 'install', '--no-index',
            '--no-deps', '--force-reinstall', str(wheel)],
            evidence / 'wheel-install.txt', cwd=work, ci=ci, timeout=180)
        require(install_code == 0, 'Verified current wheel was not installed.')
        require(child([sys.executable, '-I', '-B', '-m', 'pip', 'check'],
            evidence / 'installed-pip-check.txt', cwd=work, ci=ci, timeout=60) == 0,
            'Installed dependency closure is inconsistent.')
        staged = work / 'isolated-tests'
        staged.mkdir()
        for relative in (*SUITES, *COMPANIONS):
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
            require(sha256(target.read_bytes()).hexdigest() == before[relative]['sha256'],
                    'Companion changed during staging.')
        ini = staged / 'pytest.ini'
        ini.write_text('[pytest]\naddopts =\n', encoding='utf-8')
        wrapper = work / 'installed_child.py'
        wrapper.write_text(INSTALLED_CHILD, encoding='utf-8')
        contract = work / 'child-contract.json'
        modules = list(benchmark.MODULES + benchmark.CAPACITY_MODULES)
        require(len(modules) == len(set(modules)) == 20,
                'The explicit backup-profile harness requires all 20 installed origins.')
        write(contract, dict(pytest_child=ci.PYTEST_CHILD, modules=modules))
        installed = []
        for number, (suite, expected_count) in enumerate(SUITES.items(), 1):
            xml = evidence / f'installed-{number}.xml'
            receipt = evidence / f'installed-{number}-origins.json'
            log = evidence / f'installed-{number}.txt'
            result = child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
                str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(receipt), str(contract),
                '-v', '-c', str(ini), '--rootdir', str(staged), '--confcutdir', str(staged),
                '--import-mode=importlib', '-o', 'pythonpath=', '-o', 'addopts=',
                '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=120',
                '-o', 'faulthandler_exit_on_timeout=false', '--junitxml', str(xml),
                '--basetemp', str(work / f'pytest-{number}'), '--', str(staged / suite)],
                log, cwd=staged, ci=ci)
            cases = list(ET.parse(xml).iter('testcase'))
            failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
            skipped = sum(c.find('skipped') is not None for c in cases)
            passed = len(cases) - failed - skipped
            require(result == 0 and passed == expected_count and failed == skipped == 0
                    and receipt.is_file(), 'Installed whole contract failed: ' + suite)
            installed.append(dict(suite=suite, passed=passed, failed=failed, skipped=skipped,
                                  exit=result, origins=json.loads(receipt.read_text(encoding='utf-8'))))
        for relative in (*SUITES, *COMPANIONS):
            require(sha256((staged / relative).read_bytes()).hexdigest() == before[relative]['sha256'],
                    'An installed test or companion changed.')
    require(sources() == before, 'Pinned sources changed during actual contracts.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], all_children_reaped=True,
        expected_failures=False, qualification='Exact harness source and installed contracts only; no large corpus or release claim.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
