"""Execute installed reader lifetime/run closures with actual queued work."""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from carr_library.ui import speech_dialog


def reader_boundary():
    module = ast.parse(Path(speech_dialog.__file__).read_text(encoding='utf-8'))
    owner = next(node for node in module.body
                 if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    wanted = {'ReaderWindow', 'detach_reader', 'destroyed', 'run',
              'close_resources', 'transfer_owner', 'release_owner'}
    definitions = [node for node in owner.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                   and node.name in wanted]
    assert {node.name for node in definitions} == wanted
    template = ast.parse("""
def build(wx, executor, cancel, controls, report):
    alive, busy, resources_closed = True, False, False
    owned_registries = controls.owned_registries
    ownership_lock = RLock()
    parent = SimpleNamespace(report=report)
    source_guard = lambda: None
    autosave_timer = SimpleNamespace(Stop=lambda: None)
    controls.live_closes = 0
    def close_live():
        controls.live_closes += 1
        cancel.set()
    live = SimpleNamespace(core=SimpleNamespace(busy=False), close=close_live)
    def placeholder():
        pass
    dialog = ReaderWindow(None)
    native_id = dialog.GetId()
    dialog._reader_detach = close_resources
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    controls.transfer_owner = transfer_owner
    controls.retained_parent = parent
    def state():
        return alive, resources_closed
    return dialog, run, state
""")
    body = template.body[0].body
    index = next(i for i, node in enumerate(body)
                 if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index:index+1] = definitions
    namespace = {'SimpleNamespace': SimpleNamespace, 'RLock': RLock}
    exec(compile(ast.fix_missing_locations(template), speech_dialog.__file__, 'exec'), namespace)
    return namespace['build']


class Window:
    def __init__(self, *args, **kwargs):
        self.destroyed = False
        self.handlers = {}
    def __bool__(self): return not self.destroyed
    def GetId(self):
        assert not self.destroyed, 'Original deleted wx peer was queried for its ID.'
        return id(self)
    def Bind(self, event, handler): self.handlers[event] = handler
    def Destroy(self):
        assert not self.destroyed, 'Native Destroy was called twice.'
        self.destroyed = True
        handler = self.handlers.get('destroy')
        if handler is not None:
            handler(SimpleNamespace(GetId=lambda: id(self), GetEventObject=lambda: object(), Skip=lambda: None))
        return True


@pytest.mark.parametrize('termination', ['direct', 'native-parent'])
@pytest.mark.parametrize('when', ['worker-pending', 'completion-queued'])
def test_reader_detaches_before_nested_modal_unwinds_and_discards_actual_completion(termination, when):
    queue, cancel = Queue(), Event()
    entered, release = Event(), Event()
    accepted, forbidden, reports = [], [], []
    native = SimpleNamespace(dead=False)
    def touch(*args, **kwargs):
        if native.dead:
            forbidden.append(True)
            raise AssertionError('Queued reader work accessed a deleted native control.')
    controls = SimpleNamespace(Enable=touch, Disable=touch, check=touch, owned_registries=set())
    def report(*args, **kwargs):
        touch()
        reports.append(args)
    wx = SimpleNamespace(Dialog=Window, EVT_WINDOW_DESTROY='destroy',
                         CallAfter=lambda callback, *args: queue.put((callback, args)))
    executor = ThreadPoolExecutor(max_workers=1)
    dialog, run, state = reader_boundary()(wx, executor, cancel, controls, report)
    def work():
        entered.set()
        assert release.wait(5), 'Test worker was not released.'
        return 'actual queued result'
    try:
        assert run(work, accepted.append) is True
        assert entered.wait(5)
        dialog.handlers['destroy'](SimpleNamespace(GetId=lambda: id(dialog)+1,
                                                   GetEventObject=lambda: dialog, Skip=lambda: None))
        assert state() == (True, False) and not cancel.is_set()
        if when == 'completion-queued':
            release.set()
            callback, args = queue.get(timeout=5)
        if termination == 'direct':
            dialog.Destroy()
        else:
            Window.Destroy(dialog)
        native.dead = True
        assert state() == (False, True) and cancel.is_set()
        assert controls.live_closes == 1
        release.set()
        if when == 'worker-pending':
            executor.shutdown(wait=True, cancel_futures=False)
            assert queue.empty(), 'Detached worker completion must not require the GUI loop.'
        else:
            callback(*args)
        assert not accepted and not forbidden and not reports
        assert dialog.Destroy() is False
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('termination', ['direct', 'native-parent'])
@pytest.mark.parametrize('completion', ['done', 'done-raises-after-detach', 'failed'])
def test_reader_rechecks_owner_after_completion_callback_returns(termination, completion):
    queue, cancel = Queue(), Event()
    accepted, failed, forbidden, reports = [], [], [], []
    native = SimpleNamespace(dead=False)
    def touch(*args, **kwargs):
        if native.dead:
            forbidden.append(True)
            raise AssertionError('Completion resumed into a deleted native control.')
    controls = SimpleNamespace(Enable=touch, Disable=touch, check=touch, owned_registries=set())
    def report(*args, **kwargs):
        touch()
        reports.append(args)
    wx = SimpleNamespace(Dialog=Window, EVT_WINDOW_DESTROY='destroy',
                         CallAfter=lambda callback, *args: queue.put((callback, args)))
    executor = ThreadPoolExecutor(max_workers=1)
    dialog, run, state = reader_boundary()(wx, executor, cancel, controls, report)
    def detach():
        if termination == 'direct':
            dialog.Destroy()
        else:
            Window.Destroy(dialog)
        native.dead = True
    def work():
        if completion == 'failed':
            raise ValueError('The actual worker failed.')
        return 'actual queued result'
    def done(value):
        accepted.append(value)
        detach()
        if completion == 'done-raises-after-detach':
            raise ValueError('A nested completion dialog closed its owner.')
    def on_failed(error):
        touch()
        failed.append(str(error))
        detach()
    try:
        assert run(work, done, on_failed=on_failed) is True
        callback, args = queue.get(timeout=5)
        callback(*args)
        assert state() == (False, True) and cancel.is_set()
        assert accepted == ([] if completion == 'failed' else ['actual queued result'])
        assert failed == (['The actual worker failed.'] if completion == 'failed' else [])
        assert not forbidden and not reports
        assert dialog.Destroy() is False
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('termination', ['direct', 'native-parent'])
@pytest.mark.parametrize('when', ['worker-pending', 'completion-queued'])
def test_reader_unapplied_progress_selection_releases_actual_registry(tmp_path, termination, when):
    from carr_library.speech.progress_speech import activate_progress_voice
    from carr_library.speech.runtime import Voice, VoiceRegistry
    closed = Event()
    class Provider:
        name = 'Explicit synthetic progress voice'
        def voices(self):
            return (Voice('fixture:progress', 'Progress', 'fixture', 'en',
                          capabilities=('volume',)),)
        def close(self):
            closed.set()
    registry = VoiceRegistry([Provider()])
    selection = activate_progress_voice(tmp_path, 'fixture:progress',
        _registry_factory=lambda state_root, **options: registry)
    queue, cancel, entered, release = Queue(), Event(), Event(), Event()
    accepted, forbidden = [], []
    native = SimpleNamespace(dead=False)
    def touch(*args, **kwargs):
        if native.dead:
            forbidden.append(True)
            raise AssertionError('Unapplied selection touched a deleted native control.')
    controls = SimpleNamespace(Enable=touch, Disable=touch, check=touch, owned_registries=set())
    wx = SimpleNamespace(Dialog=Window, EVT_WINDOW_DESTROY='destroy',
                         CallAfter=lambda callback, *args: queue.put((callback, args)))
    executor = ThreadPoolExecutor(max_workers=1)
    dialog, run, state = reader_boundary()(wx, executor, cancel, controls, touch)
    def work():
        entered.set()
        assert release.wait(5)
        return selection
    try:
        assert run(work, accepted.append)
        assert entered.wait(5)
        if when == 'completion-queued':
            release.set()
            callback, args = queue.get(timeout=5)
            assert selection in controls.owned_registries
            assert not closed.is_set()
        if termination == 'direct':
            dialog.Destroy()
        else:
            Window.Destroy(dialog)
        native.dead = True
        release.set()
        if when == 'completion-queued':
            callback(*args)
        else:
            executor.shutdown(wait=True, cancel_futures=False)
            assert queue.empty()
        assert closed.wait(5), 'Unaccepted actual progress registry was not closed.'
        assert selection._ownership.finished.wait(5)
        assert not registry._providers and not controls.owned_registries
        assert selection._closed.is_set() and not accepted and not forbidden
        assert state() == (False, True)
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        selection.close()
        registry.close()


@pytest.mark.parametrize('failure', ['returns-false', 'raises'])
def test_reader_retains_failed_unapplied_cleanup_for_existing_retry(failure):
    queue, cancel, entered, release = Queue(), Event(), Event(), Event()
    controls = SimpleNamespace(Enable=lambda: None, Disable=lambda: None,
                               check=lambda: None, owned_registries=set())
    wx = SimpleNamespace(Dialog=Window, EVT_WINDOW_DESTROY='destroy',
                         CallAfter=lambda callback, *args: queue.put((callback, args)))
    executor = ThreadPoolExecutor(max_workers=1)
    dialog, run, _ = reader_boundary()(wx, executor, cancel, controls, lambda *a, **k: None)
    class Owned:
        attempts = 0
        def close(self):
            self.attempts += 1
            if failure == 'raises':
                raise OSError('Synthetic cleanup remains unconfirmed.')
            return False
    result = Owned()
    def work():
        entered.set()
        assert release.wait(5)
        return result
    try:
        assert run(work, lambda value: pytest.fail('Detached result was accepted.'))
        assert entered.wait(5)
        dialog.Destroy()
        release.set()
        executor.shutdown(wait=True, cancel_futures=False)
        assert queue.empty() and result.attempts == 1
        assert controls.owned_registries == {result}
        assert controls.retained_parent._reader_registry_retained == [result]
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('detach_during_acceptance', [False, True])
def test_reader_transfers_accepted_progress_owner_without_closing_it(tmp_path, detach_during_acceptance):
    from carr_library.speech.progress_speech import activate_progress_voice
    from carr_library.speech.runtime import Voice, VoiceRegistry
    closed = Event()
    class Provider:
        name = 'Accepted synthetic progress voice'
        def voices(self):
            return (Voice('fixture:accepted', 'Accepted', 'fixture', 'en',
                          capabilities=('volume',)),)
        def close(self):
            closed.set()
    registry = VoiceRegistry([Provider()])
    selection = activate_progress_voice(tmp_path, 'fixture:accepted',
        _registry_factory=lambda state_root, **options: registry)
    queue, cancel, accepted = Queue(), Event(), []
    controls = SimpleNamespace(Enable=lambda: None, Disable=lambda: None,
                               check=lambda: None, owned_registries=set())
    wx = SimpleNamespace(Dialog=Window, EVT_WINDOW_DESTROY='destroy',
                         CallAfter=lambda callback, *args: queue.put((callback, args)))
    executor = ThreadPoolExecutor(max_workers=1)
    dialog, run, _ = reader_boundary()(wx, executor, cancel, controls, lambda *a, **k: None)
    def accept(value):
        accepted.append(value)
        # The production activation callback transfers only after the progress
        # controller accepts the owner, before its nested settings modal.
        controls.transfer_owner(value)
        if detach_during_acceptance:
            dialog.Destroy()
    try:
        assert run(lambda: selection, accept)
        callback, args = queue.get(timeout=5)
        assert controls.owned_registries == {selection}
        callback(*args)
        dialog.Destroy()
        executor.shutdown(wait=True, cancel_futures=False)
        assert accepted == [selection] and not controls.owned_registries
        assert not closed.is_set() and not selection._closed.is_set()
        assert registry.get('fixture:accepted').id == 'fixture:accepted'
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        selection.close()
        assert selection._ownership.finished.wait(5)
        registry.close()
