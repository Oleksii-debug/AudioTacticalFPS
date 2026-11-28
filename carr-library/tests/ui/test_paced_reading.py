"""Actual pacing handlers, exact source ownership and timer cleanup."""
from types import SimpleNamespace

import pytest

from tests.ui.test_reader_appearance import view, Widget, Sizer
from carr_library.ui import paced_reading as ui


@pytest.fixture
def paced(view, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ui, 'monotonic', lambda: clock[0])
    class Timer:
        def __init__(self, owner):
            self.owner, self.running, self.stops = owner, False, 0
        def Start(self, milliseconds):
            self.owner.native()
            assert milliseconds == 25
            self.running = True
        def Stop(self):
            self.running = False
            self.stops += 1
    view.wx.Timer, view.wx.WrapSizer, view.wx.Choice = Timer, Sizer, Widget
    view.wx.EVT_CHOICE, view.wx.EVT_TIMER = 'choice', 'timer'
    source = '😀 first\r\nsecond  e\u0301 最後。'
    selection = (source.index('first'), len(source))
    initial = (2, 7)
    current, ranges, restored = [source], [], []
    revoked = [False]
    def guard():
        if revoked[0]:
            raise ValueError('Source scope closed.')
    def highlight(first, last):
        ranges.append((first, last))
        view.control.selection = (first, last)
    def restore():
        restored.append(True)
        view.control.selection = initial
    dialog = ui.create_paced_reader(view.owner, view.state.root, source, selection,
        current_text=lambda: current[0], highlight=highlight, restore_selection=restore,
        guard=guard, wx_module=view.wx)
    result = SimpleNamespace(view=view, dialog=dialog, clock=clock, source=source,
        current=current, ranges=ranges, restored=restored, revoked=revoked, initial=initial)
    try:
        yield result
    finally:
        dialog.dispose()
        if dialog:
            dialog.Destroy()


def test_start_pause_delayed_tick_stop_and_close_preserve_original_text(paced):
    dialog = paced.dialog
    assert not dialog.timer.running and paced.ranges == []
    dialog.rate.SetValue(1200); dialog.group.SetValue(1)
    dialog.on_start()
    assert dialog.preview.GetValue() == 'first' and dialog.timer.running
    assert paced.source[paced.ranges[-1][0]:paced.ranges[-1][1]] == 'first'
    paced.clock[0] = 100
    dialog.on_tick()
    assert dialog.preview.GetValue() == 'second'
    dialog.on_pause()
    assert not dialog.timer.running and dialog.model.state == 'paused'
    dialog.on_step(1)
    assert dialog.preview.GetValue() == 'e\u0301'
    dialog.on_step(-1)
    assert dialog.preview.GetValue() == 'second'
    dialog.on_stop()
    assert dialog.model is None and not dialog.timer.running
    assert paced.view.control.selection == paced.initial
    assert paced.current == [paced.source]
    dialog.on_close()
    assert not dialog.alive and not dialog.timer.running


def test_changing_a_setting_invalidates_current_sequence_without_starting_again(paced):
    dialog = paced.dialog
    dialog.on_start()
    dialog.rate.SetValue(500)
    dialog.on_change()
    assert dialog.model is None and not dialog.timer.running
    assert 'Settings changed' in dialog.status.GetValue()
    assert paced.view.control.selection == paced.initial
    dialog.on_start()
    assert dialog.model.options.words_per_minute == 500
    assert dialog.preview.GetValue() == 'first'


@pytest.mark.parametrize('change', ['text', 'guard'])
def test_stale_source_stops_before_highlighting_or_restoring_an_unrelated_selection(paced, change):
    dialog = paced.dialog
    dialog.on_start()
    before = list(paced.ranges)
    if change == 'text':
        paced.current[0] = 'A newly selected unrelated source.'
    else:
        paced.revoked[0] = True
    paced.clock[0] = 100
    dialog.on_tick()
    assert paced.ranges == before and dialog.invalid and not dialog.timer.running
    assert not dialog.buttons['start'].IsEnabled()
    dialog.on_close()
    assert not paced.restored


def test_native_owner_destruction_stops_timer_before_late_callbacks(paced):
    dialog = paced.dialog
    dialog.on_start()
    dialog.on_destroy(SimpleNamespace(GetId=lambda: dialog._native_id, Skip=lambda: None))
    assert not dialog.alive and not dialog.timer.running
    before = list(paced.ranges)
    dialog.Destroy()
    paced.clock[0] = 100
    dialog.on_tick(); dialog.on_start(); dialog.on_pause(); dialog.on_step(1); dialog.on_stop()
    assert paced.ranges == before


def test_whole_range_is_an_explicit_selection_and_source_copy_stays_byte_exact(paced):
    dialog = paced.dialog
    dialog.range.SetSelection(2)
    dialog.group.SetValue(2)
    dialog.on_start()
    assert dialog.preview.GetValue() == '😀 first'
    paced.clock[0] = 100
    dialog.on_tick()
    assert dialog.preview.GetValue() == 'second  e\u0301'
    assert paced.current[0].encode('utf-8') == paced.source.encode('utf-8')
