"""Strict installed Windows admission/Tasks acceptance with pinned test audio.

The existing native runner supplies real firewall rules. Synthetic ASR receipts
exercise the accepted worker protocol; this is not model-quality or NVDA proof.
"""
import argparse
from hashlib import file_digest
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
ORIGINS = [
    "carr_library",
    "carr_library.state_backup",
    "carr_library.ingest.worker",
    "carr_library.application",
    "carr_library.pending_backup",
    "carr_library.library_backup",
    "carr_library.speech.reading",
    "carr_library.speech.source",
    "carr_library.ui.speech_dialog",
    "carr_library.speech.runtime",
    "carr_library.academic_integration.doi",
    "carr_library.speech.session",
    "carr_library.extract.ocr_runtime",
    "carr_library.durable_state",
    "carr_library.ui.save_actions",
    "carr_library.storage.revisions",
    "carr_library.storage.schema_identity",
    "carr_library.assistant.translation",
    "carr_library.assistant.providers",
    "carr_library.ui.translation_dialog",
    "carr_library.operations",
    "carr_library.operations._locks",
    "carr_library.operations.models",
    "carr_library.operations.broker",
    "carr_library.operations.facade",
    "carr_library.transcription",
    "carr_library.studio.render",
    "carr_library.audio.process",
    "carr_library.ui.operations_dialog",
    "carr_library.ui.transcription_dialog",
    "carr_library.assistant.llm",
    "carr_library.assistant.service",
    "carr_library.ui.assistant_dialog",
    "carr_library.studio.cli"
]
SUITES = [
    "tests/operations/test_resources.py",
    "tests/operations/test_resource_backup.py",
    "tests/operations/test_production_admission.py",
    "tests/operations/test_worker_start_cleanup.py",
    "tests/operations/test_canonical_asr_admission.py",
    "tests/operations/test_facade.py",
    "tests/ui/test_operations_policy.py",
    "tests/ui/test_operations_dialog.py",
    "tests/ui/test_desktop_handlers.py",
    "tests/transcription/test_supplied_worker_bridge.py",
    "tests/transcription/test_v5_worker_profile.py",
    "tests/transcription/test_runtime_interpreter.py",
    "tests/studio/test_production_cancellation_outcomes.py",
    "tests/assistant/test_local_planning.py",
    "tests/assistant/test_planning_handlers.py"
]
CROSS_PROCESS = {
    'test_actual_cross_process_studio_asr_contention_cancel_and_crash[' + outcome + ']'
    for outcome in ('complete', 'cancel-waiter', 'crash-holder')
}


def digest(path):
    with Path(path).open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def source_identity():
    def git(*arguments):
        return subprocess.check_output(['git', *arguments], cwd=ROOT,
            text=True, encoding='utf-8', timeout=15).strip()
    return {'commit': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}')}


def installed_origins():
    import carr_library
    package = Path(carr_library.__file__).resolve().parent
    checkout = ROOT.parent
    if package.is_relative_to(checkout):
        raise RuntimeError('Acceptance imported the checkout instead of the installed package.')
    result = {}
    for name in ORIGINS:
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve(strict=True)
        relative = actual.relative_to(package)
        expected = ROOT / 'src' / 'carr_library' / relative
        if not expected.is_file() or digest(actual) != digest(expected):
            raise RuntimeError('Installed module differs from the exact checked-out source: ' + name)
        if name == 'carr_library.transcription' and relative.as_posix() != 'transcription.py':
            raise RuntimeError('The accepted transcription module was shadowed by another package.')
        result[name] = {'path': str(actual), 'sha256': digest(actual)}
    return result


def pinned_audio(directory):
    directory = directory.resolve(strict=True)
    lock_path = ROOT / 'packaging' / 'windows-audio.lock.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    manifest_path = directory / 'runtime-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['lock_sha256'] != digest(lock_path):
        raise RuntimeError('The prepared audio runtime does not match the checked-out pin.')
    result = {}
    for name in ('ffmpeg', 'ffprobe'):
        relative = 'bin/' + name + '.exe'
        path = (directory / relative).resolve(strict=True)
        found = shutil.which(name)
        if not found or not Path(found).samefile(path):
            raise RuntimeError('The selected ' + name + ' is not the explicitly prepared executable.')
        expected = lock['output_files'][relative]
        actual = {'path': str(path), 'size': path.stat().st_size, 'sha256': digest(path)}
        if actual['size'] != expected['size'] or actual['sha256'] != expected['sha256']:
            raise RuntimeError('The selected audio executable differs from its exact pin: ' + name)
        result[name] = actual
    return {'engine_version': lock['engine_version'], 'lock_sha256': digest(lock_path), 'executables': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-root', required=True, type=Path)
    parser.add_argument('--results', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('This strict gate requires actual Windows; POSIX fixtures are separate.')
    if Path.cwd().resolve() != ROOT:
        raise RuntimeError('Run this gate from the exact carr-library checkout.')
    if version('filelock') != '3.29.0':
        raise RuntimeError('The native lock backend must match the application lockfile.')
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH'):
        os.environ.pop(key, None)
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    args.results.mkdir(parents=True, exist_ok=True)
    report = args.results / 'installed-operations.xml'
    evidence = args.results / 'installed-operations.json'
    result = {'status': 'RUNNING', 'source': source_identity(), 'python': sys.version,
              'filelock': version('filelock'), 'audio': pinned_audio(args.audio_root),
              'origins': installed_origins(), 'suites': SUITES,
              'manual_nvda': 'NOT_RUN', 'acoustic_model_quality': 'NOT_RUN',
              'measured_hardware_capacity': 'NOT_RUN', 'orphan_child_crash_containment': 'NOT_RUN'}
    evidence.write_text(json.dumps(result, indent=2), encoding='utf-8')
    import pytest

    class RejectExpectedFailures:
        seen = False

        def pytest_runtest_logreport(self, report):
            if hasattr(report, 'wasxfail'):
                self.seen = True

    policy = RejectExpectedFailures()
    status = pytest.main(['-q', '-o', 'addopts=', '-o', 'pythonpath=',
                         '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=120',
                         '--import-mode=importlib', '--junitxml=' + str(report),
                         *SUITES], plugins=[policy])
    cases = list(ET.parse(report).iter('testcase')) if report.exists() else []
    failed = sum(any(case.find(tag) is not None for tag in ('failure', 'error')) for case in cases)
    skipped = sum(case.find('skipped') is not None for case in cases)
    passed = len(cases) - failed - skipped
    cross = [case.get('name') for case in cases if case.get('classname', '').rsplit('.', 1)[-1]
             == 'test_canonical_asr_admission' and
             case.get('name', '').startswith('test_actual_cross_process_studio_asr_contention_cancel_and_crash[')]
    cross_exact = len(cross) == 3 and set(cross) == CROSS_PROCESS
    observed = {case.get('classname', '').rsplit('.', 1)[-1] for case in cases}
    missing_suites = sorted({Path(path).stem for path in SUITES} - observed)
    after = installed_origins()
    origins_unchanged = after == result['origins']
    ok = (status == 0 and passed > 0 and failed == skipped == 0 and not policy.seen
          and cross_exact and not missing_suites and origins_unchanged)
    result.update(status='PASS' if ok else 'FAIL', passed=passed, failed=failed, skipped=skipped,
                  pytest_exit=int(status), expected_failures=policy.seen,
                  actual_cross_process_cases=sorted(cross), actual_cross_process_exact=cross_exact,
                  missing_suites=missing_suites, origins_after=after, origins_unchanged=origins_unchanged)
    evidence.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
