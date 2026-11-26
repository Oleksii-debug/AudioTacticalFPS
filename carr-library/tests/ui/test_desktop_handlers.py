"""Headless desktop event protocols; these are not native Windows/NVDA tests."""
import ast
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor, format_citation
from carr_library.application import LibraryApp
from carr_library.catalog.read_views import active_collections
from carr_library.reader.context import open_context
from carr_library.research.topic_tree import TopicTree
from carr_library.research.anchored_export import SavedEvidence
from carr_library.research.topic_search import TopicHit
from carr_library.ui.search_model import Hit, SearchModel
from carr_library.ui.scope_bindings import ScopeServices, scope_labels


class BaseFrame:
    def on_close(self,event):
        event.Skip()

    def refresh_scopes(self,event=None):
        pass


@pytest.fixture
def classes():
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    nodes = [node for node in parsed.body if isinstance(node,(ast.ClassDef,ast.FunctionDef)) and
             node.name in {'DesktopFrame','DesktopSearchPanel','format_topic_results'}]
    queued = []
    chooser = Mock()
    chooser.ShowModal.return_value = 1
    chooser.GetSelection.return_value = 0
    wx = SimpleNamespace(CallAfter=queued.append,YES=1,ID_OK=1,YES_NO=2,NO_DEFAULT=4,ICON_QUESTION=8,
                         SingleChoiceDialog=Mock(return_value=chooser),
                         MessageBox=Mock(return_value=0),TheClipboard=Mock(),TextDataObject=lambda value:value)
    env = dict(wx=wx,WorkspaceFrame=BaseFrame,PagedSearchPanel=object,Hit=Hit,
               __name__='carr_library.ui._handler_probe',__package__='carr_library.ui',
               format_citation=format_citation,Path=Path,LibraryApp=Mock(),
               contextmanager=contextmanager,TopicTree=TopicTree,open_context=open_context,
               ScopeServices=ScopeServices,scope_labels=scope_labels,active_collections=active_collections,
               text_dialog=Mock(return_value='Reviewer'),show_text=Mock(),show_page_context=Mock(),metadata_dialog=Mock())
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),env)
    return env,queued


def frame(classes):
    env,queued = classes
    value = object.__new__(env['DesktopFrame'])
    value.working = value.busy = value._closing = False
    value.cancel_event = threading.Event()
    value.cancel_button,value.choice,value.book = Mock(),Mock(),Mock()
    value.menu_bar = Mock()
    value.menu_bar.GetMenuCount.return_value = 3
    value.report = Mock()
    value.executor = Mock()
    return value


def close_event(can_veto):
    event = SimpleNamespace(veto=False,skipped=False)
    event.CanVeto = lambda: can_veto
    event.GetVeto = lambda: event.veto
    event.Veto = lambda: setattr(event,'veto',True)
    event.Skip = lambda: setattr(event,'skipped',True)
    return event


def panel(classes):
    env,_ = classes
    value = object.__new__(env['DesktopSearchPanel'])
    value.frame = SimpleNamespace(working=False,report=Mock(),run_job=Mock(),library=Mock(),cancel_event=threading.Event())
    value.bound_library = value.frame.library
    value.model = SearchModel()
    value.model.set_workspace('a')
    value.bound_workspace,value.bound_topic = 'a',None
    hit = Hit('a','hit','revision','Title','Author','first','Segment 1','Exact','Citation')
    value.model.replace('a',(hit,))
    value.results = {'hit':hit}
    value._context_epoch = 1
    value.query,value.retrieval,value.result_mode,value.collection = Mock(),Mock(),Mock(),Mock()
    value.query.GetValue.return_value = 'Exact'
    value.retrieval.GetSelection.return_value = 0
    value.result_mode.GetSelection.return_value = 0
    value.collection.GetSelection.return_value = 0
    value.collection_ids = [None]
    value.filters = {}
    value.grid,value.details,value.status = Mock(),Mock(),Mock()
    value._load_context,value._refresh_details = Mock(),Mock()
    value.verified_copy_service = Mock(return_value=hit.quote)
    value.selected = lambda: hit
    return value


