"""No wx application, source read or provider request when another job owns the host."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.ui import translation_dialog


@pytest.mark.parametrize('working,busy', [(True, False), (False, True), (True, True)])
def test_busy_translation_keeps_host_and_source_without_opening_native_dialog(monkeypatch, working, busy):
    factory = Mock(side_effect=AssertionError('Must not construct a controller while busy'))
    monkeypatch.setattr(translation_dialog, 'ConnectionsController', factory)
    frame = SimpleNamespace(working=working, busy=busy, _closing=False, report=Mock())
    translation_dialog.show_translation_dialog(frame, 'User text must remain unsent')
    factory.assert_not_called()
    frame.report.assert_called_once()
    assert 'Wait for the current operation' in frame.report.call_args.args[0]


def test_closing_translation_does_not_access_controls_or_credentials(monkeypatch):
    factory = Mock(side_effect=AssertionError('Must not access a closing host'))
    monkeypatch.setattr(translation_dialog, 'ConnectionsController', factory)
    frame = SimpleNamespace(_closing=True, report=Mock())
    translation_dialog.show_translation_dialog(frame, 'Unsent text')
    factory.assert_not_called()
    frame.report.assert_not_called()
