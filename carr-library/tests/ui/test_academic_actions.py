"""Actual academic action handlers with explicit wx controls and event doubles.

Reference parsing, Futures and export filesystem operations are real. These are
headless workflow checks, not native Windows or NVDA acceptance evidence.
"""
from concurrent.futures import Future
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from carr_library.catalog.read_views import EditionView
from carr_library.catalog.reference_exchange import preview_references
from carr_library.research import export_file


class Control:
    """The existing metadata-dialog test protocol, extended for reference choices."""
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.choices = kwargs.get('choices', [])
        self.selection = -1
        self.name = ''
        self.enabled = True
        self.focused = self.destroyed = False
        self.bindings = {}
        self.modal_result = None
    def Bind(self, event, callback): self.bindings[event] = callback
    def SetName(self, name): self.name = name
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def SetFocus(self): self.focused = True
    def SetMinSize(self, value): pass
    def SetSizer(self, value): pass
    def Add(self, *args): pass
    def SetDefault(self): pass
    def Enable(self, enabled): self.enabled = enabled
    def EndModal(self, result): self.modal_result = result
    def Destroy(self): self.destroyed = True
    def SetScrollRate(self, *args): pass
    def FitInside(self): pass
    def SetEscapeId(self, value): self.escape_id = value
    def SetInsertionPoint(self, value): pass


class Executor:
    def __init__(self):
        self.calls = []
    def submit(self, operation, **kwargs):
        future = Future()
        self.calls.append((operation, kwargs, future))
        return future
    def run(self):
        operation, kwargs, future = self.calls[-1]
        try:
            future.set_result(operation(**kwargs))
        except Exception as exc:
            future.set_exception(exc)


@pytest.fixture
def module(monkeypatch):
    # Overwrite consent is now a separate native question, after destination
    # capture. The real helper and publisher still run in these handler tests.
    monkeypatch.setattr('carr_library.ui.export_destination._confirm_replace', lambda *_: True)
    wx = ModuleType('wx')
    for name in ('Dialog', 'BoxSizer', 'StaticText', 'TextCtrl', 'Button', 'Choice', 'ScrolledWindow'):
        setattr(wx, name, Control)
    names = ('VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL', 'LEFT', 'RIGHT', 'TOP', 'BOTTOM',
             'ALIGN_RIGHT', 'TE_MULTILINE', 'TE_READONLY', 'VSCROLL', 'TAB_TRAVERSAL',
             'ID_SAVE', 'ID_CANCEL', 'ID_OK', 'EVT_BUTTON', 'EVT_CLOSE', 'FD_OPEN',
             'FD_FILE_MUST_EXIST', 'FD_SAVE', 'FD_OVERWRITE_PROMPT')
    for number, name in enumerate(names):
        setattr(wx, name, 1 << number)
    wx.callbacks = []
    wx.CallAfter = wx.callbacks.append
    wx.FileDialog = Mock(side_effect=AssertionError('Unexpected file dialog'))
    wx.SingleChoiceDialog = Mock(side_effect=AssertionError('Unexpected choice dialog'))
    root = Path(__file__).resolve().parents[2] / 'src/carr_library/ui'

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        value = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(value)
        return value

    with patch.dict(sys.modules, {'wx': wx}):
        metadata = load('carr_library.ui._academic_metadata_test', root / 'catalog_metadata_dialog.py')
        with patch.dict(sys.modules, {'carr_library.ui.catalog_metadata_dialog': metadata}):
            return load('carr_library.ui._academic_actions_test', root / 'academic_actions.py')


@pytest.fixture
def edition():
    return EditionView('selected-edition', 'work', 'a' * 64,
                       'Existing title', 'uk', 'First', 'II', ('Current Author', 'Олена'))


@pytest.fixture
def reference_file(tmp_path):
    path = tmp_path / 'references.ris'
    path.write_text('TY  - BOOK\nTI  - Suggested title\nTI  - Alternate title\n'
                    'AU  - Reference Group and Partners\nAU  - Інший автор\n'
                    'LA  - en\nET  - Second\n'
                    'L1  - ../../private-unrelated.pdf\nDO  - 10.1234/example\nER  -\n',
                    encoding='utf-8')
    return path


@pytest.fixture
def hint(reference_file):
    return preview_references(reference_file.read_bytes(), format='ris')[0]


def field_button(module, form, key):
    return next(control for control in form.reference_controls
                if control.name == f'Use reference {key}')


