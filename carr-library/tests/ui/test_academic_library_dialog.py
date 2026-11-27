"""Academic dialog controllers against real application SQLite transactions.

These checks exercise forms, scope, selections and exported content. Native wx
controls and actual focus are exercised separately by windows_academic_smoke.py.
"""
from dataclasses import replace
from concurrent.futures import Future, ThreadPoolExecutor
import ast
import inspect
import json
import sys
from threading import Event
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.research.academic_library import AcademicLibrary
from carr_library.ui.academic_library_dialog import (
    AcademicController, PAGE_SIZE, form_values, initial_fields,
    book_link_labels, reference_details, reference_labels, selected_row, statistics_text,
)


@pytest.fixture
def controller(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Академічна бібліотека')
    return AcademicController(library, workspace)


def draft(**updates):
    values = initial_fields()
    values.update(title='Research methods', authors='Olena Researcher\nJan Researcher', tags='',
                  year='2026', progress='0', rating=0)
    values.update(updates)
    return values


def test_form_saves_metadata_and_reading_notes_without_changing_linked_source(controller):
    app, workspace = controller.library, controller.workspace
    imported = app.add_text(workspace, 'Original title', 'Exact source wording about railways.',
                            authors=('Original author',))
    hit, = app.search(workspace, 'railways')
    raw = draft(title='Bibliographic title', edition_id=imported['edition'],
                year='circa 1920', reading_status='reading', priority='high', rating=4,
                progress='35', volume='II', issue='7', pages='xi–xx',
                tags='Transport\nУкраїна\nTransport', notes='Private line one\r\nПриватний рядок два.')
    saved = controller.create(form_values(raw))
    assert saved.year == 'circa 1920'
    assert saved.tags == ('Transport', 'Україна')
    assert saved.progress == 35 and saved.rating == 4
    assert saved.notes == raw['notes']
    assert controller.list(status='reading', tag='Україна') == (saved,)
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert app.browse(workspace)[0].title == 'Original title'
    assert initial_fields(saved)['notes'] == saved.notes
    reopened = AcademicController(LibraryApp(app.root), workspace)
    assert reopened.get(saved.id) == saved
    assert 'Pages:\nxi–xx' in reference_details(saved)


def test_form_preserves_equal_author_names_and_unknown_year(controller):
    values = form_values(draft(authors='Same Name\nSame Name', year=''))
    saved = controller.create(values)
    assert saved.authors == ('Same Name', 'Same Name') and saved.year == ''
    assert 'Unknown year' in reference_labels((saved,))[0]
    assert 'Year:\nUnknown' in reference_details(saved)


@pytest.mark.parametrize('changes', [
    {'title': ''}, {'progress': '20.5'}, {'progress': '-1'}, {'progress': '101'},
    {'rating': 6}, {'reading_status': 'complete'}, {'priority': None},
])
def test_invalid_form_retains_the_original_draft_and_saves_nothing(controller, changes):
    raw = draft(notes='Keep this unfinished note', **changes)
    before = dict(raw)
    with pytest.raises(ValueError):
        controller.create(form_values(raw))
    assert raw == before and controller.list() == ()


def test_editor_expected_reference_prevents_lost_updates(controller):
    first = controller.create(form_values(draft()))
    current = controller.update(first, {'notes': 'Saved from another window'})
    raw = draft(notes='Older unsaved form')
    with pytest.raises(ValueError, match='changed'):
        controller.update(first, form_values(raw))
    assert controller.get(first.id) == current
    assert raw['notes'] == 'Older unsaved form'
    with pytest.raises(ValueError, match='changed'):
        controller.archive(first)
    assert not controller.get(first.id).archived


def test_pinned_controller_rechecks_library_authorization(controller):
    row = controller.create(form_values(draft()))
    other = controller.library.create_workspace('Other library')
    foreign = AcademicController(controller.library, other)
    with pytest.raises(ValueError):
        foreign.get(row.id)
    with pytest.raises(ValueError):
        foreign.update(row, {'notes': 'Wrong library'})
    controller.library.archive_workspace(controller.workspace)
    with pytest.raises(ValueError):
        controller.create(form_values(draft(title='After archive')))
    controller.library.restore_workspace(controller.workspace)
    assert controller.get(row.id) == row


def test_academic_controller_captures_destination_before_render(controller, tmp_path, monkeypatch):
    controller.create(form_values(draft()))
    target = tmp_path / 'bibliography.txt'
    target.write_bytes(b'Approved previous export')
    original = controller.export

    def render(**kwargs):
        content = original(**kwargs)
        target.write_bytes(b'Independent work saved while rendering')
        return content

    monkeypatch.setattr(controller, 'export', render)
    with pytest.raises(ValueError, match='destination changed'):
        controller.export_to(target)
    assert target.read_bytes() == b'Independent work saved while rendering'
    assert not list(tmp_path.glob('.research-export-*'))


def test_import_dialog_selection_is_explicit_and_failed_batch_is_atomic(controller):
    data = json.dumps([
        {'type': 'book', 'title': 'First imported reference'},
        {'type': 'article-journal', 'title': 'Second imported reference', 'DOI': '10.5555/second'},
    ]).encode()
    preview = controller.preview(data, 'csl-json')
    assert controller.list() == ()
    invalid = replace(preview.entries[1], fields=MappingProxyType(
        {**preview.entries[1].fields, 'edition_id': 'missing-local-book'}))
    broken = replace(preview, entries=(preview.entries[0], invalid))
    with pytest.raises(ValueError):
        controller.import_selected(broken, (0, 1))
    assert controller.list() == ()
    result = controller.import_selected(preview, (1,))
    assert len(result.imported_ids) == 1
    assert controller.list()[0].title == 'Second imported reference'
    duplicate_preview = controller.preview(data, 'csl-json')
    assert duplicate_preview.entries[1].duplicate_ids == result.imported_ids
    skipped = controller.import_selected(duplicate_preview, (1,), skip_duplicates=True)
    assert skipped.imported_ids == () and skipped.skipped_indices == (1,)


@pytest.mark.parametrize('format', ['text', 'ris', 'bibtex', 'csl-json', 'csv'])
def test_export_scope_and_personal_notes_are_explicit(controller, format):
    first = controller.create(form_values(draft(title='Active reference', notes='PrivateNoteToken')))
    archived = controller.create(form_values(draft(title='Archived reference')))
    controller.archive(archived)
    foreign = controller.library.create_workspace('Other project')
    AcademicController(controller.library, foreign).create(form_values(draft(title='ForeignReferenceToken')))
    exported = controller.export(format=format)
    assert 'Active reference' in exported
    assert 'Archived reference' not in exported and 'ForeignReferenceToken' not in exported
    assert 'PrivateNoteToken' not in exported
    selected = controller.export(identity=first.id, format=format, include_notes=True)
    assert 'PrivateNoteToken' in selected


def test_export_uses_existing_destination_guard(controller, tmp_path):
    controller.create(form_values(draft()))
    target = tmp_path / 'bibliography.txt'
    controller.export_to(target)
    assert 'Research methods' in target.read_text()
    private = controller.library.root / 'do-not-overwrite.txt'
    private.write_text('Existing private file')
    with pytest.raises(ValueError):
        controller.export_to(private)
    assert private.read_text() == 'Existing private file'


def test_reference_pages_have_distinct_labels_and_all_active_export(controller):
    with controller.library.write(controller.workspace) as db:
        service = AcademicLibrary(db, controller.workspace)
        created = tuple(service.create(title='Equal title', authors=('Equal author',),
                                        reading_status='reading', tags=('Seminar',))
                        for _ in range(PAGE_SIZE + 3))
    first = controller.list(status='reading', tag='Seminar', limit=PAGE_SIZE + 1)
    second = controller.list(status='reading', tag='Seminar', limit=PAGE_SIZE + 1, offset=PAGE_SIZE)
    assert len(first) == PAGE_SIZE + 1 and len(second) == 3
    labels = reference_labels(first[:PAGE_SIZE]) + reference_labels(second, offset=PAGE_SIZE)
    assert len(labels) == len(set(labels)) == len(created)
    assert {row.id for row in first[:PAGE_SIZE]} | {row.id for row in second} == {row.id for row in created}
    assert len(json.loads(controller.export(format='csl-json'))) == len(created)


@pytest.mark.parametrize('index', [-1, 1, True, '0'])
def test_missing_reference_choice_never_defaults_to_last_reference(controller, index):
    row = controller.create(form_values(draft()))
    with pytest.raises(ValueError):
        selected_row((row,), index)


def test_linked_document_uses_verified_view_copy_and_fresh_reference(controller):
    app = controller.library
    imported = app.add_text(controller.workspace, 'Linked original', 'Source text remains exact.')
    initial = controller.from_book(imported['edition'])
    row = controller.create(initial)
    viewed = controller.prepare_document(row)
    original = app.assets.resolve(imported['asset'])
    assert viewed.path != original and viewed.path.read_bytes() == original.read_bytes()
    controller.update(row, {'title': 'Changed bibliography'})
    with pytest.raises(ValueError, match='changed'):
        controller.prepare_document(row)
    standalone = controller.create(form_values(draft(title='No local file')))
    with pytest.raises(ValueError, match='no linked'):
        controller.prepare_document(standalone)


def test_duplicate_suggestions_and_reading_overview_are_readable(controller):
    first = controller.create(form_values(draft(doi='10.5555/example', priority='high', reading_status='reading')))
    controller.create(form_values(draft(title='Alternate description', doi='https://doi.org/10.5555/EXAMPLE')))
    text = controller.duplicate_report()
    assert 'Research methods' in text and 'Alternate description' in text and 'Reason:' in text
    assert 'No references are merged or deleted automatically.' in text
    overview = statistics_text(controller.statistics())
    assert 'Active references: 2' in overview and 'Currently reading: 1' in overview
    archived = controller.archive(first)
    assert archived.archived
    assert len(controller.list()) == 1 and len(controller.list(include_archived=True)) == 2
    restored = controller.archive(archived, archived=False)
    assert not restored.archived and len(controller.list()) == 2


def test_local_book_choices_explain_editions_and_keep_the_selected_source(controller):
    app, workspace = controller.library, controller.workspace
    app.add_text(workspace, 'Same book', 'First edition text.', authors=('Same Author',),
                 edition='First', volume='I', language='en')
    second = app.add_text(workspace, 'Same book', 'Second edition source.', authors=('Same Author',),
                          edition='Second', volume='II', language='uk')
    books = controller.books()
    labels = book_link_labels(books)
    index = next(index for index, row in enumerate(books) if row.edition_id == second['edition'])
    assert 'Edition: Second' in labels[index]
    assert 'Volume: II' in labels[index] and 'Language: uk' in labels[index]
    selected = selected_row(books, index)
    reference = controller.create(controller.from_book(selected.edition_id))
    source = controller.prepare_document(reference)
    assert source.path.read_bytes() == app.assets.resolve(second['asset']).read_bytes()
    assert reference.edition_id == selected.edition_id == second['edition']


class _Control:
    """Any attempt to use a destroyed native control fails the regression."""
    def __init__(self, value=''):
        self.value, self.dead, self.enabled = value, False, True
        self.calls = []

    def _touch(self, method, *args):
        assert not self.dead, 'A completion accessed a destroyed control'
        self.calls.append((method, args))

    def GetValue(self):
        self._touch('GetValue')
        return self.value

    def SetValue(self, value):
        self._touch('SetValue', value)
        self.value = value

    def Enable(self, enabled=True):
        self._touch('Enable', enabled)
        self.enabled = enabled

    def Disable(self):
        self.Enable(False)

    def SetLabel(self, value):
        self.SetValue(value)

    def SetInsertionPoint(self, position):
        self._touch('SetInsertionPoint', position)

    def SetFocus(self):
        self._touch('SetFocus')


class _Window(_Control):
    def __init__(self):
        super().__init__()
        self.modal, self.return_code = True, None

    def IsModal(self):
        self._touch('IsModal')
        return self.modal

    def EndModal(self, result):
        self._touch('EndModal', result)
        self.modal, self.return_code = False, result

    def SetReturnCode(self, result):
        self._touch('SetReturnCode', result)
        self.return_code = result

    def Hide(self):
        self._touch('Hide')


class _DeferredExecutor:
    def __init__(self):
        self.future = Future()
        self.operation = None

    def submit(self, operation, *args):
        self.operation = lambda: operation(*args)
        return self.future

    def finish(self):
        def worker():
            try:
                self.future.set_result(self.operation())
            except Exception as exc:
                self.future.set_exception(exc)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(worker).result()


@pytest.fixture
def native_handlers(monkeypatch):
    """Execute real nested handlers without constructing native windows.

    The AcademicDialog class is local to a factory which immediately builds its
    controls. Extract that unchanged class definition; constructors and real wx
    event delivery remain covered only by the separate native smoke.
    """
    from carr_library.ui import academic_library_dialog as ui
    queued = []
    wx = SimpleNamespace(Dialog=_Window, ID_OK=1, ID_CANCEL=2,
        CallAfter=lambda callback, *args, **kwargs: queued.append((callback, args, kwargs)))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    _, editor, _, _, report, _ = ui._native_types()
    factory = ast.parse(inspect.getsource(ui.create_academic_dialog))
    definition = next(node for node in ast.walk(factory)
                      if isinstance(node, ast.ClassDef) and node.name == 'AcademicDialog')
    namespace = {**vars(ui), 'wx': wx, 'report': report}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), inspect.getfile(ui), 'exec'), namespace)

    def make(kind, controller):
        cls = editor if kind == 'editor' else namespace['AcademicDialog']
        window = cls.__new__(cls)
        _Window.__init__(window)
        window._window_alive, window.pending = True, False
        window.status = _Control()
        window.executor = _DeferredExecutor()
        if kind == 'editor':
            window.saved, window.save = None, controller.create
            window.controls = {key: _Control(value) for key, value in draft().items()}
            window.choice_values = {}
            window.save_button, window.cancel_button = _Control(), _Control()
            window._initial = window.raw_values()
            window.confirm_discard = lambda: False
            controls = (*window.controls.values(), window.save_button, window.cancel_button)
        else:
            window.cancel_event = Event()
            window.job_controls = (_Control(), _Control())
            window.close_button = _Control('&Close')
            window._buttons = Mock(side_effect=lambda: window._touch('_buttons'))
            controls = (*window.job_controls, window.close_button)
        window.all_controls = (window, window.status, *controls)
        return window

    def destroy(window):
        event = SimpleNamespace(GetEventObject=lambda: window, Skip=Mock())
        window.on_destroy(event)
        event.Skip.assert_called_once()
        for control in window.all_controls:
            control.dead = True

    def drain():
        while queued:
            callback, args, kwargs = queued.pop(0)
            callback(*args, **kwargs)

    return SimpleNamespace(make=make, destroy=destroy, drain=drain, queued=queued, wx=wx)


