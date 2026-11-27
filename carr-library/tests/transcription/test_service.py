"""Actual local subprocess/filesystem acceptance with a controlled non-ASR backend."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import time

import pytest

from carr_library.transcription import (TranscriptionService, TranscriptionOptions,
    TranscriptionBudget, TranscriptionControl, TranscriptionRuntime)
from carr_library.transcription.files import canonical, read_json


def prepared(setup, mode='valid', **kwargs):
    service, workspace, profile, source = setup(mode)
    job = service.prepare(workspace, profile, [source], **kwargs)
    return service, job, profile, source


def run(service, job, **kwargs):
    return service.run(job.id, approved_request_sha256=job.request_sha256, **kwargs)


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('The real controlled child did not reach its expected state.')
        time.sleep(.01)


def test_actual_protocol_unicode_outputs_and_receipt_never_grant_quote_authority(setup):
    options = TranscriptionOptions(language='uk', formats=('txt', 'srt', 'vtt', 'json'))
    service, job, profile, source = prepared(setup, options=options)
    original = source.read_bytes()
    progress = []
    result = run(service, job, on_progress=progress.append)
    assert result.quotation_authority is False and len(result.outputs) == 4
    assert progress[-1].state == 'completed'
    assert all(event.state != 'completed' for event in progress[:-1])
    assert source.read_bytes() == original
    assert service.verify(job.id, expected_receipt_sha256=result.receipt_sha256) == result
    assert service.read_text(result) == ((source.name, 'Контрольний текст — synthetic protocol, not ASR.'),)
    request, _ = read_json(service.jobs_root / job.id / 'request.json', 1024**2)
    assert request['sources'][0]['sha256'] == sha256(original).hexdigest()
    config = json.loads((service.jobs_root / job.id / 'job_config.json').read_text())
    assert config['folders'] == [str(service.jobs_root / job.id / 'inputs')]
    assert config['results_root'] == str(service.jobs_root / job.id / 'results')
    assert str(source) not in json.dumps(config)


def test_original_changes_after_snapshot_do_not_change_approved_audio(setup):
    service, job, _, source = prepared(setup)
    captured = source.read_bytes()
    source.write_bytes(b'new user-owned version')
    result = run(service, job)
    snapshot = service.jobs_root / job.id / 'inputs' / 'source-00001.wav'
    assert snapshot.read_bytes() == captured
    assert source.read_bytes() == b'new user-owned version'
    assert result.sources == 1


@pytest.mark.parametrize('mode', ['owner', 'settings', 'language', 'nan', 'duration', 'timestamp',
                                  'text', 'proof', 'missing', 'extra', 'count', 'input'])
def test_bad_outputs_cannot_publish_a_completed_receipt(setup, mode):
    service, job, _, source = prepared(setup, mode, options=TranscriptionOptions(formats=('txt', 'srt', 'vtt')))
    original = source.read_bytes()
    with pytest.raises((ValueError, OSError, RuntimeError)):
        run(service, job)
    assert not (service.jobs_root / job.id / 'receipt.json').exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize('relative', ['worker.py', 'models/controlled-fixture/model.bin',
                                    'models/controlled-fixture/tokenizer.json'])
def test_changed_pinned_runtime_fails_before_worker_launch(setup, relative):
    service, job, profile, _ = prepared(setup)
    path = profile.parent / relative
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='Checksum|changed'):
        run(service, job)
    assert not (service.jobs_root / job.id / 'fixture.started').exists()


def test_changed_request_requires_new_explicit_review(setup):
    service, job, _, _ = prepared(setup)
    request_path = service.jobs_root / job.id / 'request.json'
    value = json.loads(request_path.read_bytes())
    value['options']['task'] = 'translate'
    request_path.write_bytes(canonical(value))
    with pytest.raises(PermissionError, match='request changed'):
        run(service, job)
    reviewed, request = service.review(job.id)
    assert reviewed.request_sha256 != job.request_sha256 and request['options']['task'] == 'translate'
    result = run(service, reviewed)
    assert service.read_text(result)[0][1] == 'Controlled English translation.'


def test_no_plain_txt_or_self_asserted_status_reuse(setup):
    service, job, _, _ = prepared(setup)
    directory = service.jobs_root / job.id
    output = directory / 'results' / 'inputs'
    output.mkdir()
    (output / 'source-00001.txt').write_text('Unrelated old transcript', encoding='utf-8')
    with pytest.raises(ValueError, match='no ownership proof'):
        run(service, job)
    assert (output / 'source-00001.txt').read_text('utf-8') == 'Unrelated old transcript'
    assert not (directory / 'fixture.started').exists()


def test_pause_resume_and_stop_keep_owned_checkpoints(setup):
    service, job, _, source = prepared(setup, 'slow')
    control = TranscriptionControl()
    control.pause()
    directory = service.jobs_root / job.id
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, service, job, control=control)
        def paused():
            path = directory / 'job_status.json'
            try:
                return json.loads(path.read_bytes())['state'] == 'paused'
            except (OSError, ValueError, KeyError):
                return False
        wait_for(paused)
        assert not future.done() and not (directory / 'receipt.json').exists()
        control.resume()
        control.stop()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)
    assert not (directory / 'receipt.json').exists()
    assert (directory / 'controlled.partial.json').exists()
    source.unlink()  # Resumption reads the approved owned snapshot, not an old external locator.
    reopened = TranscriptionService(service.app)
    reviewed, _ = reopened.review(job.id)
    result = run(reopened, reviewed)
    assert result.sources == 1 and result.quotation_authority is False


def test_unresponsive_native_step_is_stopped_within_bound_and_can_unlock(setup):
    service, job, _, _ = prepared(setup, 'stuck',
        budget=TranscriptionBudget(max_seconds=5, stop_grace_seconds=.05))
    control = TranscriptionControl()
    directory = service.jobs_root / job.id
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, service, job, control=control)
        wait_for(lambda: (directory / 'fixture.started').exists())
        start = time.monotonic()
        control.stop()
        with pytest.raises(InterruptedError):
            future.result(timeout=6)
        assert time.monotonic() - start < 6
    from carr_library.audio.jobs import project_lock
    with project_lock(directory / 'worker-lock'):
        pass
    assert not (directory / 'receipt.json').exists()


def test_whole_run_deadline_stops_actual_child_before_receipt(setup):
    service, job, _, _ = prepared(setup, 'stuck',
        budget=TranscriptionBudget(max_seconds=3, stop_grace_seconds=.05))
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        run(service, job)
    assert time.monotonic() - start < 8
    directory = service.jobs_root / job.id
    assert (directory / 'fixture.started').exists()
    assert not (directory / 'receipt.json').exists()


@pytest.mark.parametrize('mode', ['grow', 'log'])
def test_actual_worker_disk_or_log_budget_violation_never_completes(setup, mode):
    budget = TranscriptionBudget(max_input_bytes=1000, max_job_bytes=1024**2)
    service, job, _, _ = prepared(setup, mode, budget=budget)
    with pytest.raises(ValueError, match='budget'):
        run(service, job)
    assert not (service.jobs_root / job.id / 'receipt.json').exists()


@pytest.mark.parametrize('change', ['cancel', 'output', 'root'])
def test_late_change_before_verification_cannot_publish_completion(setup, change):
    service, job, _, _ = prepared(setup)
    root = service.app.root
    directory = service.jobs_root / job.id
    stopped = False
    def progress(event):
        nonlocal stopped
        if event.phase.startswith('Verifying source snapshots'):
            if change == 'cancel':
                stopped = True
            elif change == 'root':
                service.app.root = root.parent / 'different'
            else:
                (directory / 'results' / 'inputs' / 'source-00001.txt').write_text('changed', encoding='utf-8')
    try:
        with pytest.raises((InterruptedError, ValueError, PermissionError)):
            run(service, job, cancelled=lambda: stopped, on_progress=progress)
    finally:
        service.app.root = root
    assert not (directory / 'receipt.json').exists()


def test_offline_python_audit_and_private_environment_are_actual_subprocess_properties(setup, monkeypatch):
    for key in ('HF_TOKEN', 'PRIVATE_API_KEY', 'HTTPS_PROXY', 'PYTHONPATH'):
        monkeypatch.setenv(key, 'synthetic-value-that-must-not-reach-worker')
    service, job, _, _ = prepared(setup, 'network')
    assert run(service, job).sources == 1
    assert (service.jobs_root / job.id / 'network.denied').read_bytes() == b'yes'


def test_completed_receipt_cannot_be_overwritten_and_tamper_is_detected(setup):
    service, job, _, _ = prepared(setup)
    receipt = run(service, job)
    with pytest.raises(FileExistsError, match='completed receipt'):
        run(service, job)
    path = service.jobs_root / job.id / receipt.outputs[0]
    path.write_bytes(b'edited transcript')
    with pytest.raises(ValueError, match='output changed'):
        service.verify(job.id, expected_receipt_sha256=receipt.receipt_sha256)


def test_completed_callback_failure_does_not_hide_committed_receipt(setup):
    service, job, _, _ = prepared(setup)
    def progress(event):
        if event.state == 'completed':
            raise RuntimeError('Controlled observer failure after publication')
    result = run(service, job, on_progress=progress)
    assert service.verify(job.id, expected_receipt_sha256=result.receipt_sha256) == result