def field_choice(form, key):
    return next(control for control in form.reference_controls
                if control.name.startswith(f'Reference {key} candidates;'))


def click(module, control):
    control.bindings[module.wx.EVT_BUTTON](None)


def drain(module):
    while module.wx.callbacks:
        module.wx.callbacks.pop(0)()


def values(form):
    return {key: control.GetValue() for key, control in form.fields.items()}


def test_review_requires_field_application_and_retains_every_other_current_value(module, edition, hint):
    executor = Executor()
    save = Mock(return_value='revision')
    form = module.ReferenceMetadataDialog(None, edition, hint, save=save, executor=executor)
    original = values(form)
    assert original == dict(title='Existing title', authors='Current Author\nОлена',
                            language='uk', edition='First', volume='II')
    assert form.reference_controls[0].focused
    assert 'L1' in form.reference_controls[0].value and 'not verified' in form.reference_controls[0].value
    assert '../../private-unrelated.pdf' not in form.reference_controls[0].value
    form.on_save()
    assert executor.calls == [] and 'No metadata changes' in form.status.value
    field_choice(form, 'title').SetSelection(1)
    click(module, field_button(module, form, 'title'))
    assert values(form) == {**original, 'title': 'Alternate title'}
    click(module, field_button(module, form, 'authors'))
    assert values(form) == {**original, 'title': 'Alternate title',
                           'authors': 'Reference Group and Partners\nІнший автор'}
    assert not any(control.name == 'Use reference volume' for control in form.reference_controls)
    form.on_save()
    assert len(executor.calls) == 1 and form.pending
    assert not any(control.enabled for control in (*form.reference_controls, *form.fields.values()))
    field_choice(form, 'title').SetSelection(0)
    click(module, field_button(module, form, 'title'))
    assert form.fields['title'].GetValue() == 'Alternate title'
    executor.run()
    assert form.modal_result is None
    drain(module)
    save.assert_called_once_with(title='Alternate title',
        authors=('Reference Group and Partners', 'Інший автор'), language='uk', edition='First', volume='II')
    assert not form.pending and form.modal_result == module.wx.ID_OK


@pytest.mark.parametrize('index', [-1, 2, 999])
def test_invalid_candidate_selection_cannot_replace_metadata(module, edition, hint, index):
    form = module.ReferenceMetadataDialog(None, edition, hint, save=Mock(), executor=Executor())
    before = values(form)
    field_choice(form, 'title').SetSelection(index)
    click(module, field_button(module, form, 'title'))
    assert values(form) == before


def test_save_conflict_preserves_applied_draft_and_reenables_reference_controls(module, edition, hint):
    executor = Executor()
    save = Mock(side_effect=ValueError('Book metadata changed. Refresh the catalog.'))
    form = module.ReferenceMetadataDialog(None, edition, hint, save=save, executor=executor)
    click(module, field_button(module, form, 'title'))
    form.fields['volume'].SetValue('My manual volume')
    before = values(form)
    form.on_save()
    executor.run()
    drain(module)
    assert values(form) == before and form.modal_result is None and not form.pending
    assert 'Book metadata changed' in form.status.value and 'edits remain' in form.status.value
    assert all(control.enabled for control in (*form.reference_controls, *form.fields.values()))


def host(tmp_path, edition):
    panel = SimpleNamespace(workspace='workspace', ready=True, books=Control(), status=Control(),
                            selection=('view', 'destination', edition.edition_id), refreshed=[])
    panel._selection = lambda: panel.selection
    def refresh(*, selection):
        panel.refreshed.append(selection)
        return True
    panel.refresh = refresh
    private = tmp_path / 'private-library'
    private.mkdir()
    library = SimpleNamespace(root=private, edition_metadata=Mock(return_value=edition),
        update_metadata=Mock(return_value='revision'),
        collections=Mock(return_value=(('c1', 'First collection'), ('c2', 'Second collection'))),
        export_bibliography=Mock(return_value='Bibliography — Україна\n'),
        export_outline=Mock(return_value='# Research — Україна\n'))
    frame = SimpleNamespace(library=library, active=('workspace', 'topic'), working=False, busy=False,
        _closing=False, executor=Executor(), cancel_event=threading.Event(), jobs=[], reports=[])
    frame._workspace = lambda: frame.active[0]
    frame._topic = lambda: frame.active
    frame.book = SimpleNamespace(GetPage=lambda index: panel)
    frame.report = frame.reports.append
    def run_job(title, operation, done=None):
        frame.cancel_event.clear()
        frame.jobs.append(SimpleNamespace(title=title, operation=operation, done=done))
    frame.run_job = run_job
    return frame, panel, library


