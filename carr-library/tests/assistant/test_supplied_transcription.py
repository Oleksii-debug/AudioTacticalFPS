"""Synthetic process/protocol regressions; no recognition/model quality claim."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.state import atomic_json, read_json
from carr_library.durable_state import digest_file, encode
from carr_library.integrations import supplied_transcription as module


FAKE_WORKER = r'''
import json, pathlib, sys, time
job = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
config = json.loads((job / 'job_config.json').read_text(encoding='utf-8'))
settings = json.loads((job / 'settings_live.json').read_text(encoding='utf-8'))
source = next(pathlib.Path(config['folders'][0]).iterdir())
mode = source.read_text(encoding='utf-8')
(job / 'worker-started').write_text('started')
if mode == 'sleep':
    time.sleep(60)
if mode == 'network':
    import socket
    socket.socket().connect(('127.0.0.1', 1))
if mode == 'outside-write':
    (job.parent.parent / 'outside.txt').write_text('unauthorized')
output = pathlib.Path(config['results_root']) / 'inputs'
output.mkdir(parents=True)
text = mode if mode != 'silence' else ''
segments = ([{'id': 0, 'seek': 0, 'start': 0.0, 'end': 1.0,
             'text': text, 'words': []}] if text else [])
meta = {'source': str(source), 'group': 'inputs', 'relative': source.name,
        'settings': settings, 'selected_language': settings['language'],
        'detected_language': 'uk', 'duration': 1.0, 'text': text,
        'segments': segments, 'v43_attempt': 'original', 'v43_attempt_no': 1}
if mode == 'wrong-source':
    meta['source'] = 'another.wav'
if mode == 'forced-language':
    meta['settings'] = dict(settings, language='ru')
    meta['detected_language'] = 'ru'
if mode == 'retry':
    meta['v43_attempt'] = 'force_ru_safe'
if mode == 'nan':
    meta['segments'][0]['end'] = float('nan')
if mode == 'timestamp':
    meta['segments'][0]['end'] = 6.0
if mode == 'missing-text':
    meta['text'] = 'different'
payloads = {'.txt': text + ('\n' if text else ''),
            '.srt': ('1\n00:00:00,000 --> 00:00:01,000\n' + text + '\n') if text else '',
            '.vtt': 'WEBVTT\n\n' + (('00:00:00.000 --> 00:00:01.000\n' + text + '\n') if text else ''),
            '.json': json.dumps(meta, ensure_ascii=False)}
for suffix, payload in payloads.items():
    if mode == 'missing-srt' and suffix == '.srt':
        continue
    (output / ('source' + suffix)).write_text(payload, encoding='utf-8')
status = {'state': 'завершено', 'done': 1, 'failed': 0, 'total': 1, 'active_count': 0}
if mode == 'failed':
    status['failed'] = 1
if mode == 'cached':
    status['total'] = 0
(job / 'job_status.json').write_text(json.dumps(status), encoding='utf-8')
'''


def inventory(root):
    return {path.relative_to(root).as_posix(): {'size': path.stat().st_size,
        'sha256': digest_file(path)} for path in root.rglob('*') if path.is_file()}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    # The fake is deliberate test infrastructure, never an accepted production
    # worker: the test-only patch also supplies its exact immutable identity.
    raw = FAKE_WORKER.encode('utf-8')
    raw += b'\n#' + b' ' * (37974 - len(raw) - 2)
    worker = tmp_path / 'synthetic_worker.py'
    worker.write_bytes(raw)
    monkeypatch.setattr(module, 'SOURCE_SHA256', sha256(raw).hexdigest())
    monkeypatch.setattr(module, 'prepare_worker', lambda candidate: candidate)
    packages = tmp_path / 'packages'
    (packages / 'faster_whisper').mkdir(parents=True)
    (packages / 'faster_whisper/__init__.py').write_text('# synthetic SDK fixture\n')
    model = tmp_path / 'model'
    model.mkdir()
    for name in ('config.json', 'tokenizer.json', 'model.bin'):
        (model / name).write_bytes(b'{}')
    python = Path(sys.executable).resolve()
    manifest = tmp_path / 'installation.json'
    manifest.write_bytes(encode({'schema': 1, 'engine': 'supplied-whisper-v43',
        'python_sha256': digest_file(python),
        'python_version': '.'.join(map(str, sys.version_info[:3])),
        'runtime_files': inventory(packages), 'model_files': inventory(model),
        'runtime_notice': 'Synthetic test SDK; no engine execution evidence.',
        'model_notice': 'Synthetic bytes; no model or model-rights claim.'}))
    installation = module.SuppliedWorkerInstallation(python, worker, packages, model,
                                                   manifest, digest_file(manifest))
    adapter = module.SuppliedTranscriptionAdapter(installation)
    app = LibraryApp(tmp_path / 'data')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / 'українська розмова.wav'
    source.write_text('Українські слова', encoding='utf-8')
    service = AssistantService(app, transcription=adapter)
    scope = Scope(input_roots=(inputs,), output_root=outputs, max_seconds=10, max_bytes=1024 * 1024)
    plan = Plan(scope, (Step('audio.transcribe', {'path': str(source), 'name': 'транскрипт', 'language': 'uk'}),))
    return service, source, outputs, plan, installation


def test_real_owned_process_uses_job_dir_and_existing_assistant_checkpoint(environment):
    service, source, outputs, plan, _ = environment
    identity = service.submit(plan)
    report = service.run(identity)
    assert report.status == 'complete', report
    assert report.completed == 1
    destination = outputs / 'транскрипт'
    assert {path.name for path in destination.iterdir()} == {
        'transcript.txt', 'transcript.srt', 'transcript.vtt', 'transcript.json'}
    assert (destination / 'transcript.txt').read_text(encoding='utf-8') == 'Українські слова\n'
    meta = read_json(destination / 'transcript.json')
    assert meta['source']['sha256'] == digest_file(source)
    assert meta['source']['source'] == str(source)
    assert meta['word_timestamps'] is False and meta['diarization'] is False
    assert '.transcription-' not in json.dumps(meta)
    assert not list(outputs.glob('.transcription-*'))
    record = service.store.load(identity)
    assert record['steps'][0]['intent']['transcription']['sha256'] == digest_file(source)
    assert len(record['steps'][0]['intent']['transcript_artifacts']) == 4
    assert service.resume(identity).status == 'complete'


@pytest.mark.parametrize('mode', ['missing-srt', 'wrong-source', 'forced-language',
    'retry', 'nan', 'timestamp', 'missing-text', 'failed', 'cached', 'network', 'outside-write'])
def test_worker_exit_zero_is_not_success_without_bound_complete_outputs(environment, mode):
    service, source, outputs, plan, _ = environment
    source.write_text(mode, encoding='utf-8')
    report = service.run(service.submit(plan))
    assert report.status == 'failed', report
    assert report.completed == 0
    assert not (outputs / 'транскрипт').exists()
    assert not (outputs / 'outside.txt').exists()
    assert not list(outputs.glob('.transcription-*'))


def test_empty_transcript_preserves_silence_without_language_retry(environment):
    service, source, outputs, plan, _ = environment
    source.write_text('silence')
    report = service.run(service.submit(plan))
    assert report.status == 'complete', report
    assert (outputs / 'транскрипт/transcript.txt').read_bytes() == b''
    assert (outputs / 'транскрипт/transcript.srt').read_bytes() == b''
    assert read_json(outputs / 'транскрипт/transcript.json')['segments'] == []


def test_changed_bytes_same_size_and_mtime_do_not_resume_wrong_source(environment, monkeypatch):
    service, source, outputs, plan, _ = environment
    identity = service.submit(plan)
    calls = 0
    original = module.run_process
    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise module.AudioError('synthetic interruption before publication')
    monkeypatch.setattr(module, 'run_process', fail_once)
    assert service.run(identity).status == 'failed'
    before = source.stat()
    raw = source.read_bytes()
    source.write_bytes(b'x' * len(raw))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(module, 'run_process', original)
    resumed = service.resume(identity)
    assert resumed.status == 'failed' and 'changed' in resumed.message
    assert calls == 1
    assert not (outputs / 'транскрипт').exists()


def test_partial_publication_resumes_only_exact_saved_artifacts(environment, monkeypatch):
    service, source, outputs, plan, _ = environment
    identity = service.submit(plan)
    original = module.publish_file
    calls = 0
    def interrupted(staged, target, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('synthetic publication interruption')
        return original(staged, target, **kwargs)
    monkeypatch.setattr(module, 'publish_file', interrupted)
    first = service.run(identity)
    assert first.status == 'failed' and first.completed == 0
    text = outputs / 'транскрипт/transcript.txt'
    before = text.stat()
    monkeypatch.setattr(module, 'publish_file', original)
    resumed = service.resume(identity)
    assert resumed.status == 'complete', resumed
    assert text.stat().st_mtime_ns == before.st_mtime_ns
    assert len(resumed.results[0]['artifacts']) == 4


def test_cancel_after_publication_recovers_without_running_worker_again(environment, monkeypatch):
    service, source, outputs, plan, _ = environment
    identity = service.submit(plan)
    stopped = Event()
    original = module.publish_file
    calls = 0
    def after_commit(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 4:
            stopped.set()
        return result
    monkeypatch.setattr(module, 'publish_file', after_commit)
    report = service.run(identity, cancelled=stopped.is_set)
    assert report.status == 'paused'
    assert len(list((outputs / 'транскрипт').iterdir())) == 4
    def forbidden(*args, **kwargs):
        raise AssertionError('already committed transcripts must not be transcribed again')
    monkeypatch.setattr(module, 'run_process', forbidden)
    assert service.resume(identity).status == 'complete'


def test_changed_partial_export_is_never_overwritten(environment, monkeypatch):
    service, source, outputs, plan, _ = environment
    identity = service.submit(plan)
    original = module.publish_file
    def interrupted(staged, target, **kwargs):
        if target.suffix == '.srt':
            raise OSError('synthetic disk error')
        return original(staged, target, **kwargs)
    monkeypatch.setattr(module, 'publish_file', interrupted)
    assert service.run(identity).status == 'failed'
    text = outputs / 'транскрипт/transcript.txt'
    text.write_text('Owner-edited transcript', encoding='utf-8')
    monkeypatch.setattr(module, 'publish_file', original)
    report = service.resume(identity)
    assert report.status == 'failed' and 'changed' in report.message
    assert text.read_text(encoding='utf-8') == 'Owner-edited transcript'


@pytest.mark.parametrize('component', ['manifest', 'worker', 'model', 'packages', 'python-pin'])
def test_runtime_and_model_identity_fail_before_launch(environment, monkeypatch, component):
    service, source, outputs, plan, installation = environment
    if component in ('model', 'packages'):
        root = getattr(installation, component)
        next(path for path in root.rglob('*') if path.is_file()).write_bytes(b'changed')
    elif component == 'python-pin':
        value = read_json(installation.manifest)
        value['python_sha256'] = '0' * 64
        installation.manifest.write_bytes(encode(value))
        object.__setattr__(installation, 'manifest_sha256', digest_file(installation.manifest))
    else:
        getattr(installation, component).write_bytes(b'changed')
    def forbidden(*args, **kwargs):
        raise AssertionError('unverified installation must never start')
    monkeypatch.setattr(module, 'run_process', forbidden)
    report = service.run(service.submit(plan))
    assert report.status == 'failed', report
    assert not (outputs / 'транскрипт').exists()


def test_mid_file_cancel_terminates_worker_and_keeps_saved_intent(environment):
    service, source, outputs, plan, _ = environment
    source.write_text('sleep')
    identity = service.submit(plan)
    started = Event()
    outcome = []
    def run():
        outcome.append(service.run(identity, progress=lambda *args: started.set()))
    thread = Thread(target=run)
    thread.start()
    assert started.wait(5)
    # Wait until the actual process setup has started; cancel is persisted in
    # the existing JobStore, not a second worker-specific job database.
    deadline = time.monotonic() + 5
    while not list(outputs.glob('.transcription-*/job/worker-started')) and time.monotonic() < deadline:
        time.sleep(.01)
    assert list(outputs.glob('.transcription-*/job/worker-started'))
    began = time.monotonic()
    service.cancel(identity)
    thread.join(5)
    assert not thread.is_alive()
    assert time.monotonic() - began < 5
    assert outcome[0].status == 'cancelled'
    assert service.store.load(identity)['steps'][0]['intent']['transcription']
    assert not (outputs / 'транскрипт').exists()
    assert not list(outputs.glob('.transcription-*'))


def test_native_preview_does_not_enable_an_unconfigured_engine(environment):
    service, source, outputs, plan, _ = environment
    unavailable = AssistantService(service.app)
    with pytest.raises(RuntimeError, match='Configure and verify'):
        unavailable.submit(plan)


@pytest.mark.parametrize('arguments', [
    {'path': '/outside.wav', 'name': 'a'},
    {'path': 'unused', 'name': '../outside'},
    {'path': 'unused', 'name': 'a', 'beam_size': True},
    {'path': 'unused', 'name': 'a', 'language': 'ru; shell'},
    {'path': 'unused', 'name': 'a', 'model': '/arbitrary-model'},
    {'path': 'unused', 'name': 'a', 'vad_filter': 'false'},
])
def test_typed_plan_cannot_expand_authority_or_runtime_settings(environment, arguments):
    service, source, outputs, plan, _ = environment
    if arguments['path'] == 'unused':
        arguments = dict(arguments, path=str(source))
    with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
        service.submit(Plan(plan.scope, (Step('audio.transcribe', arguments),)))


def test_unreviewed_worker_source_cannot_be_prepared():
    with pytest.raises(ValueError, match='unreviewed'):
        module.prepare_worker(b'print("not the supplied worker")')


def test_assistant_backup_retains_inert_transcription_checkpoint(environment, tmp_path):
    service, source, outputs, plan, _ = environment
    identity = service.submit(plan)
    assert service.run(identity).status == 'complete'
    backup = tmp_path / 'backup'
    service.app.backup(backup)
    manifest = read_json(backup / 'manifest.json')
    entry = next(item for item in manifest['sidecars']
                 if item['filename'].endswith('/' + identity + '.json'))
    assert entry['filename'] == 'state/assistant/' + identity + '.json'
    saved = read_json(backup / entry['filename'])
    assert saved['steps'][0]['intent']['transcription']['sha256'] == digest_file(source)
