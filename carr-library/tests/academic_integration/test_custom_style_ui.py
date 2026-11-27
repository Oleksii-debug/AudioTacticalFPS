"""Local CSL file choice through real controllers, handlers, Node and DOCX.

Headless controls expose the actual lazy native handlers. Real Windows widgets
and event dispatch are covered separately by windows_custom_csl_smoke.py.
"""
from hashlib import sha256
import json
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.academic_integration import CitationError, CitationRequest, read_citation_docx
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.ui import citation_formatting as ui


STYLE = (b'<style xmlns="http://purl.org/net/xbiblio/csl" class="in-text" version="1.0">'
         b'<info><title>Institutional test style</title><id>urn:test:institution</id></info>'
         b'<citation><layout><text variable="title" prefix="Cite: "/></layout></citation>'
         b'<bibliography><layout><text variable="title" prefix="Institution: "/></layout></bibliography></style>')


@pytest.fixture
def prepared(tmp_path):
    library = LibraryApp(tmp_path / 'private library')
    workspace = library.create_workspace('Selected library')
    original = library.add_text(workspace, 'Source', 'Exact original quotation.')
    academic = AcademicController(library, workspace)
    reference = academic.create({'title': 'Synthetic methods', 'authors': ('{Research, Group}',),
        'year': '2026', 'edition_id': original['edition'], 'notes': 'Private independent note'})
    controller = ui.CitationFormattingController(library, workspace)
    return library, workspace, academic, reference, controller, controller.load(), original


def test_chosen_local_style_formats_real_source_bound_citations_and_docx(prepared, tmp_path):
    library, workspace, academic, reference, controller, snapshot, original = prepared
    path = tmp_path / 'кафедра style.csl'
    path.write_bytes(STYLE)
    original_bytes = library.assets.resolve(original['asset']).read_bytes()
    result = controller.render(snapshot, style_path=path, citations=(CitationRequest(reference.id),))
    assert result.style.sha256 == sha256(STYLE).hexdigest()
    assert result.style.source == 'local:' + path.name
    assert result.citations[0].text == 'Cite: Synthetic methods'
    assert result.bibliography[0].text == 'Institution: Synthetic methods'
    assert str(tmp_path) not in json.dumps(result.interchange)
    # Export retains the already rendered, hash-identified result, not later file bytes.
    path.write_bytes(STYLE.replace(b'Institution: ', b'Changed: '))
    destination = controller.export_docx(snapshot, result, tmp_path / 'institution.docx')
    retained = read_citation_docx(destination)
    assert retained['style']['sha256'] == sha256(STYLE).hexdigest()
    assert retained['bibliography'][0]['text'] == 'Institution: Synthetic methods'
    assert 'Private independent note' not in json.dumps(retained)
    assert academic.get(reference.id) == reference
    assert library.assets.resolve(original['asset']).read_bytes() == original_bytes
    assert controller.load() == snapshot


@pytest.mark.parametrize('payload', [
    b'not XML',
    b'<!DOCTYPE style [<!ENTITY leak SYSTEM "file:///private">]>' + STYLE,
    STYLE.replace(b'<citation>', b'<macro name="loop"><text macro="loop"/></macro><citation>'),
    b'<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0"><info><title>Dependent</title></info></style>',
    b'x' * 1_000_001,
])
def test_invalid_local_style_never_changes_the_library_or_creates_a_result(prepared, tmp_path, payload):
    library, workspace, academic, reference, controller, snapshot, original = prepared
    path = tmp_path / 'invalid.csl'
    path.write_bytes(payload)
    with pytest.raises((CitationError, ValueError)):
        controller.render(snapshot, style_path=path)
    assert controller._result is None and academic.get(reference.id) == reference
    assert controller.load() == snapshot
    assert not list(tmp_path.glob('*.docx'))


def test_custom_style_does_not_bypass_current_reference_identity(prepared, tmp_path):
    _, _, academic, reference, controller, snapshot, _ = prepared
    path = tmp_path / 'valid.csl'
    path.write_bytes(STYLE)
    academic.update(reference, {'title': 'Updated source metadata'})
    with pytest.raises(ValueError, match='changed'):
        controller.render(snapshot, style_path=path)
    assert controller._result is None


