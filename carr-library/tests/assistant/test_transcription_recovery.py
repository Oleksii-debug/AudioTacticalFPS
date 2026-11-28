"""Actual isolated exits, owned publication recovery, and passive complete backup."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event

import pytest

import carr_library
from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.assistant import transcription_tools as tools
from carr_library.assistant.models import canonical
from carr_library.assistant.validation import validate_job
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.runtime_settings import EngineProfileStore
from carr_library import transcription_service as domain
from tests.assistant import transcription_fixture
from tests.assistant.transcription_fixture import make_environment, protocol_interpreter


CRASH_CHILD = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from transcription_fixture import open_environment, protocol_interpreter
from carr_library import transcription_service as domain
from carr_library.assistant.state import JobStore
from carr_library.audio import jobs
from carr_library import file_publication
root, identity, phase = Path(sys.argv[3]), sys.argv[4], sys.argv[5]
EXIT = 77
prepare, run, save = domain.TranscriptionService.prepare, domain.TranscriptionService.run, JobStore.save
copy, publish, mkdir = jobs.publish_new, file_publication.publish_directory_no_replace, os.mkdir
def preparing(self, *args, **kwargs):
    if phase == 'before-prepare':
        os._exit(EXIT)
    result = prepare(self, *args, **kwargs)
    if phase == 'after-unbound-prepare':
        os._exit(EXIT)
    return result
def running(self, *args, **kwargs):
    if phase == 'before-run':
        os._exit(EXIT)
    result = run(self, *args, **kwargs)
    if phase == 'after-accepted-run':
        os._exit(EXIT)
    return result
def saving(self, value):
    result = save(self, value)
    intent = value['steps'][0]['intent']['transcription']
    if phase == 'after-bound-prepare' and intent['prepared'] and not intent['run_dispatched']:
        os._exit(EXIT)
    if phase == 'after-bound-stage' and intent['publication'] and not intent['publication']['sealed']:
        os._exit(EXIT)
    return result
def making(path, *args, **kwargs):
    result = mkdir(path, *args, **kwargs)
    if phase == 'after-unbound-stage' and Path(path).name.startswith('.assistant-transcription-'):
        os._exit(EXIT)
    return result
def copying(source, target, **kwargs):
    result = copy(source, target, **kwargs)
    if phase == 'partial-copy' and Path(target).parent.name.startswith('.assistant-transcription-'):
        os._exit(EXIT)
    return result
def publishing(stage, destination, *args, **kwargs):
    if phase == 'before-publish':
        os._exit(EXIT)
    result = publish(stage, destination, *args, **kwargs)
    if phase == 'after-publish':
        os._exit(EXIT)
    return result
domain.TranscriptionService.prepare, domain.TranscriptionService.run = preparing, running
JobStore.save, jobs.publish_new = saving, copying
file_publication.publish_directory_no_replace, os.mkdir = publishing, making
with protocol_interpreter():
    env = open_environment(root)
    result = env.service.resume(identity)
raise RuntimeError('The requested crash phase was not reached: ' + repr(result))
"""


@pytest.fixture
def environment(tmp_path):
    with protocol_interpreter():
        yield make_environment(tmp_path / 'selected')


def crash(environment, identity, phase):
    completed = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', CRASH_CHILD,
        str(Path(carr_library.__file__).resolve().parent.parent),
        str(Path(transcription_fixture.__file__).resolve().parent),
        str(environment.root), identity, phase], capture_output=True, timeout=90)
    assert completed.returncode == 77, completed.stderr.decode('utf-8', errors='replace')[-6000:]
    return environment.service.store.load(identity)['steps'][0]['intent']['transcription']


def no_inference(*args, **kwargs):
    raise AssertionError('A saved accepted or uncertain dispatch was incorrectly replayed.')


