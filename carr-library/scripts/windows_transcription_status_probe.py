"""Windows-only synthetic status publication diagnosis; no private engine."""
from pathlib import Path
import importlib.util
import json
import sys
from tempfile import TemporaryDirectory

import pytest

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'status_collision_fixture', root / 'tests/transcription/test_supplied_worker_bridge.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def evidence(prepared):
    service = prepared[2]
    records = []
    if service.jobs_root.exists():
        for job in sorted(service.jobs_root.iterdir())[:4]:
            if not job.is_dir():
                continue
            entry = {'job': job.name}
            for name in ('job.json', 'job_status.json', 'synthetic-worker-error.json'):
                path = job / name
                if path.is_file() and path.stat().st_size <= 32768:
                    entry[name] = json.loads(path.read_text(encoding='utf-8'))
            records.append(entry)
    return records


assert sys.platform == 'win32', 'This diagnosis requires actual Windows file sharing.'
with TemporaryDirectory(prefix='asr-held-read-') as temporary:
    prepared = fixture.prepared.__wrapped__(Path(temporary))
    _, _, service, original, configure = prepared
    configure('progress-wait')
    identity = service.submit([original])
    job = service._path(identity)
    held = []

    def progress(done, total, phase):
        if not held:
            held.append((job / 'job_status.json').open('rb'))
            (job / 'progress-observed').write_text('release fixture with existing read handle', encoding='utf-8')

    try:
        result = service.run(identity, timeout=10, progress=progress)
    finally:
        for stream in held:
            stream.close()
    print('DETERMINISTIC HELD-READ RESULT')
    print(json.dumps({'result': result, 'evidence': evidence(prepared)}, ensure_ascii=True))
    assert held, 'The collision probe did not establish its held reader.'
    assert result['status'] == 'failed', 'Unexpected Windows collision result; inspect recorded evidence.'
    details = json.loads((job / 'synthetic-worker-error.json').read_text(encoding='utf-8'))
    assert details['type'] == 'PermissionError' and details['winerror'] in (5, 32, 33), details


class FailureEvidence:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if report.when == 'call' and report.failed:
            prepared = item.funcargs.get('prepared')
            if prepared is not None:
                print('SYNTHETIC FAILURE EVIDENCE: ' + item.nodeid)
                print(json.dumps(evidence(prepared), ensure_ascii=True))


targets = [
    'tests/transcription/test_supplied_worker_bridge.py::test_stop_does_not_rehash_inputs_and_preserves_completed_worker_files',
    'tests/transcription/test_supplied_worker_bridge.py::test_partial_progress_requires_verified_resume_before_host_success',
]
code = pytest.main(['-q', '-s', '-W', 'error', *targets], plugins=[FailureEvidence()])
print(json.dumps({'diagnostic_only': True, 'original_test_exit': int(code),
                  'private_worker_or_neural_inference': 'NOT_RUN'}))
raise SystemExit(code)
