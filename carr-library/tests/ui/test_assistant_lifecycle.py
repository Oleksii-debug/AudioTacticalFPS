"""Real assistant handlers, durable jobs and serial workers; no model or audio claim."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Event, get_ident
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.speech import Voice, VoiceRegistry
from carr_library.studio.render import ProductionService
from carr_library.ui import assistant_dialog, worker_cleanup


class WarmProvider:
    name = 'Synthetic warm worker; not a speech quality test'

    def __init__(self):
        self.process = None
        self.closed_on = []

    def voices(self):
        if self.process is None:
            self.process = subprocess.Popen([sys.executable, '-I', '-u', '-c',
                'import sys; sys.stdout.buffer.write(b"ready\\n"); '
                'sys.stdout.buffer.flush(); sys.stdin.buffer.read()'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            assert self.process.stdout.readline() == b'ready\n'
        return (Voice('fixture:warm', 'Synthetic local voice', 'fixture', 'en'),)

    def close(self):
        self.closed_on.append(get_ident())
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait(timeout=5)
                self.process.stdout.close()


@pytest.fixture
def assistant_view(tmp_path, monkeypatch):
    owner, callbacks = get_ident(), Queue()
    state = SimpleNamespace(controls=[], drive=lambda dialog: None,
        choose=lambda dialog: 5100, choices=0, executors=[], cleanup=[], reports=[],
        discover_entered=Event(), discover_release=Event(), render_entered=Event(), render_release=Event(),
        discover_error=None, discover_threads=[], discover_cancel=None, borrowed=False,
        render_cancel=None, render_threads=[])
    state.discover_release.set()
    state.render_release.set()
    state.provider = WarmProvider()

    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent = parent
            self.label = kwargs.get('label', '')
            self.name = ''
            self.value = kwargs.get('value', False if type(self).__name__ == 'CheckBox' else '')
            self.items = list(kwargs.get('choices', ()))
            self.selection, self.enabled, self.destroyed = -1, True, False
            self.handlers = {}
            state.controls.append(self)

        def native(self):
            assert get_ident() == owner, 'Assistant touched a control from a worker.'
            current = self
            while current is not None:
                assert not getattr(current, 'destroyed', False), 'Late callback touched a destroyed dialog.'
                current = getattr(current, 'parent', None)

        def SetName(self, value): self.native(); self.name = value
        def SetLabel(self, value): self.native(); self.label = value
        def SetValue(self, value): self.native(); self.value = value
        def ChangeValue(self, value): self.SetValue(value)
        def GetValue(self): self.native(); return self.value
        def SetSelection(self, value): self.native(); self.selection = value
        def GetSelection(self): self.native(); return self.selection
        def Enable(self, value=True): self.native(); self.enabled = value
        def Disable(self): self.Enable(False)
        def SetFocus(self): self.native(); assert self.enabled
        def Add(self, *args): self.native()
        def SetSizer(self, *args): self.native()
        def Wrap(self, width): self.native()
        def Bind(self, event, callback): self.native(); self.handlers[event] = callback
        def EndModal(self, value): self.native(); self.ended = value
        def IsModal(self): self.native(); return True
        def Hide(self): self.native()
        def Destroy(self): self.native(); self.destroyed = True

    class Dialog(Control):
        def ShowModal(self):
            self.native()
            state.dialog = self
            state.drive(self)
            return 5102

    class SingleChoiceDialog(Control):
        def ShowModal(self):
            self.native()
            state.choices += 1
            self.selection = 0
            return state.choose(self)

    class CheckBox(Control):
        pass

    wx = SimpleNamespace(Dialog=Dialog, SingleChoiceDialog=SingleChoiceDialog,
        StaticText=Control, TextCtrl=Control, Choice=Control, CheckBox=CheckBox,
        BoxSizer=Control, Button=Control, ID_OK=5100, ID_CANCEL=5101, ID_CLOSE=5102,
        EVT_BUTTON='button', EVT_TEXT='text', EVT_CHOICE='choice', EVT_CHECKBOX='checkbox', EVT_CLOSE='close',
        CallAfter=lambda callback, *args: callbacks.put((callback, args)))
    for name in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'LEFT', 'TOP',
                 'ALL', 'EXPAND', 'TE_MULTILINE', 'TE_READONLY'):
        setattr(wx, name, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    state.app = LibraryApp(tmp_path / 'library')
    state.workspace = state.app.create_workspace('Synthetic assistant library')
    state.output = tmp_path / 'output'
    state.output.mkdir()
    state.frame = SimpleNamespace(library=state.app, active=(state.workspace,), report=state.reports.append)

    def discover(root, *, cancelled=lambda: False):
        state.discover_threads.append(get_ident())
        state.discover_cancel = cancelled
        state.discover_entered.set()
        assert state.discover_release.wait(5), 'Voice discovery blocked the UI instead of its worker.'
        if state.discover_error:
            raise state.discover_error
        return VoiceRegistry((state.provider,))
    monkeypatch.setattr('carr_library.runtime_settings.voice_registry', discover)

    def render(self, settings, output, cancelled, progress):
        assert self.voices.voices()[0].id == 'fixture:warm'
        state.render_threads.append(get_ident())
        state.render_cancel = cancelled
        state.render_entered.set()
        assert state.render_release.wait(5), 'Closing waited for the active production worker.'
        if cancelled():
            raise InterruptedError('Synthetic render cooperatively stopped.')
        target = Path(output) / (settings['operation_id'] + '.wav')
        with wave.open(str(target), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 80)
        return {'artifacts': [str(target)]}
    monkeypatch.setattr(ProductionService, 'assistant_callback', render)

    def production(root):
        registry = VoiceRegistry((state.provider,)) if state.borrowed else None
        state.borrowed_registry = registry
        state.production = ProductionService(root, voices=registry)
        return state.production
    monkeypatch.setattr('carr_library.studio.render.ProductionService', production)

    def service(app, **kwargs):
        state.service = AssistantService(app, **kwargs)
        return state.service
    monkeypatch.setattr('carr_library.assistant.AssistantService', service)

    def executor(**kwargs):
        result = ThreadPoolExecutor(**kwargs)
        state.executors.append(result)
        return result
    monkeypatch.setattr(assistant_dialog, 'ThreadPoolExecutor', executor)
    close_after_worker = worker_cleanup.close_after_worker
    def close_worker(executor, service):
        future = close_after_worker(executor, service)
        state.cleanup.append(future)
        return future
    monkeypatch.setattr(worker_cleanup, 'close_after_worker', close_worker)

    def named(prefix):
        return next(control for control in state.controls if control.name.startswith(prefix))
    def button(label, *, invoke=True):
        control = next(control for control in state.controls if control.label == label)
        if not invoke:
            return control
        assert control.enabled
        return control.handlers['button'](None)
    def pump(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, 'Assistant did not complete its queued native callbacks.'
            try:
                callback, args = callbacks.get(timeout=.02)
            except Empty:
                continue
            callback(*args)
    def drain():
        while not callbacks.empty():
            callback, args = callbacks.get_nowait()
            callback(*args)
    def goal(text='Produce audio: Synthetic narration'):
        named('Chosen output').SetValue(str(state.output))
        named('Goal').SetValue(text)
        named('Goal').handlers['text'](SimpleNamespace(Skip=lambda: None))
    def explicit_goal():
        goal(json.dumps({'schema': 'carr-assistant-plan/1', 'summary': 'Synthetic lifecycle', 'steps': [
            {'tool': 'audio.produce', 'arguments': {'text': 'Synthetic narration', 'voice_ids': ['fixture:warm']}}
        ]}))
    def idle():
        pump(lambda: button('&Preview plan', invoke=False).enabled)
    state.named, state.button, state.pump, state.drain = named, button, pump, drain
    state.goal, state.explicit_goal, state.idle = goal, explicit_goal, idle
    state.open = lambda: assistant_dialog.show_assistant(state.frame)
    yield state
    state.discover_release.set()
    state.render_release.set()
    for executor in state.executors:
        executor.shutdown(wait=True, cancel_futures=False)
    if state.provider.process is not None and state.provider.process.poll() is None:
        state.provider.close()


def test_preview_discovers_off_ui_and_real_job_finishes_before_owned_child_closes(assistant_view):
    state = assistant_view
    state.discover_release.clear()
    def drive(dialog):
        state.goal()
        state.button('&Preview plan')
        assert state.discover_entered.wait(5)
        assert all(thread != get_ident() for thread in state.discover_threads)
        assert not state.button('Choose input folder', invoke=False).enabled
        assert not state.button('Choose output folder', invoke=False).enabled
        assert not state.button('&Run / resume', invoke=False).enabled
        state.discover_release.set()
        state.idle()
        assert state.choices == 1
        assert 'fixture:warm' in state.named('Plan preview').GetValue()
        assert not state.service.jobs(), 'Preview must never submit or render a job.'
        state.button('&Run / resume')
        state.idle()
        result, = state.service.jobs()
        assert result.status == 'complete' and result.completed == 1
        artifact, = result.results[0]['artifacts']
        assert Path(artifact['path']).is_file() and artifact['size'] == 204
        assert all(thread != get_ident() for thread in state.render_threads)
        assert state.provider.process.poll() is None
        state.button('&Close')
    state.drive = drive
    state.open()
    state.cleanup[0].result(timeout=5)
    state.drain()
    assert state.provider.process.poll() == 0
    assert len(state.provider.closed_on) == 1 and state.provider.closed_on[0] != get_ident()


def test_close_during_late_discovery_is_nonblocking_idempotent_and_owns_returned_child(assistant_view):
    state = assistant_view
    state.discover_release.clear()
    def drive(dialog):
        state.goal()
        state.button('&Preview plan')
        assert state.discover_entered.wait(5)
        state.button('&Close')
        dialog.handlers['close'](None)
        assert state.discover_cancel()
        assert len(state.cleanup) == 1 and not state.cleanup[0].done()
        assert state.provider.process is None
    state.drive = drive
    state.open()
    state.discover_release.set()
    state.cleanup[0].result(timeout=5)
    state.drain()
    assert state.choices == 0 and not state.service.jobs()
    assert state.provider.process.poll() == 0 and len(state.provider.closed_on) == 1


@pytest.mark.parametrize('pause_fails', [False, True], ids=['durable-pause', 'pause-write-failure'])
def test_active_render_close_cancels_then_releases_owned_child_even_when_pause_save_fails(
        assistant_view, monkeypatch, pause_fails):
    state = assistant_view
    state.render_release.clear()
    def drive(dialog):
        state.explicit_goal()
        state.button('&Preview plan')
        state.button('&Run / resume')
        assert state.render_entered.wait(5)
        if pause_fails:
            def fail(identity):
                raise OSError('Synthetic pause persistence failure')
            monkeypatch.setattr(state.service, 'pause', fail)
        state.button('&Close')
        dialog.handlers['close'](None)
        assert state.render_cancel()
        assert len(state.cleanup) == 1 and not state.cleanup[0].done()
        assert state.provider.process.poll() is None
    state.drive = drive
    state.open()
    state.render_release.set()
    state.cleanup[0].result(timeout=5)
    state.drain()
    result, = state.service.jobs()
    assert result.status == 'paused' and result.completed == 0
    assert not tuple(state.output.iterdir())
    assert state.provider.process.poll() == 0 and len(state.provider.closed_on) == 1
    assert bool(state.reports) == pause_fails
    if pause_fails:
        assert 'saving its pause request failed' in state.reports[0]


def test_borrowed_registry_survives_actual_assistant_close(assistant_view):
    state = assistant_view
    state.borrowed = True
    def drive(dialog):
        state.goal()
        state.button('&Preview plan')
        state.idle()
        state.button('&Close')
    state.drive = drive
    state.open()
    state.cleanup[0].result(timeout=5)
    assert state.production._closed
    assert state.provider.process.poll() is None and not state.provider.closed_on
    assert state.borrowed_registry.voices()[0].id == 'fixture:warm'
    state.borrowed_registry.close()
    assert state.provider.process.poll() == 0


def test_unused_close_never_discovers_or_starts_child(assistant_view):
    state = assistant_view
    state.open()
    state.cleanup[0].result(timeout=5)
    assert not state.discover_threads and state.provider.process is None
    assert not state.service.jobs()


@pytest.mark.parametrize('failure', ['discovery', 'submit'])
def test_failed_preview_restores_controls_and_allows_another_review(assistant_view, monkeypatch, failure):
    state = assistant_view
    def drive(dialog):
        if failure == 'discovery':
            state.discover_error = OSError('Synthetic unavailable local provider')
        else:
            executor = state.executors[0]
            real_submit = executor.submit
            def fail_once(*args, **kwargs):
                monkeypatch.setattr(executor, 'submit', real_submit)
                raise RuntimeError('Synthetic submission failure')
            monkeypatch.setattr(executor, 'submit', fail_once)
        state.goal()
        state.button('&Preview plan')
        state.idle()
        assert 'Synthetic' in state.named('Plan preview').GetValue()
        assert state.button('Choose input folder', invoke=False).enabled
        assert state.button('Choose output folder', invoke=False).enabled
        assert state.named('Goal').enabled and state.named('Library scope').enabled
        assert not state.button('&Run / resume', invoke=False).enabled
        state.goal('List libraries')
        state.button('&Preview plan')
        assert state.button('&Run / resume', invoke=False).enabled
        assert 'library.list' in state.named('Plan preview').GetValue()
    state.drive = drive
    state.open()
    state.cleanup[0].result(timeout=5)


@pytest.mark.parametrize('change', ['pause', 'edit', 'archive', 'library'])
def test_cancelled_or_stale_discovery_cannot_install_a_plan(assistant_view, change):
    state = assistant_view
    state.discover_release.clear()
    def drive(dialog):
        state.goal()
        state.button('&Preview plan')
        assert state.discover_entered.wait(5)
        if change == 'pause':
            state.button('Cancel voice &discovery')
            assert state.discover_cancel()
        elif change == 'edit':
            state.named('Goal').SetValue('List libraries')
            state.named('Goal').handlers['text'](SimpleNamespace(Skip=lambda: None))
        elif change == 'archive':
            state.app.archive_workspace(state.workspace)
        else:
            state.frame.library = LibraryApp(state.output / 'other-library')
        state.discover_release.set()
        state.idle()
        assert state.choices == 0 and not state.service.jobs()
        assert not state.button('&Run / resume', invoke=False).enabled
        assert state.named('Goal').enabled
    state.drive = drive
    state.open()
    state.cleanup[0].result(timeout=5)
    assert state.provider.process.poll() == 0


def test_close_during_voice_choice_drops_selection_without_touching_destroyed_view(assistant_view):
    state = assistant_view
    def choose(dialog):
        state.button('&Close')
        return 5100
    state.choose = choose
    def drive(dialog):
        state.goal()
        state.button('&Preview plan')
        state.pump(lambda: state.choices == 1)
    state.drive = drive
    state.open()
    state.cleanup[0].result(timeout=5)
    state.drain()
    assert not state.service.jobs() and state.provider.process.poll() == 0


def test_missing_voice_choice_preserves_other_explicit_step_identity(assistant_view):
    state = assistant_view
    def drive(dialog):
        state.goal(json.dumps({'schema': 'carr-assistant-plan/1', 'summary': 'Mixed explicit choices', 'steps': [
            {'tool': 'audio.produce', 'arguments': {'text': 'First', 'voice_ids': ['explicit:other']}},
            {'tool': 'audio.produce', 'arguments': {'text': 'Second'}}]}))
        state.button('&Preview plan')
        state.idle()
        value = state.named('Plan preview').GetValue().split('\n\n')[1]
        steps = json.loads(value)['steps']
        assert steps[0]['arguments']['voice_ids'] == ['explicit:other']
        assert steps[1]['arguments']['voice_ids'] == ['fixture:warm']
        assert not state.service.jobs()
    state.drive = drive
    state.open()
    state.cleanup[0].result(timeout=5)