def test_preview_budget_error_preserves_native_selection_and_imports_nothing(
        controller, native_handlers, monkeypatch, tmp_path):
    from carr_library.research import academic_library as academic
    original = controller.create(form_values(draft(doi='10.1234/shared')))
    target = tmp_path / 'duplicates.json'
    target.write_text(json.dumps([{'title': 'Incoming', 'DOI': '10.1234/shared'}] * 10))
    ui = native_handlers
    chooser = Mock()
    chooser.ShowModal.return_value = ui.wx.ID_OK
    chooser.GetPath.return_value = str(target)
    ui.wx.FileDialog = Mock(return_value=chooser)
    ui.wx.FD_OPEN, ui.wx.FD_FILE_MUST_EXIST = 1, 2
    window = ui.make('academic', controller)
    window.controller = controller
    window.references = Mock()
    window.references.GetSelection.return_value = 0
    window.rows = (original,)
    window.refresh = Mock()
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 10)
    window.on_import()
    assert 'Too many duplicate matches' in window.status.value
    assert window.rows == (original,) and window.references.GetSelection() == 0
    window.refresh.assert_not_called()
    assert controller.list() == (original,)
    chooser.Destroy.assert_called_once()


@pytest.mark.parametrize('timing', ['before_worker_finishes', 'after_callback_queued'])
@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_editor_late_completion_never_touches_destroyed_controls(controller, native_handlers, timing, outcome):
    ui = native_handlers
    window = ui.make('editor', controller)
    error = ValueError('Synthetic save failure')
    if outcome == 'error':
        window.save = Mock(side_effect=error)
    window.on_save()
    assert window.pending
    if timing == 'after_callback_queued':
        window.executor.finish()
    ui.destroy(window)
    before = [control.calls[:] for control in window.all_controls]
    if timing == 'before_worker_finishes':
        window.executor.finish()
    if outcome == 'error':
        assert ui.queued[0][2]['error'] is error
    ui.drain()
    assert not window.pending
    assert [control.calls for control in window.all_controls] == before
    if outcome == 'success':
        saved, = controller.list()
        assert window.saved == saved  # Destruction does not undo an already authorized save.
    else:
        assert window.saved is None and controller.list() == ()
    window.on_save()
    window.on_cancel()
    window.on_close(Mock())
    assert [control.calls for control in window.all_controls] == before


