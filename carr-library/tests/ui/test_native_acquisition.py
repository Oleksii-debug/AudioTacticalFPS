"""DOI dialog handlers with wx doubles, plus an optional real Windows probe.

Handler tests exercise the production dialog and its exact service contract.
Only the final Windows-only test creates native widgets; neither mocks nor a
headless Linux run are evidence of Windows/NVDA or real provider acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
import carr_library


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.label = kwargs.get('label', '')
        self.items = list(kwargs.get('choices', ()))
        self.index = -1
        self.enabled = True
        self.focused = self.destroyed = False
        self.bindings = {}
        self.pages = []
        self.modal_result = None
        self.options = kwargs
    def Bind(self, event, callback): self.bindings[event] = callback
    def SetName(self, name): self.name = name
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetSelection(self, index): self.index = index
    def GetSelection(self): return self.index
    def Set(self, items): self.items, self.index = list(items), -1
    def SetFocus(self): self.focused = True
    def SetInsertionPoint(self, position): self.insertion = position
    def SetMaxLength(self, length): self.max_length = length
    def SetMinSize(self, value): self.min_size = value
    def SetSizer(self, value): self.sizer = value
    def SetEscapeId(self, value): self.escape_id = value
    def Add(self, *args): pass
    def AddPage(self, page, label): self.pages.append((page, label))
    def Enable(self, enabled): self.enabled = bool(enabled)
    def EndModal(self, result): self.modal_result = result
    def ShowModal(self): return self.modal_result
    def Destroy(self): self.destroyed = True


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module(tmp_path):
    wx = ModuleType('wx')
    for name in ('Dialog', 'Panel', 'BoxSizer', 'StaticText', 'TextCtrl',
                 'Choice', 'Button', 'CheckBox', 'Notebook'):
        setattr(wx, name, Control)
    for index, name in enumerate(('VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL',
            'LEFT', 'RIGHT', 'TOP', 'BOTTOM', 'ALIGN_RIGHT', 'TE_MULTILINE',
            'TE_READONLY', 'ID_CANCEL', 'ID_OK', 'ID_YES', 'ID_NO', 'YES_NO',
            'NO_DEFAULT', 'ICON_QUESTION', 'EVT_BUTTON', 'EVT_CLOSE',
            'EVT_CHOICE', 'EVT_TEXT', 'EVT_CHECKBOX', 'DD_DIR_MUST_EXIST')):
        setattr(wx, name, 1 << index)
    wx.callbacks = []
    wx.CallAfter = lambda callback: wx.callbacks.append(callback)
    wx.dialogs = []
    wx.dialog_hook = None
    wx.folder_result = wx.name_result = wx.ID_OK
    wx.confirm_result = wx.ID_YES
    wx.folder_path = str(tmp_path)
    wx.folder_name = 'chosen-new-folder'

    class Popup(Control):
        kind = ''
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.args = args
            wx.dialogs.append(self)
        def ShowModal(self):
            if wx.dialog_hook:
                wx.dialog_hook(self.kind)
            return getattr(wx, self.kind + '_result')
        def GetPath(self): return wx.folder_path
        def GetValue(self): return wx.folder_name
    class DirDialog(Popup): kind = 'folder'
    class TextEntryDialog(Popup): kind = 'name'
    class MessageDialog(Popup): kind = 'confirm'
    wx.DirDialog, wx.TextEntryDialog, wx.MessageDialog = DirDialog, TextEntryDialog, MessageDialog
    ui = Path(carr_library.__file__).resolve().parent / 'ui'
    with patch.dict(sys.modules, {'wx': wx}):
        helper = _load(ui / 'catalog_tools_dialog.py', 'carr_library.ui._acquisition_helpers')
        with patch.dict(sys.modules, {'carr_library.ui.catalog_tools_dialog': helper}):
            value = _load(ui / 'acquisition_dialog.py', 'carr_library.ui._acquisition_handlers')
    return value


def preview_fixture(workspace='a', *, sources=True):
    raw = b'{"synthetic":true}'
    location = SimpleNamespace(provider='unpaywall', url='https://repository.example/paper.pdf',
        landing_url='https://repository.example/record/1', format='pdf', version='acceptedVersion',
        license='cc-by-4.0', reported_open_access=True, host_type='repository',
        host='repository.example')
    metadata = SimpleNamespace(provider='crossref', doi='10.1234/reading',
        title='Читання & Research — štúdia', authors=('Олена Коваль', 'Šimon Výskumník'),
        year='2024', item_type='article-journal', container_title='Research journal',
        publisher='Publisher', volume='7', issue='2', pages='100–112', isbn=('9780306406157',),
        abstract='A complete provider abstract.\nSecond paragraph.', language='uk',
        source_url='https://doi.org/10.1234/reading', locations=(location,),
        warnings=('Edition match needs review.',), raw_json=raw, raw_sha256=sha256(raw).hexdigest())
    return SimpleNamespace(token='immutable-preview-1', workspace=workspace,
        doi='10.1234/reading', metadata=metadata, provider_records=(metadata,),
        sources=(location,) if sources else (), warnings=('Provider claims are not source verification.',),
        fetched_utc='2026-11-26T12:00:00Z', cached=False)


class Library:
    def __init__(self, root):
        from carr_library.application import LibraryApp

        self.root, self.calls, self.hook = root, [], None
        # Keep UI/service responses synthetic while exercising real scope reads.
        self.scope_app = LibraryApp(root / 'scope-fixture')
        with self.scope_app.write() as db:
            for workspace in ('a', 'b'):
                db.execute('INSERT INTO workspace(id) VALUES(?)', (workspace,))
                db.execute('INSERT INTO workspace_meta VALUES(?,?,?,0)',
                           (workspace, 'Synthetic library ' + workspace, ''))
        with self.scope_app.topic_transaction('a') as tree:
            tree.create('topic-a', 'Synthetic topic')
        self.preview = preview_fixture()
        self.ignore_cancel = False
        self.error = None
        self.metadata = SimpleNamespace(edition_id='edition-exact', title='Existing selected title',
            authors=('Existing Author',), language='en', volume='1', edition='Second edition')
        self.result_state = 'quarantined'
    def read(self, workspace):
        return self.scope_app.read(workspace)
    def collections(self, workspace):
        self.calls.append(('collections', workspace))
        return (('collection-a', 'Selected collection'), ('collection-b', 'Another collection'))
    def edition_metadata(self, workspace, edition):
        self.calls.append(('edition_metadata', workspace, edition))
        return self.metadata
    def _call(self, kind, workspace, args, kwargs):
        self.calls.append((kind, workspace, args, kwargs))
        if self.hook: self.hook()
        if self.error: raise self.error
        if not self.ignore_cancel and kwargs['cancelled']():
            raise InterruptedError('Cancelled by service.')
    def lookup_doi(self, workspace, doi, **kwargs):
        self._call('lookup', workspace, (doi,), kwargs)
        return self.preview
    def save_doi_reference(self, workspace, preview, **kwargs):
        self._call('save', workspace, (preview,), kwargs)
        return SimpleNamespace(id='reference-exact')
    def apply_doi_metadata(self, workspace, edition, preview, **kwargs):
        self._call('apply', workspace, (edition, preview), kwargs)
        return 'new-metadata-revision'
    def acquire_doi_source(self, workspace, preview, index, destination, **kwargs):
        self._call('acquire', workspace, (preview, index, destination), kwargs)
        return SimpleNamespace(path=destination / 'source.pdf', receipt_path=destination / 'provenance.json',
            reference_id='reference-exact', edition_id='edition-imported', state=self.result_state,
            message='Text requires the existing import review.', sha256='a' * 64)


class Host:
    def __init__(self, library, wx):
        self.library, self.wx = library, wx
        self.active = ('a', 'topic-a')
        self.working = self.busy = self._closing = False
        self.cancel_event = threading.Event()
        self.jobs, self.messages, self.refreshes = [], [], []
        self.status = Control()
        self.selection = ('collection-a', None, 'edition-exact')
        self.panel = SimpleNamespace(workspace='a', ready=True,
            _selection=lambda: self.selection,
            refresh=lambda **kwargs: self.refreshes.append(kwargs))
        self.book = SimpleNamespace(GetPage=lambda index: self.panel)
        self.reject, self.run_error = False, None
    def _workspace(self):
        if self.active is None: raise ValueError('Open a library first.')
        return self.active[0]
    def report(self, message): self.messages.append(message)
    def run_job(self, title, operation, done=None, failed=None):
        if self.run_error: raise self.run_error
        if self.reject: return
        self.cancel_event.clear()
        self.working = self.busy = True
        self.jobs.append((title, operation, done, failed))
    def work(self):
        _, operation, done, failed = self.jobs.pop(0)
        try:
            value = operation()
        except Exception as exc:
            callback = lambda error=exc: failed(error)
        else:
            callback = lambda: done(value)
        def finish():
            self.working = self.busy = False
            callback()
        self.wx.CallAfter(finish)
    def finish(self):
        self.work()
        while self.wx.callbacks:
            self.wx.callbacks.pop(0)()


@pytest.fixture
def setup(module, tmp_path):
    library = Library(tmp_path / 'private')
    frame = Host(library, module.wx)
    form = module.AcquisitionDialog(frame, 'a', library.collections('a'),
        collection='collection-a', selected_metadata=library.metadata,
        selected_panel=frame.panel, selected_selection=frame.selection)
    library.calls.clear()
    return form, frame, library


def looked_up(setup):
    form, frame, library = setup
    form.doi.SetValue('https://doi.org/10.1234/reading')
    form.online.SetValue(True)
    form.on_online_changed()
    assert form.lookup()
    frame.finish()
    assert form.preview is library.preview
    library.calls.clear()
    return setup


def test_initial_controls_are_named_focused_and_offline_without_jobs(module, setup):
    form, frame, library = setup
    assert form.doi.name == 'DOI or DOI resolver link' and form.doi.focused
    assert form.online.GetValue() is False and form.rights.GetValue() is False
    assert form.providers['crossref'].GetValue() is True
    assert not form.providers['europe_pmc'].GetValue() and not form.providers['unpaywall'].GetValue()
    assert not form.email.enabled and not form.save_button.enabled and not form.apply_button.enabled
    assert not form.download_button.enabled and not form.cancel_button.enabled
    assert form.close_button.enabled and form.escape_id == module.wx.ID_CANCEL
    assert [label for _, label in form.tabs.pages] == ['Metadata preview', 'Download source']
    assert form.metadata_preview.max_length == module.MAX_PREVIEW_CHARACTERS
    assert form.source_preview.name == 'Source URL, version, license and access claims'
    assert library.calls == frame.jobs == []


@pytest.mark.parametrize('problem, expected', [
    ('offline', 'Enable online access'), ('missing_doi', 'Enter a DOI'),
    ('no_provider', 'Choose at least one'), ('missing_email', 'Enter your email'),
])
def test_lookup_validation_never_submits_unauthorized_request(setup, problem, expected):
    form, frame, library = setup
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(problem != 'offline')
    if problem == 'missing_doi': form.doi.SetValue(' ')
    if problem == 'no_provider': form.providers['crossref'].SetValue(False)
    if problem == 'missing_email': form.providers['unpaywall'].SetValue(True)
    assert form.lookup() is False
    assert expected in form.status.GetValue()
    assert frame.jobs == library.calls == []


def test_lookup_worker_forwards_only_explicit_doi_providers_email_and_scope(setup):
    form, frame, library = setup
    form.doi.SetValue(' https://doi.org/10.1234/reading ')
    form.online.SetValue(True)
    form.providers['europe_pmc'].SetValue(True)
    form.providers['unpaywall'].SetValue(True)
    form.email.SetValue(' user@example.org ')
    assert form.lookup()
    assert library.calls == [] and form.pending and form.cancel_button.enabled
    assert not form.doi.enabled and not form.lookup_button.enabled and form.online.enabled
    assert form.lookup() is False and len(frame.jobs) == 1
    frame.work()
    assert form.metadata_preview.GetValue() == '' and form.pending
    frame.wx.callbacks.pop(0)()
    kind, workspace, args, kwargs = library.calls[0]
    assert (kind, workspace, args) == ('lookup', 'a', ('https://doi.org/10.1234/reading',))
    assert set(kwargs) == {'online_allowed', 'providers', 'unpaywall_email', 'cancelled'}
    assert kwargs['online_allowed'] is True
    assert kwargs['providers'] == ('crossref', 'europe_pmc', 'unpaywall')
    assert kwargs['unpaywall_email'] == 'user@example.org'
    text = form.metadata_preview.GetValue()
    assert '1. Олена Коваль\n2. Šimon Výskumník' in text
    assert library.preview.metadata.abstract in text
    assert library.preview.metadata.raw_sha256 in text
    assert 'synthetic' not in text and 'Existing selected title' not in text
    assert 'Publication page extent: 100–112' in text
    assert form.metadata_preview.focused and form.metadata_preview.insertion == 0
    assert form.preview is library.preview and not form.pending
    assert form.save_button.enabled and form.apply_button.enabled and not form.download_button.enabled


def test_email_is_not_sent_when_unpaywall_is_unselected(setup):
    form, frame, library = setup
    form.email.SetValue('private@example.org')
    looked_up(setup)
    # Run another request to inspect its explicit service arguments.
    assert form.lookup()
    frame.finish()
    assert library.calls[0][3]['unpaywall_email'] == ''


@pytest.mark.parametrize('timing', ['before_worker', 'during_worker', 'before_callback'])
def test_cancelled_lookup_never_displays_late_result(setup, timing):
    form, frame, library = setup
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    if timing == 'during_worker':
        library.ignore_cancel = True
        library.hook = frame.cancel_event.set
    assert form.lookup()
    if timing == 'before_worker': form.cancel_operation()
    if timing == 'before_callback':
        frame.work()
        form.cancel_operation()
        frame.wx.callbacks.pop(0)()
    else:
        frame.finish()
    assert form.preview is None and form.metadata_preview.GetValue() == ''
    assert 'cancel' in form.status.GetValue().lower() and not form.pending


def test_consent_can_be_revoked_during_network_work(setup):
    form, frame, library = setup
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    form.online.SetValue(False)
    form.on_online_changed()
    assert frame.cancel_event.is_set()
    frame.finish()
    assert library.calls == [] and form.preview is None


@pytest.mark.parametrize('change', ['workspace', 'topic', 'library', 'closing', 'doi'])
def test_changed_scope_or_request_blocks_worker_and_stale_callback(setup, change):
    form, frame, library = setup
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    if change == 'workspace': frame.active = ('b', 'topic-a')
    elif change == 'topic': frame.active = ('a', 'topic-b')
    elif change == 'library': frame.library = object()
    elif change == 'closing': frame._closing = True
    else:
        form.doi.SetValue('10.1234/new')
        form.on_request_changed()
    frame.finish()
    assert library.calls == [] and form.preview is None
    assert form.metadata_preview.GetValue() == '' and not form.pending


def test_crossworkspace_result_is_not_accepted(setup):
    form, frame, library = setup
    library.preview = preview_fixture('other')
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    frame.finish()
    assert form.preview is None and not form.save_button.enabled
    assert 'another library' in form.status.GetValue()


def test_incomplete_native_text_display_disables_actions(setup):
    form, frame, library = setup
    form.metadata_preview.SetValue = lambda value: setattr(form.metadata_preview, 'value', value[:50])
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    frame.finish()
    assert form.preview is None and form.metadata_preview.GetValue() == ''
    assert not form.save_button.enabled and not form.apply_button.enabled
    assert 'complete preview' in form.status.GetValue()


def test_oversized_preview_is_explicitly_rejected_without_truncation(module, setup):
    form, frame, library = setup
    library.preview.metadata.abstract = 'x' * module.MAX_PREVIEW_CHARACTERS
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    frame.finish()
    assert form.preview is None and form.metadata_preview.GetValue() == ''
    assert 'too large' in form.status.GetValue()


def test_editing_doi_or_provider_discards_old_preview_and_rights(setup):
    form, frame, library = looked_up(setup)
    form.rights.SetValue(True)
    form.providers['europe_pmc'].SetValue(True)
    form.on_request_changed()
    assert form.preview is None and form.sources.items == [] and not form.rights.GetValue()
    assert form.save_reference() is False and library.calls == frame.jobs == []


def test_save_reference_is_local_after_online_consent_is_revoked(setup):
    form, frame, library = looked_up(setup)
    form.online.SetValue(False)
    form.on_online_changed()
    assert form.save_reference()
    frame.finish()
    kind, workspace, args, kwargs = library.calls[0]
    assert (kind, workspace) == ('save', 'a') and args[0] is library.preview
    assert set(kwargs) == {'cancelled'}
    assert form.changed and 'reference-exact' in form.status.GetValue()
    assert 'No document was downloaded' in form.status.GetValue()


def test_apply_preserves_exact_book_snapshot_and_requires_confirmation(module, setup):
    form, frame, library = looked_up(setup)
    assert form.apply_metadata()
    confirmation = module.wx.dialogs[0]
    assert confirmation.destroyed
    assert confirmation.options['style'] & module.wx.NO_DEFAULT
    assert 'Current title: Existing selected title' in confirmation.args[1]
    assert 'current edition label is preserved' in confirmation.args[1]
    frame.finish()
    kind, workspace, args, kwargs = library.calls[0]
    assert (kind, workspace, args[0]) == ('apply', 'a', 'edition-exact')
    assert args[1] is library.preview and kwargs['expected_metadata'] is library.metadata
    assert form.changed and not form.apply_button.enabled
    assert form.apply_metadata() is False
    form.on_request_changed()
    assert form.lookup()
    frame.finish()
    assert not form.apply_button.enabled


def test_apply_confirmation_explains_missing_fields_preserved(module, setup):
    form, frame, library = looked_up(setup)
    library.preview.metadata.authors = ()
    library.preview.metadata.title = ''
    library.preview.metadata.language = library.preview.metadata.volume = ''
    module.wx.confirm_result = module.wx.ID_NO
    assert form.apply_metadata() is False
    text = module.wx.dialogs[0].args[1]
    assert 'current authors are preserved' in text
    assert 'Current language is preserved' in text and 'Current volume is preserved' in text
    assert 'Proposed title: Existing selected title' in text
    assert 'current title is preserved' in text
    assert library.calls == frame.jobs == []
    assert form.apply_button.focused


@pytest.mark.parametrize('when', ['before', 'during_confirmation'])
def test_apply_rejects_changed_selected_identity(module, setup, when):
    form, frame, library = looked_up(setup)
    def change(*args): frame.selection = ('collection-a', None, 'other-edition')
    if when == 'before': change()
    else: module.wx.dialog_hook = change
    assert form.apply_metadata() is False
    assert library.calls == frame.jobs == []
    assert 'changed' in form.status.GetValue()


def test_service_stale_metadata_error_keeps_preview_and_does_not_report_success(setup):
    form, frame, library = looked_up(setup)
    library.error = ValueError('Book metadata changed. Refresh the catalog before editing again.')
    assert form.apply_metadata()
    frame.finish()
    assert form.preview is library.preview and not form.changed
    assert 'Book metadata changed' in form.status.GetValue()


def test_source_preview_clearly_distinguishes_provider_claims(setup):
    form, frame, library = looked_up(setup)
    text = form.source_preview.GetValue()
    assert 'https://repository.example/paper.pdf' in text
    assert 'acceptedVersion' in text and 'cc-by-4.0' in text
    assert 'according to the provider' in text
    assert 'does not by itself establish permission' in text
    assert form.sources.items == ['1. unpaywall — PDF — repository.example']
    form.rights.SetValue(True)
    form.on_rights_changed()
    assert form.download_button.enabled
    form.on_source_changed()
    assert not form.rights.GetValue() and not form.download_button.enabled


def test_complete_preview_accepts_actual_frozen_provider_models(module, setup):
    from carr_library.acquisition.models import AccessLocation, MetadataPreview
    form, frame, library = setup
    source = AccessLocation(provider='unpaywall', url='https://repository.example/paper.pdf',
        landing_url='https://repository.example/record/1', format='pdf',
        version='acceptedVersion', license='cc-by-4.0', reported_open_access=True)
    metadata = MetadataPreview(provider='crossref', doi='10.1234/reading',
        title='Читання & Research — štúdia', authors=('Олена Коваль', 'Šimon Výskumník'),
        item_type='article', abstract='Preserved provider abstract.', raw_json=b'{"synthetic":true}')
    oa_metadata = MetadataPreview(provider='unpaywall', doi='10.1234/reading',
        title=metadata.title, locations=(source,), raw_json=b'{"synthetic_oa":true}')
    library.preview.metadata = metadata
    library.preview.provider_records = (metadata, oa_metadata)
    library.preview.sources = (source,)
    looked_up(setup)
    assert form.preview.metadata is metadata
    assert metadata.raw_sha256 in form.metadata_preview.GetValue()
    assert oa_metadata.raw_sha256 in form.metadata_preview.GetValue()
    assert 'cc-by-4.0' in form.source_preview.GetValue()
    assert source.host in form.sources.items[0]


def test_incomplete_source_display_does_not_enable_download(setup):
    form, frame, library = looked_up(setup)
    form.source_preview.SetValue = lambda value: setattr(form.source_preview, 'value', value[:30])
    form.on_source_changed()
    assert form.sources.GetSelection() == -1
    assert not form.rights.enabled and not form.download_button.enabled
    assert 'complete preview' in form.status.GetValue()


@pytest.mark.parametrize('reported', [False, None])
def test_pdf_without_provider_open_access_claim_is_metadata_only(module, setup, reported):
    form, frame, library = looked_up(setup)
    library.preview.sources[0].reported_open_access = reported
    form.on_source_changed()
    assert not form.rights.enabled and not form.download_button.enabled
    form.rights.SetValue(True)
    assert form.download_source() is False
    assert 'provider-reported open-access PDF' in form.status.GetValue()
    assert frame.jobs == library.calls == module.wx.dialogs == []


@pytest.mark.parametrize('missing', ['online', 'rights', 'source', 'pdf', 'collection'])
def test_download_checks_before_opening_destination_dialog(module, setup, missing):
    form, frame, library = looked_up(setup)
    form.rights.SetValue(True)
    if missing == 'online': form.online.SetValue(False)
    elif missing == 'rights': form.rights.SetValue(False)
    elif missing == 'source': form.sources.SetSelection(-1)
    elif missing == 'pdf': library.preview.sources[0].format = 'html'
    else: form.collection.SetSelection(-1)
    assert form.download_source() is False
    assert module.wx.dialogs == [] and library.calls == frame.jobs == []


def test_download_passes_exact_candidate_index_and_chosen_new_folder(module, setup, tmp_path):
    form, frame, library = looked_up(setup)
    form.rights.SetValue(True)
    form.collection.SetSelection(2)
    assert form.download_source()
    assert library.calls == [] and not (tmp_path / 'chosen-new-folder').exists()
    assert len(module.wx.dialogs) == 2 and all(d.destroyed for d in module.wx.dialogs)
    frame.finish()
    kind, workspace, args, kwargs = library.calls[0]
    assert (kind, workspace) == ('acquire', 'a')
    assert args[0] is library.preview and args[1] == 0
    assert args[2] == tmp_path / 'chosen-new-folder'
    assert kwargs['online_allowed'] is True and kwargs['rights_confirmed'] is True
    assert kwargs['collection'] == 'collection-b'
    assert 'Import state: quarantined' in form.status.GetValue()
    assert 'provenance.json' in form.status.GetValue()
    assert 'a' * 64 in form.status.GetValue()
    assert 'requires the existing import review' in form.status.GetValue()
    assert form.changed


@pytest.mark.parametrize('cancel', ['folder', 'name'])
def test_cancel_destination_never_requests_or_imports_source(module, setup, cancel):
    form, frame, library = looked_up(setup)
    form.rights.SetValue(True)
    setattr(module.wx, cancel + '_result', module.wx.ID_CANCEL)
    assert form.download_source() is False
    assert library.calls == frame.jobs == []
    assert all(d.destroyed for d in module.wx.dialogs)
    assert 'No source was requested' in form.status.GetValue()
    assert form.download_button.focused


@pytest.mark.parametrize('folder_name', ['../escape', 'folder/file', 'folder\\file', 'bad:name',
                                          'folder.', 'folder ', '', 'x' * 121])
def test_invalid_destination_name_never_submits_download(module, setup, folder_name):
    form, frame, library = looked_up(setup)
    form.rights.SetValue(True)
    module.wx.folder_name = folder_name
    assert form.download_source() is False
    assert library.calls == frame.jobs == []


@pytest.mark.parametrize('change', ['workspace', 'topic', 'rights', 'online', 'source', 'collection', 'preview'])
def test_destination_prompt_changes_are_revalidated(module, setup, change):
    form, frame, library = looked_up(setup)
    form.rights.SetValue(True)
    def mutate(kind):
        if kind != 'name': return
        if change == 'workspace': frame.active = ('b', 'topic-a')
        elif change == 'topic': frame.active = ('a', 'topic-b')
        elif change == 'rights': form.rights.SetValue(False)
        elif change == 'online': form.online.SetValue(False)
        elif change == 'source': form.sources.SetSelection(-1)
        elif change == 'collection': form.collection.SetSelection(2)
        else: form.preview = preview_fixture()
    module.wx.dialog_hook = mutate
    assert form.download_source() is False
    assert library.calls == frame.jobs == []
    assert 'changed' in form.status.GetValue()


def test_missing_sources_and_no_selected_book_still_allow_bibliographic_save(module, tmp_path):
    library = Library(tmp_path)
    library.preview = preview_fixture(sources=False)
    frame = Host(library, module.wx)
    form = module.AcquisitionDialog(frame, 'a')
    looked_up((form, frame, library))
    assert not form.apply_button.enabled and not form.download_button.enabled
    assert 'No source locations' in form.source_preview.GetValue()
    assert form.save_reference()
    frame.finish()
    assert form.changed


def test_rejected_job_submission_recovers_controls(setup):
    form, frame, library = setup
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    frame.reject = True
    assert form.lookup()
    assert not form.pending and form.lookup_button.enabled and not form.cancel_button.enabled
    assert 'could not be started' in form.status.GetValue()
    assert library.calls == frame.jobs == []


def test_cancel_after_completed_mutation_reports_actual_result(setup):
    form, frame, library = looked_up(setup)
    assert form.save_reference()
    frame.work()
    form.cancel_operation()
    frame.wx.callbacks.pop(0)()
    assert form.changed and 'reference-exact' in form.status.GetValue()


def test_close_cancels_then_ends_modal_after_worker_callback(module, setup):
    form, frame, library = setup
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    form.close()
    assert frame.cancel_event.is_set() and form.modal_result is None and not form._closed
    frame.finish()
    assert form._closed and form.modal_result == module.wx.ID_CANCEL
    assert library.calls == []


def test_disposal_blocks_callbacks_into_destroyed_dialog(setup):
    form, frame, library = setup
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    frame.work()
    previous = form.status.GetValue()
    form.dispose()
    form.Destroy()
    frame.wx.callbacks.pop(0)()
    assert form.destroyed and form.preview is None and form.status.GetValue() == previous


def test_wrapper_uses_fresh_selected_edition_without_automatic_network(module, setup, monkeypatch):
    _, frame, library = setup
    created = []
    original = module.AcquisitionDialog
    class Capture(original):
        def ShowModal(self):
            created.append(self)
            return module.wx.ID_CANCEL
    monkeypatch.setattr(module, 'AcquisitionDialog', Capture)
    module.show_acquisition_dialog(frame)
    assert library.calls == [('collections', 'a'), ('edition_metadata', 'a', 'edition-exact')]
    assert frame.jobs == [] and frame.messages == []
    assert created[0].selected_metadata is library.metadata
    assert created[0].selected_panel is frame.panel
    assert created[0].destroyed and created[0]._closed and frame.status.focused


@pytest.mark.parametrize('switch', [False, True])
def test_wrapper_refreshes_only_original_scope_after_mutation(module, setup, monkeypatch, switch):
    _, frame, library = setup
    original = module.AcquisitionDialog
    class Changed(original):
        def ShowModal(self):
            self.changed = True
            if switch: frame.active = ('b', None)
            return module.wx.ID_CANCEL
    monkeypatch.setattr(module, 'AcquisitionDialog', Changed)
    module.show_acquisition_dialog(frame)
    assert frame.refreshes == ([] if switch else [{'selection': frame.selection}])


def test_real_application_lookup_save_apply_from_dialog_preserves_source(module, tmp_path):
    """Production UI + provider parser + service + SQLite; only HTTP is synthetic."""
    import json
    from carr_library.application import LibraryApp
    from carr_library.acquisition.service import AcquisitionService, AcquisitionPreview
    from carr_library.research.academic_library import AcademicLibrary

    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('DOI handler integration')
    source = app.add_text(workspace, 'Original title', 'Original source words are unchanged.\nДжерело.',
        authors=('Original Author',), language='uk', edition='Original edition', volume='I')
    before = app.assets.resolve(source['asset']).read_bytes()
    requests = []
    def transport(consent):
        assert consent.allowed is True and consent.purpose == 'metadata'
        def request(value, *, cancelled):
            assert not cancelled()
            requests.append(value)
            return json.dumps({'status': 'ok', 'message-type': 'work', 'message': {
                'DOI': '10.1234/reading', 'title': ['Synthetic metadata title'],
                'type': 'journal-article', 'volume': '2',
                'author': [{'given': 'Олена', 'family': 'Коваль'}, {'name': 'Research Group'}],
                'URL': 'https://doi.org/10.1234/reading'}}).encode('utf-8')
        return request
    from carr_library.providers import ProviderManager
    ProviderManager(app.root / 'provider-settings').set_mode('HYBRID')
    app._acquisition_service = AcquisitionService(app, transport_factory=transport)
    frame = Host(app, module.wx)
    frame.active, frame.panel.workspace = (workspace, None), workspace
    frame.selection = (None, None, source['edition'])
    current = app.edition_metadata(workspace, source['edition'])
    form = module.AcquisitionDialog(frame, workspace, selected_metadata=current,
        selected_panel=frame.panel, selected_selection=frame.selection)
    assert requests == []
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    frame.finish()
    assert isinstance(form.preview, AcquisitionPreview)
    assert form.preview.metadata.authors == ('Олена Коваль', 'Research Group')
    assert len(requests) == 1 and 'Original' not in repr(requests)
    assert form.save_reference()
    frame.finish()
    with app.read(workspace) as db:
        reference, = AcademicLibrary(db, workspace).list()
    assert reference.doi == form.preview.doi and reference.title == 'Synthetic metadata title'
    assert form.apply_metadata()
    frame.finish()
    updated = app.edition_metadata(workspace, source['edition'])
    assert updated.title == reference.title and updated.authors == ('Олена Коваль', 'Research Group')
    assert updated.language == 'uk' and updated.edition == 'Original edition' and updated.volume == '2'
    assert app.assets.resolve(source['asset']).read_bytes() == before
    assert not form.apply_button.enabled and form.changed
    assert len(requests) == 1  # Local save and apply never open another connection.


def test_real_metadata_apply_cancelled_after_writer_acquisition_does_not_commit(tmp_path):
    """Consent/scope can change after service preflight but before any write."""
    from contextlib import contextmanager
    from carr_library.application import LibraryApp, exclusive_writer
    from carr_library.acquisition.models import MetadataPreview
    from carr_library.acquisition.service import AcquisitionService, AcquisitionPreview
    app = LibraryApp(tmp_path / 'cancelled-metadata-library')
    workspace = app.create_workspace('Synthetic cancellation review')
    source = app.add_text(workspace, 'Before cancellation', 'Exact original source.')
    current = app.edition_metadata(workspace, source['edition'])
    original = app.assets.resolve(source['asset']).read_bytes()
    metadata = MetadataPreview(provider='crossref', doi='10.1234/review', title='Cancelled proposal')
    preview = AcquisitionPreview('review-token', workspace, metadata.doi, metadata,
        (metadata,), (), (), '2026-11-26T12:00:00Z')
    service = AcquisitionService(app)
    service._previews[preview.token] = preview
    app._acquisition_service = service
    cancelled = threading.Event()
    @contextmanager
    def cancel_at_writer_entry(root, **kwargs):
        with exclusive_writer(root, **kwargs) as handle:
            cancelled.set()
            yield handle
    with patch('carr_library.application.exclusive_writer', cancel_at_writer_entry):
        with pytest.raises(InterruptedError):
            app.apply_doi_metadata(workspace, source['edition'], preview,
                expected_metadata=current, cancelled=cancelled.is_set)
    assert cancelled.is_set()
    assert app.edition_metadata(workspace, source['edition']) == current
    assert app.assets.resolve(source['asset']).read_bytes() == original


@pytest.mark.parametrize('archived_scope', ['workspace', 'topic'])
def test_review_completed_lookup_rechecks_external_archive(module, tmp_path, archived_scope):
    """Actual service finishes before another instance archives its dialog scope."""
    import json
    from carr_library.application import LibraryApp
    from carr_library.acquisition.service import AcquisitionService

    app = LibraryApp(tmp_path / 'review-library')
    workspace = app.create_workspace('DOI scope review')
    topic = app.create_topic(workspace, 'Original topic')
    requests = []

    def transport(consent):
        assert consent.allowed and consent.purpose == 'metadata'
        def request(value, *, cancelled):
            assert not cancelled()
            requests.append(value)
            return json.dumps({'status': 'ok', 'message-type': 'work', 'message': {
                'DOI': '10.1234/reading', 'title': ['Synthetic reviewed metadata'],
                'type': 'journal-article'}}).encode()
        return request

    from carr_library.providers import ProviderManager
    ProviderManager(app.root / 'provider-settings').set_mode('HYBRID')
    app._acquisition_service = AcquisitionService(app, transport_factory=transport)
    frame = Host(app, module.wx)
    frame.active, frame.panel.workspace = (workspace, topic), workspace
    form = module.AcquisitionDialog(frame, workspace)
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup()
    frame.work()
    assert len(requests) == 1 and form.preview is None and module.wx.callbacks
    if archived_scope == 'workspace':
        app.archive_workspace(workspace)
    else:
        with app.topic_transaction(workspace) as tree:
            tree.archive(topic)
    while module.wx.callbacks:
        module.wx.callbacks.pop(0)()
    assert frame.active == (workspace, topic)
    assert form.preview is None, 'A queued result must not publish after its scope is archived.'
    assert form.metadata_preview.GetValue() == ''
    assert not form.save_button.enabled and not form.pending


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual wx window probe requires Windows desktop')
def test_windows_native_dialog_async_lookup_focus_and_close(tmp_path):
    """No network: native event dispatch plus real worker/CallAfter completion."""
    wx = pytest.importorskip('wx')
    ui = Path(carr_library.__file__).resolve().parent / 'ui'
    module = _load(ui / 'acquisition_dialog.py', 'carr_library.ui._acquisition_native_probe')
    app = wx.GetApp() or wx.App(False)
    library = Library(tmp_path)
    failures = []

    class NativeHost(wx.Frame):
        def __init__(self):
            super().__init__(None, title='Synthetic DOI dialog probe')
            self.library, self.active, self.book = library, ('a', None), None
            self.working = self.busy = self._closing = False
            self.cancel_event = threading.Event()
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.status = wx.TextCtrl(self)
        def _workspace(self): return self.active[0]
        def report(self, value): self.status.SetValue(value)
        def run_job(self, title, operation, done, failed):
            self.cancel_event.clear()
            self.working = self.busy = True
            def completed(future):
                def apply():
                    self.working = self.busy = False
                    try: value = future.result()
                    except Exception as exc: failed(exc)
                    else: done(value)
                wx.CallAfter(apply)
            self.executor.submit(operation).add_done_callback(completed)

    host = NativeHost()
    form = module.AcquisitionDialog(host, 'a')
    deadline = time.monotonic() + 8

    def finish_when_ready():
        try:
            if form.pending:
                if time.monotonic() >= deadline:
                    raise AssertionError('Native DOI completion exceeded 8 seconds.')
                wx.CallLater(10, finish_when_ready)
                return
            assert form.preview is library.preview
            assert 'Олена Коваль' in form.metadata_preview.GetValue()
            assert form.metadata_preview.GetName() == 'Complete DOI metadata and provider provenance preview'
            assert wx.Window.FindFocus() is form.metadata_preview
            assert not form.rights.GetValue() and not form.download_button.IsEnabled()
        except BaseException as exc:
            failures.append(exc)
        finally:
            if not form.pending or failures:
                if failures:
                    form.cancel_operation()
                    form.EndModal(wx.ID_CANCEL)
                else:
                    form.close()

    def exercise():
        try:
            assert not form.online.GetValue() and library.calls == []
            form.doi.SetValue('10.1234/reading')
            form.online.SetValue(True)
            form.on_online_changed()
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, form.lookup_button.GetId())
            event.SetEventObject(form.lookup_button)
            form.lookup_button.GetEventHandler().ProcessEvent(event)
            assert form.pending
            wx.CallLater(10, finish_when_ready)
        except BaseException as exc:
            failures.append(exc)
            form.EndModal(wx.ID_CANCEL)

    try:
        host.Show()
        wx.CallAfter(exercise)
        assert form.ShowModal() == wx.ID_CANCEL
        if failures: raise failures[0]
    finally:
        form.dispose()
        host.executor.shutdown(wait=True, cancel_futures=True)
        form.Destroy()
        host.Destroy()
        app.ProcessPendingEvents()