def choose(module, monkeypatch, *, path=None, file_result=None, filter_index=0,
           choice_result=None, selection=0, file_during=None, choice_during=None):
    file = Control()
    file.GetPath = lambda: str(path)
    file.GetFilterIndex = lambda: filter_index
    def show_file():
        if file_during is not None: file_during()
        return module.wx.ID_OK if file_result is None else file_result
    file.ShowModal = show_file
    chooser = Control()
    chooser.GetSelection = lambda: selection
    def show_choice():
        if choice_during is not None: choice_during()
        return module.wx.ID_OK if choice_result is None else choice_result
    chooser.ShowModal = show_choice
    monkeypatch.setattr(module.wx, 'FileDialog', Mock(return_value=file))
    monkeypatch.setattr(module.wx, 'SingleChoiceDialog', Mock(return_value=chooser))
    return file, chooser


def review_script(module, monkeypatch, callback=None):
    original = module.ReferenceMetadataDialog
    forms = []
    def create(*args, **kwargs):
        form = original(*args, **kwargs)
        def show():
            if callback is not None:
                callback(form, kwargs['executor'])
            return form.modal_result if form.modal_result is not None else module.wx.ID_CANCEL
        form.ShowModal = show
        forms.append(form)
        return form
    monkeypatch.setattr(module, 'ReferenceMetadataDialog', create)
    return forms


def finish(frame):
    job = frame.jobs[-1]
    result = job.operation()
    if job.done is not None:
        job.done(result)


def test_import_handler_uses_actual_preview_and_exact_optimistic_book_binding(
        module, edition, reference_file, tmp_path, monkeypatch):
    frame, panel, library = host(tmp_path, edition)
    file, chooser = choose(module, monkeypatch, path=reference_file)
    def apply_and_save(form, executor):
        click(module, field_button(module, form, 'title'))
        form.on_save()
        executor.run()
        drain(module)
    forms = review_script(module, monkeypatch, apply_and_save)
    module.import_reference_metadata(frame)
    assert len(frame.jobs) == 1 and file.destroyed
    finish(frame)
    library.edition_metadata.assert_called_once_with('workspace', edition.edition_id)
    library.update_metadata.assert_called_once_with('workspace', edition.edition_id,
        expected_metadata=edition, title='Suggested title', authors=edition.authors,
        language='uk', edition='First', volume='II')
    assert library.update_metadata.call_args.kwargs['expected_metadata'] is edition
    assert panel.refreshed == [panel.selection] and panel.status.focused
    assert forms[0].destroyed and chooser.destroyed and not frame.reports


@pytest.mark.parametrize('where', ['file', 'record', 'review'])
def test_import_cancellation_at_each_dialog_never_writes(module, edition, reference_file,
                                                       tmp_path, monkeypatch, where):
    frame, panel, library = host(tmp_path, edition)
    file, chooser = choose(module, monkeypatch, path=reference_file,
        file_result=module.wx.ID_CANCEL if where == 'file' else None,
        choice_result=module.wx.ID_CANCEL if where == 'record' else None)
    forms = review_script(module, monkeypatch)
    module.import_reference_metadata(frame)
    if frame.jobs:
        finish(frame)
    library.update_metadata.assert_not_called()
    assert panel.books.focused and not panel.refreshed and not frame.reports
    assert file.destroyed
    if where != 'file': assert chooser.destroyed
    if where == 'review': assert forms[0].destroyed


@pytest.mark.parametrize('problem', ['working', 'busy', 'closing', 'not_ready', 'wrong_workspace', 'no_book'])
def test_import_unavailable_selection_is_rejected_before_metadata_read(module, edition, tmp_path, problem):
    frame, panel, library = host(tmp_path, edition)
    if problem in ('working', 'busy'): setattr(frame, problem, True)
    elif problem == 'closing': frame._closing = True
    elif problem == 'not_ready': panel.ready = False
    elif problem == 'wrong_workspace': panel.workspace = 'different'
    elif problem == 'no_book': panel.selection = (None, None, None)
    module.import_reference_metadata(frame)
    library.edition_metadata.assert_not_called()
    library.update_metadata.assert_not_called()
    module.wx.FileDialog.assert_not_called()
    assert len(frame.reports) == 1 and not frame.jobs


