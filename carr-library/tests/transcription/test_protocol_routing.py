"""Explicit API/CLI selection preserves both schemas without decoder fallback."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library import transcription, transcription_cli
from carr_library.transcription import service as local_files
from carr_library.ui import transcription_dialog as ui


def test_package_preserves_strict_constants_and_both_constructor_shapes(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Receipt workspace')
    strict = transcription.TranscriptionService(app, workspace)
    checkpoint = transcription.TranscriptionService(app.root / 'state/transcription',
        protocol='local-file-job/1')
    assert type(strict) is transcription.TranscriptionService
    assert type(checkpoint) is local_files.TranscriptionService
    assert strict.protocol == 'carr-transcription-receipts/1'
    assert checkpoint.protocol == 'local-file-job/1'
    assert strict.jobs_root.parent.name != 'transcription'
    assert checkpoint.jobs_root == app.root / 'state/transcription/jobs'
    assert transcription.DEFAULT_SETTINGS['word_timestamps'] is True
    assert 'word_timestamps' not in transcription.CHECKPOINT_DEFAULT_SETTINGS
    assert Path(transcription.__file__).name == '__init__.py'
    assert transcription.settings.__globals__ is vars(transcription)


@pytest.mark.parametrize('protocol,scope', [
    ('unknown', True), ('', True), (False, True), (0, True),
    ('local-file-job/1', True), ('carr-transcription-receipts/1', False)])
def test_wrong_protocol_scope_never_constructs_another_adapter(tmp_path, protocol, scope):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original')
    with pytest.raises(ValueError):
        transcription.TranscriptionService(app if scope else app.root,
            workspace if scope else None, protocol=protocol)
    assert not (app.root / 'state/transcription').exists()


@pytest.mark.parametrize('args,expected', [
    (['--workspace', 'selected', 'jobs'], 'receipts'),
    (['--protocol', 'carr-transcription-receipts/1', '--workspace=selected', 'jobs'], 'receipts'),
    (['--protocol=local-file-job/1', 'jobs'], 'local-file'),
    (['runtimes'], 'local-file'), (['jobs'], 'local-file'),
])
def test_cli_protocol_routes_once_without_rewriting_commands(monkeypatch, args, expected):
    calls = []
    monkeypatch.setattr(transcription_cli, '_receipt_main',
        lambda argv: calls.append(('receipts', argv)) or 17)
    monkeypatch.setattr(transcription_cli, '_local_file_main',
        lambda argv: calls.append(('local-file', argv)) or 17)
    assert transcription_cli.main(args) == 17
    assert len(calls) == 1 and calls[0][0] == expected
    assert '--protocol' not in calls[0][1]


@pytest.mark.parametrize('args', [
    ['--protocol', 'local-file-job/1', '--workspace', 'x', 'jobs'],
    ['--protocol', 'invalid', 'jobs'],
    ['--protocol', 'local-file-job/1', '--protocol', 'carr-transcription-receipts/1', 'jobs'],
])
def test_cli_rejects_mixed_or_duplicate_protocol_authority(monkeypatch, args):
    operation = Mock(side_effect=AssertionError('No service should be selected'))
    monkeypatch.setattr(transcription_cli, '_receipt_main', operation)
    monkeypatch.setattr(transcription_cli, '_local_file_main', operation)
    with pytest.raises(SystemExit):
        transcription_cli.main(args)
    operation.assert_not_called()


@pytest.mark.parametrize('protocol', ['carr-transcription-receipts/1', 'local-file-job/1'])
def test_one_native_entry_routes_only_the_explicit_protocol(monkeypatch, tmp_path, protocol):
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace())
    frame = SimpleNamespace(working=False, _closing=False,
        library=SimpleNamespace(root=tmp_path), report=Mock())
    strict, checkpoint = Mock(), Mock()
    monkeypatch.setattr(ui, 'show_transcription', strict)
    monkeypatch.setattr(ui, 'show_transcription_dialog', checkpoint)
    ui.show_transcription_router(frame, protocol=protocol)
    assert strict.call_count == (protocol == 'carr-transcription-receipts/1')
    assert checkpoint.call_count == (protocol == 'local-file-job/1')


def test_failed_selected_adapter_never_falls_back_to_another_protocol(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace())
    frame = SimpleNamespace(working=False, _closing=False,
        library=SimpleNamespace(root=tmp_path), report=Mock())
    checkpoint = Mock()
    monkeypatch.setattr(ui, 'show_transcription', Mock(side_effect=ValueError('Invalid strict receipt')))
    monkeypatch.setattr(ui, 'show_transcription_dialog', checkpoint)
    with pytest.raises(ValueError, match='Invalid strict receipt'):
        ui.show_transcription_router(frame, protocol='carr-transcription-receipts/1')
    checkpoint.assert_not_called()


def test_closed_during_protocol_selection_cannot_open_either_window(monkeypatch, tmp_path):
    frame = SimpleNamespace(working=False, _closing=False,
        library=SimpleNamespace(root=tmp_path), report=Mock())
    class Picker:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def SetName(self, value): pass
        def SetSelection(self, value): pass
        def ShowModal(self):
            frame._closing = True
            return 1
        def GetSelection(self): return 0
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(SingleChoiceDialog=Picker, ID_OK=1))
    strict, checkpoint = Mock(), Mock()
    monkeypatch.setattr(ui, 'show_transcription', strict)
    monkeypatch.setattr(ui, 'show_transcription_dialog', checkpoint)
    ui.show_transcription_router(frame)
    strict.assert_not_called()
    checkpoint.assert_not_called()


def test_operations_tasks_reuses_active_native_menu_capture_and_does_not_duplicate(monkeypatch):
    from carr_library.ui.operations_dialog import install_operations_actions
    from carr_library.ui.productivity import collect_commands
    from carr_library.keybindings import validate_bindings
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU='menu'))
    item = SimpleNamespace(IsSeparator=lambda: False, GetSubMenu=lambda: None,
        GetItemLabel=lambda: 'Tasks and resources...', GetId=lambda: 12002)
    menu = SimpleNamespace(Append=Mock(return_value=item), GetMenuItems=lambda: (item,))
    bar = SimpleNamespace(GetMenuCount=lambda: 1, GetMenuLabel=lambda i: '&Tools',
                          GetMenu=lambda i: menu)
    frame = SimpleNamespace(menu_bar=bar, Bind=Mock())
    assert install_operations_actions(frame) is item
    assert install_operations_actions(frame) is item
    menu.Append.assert_called_once()
    (binding,) = collect_commands(bar)
    assert binding.command.identity == 'Tools/Tasks and resources'
    assert validate_bindings((binding.command,), {
        binding.command.identity: 'Ctrl+Alt+F12'})[binding.command.identity] == 'Ctrl+Alt+F12'
