"""Draft timer lifetime; native owner destruction is also checked on Windows."""
import ast
from contextlib import ExitStack
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import carr_library
from carr_library.ui import save_actions
from carr_library.ui.operation_progress import OperationProgress


def desktop_close_class():
    """Exercise the real installed close handler with a controlled base veto."""
    source = Path(carr_library.__file__).resolve().parent / 'ui' / 'desktop_app.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    desktop = next(node for node in parsed.body
                   if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
    methods = [node for node in desktop.body
               if isinstance(node, ast.FunctionDef) and node.name in {'on_close', 'Destroy'}]

    class BaseFrame:
        def on_close(self, event):
            if self.base_veto:
                event.Veto()
            else:
                event.Skip()

        def Destroy(self):
            self.native_destroy()
            return True

    selected = ast.ClassDef(name='DesktopFrame', bases=[ast.Name(id='BaseFrame', ctx=ast.Load())],
                            keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[selected], type_ignores=[]))
    namespace = {'BaseFrame': BaseFrame}
    exec(compile(module, str(source), 'exec'), namespace)
    return namespace['DesktopFrame']


def close_event(can_veto=True):
    event = SimpleNamespace(vetoed=False)
    event.CanVeto = lambda: can_veto
    event.GetVeto = lambda: event.vetoed
    event.Veto = lambda: setattr(event, 'vetoed', True)
    event.Skip = Mock()
    return event