@pytest.mark.parametrize('timing', ['before_worker_finishes', 'after_callback_queued'])
@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_academic_late_completion_never_runs_ui_callback_after_destroy(controller, native_handlers, timing, outcome):
    ui = native_handlers
    window = ui.make('academic', controller)
    error, done = ValueError('Synthetic read failure'), Mock()
    operation = controller.statistics if outcome == 'success' else Mock(side_effect=error)
    window._run('Loading overview', operation, done)
    assert window.pending
    if timing == 'after_callback_queued':
        window.executor.finish()
    ui.destroy(window)
    assert window.cancel_event.is_set()
    before = [control.calls[:] for control in window.all_controls]
    if timing == 'before_worker_finishes':
        window.executor.finish()
    if outcome == 'error':
        assert ui.queued[0][2]['error'] is error
    ui.drain()
    assert not window.pending
    done.assert_not_called()
    assert [control.calls for control in window.all_controls] == before
    forbidden = Mock(side_effect=AssertionError('Destroyed dialog started another operation'))
    window._run('Late operation', forbidden, done)
    window.on_cancel()
    window.on_close(Mock())
    forbidden.assert_not_called()
    assert [control.calls for control in window.all_controls] == before


@pytest.mark.parametrize('kind', ['editor', 'academic'])
@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_live_completion_and_child_destroy_keep_normal_callbacks(controller, native_handlers, kind, outcome):
    ui = native_handlers
    window, done = ui.make(kind, controller), Mock()
    error = ValueError('Visible worker failure')
    if kind == 'editor':
        if outcome == 'error':
            window.save = Mock(side_effect=error)
        window.on_save()
    else:
        window._run('Loading overview', controller.statistics if outcome == 'success' else Mock(side_effect=error), done)
    window.on_destroy(SimpleNamespace(GetEventObject=lambda: object(), Skip=Mock()))
    assert window._window_alive and window.pending
    if kind == 'academic':
        assert not window.cancel_event.is_set()
    window.executor.finish()
    assert window.pending  # Completion is dispatched on the UI turn.
    ui.drain()
    assert not window.pending and all(control.enabled for control in window.all_controls)
    if outcome == 'error':
        assert 'Visible worker failure' in window.status.value and window.modal
        done.assert_not_called()
    elif kind == 'editor':
        assert window.saved == controller.list()[0]
        assert window.return_code == ui.wx.ID_OK and not window.modal
    else:
        done.assert_called_once_with(controller.statistics())
        assert window.modal


