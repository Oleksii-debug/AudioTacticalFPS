"""Exact source/installed/native qualification of the durable audio queue.

Uses the existing offline pytest policy and native FFmpeg pin. Only synthetic
test files and textual receipts belong in the uploaded artifact.
"""
from argparse import ArgumentParser
from collections import Counter
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import tomllib
import traceback
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / 'scripts' / 'audio_batch_oracles.json'
ORACLE = json.loads(ORACLE_PATH.read_text(encoding='utf-8'))
SUPPORT = (
    'tests/__init__.py', 'tests/security/__init__.py', 'tests/storage/__init__.py',
    'tests/security/conftest.py', 'tests/security/test_domain_admission.py',
)
PHASE_SECONDS = 1200


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def file_hash(path):
    digest = sha256()
    with Path(path).open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args):
    return subprocess.check_output(
        ['git', '--no-replace-objects', *args], cwd=ROOT,
        env=dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0'),
        timeout=30, text=True).strip()


def source_identity(results):
    source = read(results / 'source-provenance.json')
    require(source['source_commit'] == git('rev-parse', 'HEAD') == os.environ['GITHUB_SHA'],
            'The event checkout source commit changed.')
    require(source['source_tree'] == git('rev-parse', 'HEAD^{tree}'), 'The source tree changed.')
    require(git('diff', '--name-only', 'HEAD', '--', ':/') == '', 'Tracked candidate source changed.')
    wheel = read(results / 'source-wheel.json')
    require(wheel['source_commit'] == source['source_commit'], 'The wheel belongs to another source.')
    require(file_hash(os.environ['CARR_BATCH_WHEEL']) == wheel['wheel_sha256'], 'The wheel bytes changed.')
    return {key: source[key] for key in ('source_commit', 'source_tree')}


def installed_identity(results):
    before = read(results / 'installed-source.json')
    require(sys.flags.isolated and before['status'] == 'PASS' and before['isolated'] is True,
            'Use the isolated source-bound installed Python.')
    origins, files = {}, {}
    prefix = Path(sys.prefix).resolve(strict=True)
    for name in ('carr_library', 'bootstrap'):
        origin = Path(importlib.import_module(name).__file__).resolve(strict=True)
        require(origin.is_relative_to(prefix)
                and 'site-packages' in {part.casefold() for part in origin.parts},
                'An application package escaped the installed environment.')
        origins[name] = str(origin)
        for path in origin.parent.rglob('*.py'):
            actual = path.resolve(strict=True)
            require(actual.is_relative_to(origin.parent), 'Installed source escaped its package.')
            files[name + '/' + path.relative_to(origin.parent).as_posix()] = file_hash(actual)
    require(origins == before['module_origins'] and files == before['installed_python'],
            'The complete installed Python inventory changed.')
    require(before['wheel_sha256'] == read(results / 'source-wheel.json')['wheel_sha256'],
            'Installed inventory belongs to another wheel.')
    return {'module_origins': origins, 'installed_python': files,
            'wheel_sha256': before['wheel_sha256']}


def checkout_inventory():
    """Retain every tracked repository file, independently of wheel packaging."""
    repository = ROOT.parent.resolve(strict=True)
    result = {}
    for row in git('ls-tree', '-rz', '--full-tree', 'HEAD').split('\0'):
        if not row:
            continue
        header, name = row.split('\t', 1)
        mode, kind, identity = header.split()
        require(kind == 'blob' and mode in ('100644', '100755'),
                'The qualification checkout contains a nonregular tracked entry: ' + name)
        path = repository / name
        require(path.resolve(strict=True).is_relative_to(repository)
                and stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink(),
                'A tracked source path escaped the checkout: ' + name)
        result[name] = {'git_blob': identity, 'mode': mode, 'sha256': file_hash(path)}
    require(result, 'The complete tracked source inventory is empty.')
    return result


