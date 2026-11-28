"""Draft timer lifetime; native owner destruction is also checked on Windows."""
from contextlib import ExitStack
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from carr_library.ui import save_actions


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
        self.frame = SimpleNamespace(
            working=False, _closing=False, active=('library', 'topic'),
            GetId=Mock(return_value=101),
            library=SimpleNamespace(root=Path('synthetic-library')),
            _dirty=Mock(return_value=True), menu_bar=Mock(), report=Mock(), Close=Mock(),
            evidence=SimpleNamespace(displayed=SimpleNamespace(evidence_id='evidence'),
                                     note=Mock()))
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

    def destroy(self, owner, *, event_id=None):
        if event_id is None:
            event_id = 101 if owner is self.frame else 102
        event = SimpleNamespace(GetId=lambda: event_id,
                                GetEventObject=Mock(return_value=owner), Skip=Mock())
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
        self.frame.Unbind.assert_not_called()
        self.frame._dirty.side_effect = AssertionError('A queued tick touched a destroyed frame.')
        self.tick(None)
        self.frame._dirty.assert_not_called()
        self.write.assert_not_called()

    def test_normal_close_stops_the_timer_before_native_owner_deletion(self):
        self.frame._closing = True
        self.assert_owner_stopped()

    def test_direct_destroy_also_blocks_a_previously_queued_autosave(self):
        self.assert_owner_stopped()

    def test_base_destructor_proxy_stops_timer_without_accessing_invalid_original_wrapper(self):
        # SIP's generated subclass destructor invalidates the original wrapper
        # before the native base destructor sends its destroy event.
        self.frame.GetId.side_effect = RuntimeError('Original native frame was deleted.')
        self.frame.Unbind.side_effect = RuntimeError('Original native frame was deleted.')
        self.frame._dirty.side_effect = AssertionError('A late tick touched a deleted frame.')
        self.destroy(object(), event_id=101)
        self.assertFalse(self.timer.running)
        self.timer.Stop.assert_called_once_with()
        self.frame.Unbind.assert_not_called()
        self.frame.GetId.assert_called_once_with()
        self.tick(None)
        self.frame._dirty.assert_not_called()
        self.write.assert_not_called()

    def test_repeated_owner_notification_stops_once_and_keeps_queued_ticks_inert(self):
        self.destroy(object(), event_id=101)
        self.destroy(object(), event_id=101)
        self.assertFalse(self.timer.running)
        self.timer.Stop.assert_called_once_with()
        self.frame.Unbind.assert_not_called()
        self.frame._dirty.side_effect = AssertionError('A late tick touched a deleted frame.')
        self.tick(None)
        self.frame._dirty.assert_not_called()
        self.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
