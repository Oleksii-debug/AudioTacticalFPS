"""Real translation stores/workers with logical native peers; Windows proof is separate."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
import runpy
import threading

import pytest

from carr_library.ui import translation_dialog as workspace

_TESTS = Path(__file__).resolve().parents[1]
_SELECTION = runpy.run_path(str(_TESTS / 'ui' / 'test_translation_selection.py'))
_BUSINESS = runpy.run_path(str(_TESTS / 'assistant' / 'test_translation.py'))


@pytest.fixture
def saved(tmp_path, monkeypatch):
    return _SELECTION['saved_translation'].__wrapped__(tmp_path, monkeypatch)


def close_owner(ui, kind):
    identity = ui.dialog.native_id
    if kind == 'direct':
        assert ui.dialog.Destroy() is True
    else:
        # This models native parent deletion without dispatch through the
        # product's Python Destroy override. The installed probe uses real wx.
        assert ui.Control._native_destroy(ui.dialog) is True
    assert not ui.dialog
    assert ui.destroyed_ids.count(identity) == 1
    assert ui.dialog.Destroy() is False
    assert not ui.frame.cancel_event.is_set()


class HeldWorker:
    """Own a real thread and hold only its actual invocation/result."""
    def __init__(self, ui, phase):
        self.ui, self.phase = ui, phase
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.started, self.ready = threading.Event(), threading.Event()
        self.release, self.posted = threading.Event(), threading.Event()
        self.queue = Queue()
        self.future, self.actual = None, None
        self.delivered = 0
        ui.wx.CallAfter = lambda callback, *args: self.queue.put((callback, args))

    def run_job(self, title, operation, *, done, failed):
        assert self.future is None
        self.ui.jobs.append(title)

        def invoke():
            self.started.set()
            if self.phase == 'before':
                assert self.release.wait(5), 'The test did not release its owned worker.'
            result = operation()
            self.actual = result
            self.ready.set()
            if self.phase == 'after':
                assert self.release.wait(5), 'The test did not release its actual result.'
            return result

        self.future = self.executor.submit(invoke)

        def deliver():
            self.delivered += 1
            try:
                result = self.future.result()
            except Exception as error:
                failed(error)
            else:
                done(result)

        def completed(unused):
            self.queue.put((deliver, ()))
            self.posted.set()

        self.future.add_done_callback(completed)
        return True

    def wait_boundary(self):
        assert self.started.wait(5), 'The actual translation worker did not start.'
        if self.phase == 'after':
            assert self.ready.wait(5), 'The actual operation did not reach the held-result boundary.'

    def drain(self):
        for unused in range(100):
            try:
                callback, args = self.queue.get_nowait()
            except Empty:
                return
            callback(*args)
        raise AssertionError('Unexpected unbounded translation callback queue.')

    def finish(self):
        self.release.set()
        assert self.posted.wait(5), 'The actual worker completion was not posted.'
        self.drain()
        assert self.delivered == 1

    def close(self):
        self.release.set()
        self.executor.shutdown(wait=True, cancel_futures=False)


@pytest.mark.parametrize('kind', ['direct', 'native-parent'])
@pytest.mark.parametrize('phase', ['before', 'after'], ids=['pending-work', 'completed-result'])
def test_closed_owner_preserves_real_approved_job_and_cannot_rebill(
        tmp_path, monkeypatch, saved, kind, phase):
    ui = _SELECTION['native_protocol_double'](monkeypatch, tmp_path, saved, [saved.profile])
    original_job = saved.service.jobs.path(saved.identity).read_bytes()
    original_calls = len(saved.connection['calls'])
    value = 'A separately approved exact source; unchanged after native owner death.'
    worker = HeldWorker(ui, phase)
    ui.frame.run_job = worker.run_job
    captured = {}

    def scenario(unused):
        fields = _SELECTION['visible'](ui)
        fields['text'].ChangeValue(value)
        fields['source'].ChangeValue('en')
        fields['target'].ChangeValue('uk')
        fields['budget'].ChangeValue('1')
        ui.buttons['Preview'].click()
        ui.buttons['Translate / resume'].click()
        worker.wait_boundary()
        identities = [identity for identity in saved.service.jobs.identities() if identity != saved.identity]
        assert len(identities) == 1
        identity = identities[0]
        captured['identity'] = identity
        if phase == 'after':
            assert worker.actual['status'] == 'complete'
            captured['completed'] = saved.service.jobs.path(identity).read_bytes()
        calls, credentials = len(saved.connection['calls']), len(saved.vault.calls)
        assert calls == original_calls + (phase == 'after')
        close_owner(ui, kind)
        worker.finish()
        assert len(saved.connection['calls']) == calls
        assert len(saved.vault.calls) == credentials
        assert not ui.forbidden

    ui.scenario = scenario
    try:
        workspace.show_translation(ui.frame)
    finally:
        worker.close()
    result = saved.service.get(captured['identity'])
    assert result['source_text'] == value
    assert result['status'] == ('complete' if phase == 'after' else 'paused')
    assert len(result['receipts']) == (1 if phase == 'after' else 0)
    if phase == 'after':
        assert saved.service.jobs.path(captured['identity']).read_bytes() == captured['completed']
    assert saved.service.jobs.path(saved.identity).read_bytes() == original_job
    assert not ui.forbidden and not ui.frame.cancel_event.is_set()


@pytest.mark.parametrize('kind', ['direct', 'native-parent'])
@pytest.mark.parametrize('phase', ['before', 'after'], ids=['pending-source-read', 'actual-source-ready'])
def test_closed_owner_cannot_apply_or_transfer_actual_retained_source(
        tmp_path, monkeypatch, saved, kind, phase):
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range

    case = _BUSINESS['bound_ready'](tmp_path / 'source-case', saved.connection, first=3, last=3)
    ui = _SELECTION['native_protocol_double'](monkeypatch, tmp_path, saved, [case.profile])
    ui.frame.library = case.app
    from carr_library.assistant.profiles import ProfileStore
    ProfileStore(case.app.root / 'state' / 'providers').save(case.profile)
    monkeypatch.setattr(workspace, 'TranslationService', lambda *args, **kwargs: case.service)
    desk = ReadingDesk(case.app, case.workspace, case.imported['edition'], topic=case.topic)
    source_path = case.app.assets.resolve(case.imported['asset'])
    source_bytes = source_path.read_bytes()
    calls, credentials = len(saved.connection['calls']), len(case.vault.calls)
    reads = []
    worker = HeldWorker(ui, phase)
    ui.frame.run_job = worker.run_job

    def guard():
        with case.app.read(case.workspace):
            pass

    def load(cancelled):
        reads.append(True)
        return read_source_range(desk, 3, 3, guard=guard, cancelled=cancelled)

    def scenario(unused):
        worker.drain()  # Runs the authored load_source callback on the UI thread.
        worker.wait_boundary()
        if phase == 'after':
            assert worker.actual.text == case.source.text
            assert worker.actual.positions == case.source.positions
        close_owner(ui, kind)
        worker.finish()
        assert ui.fields['Selected text; originals remain unchanged'].value == ''
        assert not ui.forbidden

    ui.scenario = scenario
    try:
        workspace.show_translation(ui.frame, source_loader=load, source_guard=guard, source_topic=case.topic)
    finally:
        worker.close()
    assert reads == ([True] if phase == 'after' else [])
    assert not case.service.jobs.identities()
    assert source_path.read_bytes() == source_bytes
    assert len(saved.connection['calls']) == calls and len(case.vault.calls) == credentials
    assert not ui.forbidden and not ui.frame.cancel_event.is_set()


@pytest.mark.parametrize('kind', ['direct', 'native-parent'])
@pytest.mark.parametrize('picker', ['saved-job', 'export'])
def test_nested_picker_owner_death_avoids_deleted_peers_and_new_work(
        tmp_path, monkeypatch, saved, kind, picker):
    ui = _SELECTION['native_protocol_double'](monkeypatch, tmp_path, saved, [saved.profile])
    before = saved.service.jobs.path(saved.identity).read_bytes()
    calls, credentials = len(saved.connection['calls']), len(saved.vault.calls)

    def scenario(unused):
        if picker == 'saved-job':
            ui.choice_hook = lambda: close_owner(ui, kind)
            ui.buttons['Saved jobs'].click()
        else:
            ui.buttons['Saved jobs'].click()
            ui.directory_hook = lambda: close_owner(ui, kind)
            ui.buttons['Export'].click()
        assert not ui.dialog
        assert not ui.forbidden

    ui.scenario = scenario
    workspace.show_translation(ui.frame)
    assert ui.jobs == ['Reading saved translation jobs']
    assert not tuple(ui.directory.iterdir())
    assert saved.service.jobs.path(saved.identity).read_bytes() == before
    assert len(saved.connection['calls']) == calls and len(saved.vault.calls) == credentials
    assert not ui.forbidden


@pytest.mark.parametrize('boundary', ['deleted-picker', 'parent-closing', 'parent-closes-during-picker-destroy'])
def test_source_range_modal_never_hands_off_after_peer_or_parent_death(
        tmp_path, monkeypatch, saved, boundary):
    case = _BUSINESS['bound_ready'](tmp_path / 'book-case', saved.connection)
    window = _BUSINESS['translation_window'].__wrapped__(monkeypatch, tmp_path)
    frame = window.bind(case)
    original = case.app.assets.resolve(case.imported['asset'])
    original_bytes = original.read_bytes()
    calls, credentials = len(saved.connection['calls']), len(case.vault.calls)
    opened = []
    monkeypatch.setattr(workspace, 'show_translation', lambda *args, **kwargs: opened.append(True))

    def during_range():
        chooser = next(control for control in window.controls
                       if control.title == 'Translate a book or source range')
        if boundary == 'parent-closes-during-picker-destroy':
            actual_destroy = chooser.Destroy
            def destroy():
                actual_destroy()
                frame._closing = True
            chooser.Destroy = destroy
        else:
            if boundary == 'parent-closing':
                frame._closing = True
            chooser._native_destroy()

    window.state['during_range'] = during_range
    window.ui.install_translation_actions(frame)
    window.menu_handlers[101](None)
    assert not opened
    assert not window.state.get('forbidden') and not window.errors
    assert original.read_bytes() == original_bytes
    assert len(saved.connection['calls']) == calls and len(case.vault.calls) == credentials
    assert not case.service.jobs.identities()


def test_destroyed_child_event_does_not_cancel_the_live_translation_owner(tmp_path, monkeypatch, saved):
    ui = _SELECTION['native_protocol_double'](monkeypatch, tmp_path, saved, [saved.profile])
    calls = len(saved.connection['calls'])
    before = saved.service.jobs.path(saved.identity).read_bytes()

    def scenario(unused):
        child = ui.Control(ui.dialog)
        child.Destroy()
        assert ui.dialog
        ui.buttons['Saved jobs'].click()
        ui.buttons['Translate / resume'].click()
        assert ui.focus is _SELECTION['visible'](ui)['status']
        assert not ui.forbidden

    ui.scenario = scenario
    workspace.show_translation(ui.frame)
    assert len(saved.connection['calls']) == calls
    assert saved.service.jobs.path(saved.identity).read_bytes() == before
    assert not ui.forbidden and not ui.frame.cancel_event.is_set()
