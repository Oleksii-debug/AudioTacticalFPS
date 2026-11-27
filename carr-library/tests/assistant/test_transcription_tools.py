"""Current assistant dispatch through a real synthetic owned ASR protocol child."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import time

import pytest

from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import transcription_tools as tools
from carr_library.assistant.models import TOOLS
from carr_library.assistant.validation import validate_job
from carr_library.operations import ResourceBroker
from carr_library.runtime_settings import EngineProfileStore
from carr_library import transcription_service as domain
from tests.assistant.transcription_fixture import make_environment, protocol_interpreter


@pytest.fixture
def environment(tmp_path):
    with protocol_interpreter():
        yield make_environment(tmp_path / 'selected')


def forbidden(*args, **kwargs):
    raise AssertionError('This reviewed boundary must not dispatch another worker.')


@pytest.mark.parametrize('language', ['en', 'uk'])
def test_guided_transcription_shows_all_effective_settings_without_creating_jobs(environment, language):
    e = environment
    instruction = (f'Transcribe "{e.source}" as "transcript"'
                   if language == 'en' else f'Транскрибуй "{e.source}" як "transcript"')
    plan = e.service.preview(instruction, e.scope)
    assert plan.steps[0].tool == 'audio.transcribe'
    assert plan.steps[0].arguments['source_sha256'] == sha256(e.source.read_bytes()).hexdigest()
    assert plan.steps[0].arguments['settings'] == tools._settings({})
    assert e.service.jobs() == () and not (e.app.root / 'transcription-jobs').exists()
    assert not tuple(e.outputs.iterdir())


@pytest.mark.parametrize('selection', ['none', 'disabled', 'another-library'])
def test_preview_requires_current_explicit_profile_without_activating_settings(environment, tmp_path, selection):
    e = environment
    if selection == 'none':
        e.service.transcription = None
    elif selection == 'disabled':
        EngineProfileStore(e.app.root).set_enabled(e.profile.id, False)
    else:
        other = make_environment(tmp_path / 'other')
        e.service.transcription = other.adapter
    before = EngineProfileStore(e.app.root).snapshot()
    with pytest.raises((PermissionError, ValueError)):
        e.service.validate(e.plan())
    assert EngineProfileStore(e.app.root).snapshot() == before
    assert e.service.jobs() == () and not (e.app.root / 'transcription-jobs').exists()


@pytest.mark.parametrize('key', ['profile_id', 'runtime_manifest', 'worker', 'interpreter', 'model_path'])
def test_typed_arguments_cannot_choose_runtime_or_grant_profile_authority(environment, key):
    e = environment
    args = dict(e.plan().steps[0].arguments, **{key: str(e.manifest)})
    with pytest.raises(ValueError):
        e.service.submit(Plan(e.scope, (Step('audio.transcribe', args),)))
    assert e.service.jobs() == () and not (e.app.root / 'transcription-jobs').exists()


@pytest.mark.parametrize('change', ['parent-name', 'hidden-name', 'drive-name',
                                    'unknown-setting', 'boolean-beam', 'no-output'])
def test_invalid_settings_and_output_names_refuse_before_dispatch(environment, change):
    e = environment
    args = deepcopy(e.plan().steps[0].arguments)
    if change == 'parent-name':
        args['name'] = '../outside'
    elif change == 'hidden-name':
        args['name'] = '.hidden'
    elif change == 'drive-name':
        args['name'] = 'C:outside'
    elif change == 'unknown-setting':
        args['settings']['remote_fallback'] = True
    elif change == 'boolean-beam':
        args['settings']['beam_size'] = True
    else:
        for kind in tools.FORMATS:
            args['settings']['output_' + kind] = False
    with pytest.raises((ValueError, PermissionError)):
        e.service.submit(Plan(e.scope, (Step('audio.transcribe', args),)))
    assert e.service.jobs() == () and not (e.app.root / 'transcription-jobs').exists()


def test_source_outside_selected_folder_and_private_library_are_refused(environment):
    e = environment
    outside = e.root / 'unselected.mp3'
    outside.write_bytes(e.source.read_bytes())
    args = dict(e.plan().steps[0].arguments, path=str(outside))
    with pytest.raises((ValueError, PermissionError)):
        e.service.validate(Plan(e.scope, (Step('audio.transcribe', args),)))
    private = e.app.root / 'private.mp3'
    private.write_bytes(e.source.read_bytes())
    args['path'] = str(private)
    scope = replace(e.scope, input_roots=(e.app.root,))
    with pytest.raises((ValueError, PermissionError), match='private library'):
        e.service.validate(Plan(scope, (Step('audio.transcribe', args),)))
    assert e.service.jobs() == ()


def test_export_budget_includes_all_formats_and_provenance(environment):
    e = environment
    scope = replace(e.scope, max_files=4)
    with pytest.raises(ValueError, match='provenance'):
        e.service.submit(Plan(scope, e.plan().steps))
    assert e.service.jobs() == ()


@pytest.mark.parametrize('name', ['transcript', 'TRANSCRIPT'])
def test_overlapping_transcription_directories_refuse_whole_plan_before_preparation(environment, name):
    e = environment
    with pytest.raises(ValueError, match='overlap'):
        e.service.submit(Plan(e.scope, (e.plan().steps[0], e.plan(name=name).steps[0])))
    assert e.service.jobs() == () and not (e.app.root / 'transcription-jobs').exists()


def test_actual_protocol_child_exports_all_formats_and_retains_independent_owned_inputs(environment, monkeypatch):
    e = environment
    original = e.source.read_bytes()
    identity = e.submit()
    result = e.service.run(identity)
    assert result.status == 'complete', result.message
    row, = result.results
    target = e.outputs / 'transcript'
    assert set(path.name for path in target.iterdir()) == {
        'transcript.txt', 'transcript.srt', 'transcript.vtt', 'transcript.json', 'provenance.json'}
    receipt = row['transcription_receipt']
    assert receipt['settings'] == e.settings
    assert receipt['source'] == {'path': str(e.source), 'bytes': len(original),
                                 'sha256': sha256(original).hexdigest()}
    job = e.app.root / 'transcription-jobs' / receipt['job_id']
    definition = domain.TranscriptionService(e.app.root).definition(receipt['job_id'])
    retained = job / definition['inputs'][0]['path']
    assert retained.read_bytes() == original and retained.stat().st_ino != e.source.stat().st_ino
    assert json.loads((target / 'provenance.json').read_bytes())['run_id'] == receipt['run_id']
    assert e.source.read_bytes() == original
    before = {path.name: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
              for path in target.iterdir()}
    monkeypatch.setattr(domain.TranscriptionService, 'run', forbidden)
    assert e.service.resume(identity).status == 'complete'
    assert before == {path.name: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                      for path in target.iterdir()}
    assert ResourceBroker.for_app(e.app.root).snapshot()['leases'] == []


def test_actual_empty_transcripts_are_legitimate_silence_and_never_rerun(tmp_path, monkeypatch):
    with protocol_interpreter():
        e = make_environment(tmp_path / 'silence', mode='silence')
        identity = e.submit()
        result = e.service.run(identity)
        assert result.status == 'complete', result.message
        assert all((e.outputs / 'transcript' / ('transcript.' + kind)).read_bytes() == b''
                   for kind in tools.FORMATS)
        monkeypatch.setattr(domain.TranscriptionService, 'run', forbidden)
        assert e.service.resume(identity).status == 'complete'


@pytest.mark.parametrize('mode', ['zero_only', 'nonzero', 'tamper'])
def test_actual_unaccepted_child_outputs_are_not_published_or_replayed(tmp_path, monkeypatch, mode):
    with protocol_interpreter():
        e = make_environment(tmp_path / mode, mode=mode)
        identity = e.submit()
        result = e.service.run(identity)
        assert result.status in ('failed', 'attention'), result
        saved = e.service.store.load(identity)['steps'][0]['intent']['transcription']
        assert saved['run_dispatched'] and saved['completion'] is None
        assert not (e.outputs / 'transcript').exists()
        monkeypatch.setattr(domain.TranscriptionService, 'run', forbidden)
        retried = e.service.resume(identity)
        assert retried.status == 'attention' and 'not be replayed' in retried.message
        assert ResourceBroker.for_app(e.app.root).snapshot()['leases'] == []


def test_selected_owner_revocation_cancels_actual_domain_child_and_releases_lease(tmp_path):
    current = {'active': True}
    with protocol_interpreter():
        e = make_environment(tmp_path / 'wait', mode='wait', current=lambda: current['active'])
        identity = e.submit()
        observed = []
        def progress(done, total, message):
            if 'local transcription:' in message and not observed:
                observed.append(message)
                current['active'] = False
        started = time.monotonic()
        result = e.service.run(identity, progress=progress)
        assert observed and result.status == 'paused', result
        assert time.monotonic() - started < 12
        saved = e.service.store.load(identity)['steps'][0]['intent']['transcription']
        execution = json.loads((e.app.root / 'transcription-jobs' /
                                saved['prepared']['job_id'] / 'execution.json').read_bytes())
        assert execution['state'] == 'cancelled'
        assert not (e.outputs / 'transcript').exists()
        assert ResourceBroker.for_app(e.app.root).snapshot()['leases'] == []


def test_profile_disabled_after_submission_blocks_before_owned_preparation(environment, monkeypatch):
    e = environment
    identity = e.submit()
    EngineProfileStore(e.app.root).set_enabled(e.profile.id, False)
    monkeypatch.setattr(domain.TranscriptionService, 'prepare', forbidden)
    result = e.service.run(identity)
    assert result.status in ('failed', 'attention') and result.completed == 0
    assert not (e.app.root / 'transcription-jobs').exists()


@pytest.mark.parametrize('phase', ['after-preview', 'after-submit'])
def test_same_size_and_mtime_source_change_refuses_before_inference(environment, monkeypatch, phase):
    e = environment
    plan = e.plan()
    identity = e.service.submit(plan) if phase == 'after-submit' else None
    stamp = e.source.stat()
    raw = e.source.read_bytes()
    e.source.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    os.utime(e.source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    monkeypatch.setattr(domain.TranscriptionService, 'prepare', forbidden)
    if identity is None:
        with pytest.raises(PermissionError, match='checksum'):
            e.service.submit(plan)
    else:
        result = e.service.run(identity)
        assert result.status == 'attention' and 'input changed' in result.message
    assert not (e.outputs / 'transcript').exists()


def test_new_output_directory_after_preview_preserves_foreign_data(environment, monkeypatch):
    e = environment
    identity = e.submit()
    target = e.outputs / 'transcript'
    target.mkdir()
    (target / 'foreign.txt').write_bytes(b'Keep this independently created file.')
    monkeypatch.setattr(domain.TranscriptionService, 'prepare', forbidden)
    result = e.service.run(identity)
    assert result.status == 'attention' and result.completed == 0
    assert (target / 'foreign.txt').read_bytes() == b'Keep this independently created file.'


def test_pure_pending_checkpoint_validation_does_not_open_saved_paths_or_profiles(environment, monkeypatch):
    e = environment
    identity = e.submit()
    record = e.service.store.load(identity)
    with monkeypatch.context() as guard:
        guard.setattr(Path, 'open', forbidden)
        guard.setattr(Path, 'lstat', forbidden)
        guard.setattr(EngineProfileStore, 'snapshot', forbidden)
        assert validate_job(record) == record


def test_all_seventeen_tools_are_explicit_with_no_dynamic_runtime_names():
    assert len(TOOLS) == 17 and 'audio.transcribe' in TOOLS
    assert not {'shell', 'python', 'runtime.install', 'profile.enable'} & TOOLS


@pytest.mark.parametrize('changed', ['manifest', 'worker', 'model'])
def test_changed_pinned_runtime_bytes_after_submit_refuse_before_preparation(environment, monkeypatch, changed):
    e = environment
    identity = e.submit()
    target = {'manifest': e.manifest, 'worker': e.manifest.parent / 'worker.py',
              'model': e.manifest.parent / 'models/small/model.bin'}[changed]
    target.write_bytes(target.read_bytes() + b' ')
    monkeypatch.setattr(domain.TranscriptionService, 'prepare', forbidden)
    result = e.service.run(identity)
    assert result.status in ('failed', 'attention') and result.completed == 0
    value = e.service.store.load(identity)['steps'][0]['intent']['transcription']
    assert value['prepare_dispatched'] is False and value['prepared'] is None
    assert not (e.app.root / 'transcription-jobs').exists()
    assert not tuple(e.outputs.iterdir())
