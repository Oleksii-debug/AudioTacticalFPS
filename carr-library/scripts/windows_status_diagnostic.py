"""Bounded Windows qualification of live transcription status publication.

Only the public synthetic protocol worker runs. No models, inference, acoustic
assets or output uploads are used. Production owns every replacement retry;
diagnostic instrumentation only observes native attempts and coordinates reads.
"""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import time
import traceback
from unittest.mock import patch

BASE_SOURCE = '65836dca7abb9f37a706eabe8f8f51cabe4eb101'
PINS = {
    "src/carr_library/transcription.py": "2afaac96424e830988b7ac462b819dd6c4d851979e403dc000ea50d147561c92",
    "src/carr_library/durable_state.py": "1215a3bf3960e1645d2f4cce25222e1cd705091f5aa4c6206a9d3eebb02f45ee",
    "src/carr_library/audio/process.py": "545ae5553207a604337fa156b3f3728b4532c7763f2ffe29c20d2f5a41e34649",
    "tests/transcription/test_supplied_worker_bridge.py": "e5da5b4e2fd374d2d32572b72c1bce8fd44ca150835f9c00926761f297268022"
}


def emit(kind, **fields):
    print(json.dumps(dict(kind=kind, **fields), ensure_ascii=True, sort_keys=True), flush=True)


def offline(*args, **kwargs):
    raise OSError('Network disabled during the no-model status diagnostic.')


def repeat_required(fixture, root, family, count):
    successes, failures = 0, []
    for index in range(count):
        case = root / ('%s-%02d' % (family, index))
        case.mkdir()
        prepared = fixture.prepared.__wrapped__(case)
        service = prepared[2]
        original_run, captured = service.run, []
        def capture(*args, **kwargs):
            value = original_run(*args, **kwargs)
            captured.append(value)
            emit('unchanged_case_result', family=family, index=index,
                 call=len(captured), result=value)
            return value
        service.run = capture
        try:
            if family == 'missing-output':
                fixture.test_progress_observer_failure_preserves_real_child_or_receipt_failure(
                    prepared, 'missing-output', 'export is missing')
            else:
                fixture.test_partial_progress_requires_verified_resume_before_host_success(prepared, case)
        except AssertionError:
            failures.append(index)
            emit('unchanged_case_assertion', family=family, index=index,
                 traceback=traceback.format_exc())
        else:
            successes += 1
        assert captured, 'The unchanged case did not run its actual child.'
    return successes, failures


