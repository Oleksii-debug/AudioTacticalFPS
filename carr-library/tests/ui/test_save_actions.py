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
        self.owner_id = 1047
        self.frame = SimpleNamespace(
            GetId=Mock(return_value=self.owner_id),
            working=False, _closing=False, active=('library', 'topic'),
            library=SimpleNamespace(root=Path('synthetic-library')),
            _dirty=Mock(return_value=True), _restore_note_focus=Mock(), menu_bar=Mock(), report=Mock(), Close=Mock(),
            evidence=SimpleNamespace(displayed=SimpleNamespace(evidence_id='evidence'),
                                     note=Mock(), _flush_recovery=Mock()))
        self.frame.evidence.note.GetValue.return_value = 'Retained editable note.'
        self.frame.Bind = lambda event, handler, *args: self.bindings.setdefault(event, handler)
        self.frame.Unbind = Mock(side_effect=lambda event, **kwargs: self.bindings.pop(event, None))
        wx = SimpleNamespace(Menu=Mock(), Timer=Mock(return_value=self.timer),
                             ID_ANY=-1, ID_EXIT=1, EVT_MENU='menu',
                             EVT_TIMER='timer', EVT_WINDOW_DESTROY='destroy')
        stack.enter_context(patch.dict(sys.modules, {'wx': wx}))
        stack.enter_context(patch.object(save_actions, 'register_menu_command'))
        self.write = self.frame.evidence._flush_recovery
        save_actions.install_save_actions(self.frame)
        self.tick = self.bindings['timer']
        wx.Timer.assert_called_once_with(self.frame)
        self.assertTrue(self.timer.running)
        self.assertEqual(self.timer.interval, 3000)

    def destroy(self, owner, *, identity=None):
        identity = (self.owner_id if owner is self.frame else 2097) if identity is None else identity
        event = SimpleNamespace(GetEventObject=lambda: owner, GetId=lambda: identity, Skip=Mock())
        callback = self.bindings.get('destroy')
        if callback is not None:
            callback(event)
            event.Skip.assert_called_once_with()

    def test_live_timer_flushes_the_one_canonical_draft_producer(self):
        self.bindings['timer'](None)
        self.bindings['timer'](None)
        self.assertEqual(self.write.call_count, 2)
        self.frame.evidence.note.GetValue.assert_not_called()

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

    def test_native_destroy_wrapper_needs_only_the_previously_captured_owner_id(self):
        self.frame.GetId.side_effect = AssertionError('A dying frame must not be queried.')
        self.frame.Unbind.side_effect = AssertionError('A dying frame must not be unbound.')
        self.frame._dirty.side_effect = AssertionError('A queued tick touched a destroyed frame.')
        self.destroy(object(), identity=self.owner_id)
        self.assertFalse(self.timer.running)
        self.timer.Stop.assert_called_once_with()
        self.tick(None)
        self.frame._dirty.assert_not_called()
        self.frame.Unbind.assert_not_called()
        self.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
