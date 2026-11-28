"""Actual background review/state tests; native wx/NVDA acceptance is separate."""
from copy import deepcopy
import json
from queue import Queue
import threading

import pytest

from carr_library.durable_state import atomic_json
from carr_library.runtime_settings import load_settings, settings_path
from carr_library.ui import engine_settings


def restored_document():
    return {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
            'voice_helpers': [], 'music_models': [], 'fluidsynth': None,
            'restore_requires_revalidation': True}


def review_fixture(tmp_path):
    state = tmp_path / 'state'
    atomic_json(settings_path(state), restored_document())
    callbacks = Queue()
    controller = engine_settings.EngineSettingsReview(state, dispatch=callbacks.put)
    return state, callbacks, controller


def test_review_returns_exact_published_settings_to_the_ui_dispatcher(tmp_path):
    state, callbacks, controller = review_fixture(tmp_path)
    completed = []
    try:
        future = controller.review(complete=lambda value, error: completed.append((value, error)))
        saved = future.result(timeout=5)
        assert completed == [], 'The worker must not directly call the UI completion handler.'
        assert controller.busy
        callbacks.get(timeout=5)()
        assert completed == [(saved, None)]
        assert controller.settings == saved == load_settings(state)
        assert controller.settings['restore_requires_revalidation'] is False
        assert not controller.busy
    finally:
        controller.close()


def test_cancel_is_nonblocking_and_stops_real_publication_after_background_work(tmp_path, monkeypatch):
    state, callbacks, controller = review_fixture(tmp_path)
    before = settings_path(state).read_bytes()
    entered, release = threading.Event(), threading.Event()
    worker_threads, completed = [], []
    original = engine_settings.save_settings

    def held_review(root, value, *, cancelled):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5), 'The test must release its background review barrier.'
        return original(root, value, cancelled=cancelled)

    monkeypatch.setattr(engine_settings, 'save_settings', held_review)
    try:
        future = controller.review(complete=lambda value, error: completed.append((value, error)))
        assert entered.wait(5)
        assert len(worker_threads) == 1
        assert worker_threads[0] != threading.get_ident()
        assert not future.done()
        assert controller.cancel() is True
        with pytest.raises(ValueError, match='already running'):
            controller.review(complete=lambda *args: None)
        release.set()
        with pytest.raises(InterruptedError, match='cancelled'):
            future.result(timeout=5)
        callbacks.get(timeout=5)()
        assert len(completed) == 1 and isinstance(completed[0][1], InterruptedError)
        assert not controller.busy
        assert settings_path(state).read_bytes() == before
    finally:
        release.set()
        controller.close()


def test_close_returns_while_review_is_running_and_detaches_late_ui_callbacks(tmp_path, monkeypatch):
    state, callbacks, controller = review_fixture(tmp_path)
    before = settings_path(state).read_bytes()
    entered, release = threading.Event(), threading.Event()
    original = engine_settings.save_settings
    completed = []
    def held_review(root, value, *, cancelled):
        entered.set()
        assert release.wait(5)
        return original(root, value, cancelled=cancelled)
    monkeypatch.setattr(engine_settings, 'save_settings', held_review)
    future = controller.review(complete=lambda *args: completed.append(args))
    try:
        assert entered.wait(5)
        controller.close()
        assert not future.done(), 'Close must not block waiting for the current validation.'
        release.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)
        assert callbacks.empty() and completed == []
        assert settings_path(state).read_bytes() == before
    finally:
        release.set()
        controller.close()


def test_review_uses_a_snapshot_of_the_selected_settings(tmp_path, monkeypatch):
    state, callbacks, controller = review_fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = engine_settings.save_settings
    candidate = deepcopy(controller.settings)
    def held_review(root, value, *, cancelled):
        entered.set()
        assert release.wait(5)
        return original(root, value, cancelled=cancelled)
    monkeypatch.setattr(engine_settings, 'save_settings', held_review)
    try:
        future = controller.review(candidate, complete=lambda *args: None)
        assert entered.wait(5)
        candidate['voice_helpers'].append({'unreviewed': 'later edit'})
        release.set()
        saved = future.result(timeout=5)
        callbacks.get(timeout=5)()
        assert saved['voice_helpers'] == []
        assert load_settings(state) == saved
    finally:
        release.set()
        controller.close()


def test_registration_preview_reads_metadata_without_enabling_any_helper(tmp_path, monkeypatch):
    state, callbacks, controller = review_fixture(tmp_path)
    selected = tmp_path / 'registration.json'
    declaration = {'executable': str(tmp_path / 'not-enabled.exe'), 'engine_name': 'Preview only',
                   'sha256': '0' * 64}
    selected.write_text(json.dumps(declaration), encoding='utf-8')
    before = settings_path(state).read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('Metadata preview must not activate or save a helper.')
    monkeypatch.setattr(engine_settings, 'save_settings', forbidden)
    completed = []
    try:
        future = controller.load_registration(selected, complete=lambda value, error: completed.append((value, error)))
        assert future.result(timeout=5) == declaration
        callbacks.get(timeout=5)()
        assert completed == [(declaration, None)]
        assert settings_path(state).read_bytes() == before
        assert controller.settings == restored_document()
    finally:
        controller.close()


def test_restored_status_explains_the_review_barrier_and_neural_platform_limit():
    message = engine_settings.describe_engine_settings(restored_document())
    assert 'activation is blocked' in message
    assert 'review and revalidate' in message
    assert 'Windows inference is unavailable' in message

def test_large_file_hash_checks_cancellation_between_real_read_blocks(tmp_path):
    from carr_library.speech._io import digest
    selected = tmp_path / 'model.bin'
    selected.write_bytes(b'model fixture' * 250_000)
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) >= 3
    with pytest.raises(InterruptedError, match='cancelled'):
        digest(selected, cancelled=cancelled)
    assert len(calls) == 3
    assert selected.stat().st_size == 3_250_000


def test_cancel_after_atomic_publication_keeps_the_completed_save(tmp_path, monkeypatch):
    state, callbacks, controller = review_fixture(tmp_path)
    original = engine_settings.save_settings
    submitted = threading.Event()
    def completed_save(root, value, *, cancelled):
        result = original(root, value, cancelled=cancelled)
        assert submitted.wait(5)
        assert controller.cancel() is True
        return result
    monkeypatch.setattr(engine_settings, 'save_settings', completed_save)
    completed = []
    try:
        future = controller.review(complete=lambda value, error: completed.append((value, error)))
        submitted.set()
        saved = future.result(timeout=5)
        callbacks.get(timeout=5)()
        assert completed == [(saved, None)]
        assert controller.settings == saved == load_settings(state)
        assert saved['restore_requires_revalidation'] is False
    finally:
        submitted.set()
        controller.close()