@pytest.mark.parametrize('phase,expected,prepared_count', [
    ('before-prepare', 'attention', 0),
    ('after-unbound-prepare', 'attention', 1),
    ('after-bound-prepare', 'complete', 1),
    ('before-run', 'attention', 1),
    ('after-accepted-run', 'complete', 1),
    ('after-unbound-stage', 'attention', 1),
    ('after-bound-stage', 'complete', 1),
    ('partial-copy', 'complete', 1),
    ('before-publish', 'complete', 1),
    ('after-publish', 'complete', 1),
])
def test_actual_process_exit_resumes_only_bound_work_and_never_replays_uncertain_inference(
        environment, monkeypatch, phase, expected, prepared_count):
    e = environment
    identity = e.submit()
    intent = crash(e, identity, phase)
    domain_jobs = domain.TranscriptionService(e.app.root).jobs()
    assert len(domain_jobs) == prepared_count
    if phase != 'after-bound-prepare':
        monkeypatch.setattr(domain.TranscriptionService, 'run', no_inference)
    monkeypatch.setattr(domain.TranscriptionService, 'prepare', no_inference)
    partial = e.outputs / ('.assistant-transcription-' + intent['operation_id']) / 'transcript.txt'
    public = e.outputs / 'transcript' / 'transcript.txt'
    tracked = public if public.exists() else partial
    before = ((tracked.read_bytes(), tracked.stat().st_ino, tracked.stat().st_mtime_ns)
              if tracked.exists() else None)
    result = e.service.resume(identity)
    assert result.status == expected, result.message
    assert len(domain.TranscriptionService(e.app.root).jobs()) == prepared_count
    if expected == 'complete':
        assert len(result.results[0]['artifacts']) == 5
        if before is not None:
            assert (public.read_bytes(), public.stat().st_ino, public.stat().st_mtime_ns) == before
    else:
        assert result.completed == 0 and not (e.outputs / 'transcript').exists()


def test_same_byte_foreign_output_directory_cannot_adopt_owned_sealed_stage(environment, monkeypatch):
    e = environment
    identity = e.submit()
    intent = crash(e, identity, 'before-publish')
    stage = e.outputs / intent['publication']['stage_name']
    target = e.outputs / 'transcript'
    shutil.copytree(stage, target)
    foreign_identity = target.stat().st_ino
    monkeypatch.setattr(domain.TranscriptionService, 'run', no_inference)
    result = e.service.resume(identity)
    assert result.status == 'attention'
    assert target.stat().st_ino == foreign_identity and stage.is_dir()
    assert len(tuple(target.iterdir())) == 5


def test_changed_partial_export_is_refused_without_overwrite_or_worker_replay(environment, monkeypatch):
    e = environment
    identity = e.submit()
    intent = crash(e, identity, 'partial-copy')
    target = e.outputs / intent['publication']['stage_name'] / 'transcript.txt'
    foreign = b'Changed independently after the interrupted copy.'
    target.write_bytes(foreign)
    monkeypatch.setattr(domain.TranscriptionService, 'run', no_inference)
    result = e.service.resume(identity)
    assert result.status == 'attention' and target.read_bytes() == foreign
    assert not (e.outputs / 'transcript').exists()


def test_changed_parent_after_sealing_cannot_rebind_directory_publication(environment, monkeypatch):
    e = environment
    identity = e.submit()
    crash(e, identity, 'before-publish')
    previous = e.outputs.with_name('previous-output')
    e.outputs.rename(previous)
    e.outputs.mkdir()
    monkeypatch.setattr(domain.TranscriptionService, 'run', no_inference)
    result = e.service.resume(identity)
    assert result.status == 'attention' and 'parent was replaced' in result.message
    assert not tuple(e.outputs.iterdir()) and tuple(previous.iterdir())


def test_stop_immediately_after_complete_publication_preserves_committed_success(environment, monkeypatch):
    from carr_library import file_publication
    e = environment
    stopped = Event()
    original = file_publication.publish_directory_no_replace
    def published(stage, target, *args, **kwargs):
        result = original(stage, target, *args, **kwargs)
        stopped.set()
        return result
    monkeypatch.setattr(file_publication, 'publish_directory_no_replace', published)
    identity = e.submit()
    result = e.service.run(identity, cancelled=stopped.is_set)
    assert stopped.is_set() and result.status == 'complete'
    assert len(result.results[0]['artifacts']) == 5
    monkeypatch.setattr(domain.TranscriptionService, 'run', no_inference)
    assert e.service.resume(identity).status == 'complete'


