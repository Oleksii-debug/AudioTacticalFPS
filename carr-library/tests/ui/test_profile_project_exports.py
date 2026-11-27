"""Actual profile/project export handlers with real stores and file publication.

As in the existing progress/UI suites, only nested handler definitions are
compiled from the shipped source. Controlled dialogs supply choices; actual
serializers, project persistence and filesystem writers remain in use. These
headless checks do not claim native keyboard or accessibility acceptance.
"""
import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import ProfileStore, ProviderProfile
from carr_library.durable_state import encode
from carr_library.studio.project import Project
from carr_library.ui import assistant_dialog, studio_dialog


def handler(module, outer_name, name, namespace):
    source = Path(module.__file__)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    outer = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == outer_name)
    method = next(node for node in outer.body
                  if isinstance(node, ast.FunctionDef) and node.name == name)
    values = {**vars(module), **namespace}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), values)
    return values[name]


@pytest.fixture(params=['profiles', 'studio'])
def export_case(request, tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'private-library')
    frame = SimpleNamespace(library=app)
    wx = SimpleNamespace(ID_OK=1, ID_CANCEL=2, ID_YES=3, ID_NO=4,
        FD_SAVE=8, FD_OPEN=16, FD_FILE_MUST_EXIST=32, FD_OVERWRITE_PROMPT=64,
        YES_NO=128, NO_DEFAULT=256, ICON_QUESTION=512)
    h = SimpleNamespace(kind=request.param, app=app, frame=frame, wx=wx,
        target=tmp_path / 'selected-export.json', selected=False, serialized=0,
        after_serialize=lambda: None, after_question=lambda: None, after_choice=lambda: None,
        status=Mock(), reports=[], refresh=Mock(), owner=object())
    h.picker = Mock()

    def choose():
        if h.picker_result == wx.ID_OK:
            h.after_choice()
            # The studio's existing save() has already persisted its draft at
            # this point. Export must not change that private state afterward.
            h.private_before = {path: path.read_bytes() for path in app.root.rglob('*.json')}
            h.selected = True
        return h.picker_result

    h.picker_result = wx.ID_OK
    h.picker.ShowModal.side_effect = choose
    h.picker.GetPath.side_effect = lambda: str(h.target)
    wx.FileDialog = Mock(return_value=h.picker)
    h.question = Mock()
    h.answer = wx.ID_YES

    def answer():
        h.after_question()
        return h.answer

    h.question.ShowModal.side_effect = answer
    wx.MessageDialog = Mock(return_value=h.question)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    common = dict(wx=wx, frame=frame, dialog=h.owner)
    if h.kind == 'profiles':
        h.store = ProfileStore(app.root / 'state' / 'providers')
        h.profile = ProviderProfile('synthetic_profile', 'Synthetic account',
            'openai-compatible', 'https://provider.example.invalid/v1', 'synthetic-model',
            credential_ref='carr/provider/synthetic_profile/' + 'a' * 32, enabled=True)
        h.store.save(h.profile)
        actual = h.store.export_json
        h.expected = actual().encode('utf-8')

        def serialize():
            result = actual()
            if h.selected:
                h.serialized += 1
                h.after_serialize()
            return result

        monkeypatch.setattr(h.store, 'export_json', serialize)
        h.invoke = handler(assistant_dialog, 'show_provider_settings', 'exchange',
            {**common, 'store': h.store, 'status': h.status, 'refresh': h.refresh})
    else:
        h.controller = studio_dialog.StudioController(app)
        h.project = h.controller.save('Synthetic narration — Україна.', 'Synthetic project', ('voice-a',))
        actual = Project.to_dict
        h.expected = encode(actual(h.project))

        def serialize(project):
            result = actual(project)
            # Internal project save precedes destination selection by design.
            # Only serialization after the real picker boundary is interleaved.
            if h.selected:
                h.serialized += 1
                h.after_serialize()
            return result

        monkeypatch.setattr(Project, 'to_dict', serialize)
        h.save = lambda: h.controller.save(h.project.original_script, h.project.title, ('voice-a',))
        h.invoke = handler(studio_dialog, 'show_studio', 'save_as',
            {**common, 'controller': h.controller, 'save': h.save,
             'report': lambda text, focus=False: h.reports.append((text, focus))})
    h.private_before = {path: path.read_bytes() for path in app.root.rglob('*.json')}
    return h


def messages(h):
    return [call.args[0] for call in h.status.ChangeValue.call_args_list] if h.kind == 'profiles' else [row[0] for row in h.reports]


@pytest.mark.parametrize('existing', [False, True])
def test_competing_file_during_real_serialization_survives(export_case, existing):
    h = export_case
    if existing:
        h.target.write_bytes(b'Previously approved export')
    competing = b'Newer work saved during actual serialization'
    h.after_serialize = lambda: h.target.write_bytes(competing)
    h.invoke(None)
    assert h.serialized == 1
    assert h.target.read_bytes() == competing
    assert any('destination changed' in text for text in messages(h))
    assert not list(h.target.parent.glob('.research-export-*'))


def test_competing_file_during_confirmation_survives(export_case):
    h = export_case
    h.target.write_bytes(b'File offered for replacement')
    competing = b'Newer work saved while the question is open'
    h.after_question = lambda: h.target.write_bytes(competing)
    h.invoke(None)
    assert h.serialized == 1
    assert h.target.read_bytes() == competing
    assert any('destination changed' in text for text in messages(h))
    h.question.Destroy.assert_called_once()


