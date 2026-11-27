"""One real isolated worker owner, two exact saved codecs.

All recognition peers/models are authored synthetic fixtures. These cases prove
orchestration, retained ownership and reading provenance, not acoustic quality.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import runpy
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import process
from carr_library.audio import transcription as legacy
from carr_library.audio.models import AudioBudget, AudioCleanupPending
from carr_library.audio.windows_job import JobProcess
from carr_library.durable_state import read_json
from carr_library.operations import OperationFacade, ResourceBroker, ResourceLimits
from carr_library.transcription import TranscriptionService
from carr_library.transcription.reading import prepare_reading_copy
from carr_library.speech.session import ReaderDraftStore

_fixture = runpy.run_path(str(Path(__file__).with_name('fixture_runtime.py')))


@pytest.fixture
def selected(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('One transcription owner')
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    old_root = tmp_path / 'old-fixture'
    old_root.mkdir()
    authored = runpy.run_path(str(Path(__file__).parents[1] / 'audio' / 'test_supplied_transcription.py'))
    manifest = authored['installation'].__wrapped__(old_root, monkeypatch)
    budget = AudioBudget(timeout=10, reserve_disk_bytes=0, max_output_bytes=4 * 1024**2)
    service = TranscriptionService(app.root / 'state',
        legacy.SuppliedWhisperRuntime(manifest), broker=broker, budget=budget, stop_grace=.1)
    runtime = _fixture['make_runtime'](tmp_path / 'profile-runtime')
    profile = _fixture['register'](service, runtime)
    source = tmp_path / 'source.wav'
    source.write_text('Actual authored protocol input.', encoding='utf-8')
    return app, service, runtime, profile, source


def test_both_saved_codecs_use_the_same_real_isolated_runner_without_rewriting_shape(selected, monkeypatch):
    app, service, runtime, profile, source = selected
    prior = source.read_bytes()
    old = service.create([source], settings=legacy.TranscriptionSettings(language='uk'))
    current = service.create_job(profile['id'], [source], settings={'language': 'uk'})
    old_path = service.root / old / 'job.json'
    current_path = service.jobs_root / current['id'] / 'job.json'
    before_old, before_current = read_json(old_path), read_json(current_path)
    calls, execute = [], legacy.run_process
    def actual(argv, **options):
        calls.append((tuple(argv), Path(options['cwd']), options.get('owned_tree')))
        return execute(argv, **options)
    monkeypatch.setattr(legacy, 'run_process', actual)
    assert service.run(old)['status'] == 'complete'
    assert service.run(current['id'])['status'] == 'complete'
    assert len(calls) == 2
    assert all(row[0][1:6] == ('-I', '-S', '-B', '-X', 'utf8') for row in calls)
    assert calls[1][1] == current_path.parent / 'worker' and calls[1][2] is True
    assert calls[0][1] != calls[1][1]
    saved_old, saved_current = read_json(old_path), read_json(current_path)
    assert saved_old['schema'] == saved_current['schema']
    assert len(old) == 64 and len(current['id']) == 32
    assert saved_old['request'] == before_old['request'] and 'profile_id' not in saved_old
    assert saved_current['plan_fingerprint'] == before_current['plan_fingerprint']
    assert 'request' not in saved_current and saved_current['profile_id'] == profile['id']
    assert source.read_bytes() == prior
    assert {row['id'] for row in service.list_jobs()} == {old, current['id']}
    assert [row['id'] for row in service.list_profile_jobs()] == [current['id']]
    assert not service.broker.snapshot()['leases']
    # A completed result is reused through its own validator, never re-executed.
    monkeypatch.setattr(legacy, 'run_process', lambda *a, **k: pytest.fail('Completed results must not execute again.'))
    assert service.run(old)['status'] == 'complete'
    assert service.run(current['id'])['status'] == 'complete'


@pytest.mark.parametrize('attempt', ('socket', 'outside-write'))
def test_profile_worker_is_subject_to_the_existing_isolated_execution_guards(selected, tmp_path, attempt):
    app, service, runtime, profile, source = selected
    outside = tmp_path / 'must-not-be-written.txt'
    options = {'attempt_network': True} if attempt == 'socket' else {'outside_write': str(outside)}
    runtime.options.write_text(json.dumps(options), encoding='utf-8')
    job = service.create_job(profile['id'], [source])
    result = service.run(job['id'])
    assert result['status'] == 'failed' and result['done'] == 0 and not result['results']
    assert not outside.exists()
    assert not service.broker.snapshot()['leases']
    # socket.socket() is denied by the bootstrap before any connect or listen;
    # this test never contacts an external or local network endpoint.


def test_profile_job_keeps_real_process_owner_run_lock_and_lease_until_cleanup_confirms(selected, monkeypatch):
    from filelock import FileLock, Timeout
    app, service, runtime, profile, source = selected
    job = service.create_job(profile['id'], [source])
    permitted, released = threading.Event(), threading.Event()
    target, name = (JobProcess, 'close') if os.name == 'nt' else (process, '_wait_posix_group')
    close = getattr(target, name)
    attempts, owner = [], None
    def uncertain(*args, **kwargs):
        attempts.append(args[0])
        if not permitted.is_set():
            raise OSError('Authored native cleanup denial after the real protocol child.')
        return close(*args, **kwargs)
    monkeypatch.setattr(target, name, uncertain)
    competitor = FileLock(service.jobs_root / job['id'] / '.run.lock', timeout=0)
    try:
        with pytest.raises(AudioCleanupPending) as raised:
            service.run(job['id'])
        owner = raised.value.cleanup_owner
        owner.add_quiescent_callback(released.set)
        assert attempts and not owner.quiescent
        assert service.get_job(job['id'])['status'] == 'interrupted'
        assert service.get_job(job['id'])['recovery_review_required'] is True
        with pytest.raises(Timeout):
            competitor.acquire()
        leases = service.broker.snapshot()['leases']
        assert [row['operation_id'] for row in leases] == ['asr:' + job['id']]
        assert owner.close() is False
        permitted.set()
        assert owner.quiescent_event.wait(5) and released.wait(5)
        assert owner.close() is True
        with competitor:
            pass
        assert not service.broker.snapshot()['leases']
    finally:
        permitted.set()
        if owner is not None:
            owner.close()


def test_dashboard_lists_and_controls_both_exact_paths_without_verifying_external_engines(selected, monkeypatch):
    app, service, runtime, profile, source = selected
    old = service.create([source])
    current = service.create_job(profile['id'], [source])
    facade = OperationFacade(app, broker=service.broker, transcription=service)
    def forbidden(*args, **kwargs):
        pytest.fail('A metadata-only dashboard must not verify or execute an external engine.')
    monkeypatch.setattr(service, '_verify_profile', forbidden)
    monkeypatch.setattr(service.runtime, 'verify', forbidden)
    monkeypatch.setattr(legacy, 'run_process', forbidden)
    rows = {row.id: row for row in facade.list() if row.kind == 'asr'}
    assert set(rows) == {old, current['id']}
    assert rows[old].checkpoint == 'state/transcription/' + old + '/job.json'
    assert rows[current['id']].checkpoint == 'state/transcription/jobs/' + current['id'] + '/job.json'
    assert rows[old].controls == rows[current['id']].controls == ('pause', 'cancel')
    facade.control('asr', old, 'pause')
    facade.control('asr', current['id'], 'cancel')
    assert read_json(service.root / old / 'control.json') == {'command': 'pause'}
    assert read_json(service.jobs_root / current['id'] / 'control.json') == {'action': 'cancel'}
    assert read_json(service.root / old / 'job.json')['request']['schema'] == 1
    assert read_json(service.jobs_root / current['id'] / 'job.json')['profile_id'] == profile['id']


def test_equal_transcripts_have_separate_recoverable_reading_drafts_and_keep_original_undo(selected, tmp_path, monkeypatch):
    app, service, runtime, profile, source = selected
    second = tmp_path / 'second.wav'
    second.write_bytes(b'Different source; same synthetic recognition words.')
    job = service.create_job(profile['id'], [source, second])
    assert service.run(job['id'])['status'] == 'complete'
    first_id, second_id = [row['id'] for row in job['sources']]
    first = prepare_reading_copy(app.root, service, job['id'], first_id, edited_text='First user correction.')
    other = prepare_reading_copy(app.root, service, job['id'], second_id)
    assert first['slot'] != other['slot'] and not first['recovered'] and not other['recovered']
    first_store = ReaderDraftStore(app.root, slot=first['slot'])
    recovered = first_store.load()
    original = service.read_transcript(job['id'], first_id)['text']
    assert recovered.document.text == 'First user correction.'
    assert recovered.document.undo() and recovered.document.text == original
    assert recovered.document.redo() and recovered.document.text == 'First user correction.'
    assert recovered.document.to_dict().get('source_anchor') is None
    recovered.document.set_text('Later recovery edit')
    recovered = replace(recovered, voice_id='retained-user-voice')
    first_store.save(recovered)
    raw = first_store.path.read_bytes()
    monkeypatch.setattr(service, 'read_transcript',
        lambda *a, **k: pytest.fail('An existing derivative must win over reseeding from recognition.'))
    again = prepare_reading_copy(app.root, service, job['id'], first_id, edited_text='Must not replace saved edits.')
    assert again == {'slot': first['slot'], 'recovered': True}
    assert first_store.path.read_bytes() == raw
    assert first_store.load().voice_id == 'retained-user-voice'
    assert ReaderDraftStore(app.root, slot=other['slot']).load().document.text == original


@pytest.mark.parametrize('command', ('pause', 'cancel'))
def test_manifest_cli_keeps_the_original_control_receipt_contract(selected, capsys, command):
    from carr_library.transcription_cli import main
    app, service, runtime, profile, source = selected
    identity = service.create([source])
    before = service.load(identity)['status']
    assert main(['--data-dir', str(app.root), command, identity]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {'id': identity, 'control_requested': command, 'status': before}
    assert read_json(service.root / identity / 'control.json') == {'command': command}


@pytest.mark.parametrize('boundary', ('receipt', 'edit'))
def test_saved_reading_value_is_parsed_only_from_the_exact_digest_bound_bytes(selected, monkeypatch, boundary):
    from carr_library.transcription import service as module
    app, service, runtime, profile, source = selected
    job = service.create_job(profile['id'], [source])
    complete = service.run(job['id'])
    assert complete['status'] == 'complete'
    source_id = job['sources'][0]['id']
    original = service.read_transcript(job['id'], source_id)
    if boundary == 'edit':
        service.save_edit(job['id'], source_id, 'Saved independent correction.')
        state = service._load(job['id'])
        path = service.jobs_root / job['id'] / state['edits'][source_id]['path']
        expected_text = 'Saved independent correction.'
        original_digest = module.digest_file
        def checked_then_replaced(target, **kwargs):
            value = original_digest(target, **kwargs)
            if Path(target) == path:
                replacement = json.loads(path.read_text(encoding='utf-8'))
                replacement['text'] = 'Unbound replacement text.'
                path.write_text(json.dumps(replacement), encoding='utf-8')
            return value
        monkeypatch.setattr(module, 'digest_file', checked_then_replaced)
    else:
        path = service.jobs_root / job['id'] / complete['results'][0]['receipt']
        expected_text = original['text']
        original_identity = module.file_identity
        def checked_then_replaced(target, **kwargs):
            value = original_identity(target, **kwargs)
            if Path(target) == path:
                replacement = json.loads(path.read_text(encoding='utf-8'))
                replacement['result']['text'] = 'Unbound replacement text.'
                path.write_text(json.dumps(replacement), encoding='utf-8')
            return value
        monkeypatch.setattr(module, 'file_identity', checked_then_replaced)
    before = path.read_bytes()
    # The old split hash/read path admitted the replacement. The captured-byte
    # reader never reopens through either split validation boundary.
    assert service.read_transcript(job['id'], source_id, edited=boundary == 'edit')['text'] == expected_text
    assert path.read_bytes() == before


@pytest.mark.parametrize('action', ('read', 'completed-run'))
def test_cancel_reaches_actual_retained_source_and_receipt_reads(selected, monkeypatch, action):
    from carr_library.transcription import service as module
    from carr_library.transcription import runtime as inspection
    app, service, runtime, profile, source = selected
    job = service.create_job(profile['id'], [source])
    assert service.run(job['id'])['status'] == 'complete'
    before = (service.jobs_root / job['id'] / 'job.json').read_bytes()
    real_open = inspection._open_selected_file
    opened, cancelled = [], threading.Event()
    def signal_after_open(path):
        stream = real_open(path)
        opened.append(Path(path))
        cancelled.set()
        return stream
    monkeypatch.setattr(inspection, '_open_selected_file', signal_after_open)
    with pytest.raises(InterruptedError):
        if action == 'read':
            service.read_transcript(job['id'], cancel=cancelled.is_set)
        else:
            service.run(job['id'], cancel=cancelled.is_set)
    assert opened, 'Cancellation was delivered only after an actual checked descriptor opened.'
    assert (service.jobs_root / job['id'] / 'job.json').read_bytes() == before
    assert not service.broker.snapshot()['leases']
