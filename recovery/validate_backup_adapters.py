"""Qualify a finite public typed-backup composition with unchanged real producers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryFile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'carr-library'
OUT = ROOT / 'ci-backup-results'
SHARED = '8c981c7e947f8d144c8706b5ea022990517c7dd9'
EXPECTED = {
    "carr-library/src/carr_library/backup_producers.py": "ee9e9ca2b1c08e52e5e00cb9a486f57aab27c64e",
    "carr-library/src/carr_library/backup_extensions.py": "c49fa285596d090c7f5a4f8a6b193097672f1329",
    "carr-library/tests/storage/test_backup_restore_cancellation.py": "4d33f72b1039f975e0996fa67a8a212375ade507",
    "carr-library/tests/storage/test_backup_reporting.py": "5555f2c920ae95c6a0f5bd459fce992b5ad7e41b",
    "carr-library/tests/storage/test_studio_input_backup.py": "fe8c8c97bbcd44df58d454d7f909c90f8065c016",
    "carr-library/tests/storage/test_backup_studio_media.py": "83d57bbde83cea9e040e9d4eff41e2195f0a7e54",
    "carr-library/tests/storage/test_backup_primary_producers.py": "d2540f3db2b0dc773666427242bbbc07d2a83264",
    "carr-library/src/carr_library/library_backup.py": "1a6d938e2cdc01e332b9cb33f7ec108698a160c1",
    "carr-library/src/carr_library/state_backup.py": "3295c4ba6ad6fa32aaf71034f7ea1fdf39067c6a",
    "carr-library/src/carr_library/pending_backup.py": "0fa1ec46d1ecf7f039185e69af7b94a5a9b6adc1",
    "carr-library/src/carr_library/backup_sidecars.py": "8073ccc38fb51fb16c5c64d022684cf2ca178540",
    "carr-library/src/carr_library/backup_generated.py": "4c4a2741eb7c2e9a5725604551834e5c2a9fb947",
    "carr-library/src/carr_library/progress_preferences.py": "011930e83ce70c861d6bf59e97fe47e8c09cd551",
    "carr-library/src/carr_library/keyboard_preferences.py": "ea77248e902f2974bcfcda5912a4a179c623786e",
    "carr-library/src/carr_library/ui/command_registry.py": "2fe71b6b3b7c46cef384d6fddb56997b30564e08",
    "carr-library/tests/storage/test_backup_generated_audio.py": "86a42dbdf529c3fc9406a9a233f6d3d12b1c4616",
    "recovery/backup-producers-baseline.py": "cb4c0a14038a46be3639939ccd2463d28a9433de",
    "recovery/backup-extensions-baseline.py": "5bc7bef3f5e7a2ef6ca3dc76b9311809f8163d3b",
    "carr-library/tests/speech/test_pronunciation_review.py": "e0238f5987ead8b9259a8a9752d55cf1a3bff8c2",
    "carr-library/src/carr_library/application.py": "d4ae7deeb3b713215a53172310c47f100a1ccccb",
    "carr-library/src/carr_library/ingest/catalog_asset_copy.py": "2ac0e07afbf587ec1224fb50034cde70e6c29f25",
    "carr-library/src/carr_library/backup_compat/archive.py": "2dd0021531bfdf3f57efaf392010e20d6237df3c",
    "carr-library/src/carr_library/studio/backup_inputs.py": "96101fc7c73cb268dfd77e6ed2430c7169a504ef"
}
OVERLAYS = [
    "carr-library/src/carr_library/backup_producers.py",
    "carr-library/src/carr_library/backup_extensions.py",
    "carr-library/tests/storage/test_backup_restore_cancellation.py",
    "carr-library/tests/storage/test_backup_reporting.py",
    "carr-library/tests/storage/test_studio_input_backup.py",
    "carr-library/tests/storage/test_backup_studio_media.py",
    "carr-library/tests/storage/test_backup_primary_producers.py",
    "carr-library/src/carr_library/library_backup.py",
    "carr-library/src/carr_library/state_backup.py",
    "carr-library/src/carr_library/pending_backup.py",
    "carr-library/src/carr_library/backup_sidecars.py",
    "carr-library/src/carr_library/backup_generated.py",
    "carr-library/src/carr_library/progress_preferences.py",
    "carr-library/src/carr_library/keyboard_preferences.py",
    "carr-library/src/carr_library/ui/command_registry.py",
    "carr-library/tests/storage/test_backup_generated_audio.py"
]
BASELINES = {
    "carr-library/src/carr_library/backup_producers.py": "recovery/backup-producers-baseline.py",
    "carr-library/src/carr_library/backup_extensions.py": "recovery/backup-extensions-baseline.py"
}
SUITES = (
    'tests/storage/test_backup_restore_cancellation.py',
    'tests/storage/test_studio_input_backup.py',
    'tests/storage/test_backup_studio_media.py',
    'tests/storage/test_backup_reporting.py',
    'tests/storage/test_backup_primary_producers.py',
    'tests/storage/test_backup_generated_audio.py',
    'tests/speech/test_pronunciation_review.py',
)
NEGATIVES = {
    'test_dictionary_is_restored_after_original_state_is_deleted': (
        'test_pronunciation_review', 'state/speech/pronunciations.json'),
    'test_real_render_backup_delete_original_state_restore_and_render_again': (
        'test_studio_input_backup', 'state/studio/draft.json'),
}
BASELINE_SELECTORS = (
    'tests/speech/test_pronunciation_review.py::test_dictionary_is_restored_after_original_state_is_deleted',
    'tests/storage/test_studio_input_backup.py::test_real_render_backup_delete_original_state_restore_and_render_again',
)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def git(*arguments):
    return subprocess.check_output(
        ['git', '-C', str(ROOT), *arguments], text=True).strip()


def blob(raw):
    return hashlib.sha1(
        b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def snapshot():
    return {
        path.relative_to(ROOT).as_posix(): checksum(path)
        for path in sorted((APP / 'src').rglob('*'))
        if path.is_file() and '__pycache__' not in path.parts
        and not path.name.endswith('.pyc')
    }


def write_report(name, value):
    (OUT / name).write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def require_tools():
    tools = {}
    for name in ('ffmpeg', 'ffprobe'):
        located = shutil.which(name)
        require(located is not None, 'Actual ' + name + ' is required; skipping is not qualification.')
        executable = Path(located).resolve(strict=True)
        with TemporaryFile() as captured:
            result = subprocess.run(
                [str(executable), '-version'], stdout=captured,
                stderr=subprocess.STDOUT, timeout=15)
            size = captured.tell()
            require(result.returncode == 0, name + ' version probe failed.')
            require(0 < size <= 16384, name + ' version report exceeds its 16 KiB budget.')
            captured.seek(0)
            version = captured.read(16384).decode('utf-8', errors='replace')
        tools[name] = dict(
            executable=str(executable), executable_sha256=checksum(executable),
            version=version, version_bytes=size, timeout_seconds=15)
    return tools


def run(label, selectors, timeout):
    report = OUT / (label + '.xml')
    command = [
        sys.executable, '-B', '-m', 'pytest', '-p', 'no:cacheprovider',
        '-q', '--color=no', '--import-mode=importlib', '-o', 'xfail_strict=true',
        '--junitxml=' + str(report), *selectors,
    ]
    env = dict(
        os.environ, PYTHONPATH=str(APP / 'src') + os.pathsep + str(APP),
        PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', HF_HUB_OFFLINE='1',
        TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    env.pop('CARR_TEST_SPEECH_RUNTIME_SOURCE', None)
    env.pop('PYTHONOPTIMIZE', None)
    started = time.monotonic()
    with (OUT / (label + '.log')).open('wb') as log:
        result = subprocess.run(
            command, cwd=APP, env=env, stdout=log,
            stderr=subprocess.STDOUT, timeout=timeout)
    return result.returncode, time.monotonic() - started, list(ET.parse(report).iter('testcase'))


def diagnostics():
    # Print bounded tails only on a failed gate. Complete reports remain artifacts.
    for label in ('baseline', 'fixed'):
        path = OUT / (label + '.log')
        if not path.exists():
            continue
        with path.open('rb') as source:
            source.seek(max(0, path.stat().st_size - 16384))
            text = source.read(16384).decode('utf-8', errors='replace')
        print('--- ' + label + ' diagnostic tail (at most 16 KiB / 100 lines) ---', flush=True)
        print('\n'.join(text.splitlines()[-100:]), flush=True)


def main():
    OUT.mkdir()
    evidence = dict(
        status='STARTED', shared_parent=SHARED,
        scope='Finite public typed-backup composition with real synthetic SQLite/WAV producers',
        unavailable_local_tree_equivalence='NOT_CLAIMED',
        previous_local_118_result='NOT_INHERITED',
        installed='NOT_RUN', windows='NOT_RUN', nvda='NOT_RUN',
        real_voice_or_model='NOT_RUN')
    try:
        evidence.update(
            commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'),
            python=sys.version, executable=sys.executable)
        identities = []
        for path, expected in EXPECTED.items():
            raw = (ROOT / path).read_bytes()
            actual = blob(raw)
            require(actual == expected, 'Selected public source changed: ' + path)
            if path.endswith('.py'):
                compile(raw, path, 'exec', dont_inherit=True)
            identities.append(dict(
                path=path, git_blob=actual, sha256=hashlib.sha256(raw).hexdigest(),
                bytes=len(raw), overlay=path in OVERLAYS))
        evidence['files'] = identities
        write_report('source-identity.json', evidence)
        evidence['tools'] = require_tools()
        write_report('tool-identity.json', evidence['tools'])
        before = snapshot()
        write_report('source-inventory.json', before)
        fixed = {path: (ROOT / path).read_bytes() for path in BASELINES}
        try:
            for path, saved in BASELINES.items():
                raw = (ROOT / saved).read_bytes()
                (ROOT / path).write_bytes(raw)
                require(blob((ROOT / path).read_bytes()) == EXPECTED[saved],
                        'Baseline replacement did not preserve its exact public bytes.')
            status, elapsed, tests = run('baseline', BASELINE_SELECTORS, 180)
            require(status == 1 and len(tests) == 2,
                    'Baseline must execute exactly two intended failures.')
            require(not any(case.find(tag) is not None for case in tests for tag in ('error', 'skipped')),
                    'Baseline setup errors or skips are not the intended negative.')
            names = [case.get('name', '') for case in tests]
            require(set(names) == set(NEGATIVES),
                    'The baseline executed different test cases.')
            for case in tests:
                module, component = NEGATIVES[case.get('name')]
                require(case.get('classname', '').endswith(module),
                        'The baseline case came from a different suite.')
                failure = case.find('failure')
                require(failure is not None, 'Each original producer omission must fail.')
                expected = 'No validated backup adapter is installed for ' + component + '.'
                require(expected in failure.get('message', ''),
                        'Baseline failed for a different reason: ' + case.get('name', ''))
            evidence['baseline'] = dict(
                status='EXPECTED_NEGATIVES_CONFIRMED', passed=0, failed=2,
                errors=0, skipped=0, seconds=elapsed, tests=names)
            write_report('baseline-result.json', evidence['baseline'])
            print(json.dumps(evidence['baseline']), flush=True)
        finally:
            for path, raw in fixed.items():
                (ROOT / path).write_bytes(raw)
                require((ROOT / path).read_bytes() == raw,
                        'Fixed source restoration failed: ' + path)
        require(snapshot() == before, 'Baseline changed source outside the two restored files.')
        status, elapsed, tests = run('fixed', SUITES, 600)
        require(status == 0 and tests, 'The corrected seven-suite run did not complete successfully.')
        require(not any(case.find(tag) is not None for case in tests
                        for tag in ('failure', 'error', 'skipped')),
                'Corrected producer qualification contains failures, errors or skips.')
        modules = [Path(path).stem for path in SUITES]
        require(all(any(case.get('classname', '').endswith(module) for case in tests)
                    for module in modules), 'At least one required suite did not execute.')
        require(snapshot() == before, 'The corrected run changed the application source inventory.')
        for path, expected in EXPECTED.items():
            require(blob((ROOT / path).read_bytes()) == expected,
                    'A pinned source or fixture changed during execution: ' + path)
        evidence['fixed'] = dict(
            status='PASS', cases=len(tests), suites=modules, failures=0, errors=0,
            skipped=0, seconds=elapsed, source_unchanged=True)
        evidence['status'] = 'PASS'
        write_report('result.json', evidence)
        print(json.dumps(evidence['fixed']), flush=True)
    except BaseException as error:
        evidence['status'] = 'FAIL'
        evidence['error'] = dict(type=type(error).__name__, message=str(error))
        write_report('result.json', evidence)
        diagnostics()
        raise


if __name__ == '__main__':
    main()