def test_combined_menus_keep_import_and_academic_actions_without_shortcut_collision(classes, monkeypatch):
    env, _ = classes
    value = frame(classes)
    value.host, value.layout, value.Bind, value.SetMenuBar = Mock(), Mock(), Mock(), Mock()
    menus = {}

    class Menu:
        def __init__(self):
            self.items = []

        def Append(self, identity, title):
            item = SimpleNamespace(title=title)
            self.items.append(item)
            return item

    env['wx'].Menu = Menu
    env['wx'].MenuBar = lambda: SimpleNamespace(Append=lambda menu, label: menus.update({label: menu}))
    env['wx'].Button = Mock(return_value=Mock())
    env['wx'].ID_ANY, env['wx'].ALL = -1, 1
    env['wx'].EVT_MENU, env['wx'].EVT_BUTTON = 'menu', 'button'
    installers = {name: Mock() for name in (
        'install_research_actions', 'install_source_review_actions', 'install_academic_actions',
        'install_reading_desk_actions', 'install_ebook_actions', 'install_catalog_insights_actions')}
    env.update(installers)
    ebook = Mock()
    monkeypatch.setitem(sys.modules, 'carr_library.ui.ebook_reader',
                        SimpleNamespace(install_ebook_reader=ebook))

    value._menus()

    labels = [item.title for menu in menus.values() for item in menu.items]
    shortcuts = [label.split('\t', 1)[1].casefold() for label in labels if '\t' in label]
    assert len(shortcuts) == len(set(shortcuts))
    assert 'Academic library...\tCtrl+Shift+B' in labels
    assert 'Import Center: files, folders and queues' in labels
    for handler in (value.import_center, value.open_academic_library, value.export_topic):
        assert any(call.args[1] == handler for call in value.Bind.call_args_list)
    for installer in (*installers.values(), ebook):
        installer.assert_called_once_with(value)
    value.help(None)
    guide = env['show_text'].call_args.args[2]
    assert 'Alt+A for Academic' in guide and 'Alt+B for Bibliography' in guide
    assert 'Library, Import Center' in guide
    assert 'Ctrl+Shift+B opens Import Center' not in guide


def test_repeated_enter_does_not_clear_an_active_search(classes):
    p = panel(classes)
    p.frame.working = True
    before = p.model.hits
    p.on_search(None)
    assert p.model.hits == before
    assert tuple(p.results) == ('hit',)
    assert p._context_epoch == 1
    p.grid.refresh.assert_not_called()
    p.frame.run_job.assert_not_called()


def test_async_result_cannot_repopulate_changed_scope(classes):
    p = panel(classes)
    p.on_search(None)
    done = p.frame.run_job.call_args.args[2]
    p.model.set_workspace('b')
    p._context_epoch += 1
    done([SimpleNamespace(workspace='a',ref='stale')])
    assert p.model.hits == ()
    assert p.results == {}


def test_refresh_collections_clears_revoked_results_and_keeps_explicit_scope(classes):
    p = panel(classes)
    p.collection_ids = [None,'gone']
    p.collection.GetSelection.return_value = 1
    p.frame.library.collections.return_value = [('new','Current collection')]
    p.refresh_collections()
    assert p.collection_ids == [None,'new']
    p.collection.SetSelection.assert_called_once_with(0)
    assert p.model.hits == ()
    assert p.results == {}
    assert 'no longer available' in p.status.ChangeValue.call_args.args[0]


def test_job_disables_mutable_controls_and_restores_them_on_completion(classes):
    f = frame(classes)
    future = Future()
    f.executor.submit.return_value = future
    done = Mock()
    f.run_job('Search',lambda: (),done)
    assert f.working and f.busy
    f.book.Disable.assert_called_once()
    f.choice.Disable.assert_called_once()
    future.set_result(('result',))
    assert len(classes[1]) == 1
    classes[1].pop()()
    assert not f.working and not f.busy
    f.book.Enable.assert_called_once()
    f.choice.Enable.assert_called_once()
    done.assert_called_once_with(('result',))