def source_python_inventory(checkout, installed):
    """Use the existing explicit-package recipe, not a recursive wheel guess."""
    spec = importlib.util.spec_from_file_location(
        '_batch_source_wheel', ROOT / 'packaging' / 'source_wheel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entries = {name: (row['mode'], 'blob', row['git_blob']) for name, row in checkout.items()}
    project = tomllib.loads(module._blob(ROOT.parent, entries, 'carr-library/pyproject.toml').decode('utf-8'))
    admitted, _ = module._inventory(project, entries)
    packaged = {name: checkout[path]['sha256'] for name, path in admitted.items()
                if name.endswith('.py') and name.split('/')[0] in ('carr_library', 'bootstrap')}
    require(packaged == installed,
            'Declared package Python differs from the exact source-bound installed wheel.')
    complete = {}
    for name, row in checkout.items():
        for prefix, package in (('carr-library/src/carr_library/', 'carr_library/'),
                                ('carr-library/bootstrap/', 'bootstrap/')):
            if name.startswith(prefix) and name.endswith('.py'):
                complete[package + name[len(prefix):]] = row['sha256']
    require(set(packaged) <= set(complete), 'A declared Python file is absent from the complete checkout.')
    return {'installed': installed, 'source': complete,
            'source_only': {name: value for name, value in complete.items() if name not in packaged}}


def failure_diagnostics(log, error):
    """Expose bounded actual child output without requiring artifact download."""
    print('FAILURE TRACEBACK (last 12000 characters):', flush=True)
    print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:], flush=True)
    if log.is_file():
        with log.open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 24000))
            tail = stream.read(24000).decode('utf-8-sig', errors='replace')
        print('ACTUAL PYTEST OUTPUT (last 24000 bytes):', flush=True)
        print(tail, flush=True)


def runtime_identity():
    from carr_library.audio.media_conversion import runtime_identity as inspect
    from carr_library.audio.runtime import FFMPEG_WINDOWS_SHA256, FFMPEG_VERSION
    path = Path(os.environ['CARR_FFMPEG']).resolve(strict=True)
    identity = inspect(path)
    require(sys.platform == 'win32' and identity.sha256 == FFMPEG_WINDOWS_SHA256,
            'The real Windows FFmpeg executable differs from its reviewed byte pin.')
    require(identity.version.startswith('ffmpeg version ' + FFMPEG_VERSION),
            'The actual FFmpeg version differs from its reviewed pin.')
    return {'sha256': identity.sha256, 'version': identity.version,
            'version_sha256': identity.version_sha256}


def population(report):
    root = ET.parse(report).getroot()
    cases = list(root.iter('testcase'))
    require(cases and not list(root.iter('failure')) and not list(root.iter('error'))
            and not list(root.iter('skipped')), 'A test failed, errored, skipped, or produced no cases.')
    actual = Counter()
    for case in cases:
        module = case.attrib['classname']
        name = case.attrib['name'].partition('[')[0]
        actual[(module, name)] += 1
    expected = Counter({(path[:-3].replace('/', '.'), name): count
                        for path, functions in ORACLE['tests'].items()
                        for name, count in functions.items()})
    require(actual == expected, 'JUnit function/parameter Counters differ from the exact declared oracle.')
    require(len(cases) == ORACLE['total_cases'], 'The actual test population differs from its declared total.')
    return {module: {name: count for (candidate, name), count in actual.items() if candidate == module}
            for module in sorted({module for module, _ in actual})}