@pytest.mark.parametrize('change', ['book', 'panel', 'workspace', 'library', 'cancel'])
def test_stale_preview_completion_never_opens_review_or_writes(module, edition, reference_file,
                                                            tmp_path, monkeypatch, change):
    frame, panel, library = host(tmp_path, edition)
    choose(module, monkeypatch, path=reference_file)
    forms = review_script(module, monkeypatch)
    module.import_reference_metadata(frame)
    job = frame.jobs[-1]
    hints = job.operation()
    if change == 'book': panel.selection = (None, None, 'other-edition')
    elif change == 'panel': frame.book = SimpleNamespace(GetPage=lambda index: object())
    elif change == 'workspace': frame.active = ('different', None)
    elif change == 'library': frame.library = SimpleNamespace(root=tmp_path / 'elsewhere')
    elif change == 'cancel': frame.cancel_event.set()
    job.done(hints)
    assert not forms and not panel.refreshed
    module.wx.SingleChoiceDialog.assert_not_called()
    library.update_metadata.assert_not_called()
    assert 'discarded' in frame.reports[-1]


def test_book_changed_while_record_chooser_is_open_prevents_review(module, edition, reference_file,
                                                                tmp_path, monkeypatch):
    frame, panel, library = host(tmp_path, edition)
    _, chooser = choose(module, monkeypatch, path=reference_file,
        choice_during=lambda: setattr(panel, 'selection', (None, None, 'different')))
    forms = review_script(module, monkeypatch)
    module.import_reference_metadata(frame)
    with pytest.raises(ValueError, match='Book selection changed'):
        finish(frame)
    assert chooser.destroyed and not forms
    library.update_metadata.assert_not_called()


@pytest.mark.parametrize('index', [-1, 1])
def test_invalid_reference_record_selection_is_rejected(module, edition, reference_file,
                                                       tmp_path, monkeypatch, index):
    frame, _, library = host(tmp_path, edition)
    _, chooser = choose(module, monkeypatch, path=reference_file, selection=index)
    forms = review_script(module, monkeypatch)
    module.import_reference_metadata(frame)
    with pytest.raises(ValueError, match='Choose a reference record'):
        finish(frame)
    assert chooser.destroyed and not forms
    library.update_metadata.assert_not_called()


@pytest.mark.parametrize('index,format,suffix', [(0, 'csl-json', '.json'), (1, 'ris', '.ris'), (2, 'bibtex', '.bib')])
def test_bibliography_export_maps_format_collection_and_publishes_exact_text(
        module, edition, tmp_path, monkeypatch, index, format, suffix):
    frame, _, library = host(tmp_path, edition)
    destination = tmp_path / ('bibliography' + suffix)
    destination.write_text('Previous export', encoding='utf-8')
    file, chooser = choose(module, monkeypatch, path=destination, filter_index=index, selection=2)
    module.export_bibliography(frame)
    assert file.destroyed and chooser.destroyed and len(frame.jobs) == 1
    assert destination.read_text(encoding='utf-8') == 'Previous export'
    finish(frame)
    library.export_bibliography.assert_called_once_with('workspace', collection='c2', format=format,
                                                       cancelled=frame.cancel_event.is_set)
    assert destination.read_text(encoding='utf-8') == 'Bibliography — Україна\n'
    assert 'Bibliography exported' in frame.reports[-1]


@pytest.mark.parametrize('action', ['bibliography', 'outline'])
def test_export_cancellation_after_backend_returns_preserves_existing_destination(
        module, edition, tmp_path, monkeypatch, action):
    frame, _, library = host(tmp_path, edition)
    destination = tmp_path / 'existing.txt'
    destination.write_bytes(b'Existing bytes must remain')
    choose(module, monkeypatch, path=destination)
    def cancel_after_render(*args, **kwargs):
        assert kwargs['cancelled']() is False
        frame.cancel_event.set()
        return 'Should never be written'
    if action == 'bibliography':
        library.export_bibliography.side_effect = cancel_after_render
        module.export_bibliography(frame)
    else:
        library.export_outline.side_effect = cancel_after_render
        module.export_research_outline(frame)
    with pytest.raises(InterruptedError, match='destination was not changed'):
        finish(frame)
    assert destination.read_bytes() == b'Existing bytes must remain'