def test_failed_submit_does_not_leave_application_disabled(classes):
    f = frame(classes)
    f.executor.submit.side_effect = RuntimeError('unavailable')
    f.run_job('Search',lambda: ())
    assert not f.working and not f.busy
    f.choice.Enable.assert_called_once()
    assert 'could not start' in f.report.call_args.args[0]


def test_running_job_vetoes_normal_close(classes):
    f = frame(classes)
    f.working = True
    event = close_event(True)
    f.on_close(event)
    assert event.veto and not event.skipped
    assert not f._closing
    f.executor.shutdown.assert_not_called()


@pytest.mark.parametrize('already_queued',[False,True])
def test_forced_close_blocks_late_ui_callbacks(classes,already_queued):
    f = frame(classes)
    future = Future()
    f.executor.submit.return_value = future
    done = Mock()
    f.run_job('Import',lambda: (),done)
    if already_queued:
        future.set_result('finished')
    f.on_close(close_event(False))
    assert f._closing and f.cancel_event.is_set()
    f.report.reset_mock()
    if not already_queued:
        future.set_result('finished')
    for callback in classes[1]:
        callback()
    done.assert_not_called()
    f.report.assert_not_called()


def test_saved_query_results_use_human_readable_source_and_notes(classes):
    anchor = make_anchor(Identity('a','ed','a'*64,'r'),Locator('logical_segment',1),
                         'Exact original text',0,5,Metadata('Author','Title'),text_verified=True)
    row = SavedEvidence('topic','item',anchor,'My distinct commentary')
    output = classes[0]['format_topic_results']((TopicHit(row,'note'),))
    assert 'Match in research note' in output
    assert 'Exact quotation:\n\nExact' in output
    assert 'Research note:\n\nMy distinct commentary' in output
    assert 'Author, Title' in output
    assert 'Identity(' not in output and 'Anchor(' not in output and 'raw_sha256' not in output


def test_pdf_review_delegates_to_the_source_review_controller(classes):
    p = panel(classes)
    p.frame.source_review_actions = SimpleNamespace(review=Mock())
    event = object()
    p.review_selected(event)
    p.frame.source_review_actions.review.assert_called_once_with(event)
    p.frame.library.preview.assert_not_called()
    p.frame.library.review_pdf_page.assert_not_called()
    p.frame.run_job.assert_not_called()


def test_unverified_preview_keeps_the_bound_topic_authorization(classes):
    p = panel(classes)
    p.bound_topic = 'research'
    p.results['hit'] = SimpleNamespace(verified=False)
    p.on_more(None)
    p.frame.library.preview.assert_not_called()
    _,operation,done = p.frame.run_job.call_args.args
    done(operation())
    p.frame.library.preview.assert_called_once_with('a','hit','revision',authorized_topic='research')


def test_save_quotation_keeps_the_bound_topic_authorization(classes):
    p = panel(classes)
    p.bound_topic = 'research'
    p.frame.active = ('a','research')
    p.frame.evidence = None
    p.frame.library.topics.return_value = (SimpleNamespace(id='research',name='Research'),)
    p.save_selected(None)
    p.frame.library.save_hit.assert_not_called()
    p.frame.run_job.call_args.args[1]()
    p.frame.library.save_hit.assert_called_once_with('a','research','hit','revision',
        authorized_topic='research',cancelled=p.frame.cancel_event.is_set)


@pytest.mark.parametrize('generated_name_collision', [False, True])
def test_save_quotation_distinguishes_same_name_topics_without_retargeting(classes, generated_name_collision):
    p = panel(classes)
    p.frame.active = ('a', None)
    p.frame.evidence = None
    topics = [SimpleNamespace(id='first', name='History'), SimpleNamespace(id='second', name='History')]
    if generated_name_collision:
        topics.append(SimpleNamespace(id='third', name='History (library a; topic first)'))
    p.frame.library.topics.return_value = tuple(topics)
    chooser = classes[0]['wx'].SingleChoiceDialog
    chooser.return_value.GetSelection.return_value = 1

    p.save_selected(None)

    labels = chooser.call_args.args[3]
    assert len(labels) == len(set(labels)) == len(topics)
    p.frame.run_job.call_args.args[1]()
    p.frame.library.save_hit.assert_called_once_with('a', 'second', 'hit', 'revision',
        authorized_topic=None, cancelled=p.frame.cancel_event.is_set)
    chooser.return_value.Destroy.assert_called_once()