CHILD = r"""
import hashlib, importlib, json, pathlib, sys
mode, project, staging, expected_path, identity_path, runner = sys.argv[1:7]
sys.argv = ['audio-batch-' + mode] + sys.argv[7:]
project, staging = pathlib.Path(project), pathlib.Path(staging)
expected = json.loads(pathlib.Path(expected_path).read_text(encoding='utf-8'))
# The installed lane adds only a temporary tests-only directory. It contains
# no application packages and never adds the checkout or its src directory.
sys.path.insert(0, str(staging))
if mode == 'source':
    sys.path[1:1] = [str(project / 'src'), str(project)]
prefix = pathlib.Path(sys.prefix).resolve(strict=True)
def inventory():
    origins, files = {}, {}
    for name in ('carr_library', 'bootstrap'):
        origin = pathlib.Path(importlib.import_module(name).__file__).resolve(strict=True)
        if mode == 'source':
            wanted = (project / ('src/carr_library' if name == 'carr_library' else 'bootstrap')).resolve()
            if origin.parent != wanted:
                raise ValueError('The source test lane did not load the exact checkout package.')
        elif not origin.is_relative_to(prefix) or 'site-packages' not in {p.casefold() for p in origin.parts}:
            raise ValueError('The installed test lane imported a checkout application package.')
        origins[name] = str(origin)
        for path in origin.parent.rglob('*.py'):
            actual = path.resolve(strict=True)
            if not actual.is_relative_to(origin.parent):
                raise ValueError('An application source escaped its declared package.')
            files[name + '/' + path.relative_to(origin.parent).as_posix()] = hashlib.sha256(actual.read_bytes()).hexdigest()
    wanted_files = expected['source' if mode == 'source' else 'installed']
    if files != wanted_files:
        missing = sorted(wanted_files.keys() - files.keys())
        extra = sorted(files.keys() - wanted_files.keys())
        changed = sorted(name for name in files.keys() & wanted_files.keys() if files[name] != wanted_files[name])
        raise ValueError('Application inventory differs: ' + json.dumps(
            {'missing': missing, 'extra': extra, 'changed': changed})[:12000])
    # The complete checkout can contain explicitly unshipped diagnostics. Those
    # bytes remain attested above; they are never admitted as application code.
    for name, module in tuple(sys.modules.items()):
        top = name.partition('.')[0]
        if top not in origins or module is None or not getattr(module, '__file__', None):
            continue
        actual = pathlib.Path(module.__file__).resolve(strict=True)
        package = pathlib.Path(origins[top]).parent
        if not actual.is_relative_to(package):
            raise ValueError('An executed application module escaped its exact package: ' + name)
        key = top + '/' + actual.relative_to(package).as_posix()
        if key not in expected['installed'] or hashlib.sha256(actual.read_bytes()).hexdigest() != expected['installed'][key]:
            raise ValueError('An executed application module is not declared in the source-bound wheel: ' + name)
    packaged = {name: files[name] for name in expected['installed']}
    return {'mode': mode, 'isolated': bool(sys.flags.isolated), 'module_origins': origins,
            'files': packaged, 'complete_python': files}
before = inventory()
receipt = {'status': 'FAIL', 'before': before}
try:
    import runpy
    policy = runpy.run_path(runner)['PYTEST_CHILD']
    exec(compile(policy, 'existing-audio-batch-offline-test-policy', 'exec'))
finally:
    after = inventory()
    if after != before:
        raise ValueError('Application bytes or origins changed during the actual tests.')
    receipt.update(status='PASS', after=after)
    pathlib.Path(identity_path).write_text(json.dumps(receipt, sort_keys=True) + '\n', encoding='utf-8')
"""


