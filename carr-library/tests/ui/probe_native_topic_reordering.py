"""Explicit desktop probe: pytest tests/ui/probe_native_topic_reordering.py.

Requires wx and an active display. A skipped probe is NOT_RUN, never native
keyboard, Windows, or NVDA acceptance evidence.
"""
import os
import time

import pytest

from test_topic_reordering import topics


def test_native_buttons_support_space_and_restore_tree_focus(topics):
    if os.name != 'nt' and not os.environ.get('DISPLAY'):
        pytest.skip('Native wx keyboard check requires a display.')
    wx = pytest.importorskip('wx', reason='Native wx check requires the desktop runtime.')
    from carr_library.ui.topic_panel import TopicPanel

    app = wx.App.Get() or wx.App(False)
    frame = wx.Frame(None, title='Synthetic topic reordering', size=(700, 900))
    current = TopicPanel(frame, 'a', topics.transaction)
    frame.Show()

    def pump():
        for _ in range(10):
            wx.Yield()
            time.sleep(0.01)

    try:
        pump()
        current.tree.SelectItem(current.items['root-b'])
        for label, order in [
            ('Move selected topic up', ('root-b', 'root-a', 'root-c')),
            ('Move selected topic down', ('root-a', 'root-b', 'root-c')),
        ]:
            button = wx.FindWindowByLabel(label, current)
            assert isinstance(button, wx.Button) and button.AcceptsFocusFromKeyboard()
            button.SetFocus()
            pump()
            assert wx.Window.FindFocus() is button
            simulator = wx.UIActionSimulator()
            assert simulator.KeyDown(wx.WXK_SPACE)
            pump()
            assert simulator.KeyUp(wx.WXK_SPACE)
            pump()
            assert topics.order() == order
            assert current._selected() == 'root-b'
            assert wx.Window.FindFocus() is current.tree
    finally:
        frame.Destroy()
        pump()
