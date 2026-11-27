"""Windows-only, no-model qualification of concurrent transcription status reads.

The required test is repeated without modifying its implementation. A separate
controlled schedule holds the actual host reader open while the original
synthetic worker publishes its next status. This narrow runtime proof is not a
whole-product acceptance or a neural inference result.
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

BASE_SOURCE = '865aa3223a154278d086ef5c98e07a4274a10202'
PINS = {
    'src/carr_library/transcription.py': '6b3b3ff112e8d96d7f8000181bd0521e57ed2876065aeea425523221a49ffc62',
    'src/carr_library/durable_state.py': 'be38c92f88c6d31d21de4982bb10e5a2c7280c55917dc2cf01b7a1493750d002',
    'src/carr_library/audio/process.py': '545ae5553207a604337fa156b3f3728b4532c7763f2ffe29c20d2f5a41e34649',
    'tests/transcription/test_supplied_worker_bridge.py': '58e68412909fbd561e36242df10506d6d324408a5c65f5c317c3b9ebb0a269fe',
}


def emit(kind, **fields):
    print(json.dumps(dict(kind=kind, **fields), ensure_ascii=True, sort_keys=True), flush=True)


def offline(*args, **kwargs):
    raise OSError('Network disabled during the no-model status diagnostic.')


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
        assert isinstance(native_error, PermissionError), 'The proposed Windows collision was not reproduced.'
        os.replace(staged, current)
        assert json.loads(current.read_bytes())['state'] == 'finished'
        emit('replace_after_close', successful=True)

        successes, failures = 0, []
        for index in range(20):
            case = root / ('unchanged-%02d' % index)
            case.mkdir()
            prepared = fixture.prepared.__wrapped__(case)
            service = prepared[2]
            original_run = service.run
            captured = []
            def capture(*args, **kwargs):
                value = original_run(*args, **kwargs)
                captured.append(value)
                emit('unchanged_case_result', index=index, result=value)
                return value
            service.run = capture
            try:
                fixture.test_progress_observer_failure_preserves_real_child_or_receipt_failure(
                    prepared, 'missing-output', 'export is missing')
            except AssertionError:
                failures.append(index)
                emit('unchanged_case_assertion', index=index, traceback=traceback.format_exc())
            else:
                successes += 1
            assert len(captured) == 1

        case = root / 'controlled-reader'
        case.mkdir()
        _, _, service, original, configure = fixture.prepared.__wrapped__(case)
        worker = configure('missing-output')
        raw = worker.read_text(encoding='utf-8')
        needle = "publish()\nif MODE == 'progress-wait':"
        replacement = """publish()
deadline = time.monotonic() + 5
while not (job / 'host-reader-held').exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('Controlled diagnostic reader did not arrive.')
    time.sleep(.001)
if MODE == 'progress-wait':"""
        assert raw.count(needle) == 1
        raw = raw.replace(needle, replacement)
        publish_line = "    os.replace(temp, job / 'job_status.json')"
        assert raw.count(publish_line) == 1
        raw = raw.replace(publish_line, """    try:
        os.replace(temp, job / 'job_status.json')
    finally:
        if (job / 'host-reader-held').exists():
            (job / 'status-replace-attempted').write_bytes(b'attempted')""")
        worker.write_text(raw, encoding='utf-8')
        service.configure(Path(sys.executable).resolve(), worker, case / 'models')
        identity = service.submit([original])
        status = service._path(identity) / 'job_status.json'
        original_open = durable_state._open_json_reader
        held = []
        class DelayedReader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def read(self, *args, **kwargs):
                (status.parent / 'host-reader-held').write_bytes(b'ready\n')
                deadline = time.monotonic() + 5
                while not (status.parent / 'status-replace-attempted').exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Child did not attempt publication while its reader was held.')
                    time.sleep(.001)
                return self.stream.read(*args, **kwargs)
        def overlap(path):
            stream = original_open(path)
            if path == status and not held:
                held.append(True)
                return DelayedReader(stream)
            return stream
        def broken(*args):
            raise RuntimeError('Synthetic detached observer')
        with patch.object(durable_state, '_open_json_reader', overlap):
            result = service.run(identity, timeout=10, progress=broken)
        emit('controlled_actual_host_reader_result', result=result, reader_held=bool(held))
        assert held and result['status'] == 'failed'
        assert result['last_error'] == 'A required transcription export is missing.'
        assert (status.parent / 'status-replace-attempted').exists()
        assert result['artifacts'] == []
        assert not failures, 'An unchanged required case failed; see its complete result above.'
        emit('diagnostic_summary', status='WINDOWS_SHARED_READER_QUALIFIED', unchanged_passed=successes,
             unchanged_failed=failures, controlled_publication=True, neural_inference='NOT_RUN',
             production_fix='NATIVE_READER_VERIFIED', acoustic_assets='NONE')


if __name__ == '__main__':
    main()