def test_completed_step_same_size_source_change_refuses_replay_and_preserves_export(environment, monkeypatch):
    e = environment
    identity = e.submit()
    result = e.service.run(identity)
    assert result.status == 'complete', result.message
    exported = (e.outputs / 'transcript' / 'transcript.txt').read_bytes()
    stamp, original = e.source.stat(), e.source.read_bytes()
    e.source.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    os.utime(e.source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    monkeypatch.setattr(domain.TranscriptionService, 'run', no_inference)
    result = e.service.resume(identity)
    assert result.status == 'attention' and result.completed == 0
    assert (e.outputs / 'transcript' / 'transcript.txt').read_bytes() == exported


def test_completed_receipt_cannot_change_source_settings_runtime_or_artifact_binding(environment):
    e = environment
    identity = e.submit()
    assert e.service.run(identity).status == 'complete'
    original = e.service.store.load(identity)
    variants = []
    for field in ('run_id', 'profile_sha256', 'runtime_manifest_sha256', 'model_sha256'):
        value = deepcopy(original)
        value['steps'][0]['result']['transcription_receipt'][field] = (
            'f' * 32 if field == 'run_id' else 'f' * 64)
        variants.append(value)
    value = deepcopy(original)
    value['steps'][0]['result']['transcription_receipt']['settings']['language'] = 'uk'
    variants.append(value)
    value = deepcopy(original)
    value['steps'][0]['result']['artifacts'][0]['size'] += 1
    variants.append(value)
    for forged in variants:
        with pytest.raises(ValueError):
            validate_job(forged)


def test_restore_and_rebackup_keep_retained_transcripts_without_external_paths_or_launch_authority(
        environment, tmp_path, monkeypatch):
    e = environment
    identity = e.submit()
    result = e.service.run(identity)
    assert result.status == 'complete', result.message
    receipt = result.results[0]['transcription_receipt']
    saved = e.service.store.path(identity).read_bytes()
    backup = tmp_path / 'backup'
    digest = create_backup(e.app, backup)
    verify_backup(backup, digest)
    shutil.rmtree(e.root / 'runtime')
    shutil.rmtree(e.root / 'input')
    shutil.rmtree(e.outputs)
    monkeypatch.setattr(domain.TranscriptionService, 'run', no_inference)
    monkeypatch.setattr(domain, 'review_runtime', no_inference)
    monkeypatch.setattr(EngineProfileStore, 'admit', no_inference)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    restored_app = LibraryApp(restored)
    reopened = AssistantService(restored_app)
    assert reopened.store.path(identity).read_bytes() == saved
    assert reopened.get(identity).status == 'attention'
    with pytest.raises(PermissionError, match='another data directory'):
        reopened.resume(identity)
    accepted = domain.TranscriptionService(restored).completed(receipt['job_id'])
    assert accepted['run_id'] == receipt['run_id'] and accepted['historical'] is True
    assert accepted['runtime_current_verified'] is False
    second = tmp_path / 'second-backup'
    second_digest = create_backup(restored_app, second)
    verify_backup(second, second_digest)
    assert not (e.root / 'runtime').exists() and not e.outputs.exists()


@pytest.mark.parametrize('field', ['definition_sha256', 'model_sha256',
                                   'runtime_manifest_sha256', 'source_bytes'])
def test_backup_rejects_shape_valid_assistant_binding_to_different_managed_definition(
        environment, tmp_path, monkeypatch, field):
    e = environment
    identity = e.submit()
    def interrupted(*args, **kwargs):
        raise InterruptedError('Synthetic interruption before inference.')
    monkeypatch.setattr(domain.TranscriptionService, 'run', interrupted)
    assert e.service.run(identity).status == 'paused'
    record = e.service.store.load(identity)
    value = record['steps'][0]['intent']['transcription']
    if field == 'definition_sha256':
        value['prepared'][field] = 'f' * 64
    elif field == 'source_bytes':
        value['review'][field] += 1
    else:
        value['review'][field] = 'f' * 64
    # Structurally valid input must still fail retained producer cross-binding.
    validate_job(record)
    e.service.store.save(record)
    with pytest.raises((ValueError, PermissionError)):
        create_backup(e.app, tmp_path / 'refused')
    assert not (tmp_path / 'refused' / 'manifest.json').exists()