@pytest.mark.parametrize('kind', ['editor', 'academic'])
def test_pending_close_veto_remains_until_worker_completion(controller, native_handlers, kind):
    ui = native_handlers
    window, done = ui.make(kind, controller), Mock()
    if kind == 'editor':
        window.on_save()
    else:
        window._run('Loading overview', controller.statistics, done)
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock(), Skip=Mock())
    window.on_close(event)
    event.Veto.assert_called_once()
    assert window.modal and window.pending and window._window_alive
    window.executor.finish()
    ui.drain()
    assert not window.pending
    if kind == 'editor':
        assert window.return_code == ui.wx.ID_OK and window.saved is not None
    else:
        assert window.modal and 'cancelled' in window.status.value.casefold()
        done.assert_not_called()
        window.on_close(event)
        assert window.return_code == ui.wx.ID_CANCEL and not window.modal


@pytest.mark.parametrize('kind', ['editor', 'academic'])
def test_executor_submission_error_restores_live_controls(controller, native_handlers, kind):
    window, done = native_handlers.make(kind, controller), Mock()
    window.executor = SimpleNamespace(submit=Mock(side_effect=RuntimeError('Executor stopped')))
    if kind == 'editor':
        window.on_save()
    else:
        window._run('Loading overview', controller.statistics, done)
    assert not window.pending and window.modal
    assert 'Executor stopped' in window.status.value
    assert all(control.enabled for control in window.all_controls)
    done.assert_not_called()