def test_save_quotation_cancel_does_not_save_or_start_job(classes):
    p = panel(classes)
    p.frame.active = ('a', None)
    p.frame.library.topics.return_value = (SimpleNamespace(id='first', name='History'),)
    chooser = classes[0]['wx'].SingleChoiceDialog
    chooser.return_value.ShowModal.return_value = 0

    p.save_selected(None)

    p.frame.run_job.assert_not_called()
    p.frame.library.save_hit.assert_not_called()
    chooser.return_value.Destroy.assert_called_once()


@pytest.mark.parametrize('selection', [-1, 2])
def test_save_quotation_rejects_missing_or_invalid_topic_choice(classes, selection):
    p = panel(classes)
    p.frame.active = ('a', None)
    p.frame.library.topics.return_value = (SimpleNamespace(id='first', name='History'),)
    chooser = classes[0]['wx'].SingleChoiceDialog
    chooser.return_value.GetSelection.return_value = selection

    p.save_selected(None)

    p.frame.run_job.assert_not_called()
    p.frame.library.save_hit.assert_not_called()
    assert 'Quotation not saved' in p.status.ChangeValue.call_args.args[0]
    p.status.SetFocus.assert_called_once()
    chooser.return_value.Destroy.assert_called_once()


def test_verified_context_loads_in_job_before_opening_page_dialog(classes):
    p = panel(classes)
    p.results['hit'] = SimpleNamespace(verified=True)
    p.on_more(None)
    p._load_context.assert_not_called()
    classes[0]['show_page_context'].assert_not_called()
    _,load,display = p.frame.run_job.call_args.args
    context = load()
    p._load_context.assert_called_once_with('a','hit','revision')
    classes[0]['show_page_context'].assert_not_called()
    display(context)
    call = classes[0]['show_page_context'].call_args
    assert call.args[1] == ('a','hit','revision')
    assert call.kwargs['initial_context'] is context
    assert call.kwargs['run_job'] == p.frame.run_job


@pytest.mark.parametrize('changed',['scope','selection','epoch'])
def test_late_verified_copy_cannot_write_clipboard_after_selection_changes(classes,changed):
    p = panel(classes)
    p.copy('quote')
    p.verified_copy_service.assert_not_called()
    clipboard = classes[0]['wx'].TheClipboard
    clipboard.Open.assert_not_called()
    _,verify,publish = p.frame.run_job.call_args.args
    value = verify()
    if changed == 'scope':
        p.model.set_workspace('b')
    elif changed == 'epoch':
        p._context_epoch += 1
    else:
        p.selected = lambda: None
    publish(value)
    clipboard.Open.assert_not_called()


def test_copy_clipboard_work_happens_only_in_ui_completion(classes):
    p = panel(classes)
    p.copy('quote')
    _,verify,publish = p.frame.run_job.call_args.args
    value = verify()
    clipboard = classes[0]['wx'].TheClipboard
    clipboard.Open.assert_not_called()
    publish(value)
    clipboard.SetData.assert_called_once_with('Exact')
    clipboard.Close.assert_called_once()


def test_cancelled_copy_does_not_publish_verified_text(classes):
    p = panel(classes)
    p.copy('quote')
    _,verify,_ = p.frame.run_job.call_args.args
    def verify_and_cancel(*args):
        p.frame.cancel_event.set()
        return 'Exact'
    p.verified_copy_service.side_effect = verify_and_cancel
    with pytest.raises(TimeoutError,match='Nothing copied'):
        verify()
    classes[0]['wx'].TheClipboard.Open.assert_not_called()