def tests(mode, results, work):
    target = results / (mode + '-gate.json')
    report = {'schema': 1, 'status': 'FAIL', 'mode': mode, 'declared_cases': ORACLE['total_cases'],
              'timeout_seconds': PHASE_SECONDS, 'oracle_sha256': file_hash(ORACLE_PATH)}
    started = monotonic()
    log_path = results / (mode + '-pytest.txt')
    try:
        report.update(source_identity(results))
        installed = installed_identity(results)
        report['wheel_sha256'] = installed['wheel_sha256']
        checkout = checkout_inventory()
        checkout_path = results / (mode + '-checkout.json')
        write(checkout_path, {'source_commit': report['source_commit'],
                              'source_tree': report['source_tree'], 'files': checkout})
        report['checkout_manifest_sha256'] = file_hash(checkout_path)
        report['tracked_source_files'] = len(checkout)
        expected_python = source_python_inventory(checkout, installed['installed_python'])
        report['source_only_python'] = expected_python['source_only']
        runtime = runtime_identity()
        report['actual_ffmpeg'] = runtime
        names = tuple(ORACLE['tests']) + SUPPORT
        report['test_source_sha256'] = {name: file_hash(ROOT / name) for name in names}
        report['runner_sha256'] = {name: file_hash(ROOT / 'scripts' / name)
                                  for name in ('audio_batch_acceptance.py', 'ci_check.py', 'ci_watchdog.py')}
        with TemporaryDirectory(prefix='batch-' + mode + '-', dir=work) as folder:
            staging = Path(folder)
            for name in names:
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, destination)
                require(file_hash(destination) == report['test_source_sha256'][name],
                        'An exact test fixture changed while staging.')
            manifest = staging / 'expected-source.json'
            write(manifest, expected_python)
            interpreter_report = results / (mode + '-interpreter.json')
            xml = results / (mode + '-junit.xml')
            arguments = [
                sys.executable, '-I', '-B', '-X', 'utf8', '-c', CHILD, mode, str(ROOT), str(staging),
                str(manifest), str(interpreter_report), str(ROOT / 'scripts' / 'ci_check.py'),
                'offline', '-c', str(ROOT / 'pyproject.toml'), '--rootdir', str(staging), '-o', 'addopts=',
                '-o', 'xfail_strict=true', '-o', 'pythonpath=', '--import-mode=importlib',
                '--durations=15', '--junitxml', str(xml), '--basetemp', str(staging / 'pytest'),
                *[str(staging / name) for name in ORACLE['tests']],
            ]
            environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
            for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH', 'CARR_CI_ALLOW_LOOPBACK'):
                environment.pop(key, None)
            with log_path.open('wb') as log:
                process = subprocess.run(arguments, cwd=ROOT, env=environment,
                                         stdout=log, stderr=subprocess.STDOUT,
                                         timeout=PHASE_SECONDS, check=False)
            report['pytest_exit'] = process.returncode
            require(process.returncode == 0, 'The strict actual pytest process failed.')
            report['case_counter'] = population(xml)
            executed = read(interpreter_report)
            require(executed['status'] == 'PASS' and executed['before'] == executed['after']
                    and executed['before']['mode'] == mode
                    and executed['before']['isolated'] is True
                    and executed['before']['files'] == installed['installed_python']
                    and executed['before']['complete_python'] == expected_python[
                        'source' if mode == 'source' else 'installed'],
                    'The actual test interpreter did not preserve the complete source identity.')
            report['interpreter_sha256'] = file_hash(interpreter_report)
        require(source_identity(results) == {key: report[key] for key in ('source_commit', 'source_tree')},
                'Candidate source changed during tests.')
        require(checkout_inventory() == checkout, 'The complete tracked source inventory changed during tests.')
        require(installed_identity(results) == installed, 'Installed Python changed during tests.')
        require(runtime_identity() == runtime, 'The selected real FFmpeg runtime changed during tests.')
        report.update(status='PASS', actual_cases=ORACLE['total_cases'])
    except Exception as error:
        failure_diagnostics(log_path, error)
        report['reason'] = type(error).__name__ + ': ' + str(error)
        if isinstance(error, subprocess.TimeoutExpired):
            report['reason'] = 'TIMEOUT: actual pytest phase exceeded ' + str(PHASE_SECONDS) + ' seconds.'
    finally:
        report['elapsed_seconds'] = monotonic() - started
        write(target, report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


def finalize(results):
    gate = {'schema': 1, 'status': 'FAIL', 'source_cases': ORACLE['total_cases'],
            'installed_cases': ORACLE['total_cases'], 'native_checks': ORACLE['native']['total_checks'],
            'oracle_sha256': file_hash(ORACLE_PATH), 'nvda': 'NOT_RUN',
            'physical_audio': 'NOT_RUN', 'speech_quality': 'NOT_CLAIMED'}
    try:
        source = source_identity(results)
        installed = installed_identity(results)
        runtime = runtime_identity()
        gate.update(source)
        for mode in ('source', 'installed'):
            phase = read(results / (mode + '-gate.json'))
            require(os.environ.get(mode.upper() + '_OUTCOME') == 'success'
                    and phase['status'] == 'PASS' and phase['pytest_exit'] == 0
                    and phase['actual_cases'] == ORACLE['total_cases'],
                    'A required test phase failed, timed out, skipped, or did not run.')
            require(all(phase[key] == source[key] for key in source)
                    and phase['wheel_sha256'] == installed['wheel_sha256']
                    and phase['oracle_sha256'] == gate['oracle_sha256']
                    and phase['actual_ffmpeg'] == runtime,
                    'A test phase belongs to another source, wheel, oracle, or runtime.')
            require(population(results / (mode + '-junit.xml')) == phase['case_counter'],
                    'The retained actual JUnit population changed.')
            require(file_hash(results / (mode + '-interpreter.json')) == phase['interpreter_sha256'],
                    'The test interpreter identity receipt changed.')
            checkout_path = results / (mode + '-checkout.json')
            require(file_hash(checkout_path) == phase['checkout_manifest_sha256'],
                    'The retained complete source inventory receipt changed.')
            checkout = read(checkout_path)
            require(checkout['source_commit'] == source['source_commit']
                    and checkout['source_tree'] == source['source_tree']
                    and checkout['files'] == checkout_inventory()
                    and len(checkout['files']) == phase['tracked_source_files'],
                    'The complete tracked source inventory differs from the executed checkout.')
            expected_python = source_python_inventory(checkout['files'], installed['installed_python'])
            require(expected_python['source_only'] == phase['source_only_python'],
                    'Unshipped source diagnostics changed after execution.')
            for name, expected in phase['test_source_sha256'].items():
                require(file_hash(ROOT / name) == expected, 'A test or fixture changed after execution.')
            for name, expected in phase['runner_sha256'].items():
                require(file_hash(ROOT / 'scripts' / name) == expected, 'A runner changed after execution.')
        require(os.environ.get('NATIVE_OUTCOME') == 'success', 'The required native probe failed or did not run.')
        lines = (results / 'native.txt').read_text(encoding='utf-8-sig').splitlines()
        reports = [json.loads(line) for line in lines if line.startswith('{')]
        require(len(reports) == 1, 'The native probe did not produce one unambiguous receipt.')
        native = reports[0]
        expected = Counter(ORACLE['native']['expected_label_counter'])
        require(native['status'] == 'PASS' and native['installed_mode'] is True
                and native['isolated'] is True and native['native_controls'] == 'PASS'
                and Counter(native['checks']) == expected
                and Counter(line[6:] for line in lines if line.startswith('PASS: ')) == expected,
                'The actual native label Counters differ from the exact oracle.')
        require(set(native['module_origins']) == set(ORACLE['native']['probe_modules']),
                'The actual native implementation origin inventory is incomplete.')
        prefix = Path(sys.prefix).resolve(strict=True)
        for name, value in native['module_origins'].items():
            path = Path(value['path']).resolve(strict=True)
            key = name.replace('.', '/') + '.py'
            require(path.is_relative_to(prefix)
                    and 'site-packages' in {part.casefold() for part in path.parts}
                    and file_hash(path) == value['sha256'] == installed['installed_python'].get(key),
                    'The native probe did not execute exact installed candidate bytes.')
        require(native['actual_ffmpeg'] == runtime, 'The native probe used a different actual FFmpeg runtime.')
        output = native['verified_output']
        require(output['sample_rate'] == 8000 and output['channels'] == 1
                and output['frames'] == 2000 and output['bytes'] > 0,
                'The native synthetic conversion receipt is incomplete.')
        gate.update(status='PASS', wheel_sha256=installed['wheel_sha256'], actual_ffmpeg=runtime,
                    native_receipt_sha256=file_hash(results / 'native.txt'))
        write(results / 'native.json', native)
    except Exception as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:], flush=True)
        gate['reason'] = type(error).__name__ + ': ' + str(error)
    write(results / 'audio-batch-gate.json', gate)
    print(json.dumps(gate, ensure_ascii=False), flush=True)
    return 0 if gate['status'] == 'PASS' else 1


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('source', 'installed', 'finalize'))
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    results = args.results.resolve(strict=True)
    if args.phase == 'finalize':
        return finalize(results)
    if args.work is None:
        parser.error('Actual test staging requires this run\'s explicit temporary work directory.')
    return tests(args.phase, results, args.work.resolve(strict=True))


if __name__ == '__main__':
    raise SystemExit(main())