def test_live_academic_completion_reports_done_callback_error(controller, native_handlers):
    window = native_handlers.make('academic', controller)
    window._run('Loading overview', controller.statistics,
                Mock(side_effect=ValueError('Visible callback failure')))
    window.executor.finish()
    native_handlers.drain()
    assert not window.pending and window.modal
    assert 'Visible callback failure' in window.status.value


@pytest.mark.parametrize('existing', [False, True])
def test_export_to_preserves_a_competing_save_during_real_render(
        controller, tmp_path, monkeypatch, existing):
    controller.create(form_values(draft()))
    target = tmp_path / 'competing-bibliography.txt'
    if existing:
        target.write_bytes(b'Previously approved bibliography')
    original_export = controller.export
    competing = b'Newer bibliography saved during rendering'

    def render_with_competing_save(**kwargs):
        content = original_export(**kwargs)
        target.write_bytes(competing)
        return content

    monkeypatch.setattr(controller, 'export', render_with_competing_save)
    with pytest.raises(ValueError, match='destination changed'):
        controller.export_to(target)
    assert target.read_bytes() == competing
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize(('answer', 'competing'), [('no', False), ('yes', False), ('yes', True)])
def test_export_handler_binds_actual_controller_publication_to_confirmation(
        controller, native_handlers, tmp_path, monkeypatch, answer, competing):
    from carr_library.ui.academic_library_dialog import FORMATS

    controller.create(form_values(draft()))
    target = tmp_path / 'confirmed-bibliography.txt'
    original_bytes = b'Existing bibliography selected for replacement'
    competing_bytes = b'Newer bibliography saved while the question is open'
    target.write_bytes(original_bytes)
    ui = native_handlers
    ui.wx.FD_SAVE, ui.wx.FD_OVERWRITE_PROMPT = 4, 8
    ui.wx.YES, ui.wx.NO = 10, 11
    ui.wx.YES_NO, ui.wx.NO_DEFAULT, ui.wx.ICON_QUESTION = 16, 32, 64
    chooser = Mock()
    chooser.ShowModal.return_value = ui.wx.ID_OK
    chooser.GetPath.return_value = str(target)
    ui.wx.FileDialog = Mock(return_value=chooser)
    def answer_question(message, caption, style, parent):
        if competing:
            target.write_bytes(competing_bytes)
        return ui.wx.YES if answer == 'yes' else ui.wx.NO

    ui.wx.MessageBox = Mock(side_effect=answer_question)
    window = ui.make('academic', controller)
    window.controller = controller
    window._export_options = Mock(return_value=(FORMATS[0], None, False))
    actual_render = Mock(wraps=controller.export)
    monkeypatch.setattr(controller, 'export', actual_render)
    window.on_export()

    assert ui.wx.FileDialog.call_args.kwargs['style'] == ui.wx.FD_SAVE
    ui.wx.MessageBox.assert_called_once()
    message, caption, style, parent = ui.wx.MessageBox.call_args.args
    assert parent is window and target.name in message and caption == 'Replace academic bibliography'
    assert style == ui.wx.YES_NO | ui.wx.NO_DEFAULT | ui.wx.ICON_QUESTION
    chooser.Destroy.assert_called_once()
    if answer == 'no':
        actual_render.assert_not_called()
        assert target.read_bytes() == original_bytes
    elif competing:
        actual_render.assert_called_once()
        assert target.read_bytes() == competing_bytes
        assert 'Bibliography export:' in window.status.value
        assert 'destination changed' in window.status.value
    else:
        actual_render.assert_called_once()
        assert target.read_text(encoding='utf-8') == controller.export()
        assert window.status.value == f'Bibliography exported to {target}. Personal notes were not included.'
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('existing', [False, True])
def test_bibliography_preserves_newer_destination_during_render(
        controller, tmp_path, monkeypatch, existing):
    target = tmp_path / 'bibliography.txt'
    if existing:
        target.write_bytes(b'Explicitly selected old bibliography.')
    render = controller.export
    newer = b'Independent work saved while bibliography renders.'

    def render_and_change(**options):
        value = render(**options)
        target.write_bytes(newer)
        return value

    monkeypatch.setattr(controller, 'export', render_and_change)
    with pytest.raises(ValueError, match='destination changed'):
        controller.export_to(target)
    assert target.read_bytes() == newer
    assert not list(tmp_path.glob('.research-export-*'))