def test_copy_cancelled_after_verification_keeps_clipboard_unchanged(classes):
    p = panel(classes)
    p.copy('quote')
    _,verify,publish = p.frame.run_job.call_args.args
    value = verify()
    p.frame.cancel_event.set()
    publish(value)
    classes[0]['wx'].TheClipboard.Open.assert_not_called()
    assert 'Nothing copied' in p.status.ChangeValue.call_args.args[0]


@pytest.mark.parametrize('choice,languages',[(0,'eng'),(1,'ukr'),(2,'slk'),(3,'eng+ukr+slk')])
def test_scanned_pdf_import_reuses_canonical_recognition_config(classes,monkeypatch,choice,languages):
    from carr_library.extract.page_ocr import Config
    from carr_library.ui import ocr_options
    f = frame(classes)
    f.library = Mock()
    f._workspace = lambda:'a'
    f.run_job = Mock()
    wx = classes[0]['wx']
    monkeypatch.setitem(sys.modules,'wx',wx)
    chooser = Mock(wraps=ocr_options.choose_ocr_config)
    monkeypatch.setattr(ocr_options,'choose_ocr_config',chooser)
    wx.FD_OPEN,wx.FD_FILE_MUST_EXIST,wx.FD_MULTIPLE = 1,2,4
    file_dialog = Mock()
    file_dialog.ShowModal.return_value = wx.ID_OK
    file_dialog.GetPaths.return_value = ['/sources/Scanned.PDF','/sources/notes.txt']
    wx.FileDialog = Mock(return_value=file_dialog)
    wx.SingleChoiceDialog.return_value.GetSelection.return_value = choice
    f.import_books(None)
    chooser.assert_called_once_with(f,['/sources/Scanned.PDF','/sources/notes.txt'])
    f.library.import_files.assert_not_called()
    _,import_files,complete = f.run_job.call_args.args
    import_files()
    call = f.library.import_files.call_args
    assert call.args == ('a',['/sources/Scanned.PDF','/sources/notes.txt'])
    config = call.kwargs['config']
    assert config == Config(languages=languages)
    complete(({'file':'Scanned.PDF','state':'quarantined','message':'Page 2 needs independent review'},))
    assert 'Page 2 needs independent review' in f.report.call_args.args[0]
    assert 'Review pending imported page' in f.report.call_args.args[0]


def test_cancelled_recognition_choice_imports_nothing(classes,monkeypatch):
    f = frame(classes)
    f.library = Mock()
    f._workspace = lambda:'a'
    f.run_job = Mock()
    wx = classes[0]['wx']
    monkeypatch.setitem(sys.modules,'wx',wx)
    wx.FD_OPEN,wx.FD_FILE_MUST_EXIST,wx.FD_MULTIPLE = 1,2,4
    dialog = Mock()
    dialog.ShowModal.return_value = wx.ID_OK
    dialog.GetPaths.return_value = ['book.pdf']
    wx.FileDialog = Mock(return_value=dialog)
    wx.SingleChoiceDialog.return_value.ShowModal.return_value = 0
    f.import_books(None)
    f.run_job.assert_not_called()
    f.library.import_files.assert_not_called()
    f.report.assert_called_once_with('Import cancelled.')


def test_text_and_docx_import_reuse_helper_config_without_language_dialog(classes,monkeypatch):
    from carr_library.extract.page_ocr import Config
    from carr_library.ui import ocr_options
    f = frame(classes)
    f.library = Mock()
    f._workspace = lambda:'a'
    f.run_job = Mock()
    wx = classes[0]['wx']
    wx.FD_OPEN,wx.FD_FILE_MUST_EXIST,wx.FD_MULTIPLE = 1,2,4
    dialog = Mock()
    dialog.ShowModal.return_value = wx.ID_OK
    dialog.GetPaths.return_value = ['notes.txt','chapter.docx']
    wx.FileDialog = Mock(return_value=dialog)
    chooser = Mock(wraps=ocr_options.choose_ocr_config)
    monkeypatch.setattr(ocr_options,'choose_ocr_config',chooser)
    f.import_books(None)
    chooser.assert_called_once_with(f,['notes.txt','chapter.docx'])
    wx.SingleChoiceDialog.assert_not_called()
    f.run_job.call_args.args[1]()
    assert f.library.import_files.call_args.kwargs['config'] == Config()


