"""Real host folders, production registries and controlled worker races.

Metadata adapters below test ownership and state; they do not synthesize audio
and are never treated as pronunciation or OCR quality evidence.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.engines import Voice, VoiceRegistry
from carr_library.language_qualification.runtime_inventory import (
    InstalledOcrInventory, collect_installed_ocr, collect_installed_voices,
)
from carr_library.ui import language_matrix as ui


def host(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Synthetic language inventory scope')
    library.add_text(workspace, 'Original synthetic text', 'Український текст remains exact.')
    jobs = []
    frame = SimpleNamespace(library=library, _closing=False, busy=False, working=False,
                            cancel_event=Event())
    frame.run_job = lambda title, operation, done, failed: jobs.append((operation, done, failed))
    return frame, jobs


def registry(*, enumerate_voices=None):
    row = Voice('metadata:uk', 'Synthetic voice metadata', ('uk', 'uk-UA'), 'metadata')
    engine = SimpleNamespace(id='metadata', voices=enumerate_voices or (lambda: (row,)))
    return VoiceRegistry((engine,), notices=({'engine': 'optional', 'available': False,
                                            'reason': 'Not installed in this metadata fixture.'},))


def tree_digest(root):
    return {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def test_explicit_production_registry_snapshot_is_read_only_and_separate(tmp_path):
    frame, jobs = host(tmp_path)
    before = tree_digest(frame.library.root)
    controller, results, errors = ui.LanguageMatrixController(), [], []
    request = ui.request_voice_inventory(frame, controller, registry=registry(),
        completed=results.append, failed=errors.append)
    assert not results and controller.inventory is None
    operation, done, _failed = jobs.pop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        inventory = executor.submit(operation).result(timeout=5)
    done(inventory)
    assert controller.inventory is inventory and results == [inventory] and not errors
    assert not request.cancelled()
    assert controller.matrix.summary()['qualified_entries'] == 0
    assert controller.matrix.summary()['unmatched_voices'] == 1
    assert 'uk-UA' in controller.inventory_text()
    assert tree_digest(frame.library.root) == before


@pytest.mark.parametrize('drift', ['library', 'path', 'inode', 'removed', 'closing', 'window'])
def test_late_completed_inventory_cannot_publish_after_scope_or_lifetime_drift(tmp_path, drift):
    frame, jobs = host(tmp_path)
    controller, results, alive = ui.LanguageMatrixController(), [], [True]
    previous = collect_installed_voices(registry=registry())
    controller.set_inventory(previous)
    ui.request_voice_inventory(frame, controller, registry=registry(), alive=lambda: alive[0],
        completed=results.append, failed=lambda error: None)
    operation, done, _failed = jobs.pop()
    inventory = operation()
    if drift == 'library':
        frame.library = LibraryApp(tmp_path / 'second')
    elif drift == 'path':
        frame.library.root = tmp_path / 'other'
    elif drift in ('inode', 'removed'):
        root = frame.library.root
        root.rename(tmp_path / 'retained-original')
        if drift == 'inode':
            LibraryApp(root)
    elif drift == 'closing':
        frame._closing = True
    else:
        alive[0] = False
    with pytest.raises(InterruptedError, match='changed'):
        done(inventory)
    assert controller.inventory is previous and not results


def test_worker_cancel_is_sticky_and_does_not_cancel_a_later_host_job(tmp_path):
    frame, jobs = host(tmp_path)
    entered, release = Event(), Event()
    rows = registry().voices()

    def paused_enumeration():
        entered.set()
        assert release.wait(5)
        return rows

    controller, results = ui.LanguageMatrixController(), []
    previous = collect_installed_voices(registry=registry())
    controller.set_inventory(previous)
    request = ui.request_voice_inventory(frame, controller,
        registry=registry(enumerate_voices=paused_enumeration),
        completed=results.append, failed=lambda error: None)
    operation, _done, _failed = jobs.pop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(operation)
        try:
            assert entered.wait(5)
            request.cancel()
            assert not frame.cancel_event.is_set()
        finally:
            release.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)
    frame.cancel_event.clear()
    assert request.cancelled() and controller.inventory is previous and not results


def test_newer_scan_invalidates_queued_publication_of_previous_scan(tmp_path):
    frame, jobs = host(tmp_path)
    controller, results = ui.LanguageMatrixController(), []
    first = ui.request_voice_inventory(frame, controller, registry=registry(),
        completed=results.append, failed=lambda error: None)
    old_operation, old_done, _ = jobs.pop()
    old_inventory = old_operation()
    ui.request_voice_inventory(frame, controller, registry=registry(),
        completed=results.append, failed=lambda error: None)
    operation, done, _ = jobs.pop()
    current = operation()
    done(current)
    with pytest.raises(InterruptedError):
        old_done(old_inventory)
    assert first.cancelled() and controller.inventory is current and results == [current]


def test_late_host_cancel_blocks_publication_even_after_successful_enumeration(tmp_path):
    frame, jobs = host(tmp_path)
    controller = ui.LanguageMatrixController()
    ui.request_voice_inventory(frame, controller, registry=registry(),
        completed=lambda value: pytest.fail('Cancelled result published'), failed=lambda error: None)
    operation, done, _ = jobs.pop()
    result = operation()
    frame.cancel_event.set()
    with pytest.raises(InterruptedError):
        done(result)
    assert controller.inventory is None


def test_ocr_inventory_reuses_existing_picker_and_retains_script_auxiliary_identity(tmp_path, monkeypatch):
    from carr_library.ui import ocr_selection
    calls = []
    def inventory(**kwargs):
        calls.append(kwargs)
        return ('ukr', 'eng', 'script/Latin', 'osd')
    monkeypatch.setattr(ocr_selection, 'discover_installed_models', inventory)
    frame, jobs = host(tmp_path)
    controller, results = ui.LanguageMatrixController(), []
    before = tree_digest(frame.library.root)
    ui.request_inventory(frame, controller, kind='ocr', completed=results.append,
                         failed=lambda error: pytest.fail(str(error)))
    operation, done, _ = jobs.pop()
    snapshot = operation()
    done(snapshot)
    assert len(calls) == 1 and callable(calls[0]['cancelled'])
    assert controller.ocr_inventory.identifiers == ('ukr', 'eng', 'script/Latin', 'osd')
    assert {row[0] for row in snapshot.text_models} == {'ukr', 'eng', 'script/Latin'}
    assert 'Auxiliary models' in controller.ocr_inventory_text()
    assert 'Latin' in controller.ocr_inventory_text()
    assert 'no OCR language qualification' in controller.summary()
    assert all(row.capability('ocr').status == 'untested' for row in controller.filter())
    assert controller.matrix.summary()['qualified_entries'] == 0
    assert tree_digest(frame.library.root) == before


def test_ocr_snapshot_cannot_invent_labels_or_fall_back_after_missing_engine(monkeypatch):
    from carr_library.ui import ocr_selection
    controller = ui.LanguageMatrixController()
    valid = InstalledOcrInventory(('ukr',), ocr_selection.selectable_models('installed', ('ukr',)), 'fixture')
    controller.set_ocr_inventory(valid)
    with pytest.raises(ValueError, match='catalogue'):
        controller.set_ocr_inventory(replace(valid, text_models=(('ukr', 'All 135 languages verified'),)))
    def missing(**kwargs):
        raise ocr_selection.OcrEngineMissingError('No selected local OCR engine.')
    monkeypatch.setattr(ocr_selection, 'discover_installed_models', missing)
    with pytest.raises(ocr_selection.OcrEngineMissingError):
        collect_installed_ocr()
    assert controller.ocr_inventory is valid


def test_ocr_cancel_checks_run_before_and_after_existing_discovery(monkeypatch):
    from carr_library.ui import ocr_selection
    cancelled, calls = Event(), []
    def inventory(**kwargs):
        calls.append(1)
        cancelled.set()
        return ('eng',)
    monkeypatch.setattr(ocr_selection, 'discover_installed_models', inventory)
    with pytest.raises(InterruptedError):
        collect_installed_ocr(cancelled=cancelled.is_set)
    with pytest.raises(InterruptedError):
        collect_installed_ocr(cancelled=cancelled.is_set)
    assert calls == [1]


def test_child_destroy_event_does_not_dispose_parent_but_own_destroy_cancels(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Dialog=object))
    native_type = ui._native_dialog_type()
    dialog = object.__new__(native_type)
    cancelled = Event()
    dialog._alive = dialog._window_alive = True
    dialog.request = SimpleNamespace(cancel=cancelled.set)
    events = []
    dialog.destroyed(SimpleNamespace(GetEventObject=lambda: object(), Skip=lambda: events.append('child')))
    assert dialog._alive and dialog._window_alive and not cancelled.is_set()
    dialog.destroyed(SimpleNamespace(GetEventObject=lambda: dialog, Skip=lambda: events.append('own')))
    assert not dialog._alive and not dialog._window_alive and cancelled.is_set()
    # Dead controls were never supplied: each stale handler must return first.
    dialog.filter(); dialog.select(); dialog.refresh(); dialog.cancel(); dialog.close()
    assert events == ['child', 'own']


def test_native_handler_restores_controls_when_host_executor_rejects_submission(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Dialog=object))
    native_type = ui._native_dialog_type()
    dialog = object.__new__(native_type)
    frame, _jobs = host(tmp_path)
    def reject(*args):
        raise RuntimeError('Executor is shut down.')
    frame.run_job = reject
    class Control:
        def __init__(self, enabled=True):
            self.enabled, self.value = enabled, ''
        def Enable(self): self.enabled = True
        def Disable(self): self.enabled = False
        def ChangeValue(self, value): self.value = value
    dialog._alive = dialog._window_alive = True
    dialog.pending = False
    dialog.request = None
    dialog.controller = ui.LanguageMatrixController()
    dialog.refresh_button = Control()
    dialog.ocr_button = Control()
    dialog.cancel_button = Control(False)
    dialog.status = Control()
    dialog.GetParent = lambda: frame
    dialog.refresh()
    assert not dialog.pending and dialog.request is None
    assert dialog.refresh_button.enabled and dialog.ocr_button.enabled
    assert not dialog.cancel_button.enabled
    assert 'not updated' in dialog.status.value and 'shut down' in dialog.status.value
    assert dialog.controller.inventory is None


def test_scope_drift_before_worker_start_prevents_adapter_discovery(tmp_path):
    frame, jobs = host(tmp_path)
    def forbidden():
        pytest.fail('A stale worker enumerated an adapter.')
    controller = ui.LanguageMatrixController()
    ui.request_voice_inventory(frame, controller, registry=registry(enumerate_voices=forbidden),
        completed=lambda result: pytest.fail('Stale result published'), failed=lambda error: None)
    operation, _done, _failed = jobs.pop()
    frame.library = LibraryApp(tmp_path / 'other')
    with pytest.raises(InterruptedError):
        operation()
    assert controller.inventory is None


def test_late_error_from_previous_generation_cannot_reset_the_current_scan(tmp_path):
    frame, jobs = host(tmp_path)
    controller, failures, results = ui.LanguageMatrixController(), [], []
    ui.request_voice_inventory(frame, controller, registry=registry(),
        completed=results.append, failed=lambda error: failures.append(('old', error)))
    _old_operation, _old_done, old_failed = jobs.pop()
    ui.request_voice_inventory(frame, controller, registry=registry(),
        completed=results.append, failed=lambda error: failures.append(('current', error)))
    operation, done, current_failed = jobs.pop()
    old_failed(RuntimeError('Obsolete queued failure'))
    assert not failures and controller.inventory is None
    result = operation()
    done(result)
    assert results == [result] and controller.inventory is result
    current_failed(InterruptedError('The current operation was cancelled'))
    assert len(failures) == 1 and failures[0][0] == 'current'