def test_native_export_handler_retains_text_return_and_cleanup_notice(
        controller, native_handlers, monkeypatch, tmp_path):
    from carr_library.ui import academic_library_dialog as ui
    from carr_library.research.export_file import ExportReceipt
    controller.create(form_values(draft()))
    actual_writer = ui.write_export

    def completed_with_notice(*args, **kwargs):
        receipt = actual_writer(*args, **kwargs)
        return ExportReceipt(receipt.path, 'Completed; temporary cleanup needs attention.')

    monkeypatch.setattr(ui, 'write_export', completed_with_notice)
    window = native_handlers.make('academic', controller)
    window.controller = controller
    destination = tmp_path / 'references.ris'
    result = window.export_to(destination, format='ris')
    assert isinstance(result, str)
    assert result.encode('utf-8') == destination.read_bytes()
    assert len(controller.preview(result.encode('utf-8'), 'ris').entries) == 1
    assert 'Completed; temporary cleanup needs attention.' in window.status.value


def test_controller_can_return_the_publication_receipt_without_rereading(controller, tmp_path):
    from carr_library.research.export_file import ExportReceipt
    controller.create(form_values(draft()))
    target = tmp_path / 'receipt-bibliography.ris'
    receipt = controller.export_to(target, format='ris', return_receipt=True)
    assert isinstance(receipt, ExportReceipt) and receipt.path == target
    assert len(controller.preview(target.read_bytes(), 'ris').entries) == 1