def test_recognition_configuration_error_prevents_import_and_is_readable(classes,monkeypatch):
    from carr_library.ui import ocr_options
    f = frame(classes)
    f.library = Mock()
    f._workspace = lambda:'a'
    f.run_job = Mock()
    wx = classes[0]['wx']
    wx.FD_OPEN,wx.FD_FILE_MUST_EXIST,wx.FD_MULTIPLE = 1,2,4
    dialog = Mock()
    dialog.ShowModal.return_value = wx.ID_OK
    dialog.GetPaths.return_value = ['scan.pdf']
    wx.FileDialog = Mock(return_value=dialog)
    monkeypatch.setattr(ocr_options,'choose_ocr_config',Mock(side_effect=ValueError('Choose a recognition language.')))
    f.import_books(None)
    f.run_job.assert_not_called()
    f.library.import_files.assert_not_called()
    f.report.assert_called_once_with('Import not started: Choose a recognition language.')


def pending_import_frame(classes):
    f = frame(classes)
    f._workspace = Mock(return_value='a')
    f.library = Mock()
    f.library.import_requests.return_value = (
        {'request_id':'finished','file':'Already imported.pdf','state':'complete'},
        {'request_id':'request','file':'Original.pdf','state':'quarantined'},
    )
    page = {'physical':2,'raw_text':'Full original page\nincluding its last line.',
            'checksum':'a'*64}
    f.library.pending_review.return_value = page
    f.run_job = Mock()
    return f,page


@pytest.mark.parametrize('confirmed,reviewer',[(False,'Person'),(True,None),(True,'Person')])
def test_pending_import_review_requires_explicit_approval_of_displayed_page(classes,confirmed,reviewer):
    f,page = pending_import_frame(classes)
    env,_ = classes
    env['wx'].MessageBox.return_value = int(confirmed)
    env['text_dialog'].return_value = reviewer
    f.review_pending_import(None)
    _,prepare,display = f.run_job.call_args.args
    f.library.pending_review.assert_not_called()
    display(prepare())
    f.library.pending_review.assert_called_once_with('a','request')
    shown = env['show_text'].call_args.args[2]
    assert 'Original.pdf' in shown and 'Physical PDF page 2' in shown
    assert page['raw_text'] in shown
    f.library.confirm_import_review.assert_not_called()
    if confirmed and reviewer is not None:
        assert f.run_job.call_count == 2
        _,approve,complete = f.run_job.call_args.args
        f.library.resume_import.return_value = {'state':'quarantined'}
        complete(approve())
        f.library.confirm_import_review.assert_called_once_with('a','request','Person',
            confirmed=True,expected_checksum=page['checksum'])
        assert f.library.resume_import.call_args.args == ('a','request')
        assert 'Another page needs independent review' in f.report.call_args.args[0]
    else:
        assert f.run_job.call_count == 1
        f.library.resume_import.assert_not_called()


def test_changed_pending_page_cannot_resume_after_failed_review(classes):
    f,page = pending_import_frame(classes)
    classes[0]['wx'].MessageBox.return_value = 1
    f.review_pending_import(None)
    _,prepare,display = f.run_job.call_args.args
    display(prepare())
    _,approve,_ = f.run_job.call_args.args
    f.library.confirm_import_review.side_effect = ValueError('Displayed page changed')
    with pytest.raises(ValueError,match='Displayed page changed'):
        approve()
    assert f.library.confirm_import_review.call_args.kwargs['expected_checksum'] == page['checksum']
    f.library.resume_import.assert_not_called()


def test_no_pending_review_starts_no_approval(classes):
    f,_ = pending_import_frame(classes)
    f.library.pending_review.return_value = None
    f.review_pending_import(None)
    _,prepare,display = f.run_job.call_args.args
    display(prepare())
    assert f.run_job.call_count == 1
    classes[0]['show_text'].assert_not_called()
    f.library.confirm_import_review.assert_not_called()
    assert 'no page awaiting review' in f.report.call_args.args[0]