def controlled_reader(fixture, root, permanent):
    case = root / ('permanent-reader' if permanent else 'transient-reader')
    case.mkdir()
    _, _, service, original, configure = fixture.prepared.__wrapped__(case)
    worker = configure('missing-output')
    raw = worker.read_text(encoding='utf-8')
    initial = "publish()\nif MODE == 'progress-wait':"
    observed = """publish()
native_attempts = 0
def observe_native(event, args):
    global native_attempts
    if (event == 'os.rename'
            and os.path.normcase(os.path.abspath(os.fsdecode(args[1])))
                == os.path.normcase(str(job / 'job_status.json'))):
        native_attempts += 1
        (job / ('status-native-attempt-%d' % native_attempts)).write_bytes(b'attempt')
sys.addaudithook(observe_native)
deadline = time.monotonic() + 5
while not (job / 'host-reader-held').exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('Controlled diagnostic reader did not arrive.')
    time.sleep(.001)
if MODE == 'progress-wait':"""
    assert raw.count(initial) == 1
    raw = raw.replace(initial, observed)
    native_line = "    os.replace(temp, job / 'job_status.json')"
    measured = """    measured = (job / 'host-reader-held').exists()
    started, published, failure = time.monotonic(), False, None
    try:
        os.replace(temp, job / 'job_status.json')
        published = True
    except BaseException as error:
        failure = {'type': type(error).__name__, 'winerror': getattr(error, 'winerror', None),
                   'errno': getattr(error, 'errno', None), 'message': str(error)}
        raise
    finally:
        if measured:
            report = {'elapsed_seconds': time.monotonic() - started, 'published': published,
                      'failure': failure, 'native_attempts': native_attempts}
            (job / 'status-publication-report.json').write_text(json.dumps(report), encoding='utf-8')
            (job / 'status-replace-finished').write_bytes(b'finished')"""
    assert raw.count(native_line) == 1
    raw = raw.replace(native_line, measured)
    worker.write_text(raw, encoding='utf-8')
    service.configure(Path(sys.executable).resolve(), worker, case / 'models')
    identity = service.submit([original])
    status = service._path(identity) / 'job_status.json'
    original_open, held, before = Path.open, [], []

    class HeldReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, *args, **kwargs):
            value = self.stream.read(*args, **kwargs)
            before.append(value)
            (status.parent / 'host-reader-held').write_bytes(b'ready\n')
            milestone = ('status-replace-finished' if permanent else 'status-native-attempt-2')
            deadline = time.monotonic() + 5
            while not (status.parent / milestone).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('The child did not reach its bounded publication milestone.')
                time.sleep(.001)
            return value

    def overlap(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path == status and mode == 'rb' and not held:
            held.append(True)
            return HeldReader(stream)
        return stream

    def broken(*args):
        raise RuntimeError('Synthetic detached observer')

    with patch.object(Path, 'open', overlap):
        result = service.run(identity, timeout=10, progress=broken)
    report_path = status.parent / 'status-publication-report.json'
    report = json.loads(report_path.read_bytes()) if report_path.exists() else None
    emit('controlled_actual_host_reader_result', permanent=permanent, result=result,
         reader_held=bool(held), publication=report)
    assert held and result['status'] == 'failed' and result['artifacts'] == []
    assert report and report['native_attempts'] >= 2
    assert (status.parent / 'status-replace-finished').exists()
    if permanent:
        assert not report['published']
        assert report['failure']['type'] == 'PermissionError'
        assert report['failure']['winerror'] in (5, 32, 33)
        assert .5 <= report['elapsed_seconds'] < 3
        assert 'PermissionError' in result['last_error'] and 'job_status.json' in result['last_error']
        assert status.read_bytes() == before[0], 'A failed replacement changed the original generation.'
    else:
        assert report['published'] and report['failure'] is None
        assert result['last_error'] == 'A required transcription export is missing.'
        assert json.loads(status.read_bytes())['result_state'] == 'completed'
    return report


def main():
    assert sys.platform == 'win32' and sys.version_info[:2] == (3, 11)
    source = Path(__file__).resolve().parents[1]
    for name, expected in PINS.items():
        assert sha256((source / name).read_bytes()).hexdigest() == expected, name
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    socket.create_connection = offline
    socket.getaddrinfo = offline
    from carr_library import transcription, durable_state
    from carr_library.audio import process
    for module, name in ((transcription, 'transcription.py'), (durable_state, 'durable_state.py'),
                         (process, 'audio/process.py')):
        assert sha256(Path(module.__file__).read_bytes()).hexdigest() == PINS['src/carr_library/' + name]
    spec = importlib.util.spec_from_file_location('exact_status_fixture',
        source / 'tests/transcription/test_supplied_worker_bridge.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    emit('source_identity', base=BASE_SOURCE, head=os.environ.get('GITHUB_SHA'),
         python=sys.version, hashes=PINS, inference='NOT_RUN', models='NOT_DOWNLOADED')
    with TemporaryDirectory(prefix='carr-status-diagnostic-') as directory:
        root = Path(directory)
        current, staged = root / 'status.json', root / 'status.tmp'
        current.write_text('{"state":"running"}', encoding='utf-8')
        staged.write_text('{"state":"finished"}', encoding='utf-8')
        native_error = None
        with current.open('rb') as reader:
            try:
                os.replace(staged, current)
            except OSError as exc:
                native_error = exc
                emit('actual_builtin_reader_replace', type=type(exc).__name__, errno=exc.errno,
                     winerror=getattr(exc, 'winerror', None), message=str(exc))
            assert reader.read() == b'{"state":"running"}'
        assert isinstance(native_error, PermissionError), 'The Windows collision was not reproduced.'
        os.replace(staged, current)
        assert json.loads(current.read_bytes())['state'] == 'finished'
        emit('replace_after_close', successful=True)
        missing_pass, missing_fail = repeat_required(fixture, root, 'missing-output', 20)
        resume_pass, resume_fail = repeat_required(fixture, root, 'partial-resume', 10)
        transient = controlled_reader(fixture, root, permanent=False)
        permanent = controlled_reader(fixture, root, permanent=True)
        assert not missing_fail and not resume_fail, 'An unchanged case failed; see the complete result above.'
        emit('diagnostic_summary', status='WINDOWS_STATUS_PUBLICATION_QUALIFIED',
             missing_output_passed=missing_pass, missing_output_failed=missing_fail,
             partial_resume_passed=resume_pass, partial_resume_failed=resume_fail,
             transient_publication=transient, persistent_refusal=permanent,
             neural_inference='NOT_RUN', production_fix='BOUNDED_PUBLISHER_RETRY',
             acoustic_assets='NONE')


if __name__ == '__main__':
    main()
