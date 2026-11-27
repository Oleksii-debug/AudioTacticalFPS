"""Recovery timer lifetime on the current evidence-panel owner.

The shared fixture uses a real library and EvidenceDrafts store. Only native
controls are doubles; installed Windows timer delivery has its separate probe.
"""
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# Keep one real-library recovery fixture across the two regression suites.
recovery = runpy.run_path(str(Path(__file__).with_name(
    'test_evidence_draft_recovery.py')))['recovery']


def edit(panel, text):
    panel.note.SetValue(text)
    event = SimpleNamespace(Skip=Mock())
    panel._recovery_edited(event)
    event.Skip.assert_called_once_with()


def destroyed(panel, owner, native_id):
    event = SimpleNamespace(GetId=lambda: native_id,
                            GetEventObject=lambda: owner, Skip=Mock())
    panel._recovery_destroy(event)
    event.Skip.assert_called_once_with()


def test_live_draft_timer_saves_and_deduplicates_the_current_note(recovery):
    panel, app, _, _, _ = recovery
    write = panel._recovery_store.checkpoint = Mock(
        wraps=panel._recovery_store.checkpoint)
    edit(panel, 'Retained editable note.')
    panel._recovery_timer.StartOnce.assert_called_once_with(1000)
    panel._flush_recovery()
    panel._flush_recovery()
    write.assert_called_once()
    draft = panel._recovery_store.get(panel.workspace, panel.topic,
                                      panel.displayed.evidence_id)
    assert draft.text == 'Retained editable note.'
    assert draft.matches(panel.displayed)
    assert app.evidence(panel.workspace, panel.topic)[0].note == 'Saved note'


def test_child_destruction_keeps_the_live_panel_autosave_running(recovery):
    panel, _, _, _, _ = recovery
    edit(panel, 'First checkpoint')
    edit(panel, 'Coalesced child-safe edit')
    destroyed(panel, object(), panel._recovery_native_id + 1)
    assert not panel._recovery_disposed
    panel._recovery_timer.Stop.assert_not_called()
    panel.Unbind.assert_not_called()
    assert panel._flush_recovery()
    assert panel._recovery_store.get(panel.workspace, panel.topic,
        panel.displayed.evidence_id).text == 'Coalesced child-safe edit'


def assert_owner_stopped(recovery, *, rewrapped=False):
    panel, _, _, wx, _ = recovery
    edit(panel, 'First checkpoint')
    edit(panel, 'Final cached edit before deletion')
    panel.note.GetValue = Mock(side_effect=AssertionError(
        'A queued callback read a destroyed text control.'))
    panel._dirty = Mock(side_effect=AssertionError(
        'A queued callback touched a destroyed native owner.'))
    panel.GetId = Mock(side_effect=AssertionError(
        'The original native wrapper is invalid.'))
    owner = SimpleNamespace(Unbind=Mock()) if rewrapped else panel
    if rewrapped:
        panel.Unbind.side_effect = AssertionError(
            'The original native wrapper is invalid.')
    destroyed(panel, owner, panel._recovery_native_id)
    assert panel._recovery_disposed
    panel._recovery_timer.Stop.assert_called_once_with()
    owner.Unbind.assert_called_once_with(wx.EVT_TIMER,
        handler=panel._flush_recovery, source=panel._recovery_timer)
    if rewrapped:
        panel.Unbind.assert_not_called()
    panel.GetId.assert_not_called()
    saved = panel._recovery_store.path.read_bytes()
    assert panel._recovery_store.get(panel.workspace, panel.topic,
        panel.displayed.evidence_id).text == 'Final cached edit before deletion'
    panel._recovery_pending = panel.displayed, 'Obsolete queued edit'
    assert panel._flush_recovery(None) is False
    assert panel._recovery_store.path.read_bytes() == saved
    panel.note.GetValue.assert_not_called()
    panel._dirty.assert_not_called()


def test_normal_close_stops_the_timer_before_native_owner_deletion(recovery):
    recovery[0].frame._closing = True
    assert_owner_stopped(recovery)


def test_direct_destroy_also_blocks_a_previously_queued_autosave(recovery):
    assert_owner_stopped(recovery)


def test_rewrapped_native_owner_stops_without_accessing_invalid_original_wrapper(recovery):
    assert_owner_stopped(recovery, rewrapped=True)