def test_failed_open_retains_current_library_and_draft(classes,tmp_path):
    f = frame(classes)
    old = SimpleNamespace(semantic_ranker=None)
    f.library = old
    f._confirm = Mock(return_value=True)
    f._drop = Mock()
    f.remember_data_dir = Mock()
    (tmp_path / 'library.sqlite').write_bytes(b'fixture')
    classes[0]['LibraryApp'].side_effect = ValueError('corrupt library')
    with pytest.raises(ValueError,match='corrupt'):
        f._open_data_root(tmp_path)
    assert f.library is old
    f._drop.assert_not_called()
    f.remember_data_dir.assert_not_called()


def test_folder_switch_remembers_only_valid_opened_library(classes,tmp_path):
    f = frame(classes)
    f.library = SimpleNamespace(semantic_ranker=None)
    f._confirm = Mock(return_value=True)
    f._drop,f.refresh_scopes = Mock(),Mock()
    f.scopes = []
    f.remember_data_dir = Mock()
    (tmp_path / 'library.sqlite').write_bytes(b'fixture')
    candidate = SimpleNamespace(root=tmp_path,list_scopes=Mock(return_value=()))
    classes[0]['LibraryApp'].return_value = candidate
    assert f._open_data_root(tmp_path)
    assert f.library is candidate
    f.remember_data_dir.assert_called_once_with(tmp_path)
    f._drop.assert_called_once()


def test_bound_services_recheck_topic_in_the_source_transaction(classes,tmp_path):
    app = LibraryApp(tmp_path / 'library')
    a = app.create_workspace('A')
    b = app.create_workspace('B')
    topic = app.create_topic(a,'Research')
    app.add_text(a,'Exact source','Exact source text',authors=('Author',))
    hit, = app.search(a,'Exact')
    f = frame(classes)
    f.library = app
    services = f._bind(a,topic)
    assert services.verified_copy(a,hit.ref,hit.revision,'quote') == 'Exact'
    assert services.load_context(a,hit.ref,hit.revision).pages[0].quote == 'Exact'
    with pytest.raises(ValueError,match='Library changed'):
        services.verified_copy(b,hit.ref,hit.revision,'quote')
    with app.topic_transaction(a) as tree:
        tree.archive(topic)
    with pytest.raises(ValueError,match='archived'):
        services.load_context(a,hit.ref,hit.revision)
    with pytest.raises(ValueError,match='archived'):
        services.verified_copy(a,hit.ref,hit.revision,'quote')
    for transaction in (services.catalog_tx,services.topic_tx,services.evidence_tx):
        with pytest.raises(ValueError,match='archived'):
            with transaction():
                pass


def test_import_uses_the_chosen_recognition_configuration(classes,monkeypatch):
    config = object()
    choose = Mock(return_value=config)
    monkeypatch.setitem(sys.modules,'carr_library.ui.ocr_options',SimpleNamespace(choose_ocr_config=choose))
    f = frame(classes)
    f.library = Mock()
    f._workspace = lambda:'a'
    f.run_job = Mock()
    wx = classes[0]['wx']
    wx.FD_OPEN,wx.FD_FILE_MUST_EXIST,wx.FD_MULTIPLE = 1,2,4
    file_dialog = Mock()
    file_dialog.ShowModal.return_value = wx.ID_OK
    file_dialog.GetPaths.return_value = ['/sources/Scanned.PDF','/sources/notes.txt']
    wx.FileDialog = Mock(return_value=file_dialog)
    f.import_books(None)
    choose.assert_called_once_with(f,['/sources/Scanned.PDF','/sources/notes.txt'])
    file_dialog.Destroy.assert_called_once()
    f.library.import_files.assert_not_called()
    _,import_files,complete = f.run_job.call_args.args
    import_files()
    call = f.library.import_files.call_args
    assert call.args == ('a',['/sources/Scanned.PDF','/sources/notes.txt'])
    assert call.kwargs['config'] is config
    assert call.kwargs['cancelled'] == f.cancel_event.is_set
    complete(({'file':'Scanned.PDF','state':'quarantined','message':'Page 2 needs independent review'},))
    assert 'Page 2 needs independent review' in f.report.call_args.args[0]
    assert 'Review pending imported page' in f.report.call_args.args[0]


