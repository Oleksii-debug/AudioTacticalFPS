"""Backup selections bind the chosen library and remain cancellation-aware."""
from types import SimpleNamespace as N
from unittest.mock import Mock
import sys

import pytest

from carr_library.ui import backup_options
from test_desktop_handlers import classes, frame


@pytest.mark.parametrize('accepted,checked,expected', [(True, False, False),
    (True, True, True), (False, True, None)])
def test_native_option_protocol_defaults_off_and_disposes(accepted, checked, expected, monkeypatch):
    option = Mock()
    option.GetValue.return_value = checked
    dialog = Mock()
    dialog.ShowModal.return_value = 1 if accepted else 0
    wx = N(Dialog=Mock(return_value=dialog), BoxSizer=Mock(), StaticText=Mock(),
           CheckBox=Mock(return_value=option), VERTICAL=1, EXPAND=2, ALL=4,
           OK=8, CANCEL=16, ID_OK=1)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    assert backup_options.choose_backup_options(object()) is expected
    option.SetValue.assert_called_once_with(False)
    option.SetName.assert_called_once_with('Include generated audio saved in this library')
    option.SetFocus.assert_called_once()
    dialog.Destroy.assert_called_once()
    description = wx.StaticText.call_args.kwargs['label']
    assert 'captured source bytes' in description and 'require a new review' in description
    assert 'Unfinished imports are excluded' not in description


def prepare(classes, tmp_path, monkeypatch, selected):
    value = frame(classes)
    value.library = N(root=tmp_path / 'original-library', backup=Mock(return_value='synthetic-checksum'))
    value.run_job = Mock()
    dialog = Mock()
    dialog.ShowModal.return_value = classes[0]['wx'].ID_OK
    dialog.GetPath.return_value = str(tmp_path / 'backups')
    classes[0]['wx'].DirDialog = Mock(return_value=dialog)
    monkeypatch.setattr(backup_options, 'choose_backup_options', Mock(return_value=selected))
    return value, dialog


@pytest.mark.parametrize('selected', [False, True])
def test_explicit_mode_and_live_cancellation_reach_selected_library(classes, tmp_path, monkeypatch, selected):
    value, picker = prepare(classes, tmp_path, monkeypatch, selected)
    library = value.library
    value.backup(None)
    library.backup.assert_not_called()
    operation, completed = value.run_job.call_args.args[1:]
    assert operation() == 'synthetic-checksum'
    assert library.backup.call_args.kwargs['include_generated'] is selected
    cancellation = library.backup.call_args.kwargs['cancelled']
    assert cancellation() is False
    value.cancel_event.set()
    assert cancellation() is True
    completed('synthetic-checksum')
    result = classes[0]['show_text'].call_args.args[2]
    assert ('Generated audio is included.' if selected else 'Generated audio is omitted;') in result
    picker.Destroy.assert_called_once()


def test_cancelled_option_opens_no_destination_and_schedules_no_backup(classes, tmp_path, monkeypatch):
    value, _ = prepare(classes, tmp_path, monkeypatch, None)
    value.backup(None)
    classes[0]['wx'].DirDialog.assert_not_called()
    value.run_job.assert_not_called()


@pytest.mark.parametrize('stage', ['options', 'destination', 'queued', 'completed'])
def test_library_change_never_routes_backup_or_receipt_to_new_root(classes, tmp_path, monkeypatch, stage):
    value, picker = prepare(classes, tmp_path, monkeypatch, True)
    original = value.library
    replacement = N(root=tmp_path / 'other-library', backup=Mock())
    if stage == 'options':
        def options(parent):
            value.library = replacement
            return True
        monkeypatch.setattr(backup_options, 'choose_backup_options', options)
    elif stage == 'destination':
        def path():
            value.library = replacement
            return str(tmp_path / 'backups')
        picker.GetPath.side_effect = path
    value.backup(None)
    if stage in ('options', 'destination'):
        value.run_job.assert_not_called()
    else:
        operation, completed = value.run_job.call_args.args[1:]
        value.library = replacement
        if stage == 'queued':
            with pytest.raises(ValueError, match='folder changed'):
                operation()
        else:
            completed('synthetic-checksum')
            classes[0]['show_text'].assert_not_called()
    original.backup.assert_not_called()
    replacement.backup.assert_not_called()