@pytest.mark.parametrize('action', ['bibliography', 'outline'])
def test_export_keeps_original_private_root_even_if_active_library_changes(
        module, edition, tmp_path, monkeypatch, action):
    frame, _, library = host(tmp_path, edition)
    destination = library.root / 'keep-private.sqlite'
    destination.write_bytes(b'Original library bytes')
    choose(module, monkeypatch, path=destination)
    if action == 'bibliography': module.export_bibliography(frame)
    else: module.export_research_outline(frame)
    other_root = tmp_path / 'another-library'
    other_root.mkdir()
    frame.library = SimpleNamespace(root=other_root)
    assert not frame.jobs
    assert 'outside the private library' in frame.reports[-1]
    library.export_bibliography.assert_not_called()
    library.export_outline.assert_not_called()
    assert destination.read_bytes() == b'Original library bytes'


@pytest.mark.parametrize('action', ['bibliography', 'outline'])
def test_stale_export_completion_cannot_announce_success_in_another_scope(
        module, edition, tmp_path, monkeypatch, action):
    frame, _, _ = host(tmp_path, edition)
    destination = tmp_path / 'export.txt'
    choose(module, monkeypatch, path=destination)
    if action == 'bibliography': module.export_bibliography(frame)
    else: module.export_research_outline(frame)
    job = frame.jobs[-1]
    result = job.operation()
    frame.active = ('different-library', 'another-topic')
    job.done(result)
    assert not frame.reports and destination.exists()


@pytest.mark.parametrize('where', ['scope_cancel', 'scope_invalid', 'file_cancel', 'format_invalid'])
def test_bibliography_cancel_or_invalid_dialog_choice_never_starts_export(
        module, edition, tmp_path, monkeypatch, where):
    frame, _, library = host(tmp_path, edition)
    destination = tmp_path / 'untouched.json'
    file, chooser = choose(module, monkeypatch, path=destination,
        choice_result=module.wx.ID_CANCEL if where == 'scope_cancel' else None,
        selection=-1 if where == 'scope_invalid' else 0,
        file_result=module.wx.ID_CANCEL if where == 'file_cancel' else None,
        filter_index=3 if where == 'format_invalid' else 0)
    module.export_bibliography(frame)
    assert chooser.destroyed and not frame.jobs and not destination.exists()
    library.export_bibliography.assert_not_called()
    if where.startswith('scope'): module.wx.FileDialog.assert_not_called()
    else: assert file.destroyed


def test_outline_handler_passes_exact_topic_and_atomic_export_text(module, edition, tmp_path, monkeypatch):
    frame, _, library = host(tmp_path, edition)
    destination = tmp_path / 'outline.md'
    file, _ = choose(module, monkeypatch, path=destination)
    module.export_research_outline(frame)
    finish(frame)
    library.export_outline.assert_called_once_with('workspace', 'topic', cancelled=frame.cancel_event.is_set)
    assert destination.read_text(encoding='utf-8') == '# Research — Україна\n'
    assert file.destroyed and 'Research outline exported' in frame.reports[-1]


@pytest.mark.parametrize('action', ['bibliography', 'outline'])
@pytest.mark.parametrize('existing', [False, True], ids=['new', 'confirmed-existing'])
def test_queued_academic_export_keeps_destination_changes(module, edition, tmp_path, monkeypatch,
                                                         action, existing):
    frame, _, _ = host(tmp_path, edition)
    target = tmp_path / 'export.txt'
    if existing:
        target.write_bytes(b'Approved old output')
    choose(module, monkeypatch, path=target)
    handler = module.export_bibliography if action == 'bibliography' else module.export_research_outline
    handler(frame)
    assert len(frame.jobs) == 1
    target.write_bytes(b'External change after the destination question')
    with pytest.raises(ValueError, match='destination changed'):
        finish(frame)
    assert target.read_bytes() == b'External change after the destination question'


@pytest.mark.parametrize('action', ['bibliography', 'outline'])
def test_declined_academic_overwrite_starts_no_render(module, edition, tmp_path, monkeypatch, action):
    frame, _, library = host(tmp_path, edition)
    target = tmp_path / 'existing.txt'
    target.write_bytes(b'Keep this output')
    choose(module, monkeypatch, path=target)
    monkeypatch.setattr('carr_library.ui.export_destination._confirm_replace', lambda *_: False)
    handler = module.export_bibliography if action == 'bibliography' else module.export_research_outline
    handler(frame)
    assert not frame.jobs and target.read_bytes() == b'Keep this output'
    library.export_bibliography.assert_not_called()
    library.export_outline.assert_not_called()