class SaveActionsLifetime(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.bindings = {}
        self.timer = SimpleNamespace(running=False, interval=None)

        def start(interval):
            self.timer.running, self.timer.interval = True, interval

        def stop():
            self.timer.running = False

        self.timer.Start, self.timer.Stop = start, Mock(side_effect=stop)
        self.frame = desktop_close_class()()
        self.frame.__dict__.update(
            working=False, _closing=False, active=('library', 'topic'),
            base_veto=False, cancel_event=Event(), executor=Mock(),
            GetId=Mock(return_value=417), native_destroy=Mock(),
            library=SimpleNamespace(root=Path('synthetic-library')),
            _dirty=Mock(return_value=True), menu_bar=Mock(), report=Mock(), Close=Mock(),
            evidence=SimpleNamespace(displayed=SimpleNamespace(evidence_id='evidence'),
                                     note=Mock()))
        self.progress = object.__new__(OperationProgress)
        self.progress.closed, self.progress._pending_sound = False, object()
        self.progress.timer = SimpleNamespace(running=True)
        self.progress.timer.Stop = Mock(side_effect=lambda: setattr(self.progress.timer, 'running', False))
        self.progress.sound = SimpleNamespace(close=Mock())
        self.progress._players, self.progress._audio_holds = set(), set()
        self.frame.operation_progress = self.progress
        self.frame.evidence.note.GetValue.return_value = 'Retained editable note.'
        self.frame.Bind = lambda event, handler, *args: self.bindings.setdefault(event, handler)
        self.frame.Unbind = Mock(side_effect=lambda event, **kwargs: self.bindings.pop(event, None))
        wx = SimpleNamespace(Menu=Mock(), Timer=Mock(return_value=self.timer),
                             ID_ANY=-1, ID_EXIT=1, EVT_MENU='menu',
                             EVT_TIMER='timer', EVT_WINDOW_DESTROY='destroy')
        stack.enter_context(patch.dict(sys.modules, {'wx': wx}))
        self.write = stack.enter_context(patch.object(save_actions, 'atomic_json'))
        save_actions.install_save_actions(self.frame)
        self.tick = self.bindings['timer']
        wx.Timer.assert_called_once_with(self.frame)
        self.assertTrue(self.timer.running)
        self.assertEqual(self.timer.interval, 3000)

    def destroy(self, owner, *, owner_id=None):
        if owner_id is None:
            owner_id = 417 if owner is self.frame else 418
        event = SimpleNamespace(GetEventObject=lambda: owner, GetId=lambda: owner_id, Skip=Mock())
        callback = self.bindings.get('destroy')
        if callback is not None:
            callback(event)
            event.Skip.assert_called_once_with()

    def test_live_draft_timer_saves_and_deduplicates_the_current_note(self):
        self.bindings['timer'](None)
        self.bindings['timer'](None)
        self.write.assert_called_once()
        path, value = self.write.call_args.args
        self.assertEqual(path, Path('synthetic-library/state/recovery/research-note.json'))
        self.assertEqual(value['note'], 'Retained editable note.')
        self.assertEqual((value['workspace'], value['topic'], value['evidence']),
                         ('library', 'topic', 'evidence'))

    def test_child_destruction_keeps_the_live_frame_autosave_running(self):
        self.destroy(object())
        self.assertTrue(self.timer.running)
        self.timer.Stop.assert_not_called()
        self.frame.Unbind.assert_not_called()
        self.bindings['timer'](None)
        self.write.assert_called_once()

    def assert_owner_stopped(self):
        self.destroy(self.frame)
        self.assertFalse(self.timer.running)
        self.timer.Stop.assert_called_once_with()
        self.frame.Unbind.assert_called_once_with('timer', handler=self.tick, source=self.timer)
        self.frame._dirty.side_effect = AssertionError('A queued tick touched a destroyed frame.')
        self.tick(None)
        self.frame._dirty.assert_not_called()
        self.write.assert_not_called()

    def test_normal_close_stops_the_timer_before_native_owner_deletion(self):
        self.frame._closing = True
        self.assert_owner_stopped()

    def test_direct_destroy_also_blocks_a_previously_queued_autosave(self):
        self.assert_owner_stopped()

    def assert_accepted_close_stopped(self, *, working=False, can_veto=True):
        self.frame.working = working
        event = close_event(can_veto)
        self.frame.on_close(event)
        self.assertFalse(event.vetoed)
        self.assertTrue(self.frame._closing)
        self.assertTrue(self.frame.cancel_event.is_set())
        self.frame.executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        self.assertFalse(self.timer.running)
        self.timer.Stop.assert_called_once_with()
        self.frame.Unbind.assert_called_once_with('timer', handler=self.tick, source=self.timer)
        self.frame._dirty.side_effect = AssertionError('A queued tick touched a closing frame.')
        self.tick(None)
        self.frame._dirty.assert_not_called()
        self.write.assert_not_called()
        self.assertTrue(self.progress.closed)
        self.assertFalse(self.progress.timer.running)
        self.progress.timer.Stop.assert_called_once_with()
        self.progress.sound.close.assert_called_once_with()
        self.progress._tick()
        # Deferred destruction must neither restart nor clean up the timer twice.
        self.destroy(self.frame)
        self.timer.Stop.assert_called_once_with()
        self.frame.Unbind.assert_called_once()

    def test_accepted_close_stops_before_any_destroy_notification(self):
        self.assert_accepted_close_stopped()

    def test_forced_close_stops_before_any_destroy_notification(self):
        self.assert_accepted_close_stopped(working=True, can_veto=False)

    def test_running_job_veto_keeps_the_live_timer_and_editor(self):
        self.frame.working = True
        event = close_event()
        self.frame.on_close(event)
        self.assertTrue(event.vetoed)
        self.assertFalse(self.frame._closing or self.frame.cancel_event.is_set())
        self.frame.executor.shutdown.assert_not_called()
        self.timer.Stop.assert_not_called()
        self.frame.Unbind.assert_not_called()
        self.assertTrue(self.timer.running)

        self.assertFalse(self.progress.closed)
        self.assertTrue(self.progress.timer.running)
        self.progress.timer.Stop.assert_not_called()

    def test_parent_unsaved_note_veto_keeps_autosave_working(self):
        self.frame.base_veto = True
        event = close_event()
        self.frame.on_close(event)
        self.assertTrue(event.vetoed)
        self.assertFalse(self.frame._closing or self.frame.cancel_event.is_set())
        self.frame.executor.shutdown.assert_not_called()
        self.timer.Stop.assert_not_called()
        self.frame.Unbind.assert_not_called()
        self.assertTrue(self.timer.running)
        self.tick(None)
        self.write.assert_called_once()
        self.assertFalse(self.progress.closed)
        self.assertTrue(self.progress.timer.running)
        self.progress.timer.Stop.assert_not_called()

    def test_direct_destroy_stops_both_timers_before_the_native_base(self):
        def native_destroy():
            self.assertFalse(self.timer.running or self.progress.timer.running)
            self.assertTrue(self.progress.closed and self.frame._closing)
            self.assertTrue(self.frame.cancel_event.is_set())
            self.frame.executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        self.frame.native_destroy.side_effect = native_destroy
        self.assertTrue(self.frame.Destroy())
        self.frame.native_destroy.assert_called_once_with()
        self.assert_owner_stopped()
        self.progress.timer.Stop.assert_called_once_with()
        self.progress.sound.close.assert_called_once_with()
        self.progress._tick()

    def test_close_then_direct_destroy_closes_each_owned_timer_once(self):
        self.assert_accepted_close_stopped()
        self.assertTrue(self.frame.Destroy())
        self.timer.Stop.assert_called_once_with()
        self.frame.Unbind.assert_called_once()
        self.progress.timer.Stop.assert_called_once_with()
        self.progress.sound.close.assert_called_once_with()
        self.frame.executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_native_owner_proxy_stops_draft_without_touching_dead_wrapper(self):
        proxy = SimpleNamespace(Unbind=Mock(side_effect=lambda event, **kwargs: self.bindings.pop(event, None)))
        self.frame.Unbind.side_effect = AssertionError('The derived native wrapper has already been destroyed.')
        self.destroy(proxy, owner_id=417)
        self.assertFalse(self.timer.running)
        self.timer.Stop.assert_called_once_with()
        self.frame.Unbind.assert_not_called()
        proxy.Unbind.assert_called_once_with('timer', handler=self.tick, source=self.timer)
        self.frame._dirty.side_effect = AssertionError('A queued tick touched a destroyed frame.')
        self.tick(None)
        self.frame._dirty.assert_not_called()
        self.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