@pytest.fixture
def native_handlers(monkeypatch):
    picker = Mock()
    picker.__enter__ = Mock(return_value=picker)
    picker.__exit__ = Mock(return_value=False)
    wx = SimpleNamespace(Dialog=object, FileDialog=Mock(return_value=picker),
                         ID_OK=1, ID_CANCEL=2, FD_OPEN=4, FD_FILE_MUST_EXIST=8)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    cls, _ = ui._native_types()
    dialog = cls.__new__(cls)
    dialog.pending, dialog.style_path = False, None
    dialog.local_style, dialog.invalidate, dialog.report = Mock(), Mock(), Mock()
    picker.ShowModal.return_value = wx.ID_OK
    return wx, picker, dialog


def test_real_file_choice_handler_retains_explicit_path_and_invalidates_preview(native_handlers, tmp_path):
    wx, picker, dialog = native_handlers
    path = tmp_path / 'explicit.csl'
    picker.GetPath.return_value = str(path)
    dialog.choose_local_style()
    assert dialog.style_path == path
    dialog.local_style.ChangeValue.assert_called_once_with(str(path))
    dialog.invalidate.assert_called_once_with()
    assert wx.FileDialog.call_args.kwargs['style'] == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST
    # File loading is deferred to the existing cancellable formatting job.
    assert not path.exists()
    dialog.use_installed_style()
    assert dialog.style_path is None and dialog.invalidate.call_count == 2


def test_cancelled_file_choice_preserves_the_previous_selection(native_handlers, tmp_path):
    wx, picker, dialog = native_handlers
    dialog.style_path = tmp_path / 'previous.csl'
    picker.ShowModal.return_value = wx.ID_CANCEL
    dialog.choose_local_style()
    assert dialog.style_path == tmp_path / 'previous.csl'
    dialog.invalidate.assert_not_called()
    dialog.local_style.ChangeValue.assert_not_called()


@pytest.mark.parametrize('action', ['choose_local_style', 'use_installed_style'])
def test_pending_format_cannot_change_the_selected_style(native_handlers, tmp_path, action):
    wx, picker, dialog = native_handlers
    dialog.pending, dialog.style_path = True, tmp_path / 'selected.csl'
    getattr(dialog, action)()
    assert dialog.style_path == tmp_path / 'selected.csl'
    wx.FileDialog.assert_not_called()
    dialog.invalidate.assert_not_called()


def test_native_format_handler_queues_exact_selected_path_to_real_controller(native_handlers, prepared, tmp_path):
    _, _, dialog = native_handlers
    _, _, _, reference, controller, snapshot, _ = prepared
    path = tmp_path / 'selected.csl'
    path.write_bytes(STYLE)
    dialog.snapshot, dialog.references, dialog.clusters = snapshot, snapshot.references, []
    dialog.references_box = SimpleNamespace(GetCheckedItems=lambda: (0,))
    dialog.style, dialog.locale = (SimpleNamespace(GetSelection=lambda: 0) for _ in range(2))
    dialog.styles = ui.available_styles()
    dialog.style_path = path
    dialog.node, dialog.cancel_event = SimpleNamespace(GetValue=lambda: ''), Event()
    dialog.controller = controller
    dialog.run = Mock()
    dialog.format()
    dialog.run.assert_called_once()
    title, operation, completed = dialog.run.call_args.args
    assert title == 'Formatting citations locally' and callable(completed)
    assert controller._result is None
    # The queued operation captured this choice before dispatch.
    dialog.style_path = tmp_path / 'later-unselected.csl'
    result = operation()
    assert result.style.sha256 == sha256(STYLE).hexdigest()
    assert result.bibliography[0].text == 'Institution: Synthetic methods'
    assert result.references[0]['id'] == reference.id


@pytest.mark.parametrize('pending,local', [(False, False), (False, True), (True, False), (True, True)])
def test_native_controls_cannot_mix_installed_and_custom_choices(native_handlers, pending, local):
    _, _, dialog = native_handlers
    dialog.pending, dialog.style_path = pending, Path('selected.csl') if local else None
    for name in ('style', 'choose_style', 'installed_style', 'format_button', 'export_button',
                 'add_citation', 'all_button', 'none_button', 'remove_citation', 'move_up', 'move_down', 'stop'):
        setattr(dialog, name, Mock())
    dialog.controls = [dialog.style, dialog.choose_style, dialog.installed_style]
    dialog.references, dialog.clusters, dialog.result = ({'id': 'selected'},), [], None
    dialog.update_controls()
    dialog.choose_style.Enable.assert_called_with(not pending)
    dialog.style.Enable.assert_called_with(not pending and not local)
    dialog.installed_style.Enable.assert_called_with(not pending and local)