@pytest.mark.parametrize('action,change', [
    ('bibliography', 'workspace'), ('bibliography', 'library'), ('bibliography', 'closing'),
    ('outline', 'workspace'), ('outline', 'library'), ('outline', 'closing'), ('outline', 'topic'),
])
def test_export_rechecks_scope_after_replacement_confirmation(
        module, edition, tmp_path, monkeypatch, action, change):
    frame, _, library = host(tmp_path, edition)
    destination = tmp_path / 'selected-export.txt'
    previous = b'Existing explicitly selected export'
    destination.write_bytes(previous)
    choose(module, monkeypatch, path=destination)
    def confirm(*args):
        if change == 'library':
            frame.library = SimpleNamespace(root=tmp_path / 'another-library')
        elif change == 'closing':
            frame._closing = True
        elif change == 'topic':
            frame.active = ('workspace', 'another-topic')
        else:
            frame.active = ('another-workspace', 'another-topic')
        return True
    question = Mock(side_effect=confirm)
    monkeypatch.setattr('carr_library.ui.export_destination._confirm_replace', question)
    handler = module.export_bibliography if action == 'bibliography' else module.export_research_outline
    handler(frame)
    question.assert_called_once()
    assert not frame.jobs
    library.export_bibliography.assert_not_called()
    library.export_outline.assert_not_called()
    assert destination.read_bytes() == previous
    assert len(frame.reports) == 1 and 'selection changed' in frame.reports[0]


@pytest.mark.parametrize('action', ['bibliography', 'outline'])
@pytest.mark.parametrize('phase', ['queued', 'rendering', 'staging'])
def test_export_preserves_existing_file_when_scope_changes_before_publication(
        module, edition, tmp_path, monkeypatch, action, phase):
    frame, _, library = host(tmp_path, edition)
    destination = tmp_path / 'selected-export.txt'
    previous = b'Existing explicitly selected export'
    destination.write_bytes(previous)
    choose(module, monkeypatch, path=destination)
    handler = module.export_bibliography if action == 'bibliography' else module.export_research_outline
    backend = library.export_bibliography if action == 'bibliography' else library.export_outline
    handler(frame)
    assert len(frame.jobs) == 1
    def change_scope():
        frame.active = ('another-workspace', 'another-topic')
    if phase == 'queued':
        change_scope()
    elif phase == 'rendering':
        def render(*args, **kwargs):
            change_scope()
            return backend.return_value
        backend.side_effect = render
    else:
        original_fsync = export_file.os.fsync
        def flush_then_change_scope(descriptor):
            original_fsync(descriptor)
            change_scope()
        monkeypatch.setattr(export_file.os, 'fsync', flush_then_change_scope)
    with pytest.raises(ValueError, match='selection changed'):
        finish(frame)
    if phase == 'queued':
        backend.assert_not_called()
    else:
        assert backend.call_count == 1
    assert destination.read_bytes() == previous
    assert not list(tmp_path.glob('.research-export-*'))
    assert not frame.reports


def test_bibliography_confirmation_can_change_topic_within_its_selected_workspace(
        module, edition, tmp_path, monkeypatch):
    frame, _, library = host(tmp_path, edition)
    destination = tmp_path / 'selected-export.json'
    destination.write_bytes(b'Previous bibliography')
    choose(module, monkeypatch, path=destination)
    def confirm(*args):
        frame.active = ('workspace', 'another-topic')
        return True
    monkeypatch.setattr('carr_library.ui.export_destination._confirm_replace', confirm)
    module.export_bibliography(frame)
    finish(frame)
    assert destination.read_text(encoding='utf-8') == library.export_bibliography.return_value
    assert len(frame.reports) == 1 and 'Bibliography exported' in frame.reports[0]


@pytest.mark.parametrize('action', ['bibliography', 'outline'])
def test_academic_export_reports_saved_receipt_cleanup_notice(
        module, edition, tmp_path, monkeypatch, action):
    frame, _, library = host(tmp_path, edition)
    destination = tmp_path / 'selected-export.txt'
    choose(module, monkeypatch, path=destination)
    writer = module.write_export
    def saved(*args, **kwargs):
        receipt = writer(*args, **kwargs)
        return export_file.ExportReceipt(receipt.path, 'Saved; temporary cleanup needs attention.')
    monkeypatch.setattr(module, 'write_export', saved)
    handler = module.export_bibliography if action == 'bibliography' else module.export_research_outline
    handler(frame)
    finish(frame)
    assert destination.is_file() and len(frame.jobs) == 1
    assert 'Saved; temporary cleanup needs attention.' in frame.reports[-1]