@pytest.mark.parametrize('existing', [False, True])
def test_confirmed_export_keeps_exact_json_bytes_and_private_data(export_case, existing):
    h = export_case
    if existing:
        h.target.write_bytes(b'Explicitly approved old export')
    h.invoke(None)
    assert h.serialized == 1 and h.target.read_bytes() == h.expected
    assert h.wx.FileDialog.call_args.kwargs['style'] == h.wx.FD_SAVE
    assert {path: path.read_bytes() for path in h.private_before} == h.private_before
    if existing:
        parent, message, _, style = h.wx.MessageDialog.call_args.args
        assert parent is h.owner and h.target.name in message
        assert style == h.wx.YES_NO | h.wx.NO_DEFAULT | h.wx.ICON_QUESTION
        h.question.Destroy.assert_called_once()
    else:
        h.wx.MessageDialog.assert_not_called()
    if h.kind == 'profiles':
        profile, = json.loads(h.target.read_bytes())['profiles']
        assert profile['credential_ref'] is None and profile['enabled'] is False
        assert 'carr/provider/' not in h.target.read_text(encoding='utf-8')
        assert h.store.get(h.profile.id) == h.profile
        assert messages(h) == ['Exported non-secret profiles. No API keys or vault references are included.']
    else:
        assert Project.from_dict(json.loads(h.target.read_bytes())) == h.controller.project
        assert h.target.read_bytes().endswith(b'\n')
        assert h.reports == [('Project exported. Voice credentials and source library files are not embedded.', True)]


@pytest.mark.parametrize('answer', ['ID_NO', 'ID_CANCEL'])
def test_no_or_cancel_never_serializes_or_replaces_destination(export_case, answer):
    h = export_case
    h.target.write_bytes(b'Keep this existing export')
    h.answer = getattr(h.wx, answer)
    h.invoke(None)
    assert h.serialized == 0 and h.target.read_bytes() == b'Keep this existing export'
    assert not messages(h)
    h.question.Destroy.assert_called_once()
    h.picker.Destroy.assert_called_once()


def test_cancelled_picker_never_reads_path_or_serializes(export_case):
    h = export_case
    h.picker_result = h.wx.ID_CANCEL
    h.invoke(None)
    h.picker.GetPath.assert_not_called()
    h.wx.MessageDialog.assert_not_called()
    h.picker.Destroy.assert_called_once()
    assert h.serialized == 0 and not h.target.exists() and not messages(h)


def test_selected_private_database_cannot_be_overwritten(export_case):
    h = export_case
    h.target = h.app.database
    original = h.target.read_bytes()
    h.invoke(None)
    assert h.target.read_bytes() == original and h.serialized == 0
    assert any('outside the private library folder' in text for text in messages(h))
    h.wx.MessageDialog.assert_not_called()


def test_changed_library_does_not_export_another_bound_private_store(export_case, tmp_path):
    h = export_case
    h.frame.library = LibraryApp(tmp_path / 'another-library')
    h.invoke(None)
    assert not h.target.exists() and h.serialized == 0
    assert any('library changed' in text for text in messages(h))
    h.wx.MessageDialog.assert_not_called()


def test_library_switch_in_picker_cannot_unprotect_original_private_files(export_case, tmp_path):
    h = export_case
    h.target = h.app.database
    original = h.target.read_bytes()
    other = LibraryApp(tmp_path / 'another-library')
    h.after_choice = lambda: setattr(h.frame, 'library', other)
    h.invoke(None)
    assert h.frame.library is other
    assert h.target.read_bytes() == original and h.serialized == 0
    assert any('outside the private library folder' in text for text in messages(h))
    h.wx.MessageDialog.assert_not_called()


@pytest.mark.parametrize('modal', ['picker', 'confirmation'])
def test_library_switch_during_modal_does_not_export_old_store(export_case, tmp_path, modal):
    h = export_case
    other = LibraryApp(tmp_path / 'another-library')
    change_library = lambda: setattr(h.frame, 'library', other)
    if modal == 'confirmation':
        h.target.write_bytes(b'Preserve the previously exported project or profiles')
        h.after_question = change_library
    else:
        h.after_choice = change_library
    original = h.target.read_bytes() if h.target.exists() else None
    h.invoke(None)
    assert h.frame.library is other and h.serialized == 0
    assert (h.target.read_bytes() if h.target.exists() else None) == original
    assert {path: path.read_bytes() for path in h.app.root.rglob('*.json')} == h.private_before
    assert any('library changed' in text for text in messages(h))
    assert not list(h.target.parent.glob('.research-export-*'))
    h.picker.Destroy.assert_called_once()
    if modal == 'confirmation':
        h.question.Destroy.assert_called_once()
    else:
        h.wx.MessageDialog.assert_not_called()


@pytest.mark.parametrize('export_case', ['profiles'], indirect=True)
def test_profile_import_remains_separate_from_export_confirmation(export_case):
    h = export_case
    value = json.loads(h.expected)
    value['profiles'][0]['id'] = 'second_profile'
    h.target.write_text(json.dumps(value), encoding='utf-8')
    original = h.target.read_bytes()
    h.invoke(None, importing=True)
    imported = h.store.get('second_profile')
    assert imported.credential_ref is None and imported.enabled is False
    assert h.target.read_bytes() == original and h.serialized == 0
    assert h.wx.FileDialog.call_args.kwargs['style'] == h.wx.FD_OPEN | h.wx.FD_FILE_MUST_EXIST
    h.wx.MessageDialog.assert_not_called()
    h.refresh.assert_called_once()
    h.picker.Destroy.assert_called_once()
    assert messages(h) == ['Profiles imported disabled. Reconnect each account to its credential.']