def test_cancelled_canonical_config_starts_no_import(classes,monkeypatch):
    choose = Mock(return_value=None)
    monkeypatch.setitem(sys.modules,'carr_library.ui.ocr_options',SimpleNamespace(choose_ocr_config=choose))
    f = frame(classes)
    f.library = Mock()
    f._workspace = lambda:'a'
    f.run_job = Mock()
    wx = classes[0]['wx']
    wx.FD_OPEN,wx.FD_FILE_MUST_EXIST,wx.FD_MULTIPLE = 1,2,4
    dialog = Mock()
    dialog.ShowModal.return_value = wx.ID_OK
    dialog.GetPaths.return_value = ['book.pdf']
    wx.FileDialog = Mock(return_value=dialog)
    f.import_books(None)
    choose.assert_called_once_with(f,['book.pdf'])
    f.run_job.assert_not_called()
    f.library.import_files.assert_not_called()
    f.report.assert_called_once_with('Import cancelled.')


def test_save_duplicate_topic_labels_route_by_original_id(classes):
    p = panel(classes)
    p.frame.active = ('a', None)
    p.frame.library.topics.return_value = (
        SimpleNamespace(id='one', name='Історія'),
        SimpleNamespace(id='two', name='Історія'),
        SimpleNamespace(id='three', name='Історія (library a; topic one)'),
    )
    chooser = classes[0]['wx'].SingleChoiceDialog
    chooser.return_value.GetSelection.return_value = 1
    p.save_selected(None)
    labels = chooser.call_args.args[3]
    assert len(labels) == len(set(labels)) == 3
    assert 'topic two' in labels[1]
    chooser.return_value.Destroy.assert_called_once()
    p.frame.run_job.call_args.args[1]()
    p.frame.library.save_hit.assert_called_once_with('a','two','hit','revision',
        authorized_topic=None,cancelled=p.frame.cancel_event.is_set)


@pytest.mark.parametrize('modal,index', [(0, 0), (1, -1), (1, 2)])
def test_save_topic_dialog_cancel_or_invalid_selection_never_saves(classes, modal, index):
    p = panel(classes)
    p.frame.active = ('a', None)
    p.frame.library.topics.return_value = (SimpleNamespace(id='one', name='Výskum'),)
    chooser = classes[0]['wx'].SingleChoiceDialog.return_value
    chooser.ShowModal.return_value = modal
    chooser.GetSelection.return_value = index
    p.save_selected(None)
    chooser.Destroy.assert_called_once()
    p.frame.run_job.assert_not_called()
    p.frame.library.save_hit.assert_not_called()

@pytest.mark.parametrize('names', [
    ('Café', 'Cafe\u0301'),
    ('Історія', 'Історія '),
    ('Research  methods', 'Research methods'),
])
@pytest.mark.parametrize('index', [0, 1])
def test_save_presentation_equivalent_topic_labels(classes, names, index):
    import unicodedata
    p = panel(classes)
    p.frame.active = ('a', None)
    p.frame.library.topics.return_value = tuple(
        SimpleNamespace(id=f'topic-{i}', name=name)
        for i, name in enumerate(names))
    chooser = classes[0]['wx'].SingleChoiceDialog
    chooser.return_value.GetSelection.return_value = index
    p.save_selected(None)
    labels = chooser.call_args.args[3]
    assert len({' '.join(unicodedata.normalize('NFC', x).split())
                for x in labels}) == 2
    p.frame.run_job.call_args.args[1]()
    p.frame.library.save_hit.assert_called_once_with(
        'a', f'topic-{index}', 'hit', 'revision',
        authorized_topic=None, cancelled=p.frame.cancel_event.is_set)
    chooser.return_value.Destroy.assert_called_once()
