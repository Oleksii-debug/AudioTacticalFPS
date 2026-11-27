"""Headless dialog workflows with real local search, source and evidence services.

Fake controls expose outcomes; actual operations execute on a worker thread.
These checks do not claim native Windows or NVDA acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import unicodedata

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.federated_search import (
    NativeSearchCenter, SearchCenter, SearchOptions, default_libraries,
    install_federated_search_actions, _topic_labels,
)


class View:
    parent = object()

    def __init__(self):
        self.index = -1
        self.labels, self.details, self.messages = (), '', []
        self.verified = self.selected = self.busy = False
        self.copied, self.focus = [], None
        self.busy_history = []

    def clear_results(self):
        self.labels, self.details, self.index = (), '', -1
        self.verified = self.selected = False

    def selection(self):
        return self.index

    def select(self, index):
        self.index = index

    def show_results(self, labels):
        self.labels = labels

    def show_details(self, text, *, verified, selected=True):
        self.details, self.verified, self.selected = text, verified, selected

    def set_busy(self, busy):
        self.busy = busy
        self.busy_history.append(busy)

    def report(self, text, *, focus=False):
        self.messages.append(text)
        if focus:
            self.focus = 'status'

    def focus_results(self):
        self.focus = 'results'

    def copy_text(self, text):
        self.copied.append(text)
        return True


class Frame:
    def __init__(self, library, workspace, topic):
        self.library, self.active = library, (workspace, topic)
        self.working = self.busy = self._closing = False
        self.cancel_event = Event()
        self.jobs, self.pending, self.worker_threads = [], [], []
        self.deferred = False
        self.unsaved_note = 'An unsaved notebook draft — retain exactly.'
        self.refresh_scopes = Mock(side_effect=AssertionError('Do not replace the active notebook'))
        self._open_workspace = Mock(side_effect=AssertionError('Do not switch the active notebook'))

    def run_job(self, title, operation, done=None, failed=None):
        assert not self.working
        self.working = self.busy = True
        self.cancel_event.clear()
        self.jobs.append(title)
        self.pending.append((operation, done, failed))
        if not self.deferred:
            self.finish()

    def finish(self, *, before_callback=None):
        operation, done, failed = self.pending.pop(0)
        def worker():
            self.worker_threads.append(get_ident())
            return operation()
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                value = executor.submit(worker).result()
        except Exception as exc:
            self.working = self.busy = False
            failed(exc)
        else:
            self.working = self.busy = False
            if before_callback is not None:
                before_callback()
            done(value)


class Dialogs:
    def __init__(self, *, choice=0, note='A note for this quotation', during_choose=None,
                 during_note=None):
        self.choice, self.note = choice, note
        self.during_choose, self.during_note = during_choose, during_note
        self.choices, self.shown = [], []

    def choose(self, parent, title, label, choices):
        self.choices.append((title, label, choices))
        if self.during_choose is not None:
            self.during_choose()
        return self.choice

    def text(self, *args, **kwargs):
        if self.during_note is not None:
            self.during_note()
        return self.note

    def show(self, parent, title, value):
        self.shown.append((title, value))


@pytest.fixture
def center(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    one, two, hidden = (app.create_workspace(name) for name in ('Current Library', 'Source Library', 'Unselected Library'))
    local_topic = app.create_topic(one, 'Current notebook')
    root = app.create_topic(two, 'Academic research')
    destination = app.create_topic(two, 'Source notes', parent=root)
    first = app.add_text(one, 'Current title', 'Railway history in the current library.',
                        authors=('Author One',), language='uk', edition='First')
    second = app.add_text(two, 'Source title', 'Railway history from another library.\nOriginal context.',
                         authors=('Author Two',), language='en', edition='Second')
    app.add_text(hidden, 'Unselected source', 'Railway history hidden from this search.')
    frame, view, dialogs = Frame(app, one, local_topic), View(), Dialogs(choice=1)
    controller = SearchCenter(frame, view, dialogs=dialogs)
    return SimpleNamespace(app=app, one=one, two=two, hidden=hidden, local_topic=local_topic,
        destination=destination, root=root, first=first, second=second, frame=frame,
        view=view, dialogs=dialogs, controller=controller)


def search(state, **options):
    state.controller.search(SearchOptions((state.one, state.two), 'Railway history', **options))


def source_hit(state):
    index = next(index for index, hit in enumerate(state.controller.rows) if hit.workspace == state.two)
    state.view.select(index)
    state.controller.selection_changed()
    return state.controller.rows[index]


def test_only_explicit_libraries_default_current_and_no_implicit_all(center):
    rows = center.app.workspaces()
    assert default_libraries(rows, center.frame.active) == (center.one,)
    assert default_libraries(rows, None) == ()
    assert default_libraries(rows, ('missing', None)) == ()
    center.controller.search(SearchOptions((), 'Railway history'))
    assert center.frame.jobs == [] and center.controller.rows == ()
    assert 'check' in center.view.messages[-1]
    center.controller.search(SearchOptions((center.one,), 'Railway history', limit_per_library=1))
    assert len(center.controller.rows) == 1
    assert center.controller.rows[0].workspace == center.one
    assert 'Current Library' in center.view.labels[0]
    assert all(thread != get_ident() for thread in center.frame.worker_threads)


def test_filters_group_labels_limit_and_source_details(center):
    search(center, title='Source title', author='Author Two', language='en', edition='Second',
           mode='word', result_mode='thematic', limit_per_library=1)
    # Whole-word queries reject whitespace; the invalid attempt exposes no rows.
    assert center.controller.rows == ()
    center.controller.search(SearchOptions((center.one, center.two), 'history',
        title='Source title', author='Author Two', language='en', edition='Second',
        mode='word', result_mode='thematic', limit_per_library=1))
    hit, = center.controller.rows
    assert hit.workspace == center.two
    assert center.view.verified is True
    assert 'Source Library' in center.view.labels[0]
    assert center.two in center.view.details and 'Original context.' in center.view.details
    assert 'Current Library: 0' in center.view.messages[-1]
    assert 'Up to 1 results per library' in center.view.messages[-1]


def test_copy_context_and_save_route_to_result_library_without_touching_notebook(center):
    local = center.app.search(center.one, 'current')[0]
    selections = dict(center.app._selections)
    active, draft = center.frame.active, center.frame.unsaved_note
    search(center)
    hit = source_hit(center)
    center.controller.context()
    title, text = center.dialogs.shown[-1]
    assert title == 'Verified original source context'
    assert 'Library: Source Library' in text and 'Original context.' in text
    center.controller.copy('quote')
    center.controller.copy('citation')
    assert center.view.copied == [hit.result.quote, hit.result.citation]
    center.controller.save()
    saved, = center.app.evidence(center.two, center.destination)
    assert saved.anchor.identity.workspace == center.two
    assert saved.anchor.quote == hit.result.quote
    assert saved.note == center.dialogs.note
    assert center.app.evidence(center.one, center.local_topic) == ()
    assert center.dialogs.choices[0][2] == ('Academic research', 'Academic research / Source notes')
    assert center.frame.active == active and center.frame.unsaved_note == draft
    assert center.app._selections == selections
    assert center.app.copy(center.one, local.ref, local.revision, 'quote') == 'current'
    center.frame.refresh_scopes.assert_not_called()
    center.frame._open_workspace.assert_not_called()
    assert center.view.focus == 'results' and not center.controller.running


def test_pdf_preview_has_no_verified_actions(center, tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    pdf = tmp_path / 'unreviewed.pdf'
    canvas = Canvas(str(pdf), invariant=1, pageCompression=0)
    canvas.drawString(40, 700, 'Unreviewed railway source text.')
    canvas.save()
    center.app.import_file(center.two, pdf, title='Unreviewed PDF')
    center.controller.search(SearchOptions((center.two,), 'Unreviewed'))
    hit, = center.controller.rows
    assert hit.result.verified is False
    assert center.view.verified is False and 'Unverified extraction' in center.view.labels[0]
    assert 'UNVERIFIED EXTRACTION' in center.view.details
    jobs = len(center.frame.jobs)
    center.controller.copy('quote')
    center.controller.copy('citation')
    center.controller.save()
    assert center.view.copied == [] and center.dialogs.choices == []
    assert len(center.frame.jobs) == jobs
    center.controller.context()
    title, text = center.dialogs.shown[-1]
    assert title == 'Unverified extracted source text'
    assert 'independent review required' in text and 'Unreviewed railway source text.' in text
    assert center.app.evidence(center.two, center.destination) == ()


@pytest.mark.parametrize('action', ['copy', 'context', 'save'])
def test_actions_reverify_original_bytes_and_clear_failed_results(center, action):
    search(center)
    source_hit(center)
    path = center.app.assets.resolve(center.second['asset'])
    data = path.read_bytes()
    path.write_bytes(data.replace(b'Railway', b'Highway', 1))
    getattr(center.controller, action)(*(['quote'] if action == 'copy' else []))
    assert center.view.copied == [] and center.dialogs.shown == []
    assert center.controller.rows == () and center.view.labels == ()
    assert center.app.evidence(center.two, center.destination) == ()
    assert 'not completed' in center.view.messages[-1]


@pytest.mark.parametrize('during', ['choose', 'note'])
@pytest.mark.parametrize('change', ['selection', 'query', 'topic', 'scope'])
def test_save_dialog_rechecks_result_options_topic_and_active_scope(center, during, change):
    search(center)
    source_hit(center)
    def mutate():
        if change == 'selection':
            center.view.select(0)
        elif change == 'query':
            center.controller.options_changed()
        elif change == 'topic':
            with center.app.topic_transaction(center.two) as tree:
                tree.archive(center.destination)
        else:
            center.frame.active = (center.two, center.destination)
    setattr(center.dialogs, 'during_' + during, mutate)
    center.controller.save()
    assert center.view.copied == []
    with center.app.read(center.two) as db:
        assert db.execute('SELECT COUNT(*) FROM research_evidence').fetchone()[0] == 0
    assert 'not completed' in center.view.messages[-1]


def test_failed_composite_hides_old_results_and_does_not_publish_partial(center):
    search(center)
    previous = center.controller.service.current
    center.controller.search(SearchOptions((center.one, 'missing-last-library'), 'Railway history'))
    assert center.controller.rows == () and center.view.labels == () and center.view.details == ''
    assert center.controller.service.current is previous
    assert not center.view.selected


def test_cancel_between_libraries_never_shows_first_library_rows(center, monkeypatch):
    original = LibraryApp.search
    def cancel_after_first(app, workspace, *args, **kwargs):
        result = original(app, workspace, *args, **kwargs)
        center.frame.cancel_event.set()
        return result
    monkeypatch.setattr(LibraryApp, 'search', cancel_after_first)
    search(center)
    assert center.controller.rows == () and center.view.labels == ()
    assert 'cancelled' in center.view.messages[-1]
    assert not center.controller.running and center.view.busy_history == [True, False]


@pytest.mark.parametrize('change', ['options', 'active', 'folder', 'cancel', 'dispose'])
def test_queued_worker_rejects_changed_scope_and_late_callbacks(center, tmp_path, change):
    center.frame.deferred = True
    search(center)
    assert center.controller.running and center.view.busy
    if change == 'options':
        center.controller.options_changed()
    elif change == 'active':
        center.frame.active = (center.two, center.destination)
    elif change == 'folder':
        center.frame.library = LibraryApp(tmp_path / 'other-folder')
    elif change == 'dispose':
        center.controller.dispose()
    else:
        center.controller.cancel()
    before = center.view.messages[:]
    center.frame.finish()
    assert center.controller.rows == () and center.view.labels == ()
    assert center.controller.service.current is None
    assert not center.controller.running
    if change == 'dispose':
        assert center.view.messages == before


def test_cancellation_after_worker_finished_still_hides_search_results(center):
    center.frame.deferred = True
    search(center)
    center.frame.finish(before_callback=center.frame.cancel_event.set)
    assert center.controller.rows == () and center.view.labels == ()
    assert 'cancelled' in center.view.messages[-1]


def test_late_copy_completion_cannot_copy_new_selection(center):
    search(center)
    source_hit(center)
    center.frame.deferred = True
    center.controller.copy('quote')
    center.frame.finish(before_callback=lambda: center.view.select(0))
    assert center.view.copied == [] and center.controller.rows == ()


def test_queued_save_rechecks_scope_before_any_write(center):
    search(center)
    source_hit(center)
    center.frame.deferred = True
    center.controller.save()
    center.frame.finish()  # Fresh topic list, then the two native choices.
    assert center.frame.pending and center.controller.running
    center.frame.active = (center.two, center.destination)
    center.frame.finish()
    assert center.app.evidence(center.two, center.destination) == ()


def test_data_folder_change_withdraws_idle_results_before_copy(center, tmp_path):
    search(center)
    source_hit(center)
    center.frame.library = LibraryApp(tmp_path / 'changed-library')
    center.controller.copy('quote')
    assert center.view.copied == [] and center.controller.rows == ()
    assert center.controller.service.current is None
    assert 'Reopen Research Search Center' in center.view.messages[-1]


def test_close_requests_cancel_and_keeps_modal_alive_until_worker_finishes(center):
    center.frame.deferred = True
    search(center)
    assert center.controller.request_close() is False
    assert center.controller.alive and center.frame.cancel_event.is_set()
    center.frame.finish()
    assert center.controller.request_close() is True
    assert not center.controller.alive and center.controller.service.current is None


def test_native_close_veto_and_deferred_destruction(center):
    native = NativeSearchCenter.__new__(NativeSearchCenter)
    native.wx = SimpleNamespace(ID_CANCEL=9)
    native.window = Mock()
    native.controller = center.controller
    native._window_alive = True
    native._deferred_destroy = False
    center.frame.deferred = True
    search(center)
    event = Mock()
    event.CanVeto.return_value = True
    native._close(event)
    event.Veto.assert_called_once()
    native.window.EndModal.assert_not_called()
    native.window.Destroy.assert_not_called()
    # An unexpected modal-loop return must delay destruction as well.
    center.view.finish_disposal = native.finish_disposal
    native.show()
    native.window.Hide.assert_called_once()
    native.window.Destroy.assert_not_called()
    center.frame.finish()
    native.window.Destroy.assert_called_once()


def test_forced_window_destruction_never_hides_or_destroys_deleted_controls(center):
    native = NativeSearchCenter.__new__(NativeSearchCenter)
    native.window = Mock()
    native.controller = center.controller
    native._window_alive = True
    native._deferred_destroy = False
    center.view.finish_disposal = native.finish_disposal
    center.frame.deferred = True
    search(center)
    event = Mock()
    event.GetEventObject.return_value = native.window
    native.window.ShowModal.side_effect = lambda: native._destroyed(event)
    native.window.Hide.side_effect = AssertionError('The native window was already deleted')
    native.window.Destroy.side_effect = AssertionError('The native window was already deleted')
    native.show()
    assert not native._window_alive and not center.controller.alive
    assert center.frame.cancel_event.is_set() and center.controller.running
    before = center.view.messages[:]
    center.frame.finish()
    assert center.view.messages == before
    assert center.controller.rows == () and not center.controller.running
    native._close()
    native._search()
    native.window.Hide.assert_not_called()
    native.window.Destroy.assert_not_called()
    native.window.EndModal.assert_not_called()


def test_menu_installer_keeps_notebook_shortcut_and_dispatches_independent_search(monkeypatch):
    wx = SimpleNamespace(ID_ANY=-1, EVT_MENU=object())
    monkeypatch.setitem(sys.modules, 'wx', wx)
    notebook_called, center_called = Mock(), Mock()
    monkeypatch.setattr('carr_library.ui.federated_search.FederatedSearchActions.open',
                        lambda self, event: center_called(event))

    class Menu:
        def __init__(self):
            self.items = []
            self.separators = 0

        def Append(self, identity, label):
            item = SimpleNamespace(id=identity if identity >= 0 else 100 + len(self.items), label=label)
            self.items.append(item)
            return item

        def AppendSeparator(self):
            self.separators += 1

    menu = Menu()
    notebook = menu.Append(10, 'Search saved research\tCtrl+Shift+F')
    bindings = {notebook.id: notebook_called}
    frame = SimpleNamespace(
        menu_bar=SimpleNamespace(FindMenu=lambda title: 1 if title == 'Research' else -1,
                                 GetMenu=lambda index: menu),
        Bind=lambda event, handler, item: bindings.__setitem__(item.id, handler))
    installed = install_federated_search_actions(frame)
    assert install_federated_search_actions(frame) is installed
    assert len(menu.items) == 2 and menu.separators == 1
    assert menu.items[0] is notebook and bindings[notebook.id] is notebook_called

    def press(shortcut, event):
        matches = [item for item in menu.items if item.label.split('\t', 1)[-1] == shortcut]
        assert len(matches) == 1, 'Each search shortcut must identify exactly one command.'
        bindings[matches[0].id](event)

    notebook_event, center_event = object(), object()
    press('Ctrl+Shift+F', notebook_event)
    notebook_called.assert_called_once_with(notebook_event)
    center_called.assert_not_called()
    press('Ctrl+Alt+F', center_event)
    center_called.assert_called_once_with(center_event)
    notebook_called.assert_called_once()


COLLIDING_NAMES = [('Café', 'Cafe\u0301'), ('Історія', 'Історія '),
    ('Research  methods', 'Research methods'), ('Same', 'Same', 'Same [0]'),
    ('Same', 'Same', 'Same (library w; topic 0)'), ('Same', 'Same'), ('Unique', 'Distinct')]


def label_key(label):
    return ' '.join(unicodedata.normalize('NFC', label).split())


@pytest.mark.parametrize('names', COLLIDING_NAMES)
@pytest.mark.parametrize('reverse', [False, True])
def test_federated_topic_choices_are_distinct_and_retain_identity_order(names, reverse):
    rows = [SimpleNamespace(id=str(index), name=name, parent=None)
            for index, name in enumerate(names)]
    if reverse:
        rows.reverse()
    labels = _topic_labels(rows, 'w')
    assert len(labels) == len(rows) == len(set(map(label_key, labels)))
    assert all(label.startswith(row.name) for row, label in zip(rows, labels))


def test_federated_topic_labels_preserve_paths_and_reject_cycles():
    root = SimpleNamespace(id='root', name='Research', parent=None)
    child = SimpleNamespace(id='child', name='Methods', parent='root')
    assert _topic_labels((root, child), 'w') == ('Research', 'Research / Methods')
    root.parent = 'child'
    with pytest.raises(ValueError, match='hierarchy changed'):
        _topic_labels((root, child), 'w')


@pytest.mark.parametrize('names', COLLIDING_NAMES)
@pytest.mark.parametrize('reverse', [False, True])
def test_native_library_checklist_uses_distinct_choices_without_reordering(monkeypatch, names, reverse):
    import carr_library.ui.federated_search as module
    rows = [SimpleNamespace(id=str(index), name=name) for index, name in enumerate(names)]
    if reverse:
        rows.reverse()
    wx = SimpleNamespace(**{name: 1 << index for index, name in enumerate((
        'VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL', 'ALIGN_CENTER_VERTICAL',
        'LEFT', 'RIGHT', 'TE_PROCESS_ENTER', 'LB_SINGLE', 'TE_READONLY',
        'TE_MULTILINE', 'ALIGN_RIGHT'))})
    for name in ('Dialog', 'BoxSizer', 'StaticText', 'FlexGridSizer', 'CheckListBox',
                 'TextCtrl', 'Choice', 'Button', 'ListBox'):
        setattr(wx, name, Mock(side_effect=lambda *args, **kwargs: Mock()))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(module, 'SearchCenter', Mock())
    for method in ('_bind', 'clear_results', 'set_busy', 'report'):
        monkeypatch.setattr(NativeSearchCenter, method, Mock())
    active = rows[-1].id
    native = NativeSearchCenter(SimpleNamespace(active=(active, None)), rows)
    choices = wx.CheckListBox.call_args.kwargs['choices']
    assert len(choices) == len(rows) == len(set(map(label_key, choices)))
    assert all(label.startswith(row.name) for row, label in zip(rows, choices))
    assert native.libraries == tuple(rows)
    assert [call.args for call in native.libraries_control.Check.call_args_list] == [
        (index, row.id == active) for index, row in enumerate(rows)]


@pytest.mark.parametrize('names', [('Same', 'Same'), ('Café', 'Cafe\u0301'), ('A  B', 'A B')])
@pytest.mark.parametrize('reverse', [False, True])
def test_same_title_and_library_results_identify_editions_and_save_exact_source(tmp_path, names, reverse):
    app = LibraryApp(tmp_path / 'library')
    first, empty, second = (app.create_workspace(name) for name in (names[0], names[0], names[1]))
    topics = {workspace: app.create_topic(workspace, 'Notes') for workspace in (first, second)}
    for workspace, edition in ((first, 'First'), (second, 'Second')):
        app.add_text(workspace, 'Same title', 'Railway history.', authors=('Same Author',), edition=edition)
    frame, view, dialogs = Frame(app, first, topics[first]), View(), Dialogs(choice=0)
    controller = SearchCenter(frame, view, dialogs=dialogs)
    workspaces = (first, empty, second)
    if reverse:
        workspaces = tuple(reversed(workspaces))
    controller.search(SearchOptions(workspaces, 'Railway history'))
    assert len(controller.rows) == len(view.labels) == 2
    assert len(set(map(label_key, view.labels))) == 2
    assert f'library {empty}; library only): 0' in view.messages[-1]
    for index, (hit, label) in enumerate(zip(controller.rows, view.labels)):
        assert label.startswith(f'{index + 1}. ')
        assert f'library {hit.workspace};' in label
        assert hit.result.edition in label
        view.select(index)
        controller.selection_changed()
        controller.copy('citation')
        assert view.copied[-1] == hit.result.citation
        controller.save()
        saved, = app.evidence(hit.workspace, topics[hit.workspace])
        assert saved.anchor.identity.workspace == hit.workspace
        assert saved.anchor.quote == hit.result.quote
    assert frame.active == (first, topics[first])
    assert frame.unsaved_note == 'An unsaved notebook draft — retain exactly.'


def test_result_ordinals_distinguish_repeated_page_and_unknown_edition():
    def hit(edition, verified):
        return SimpleNamespace(workspace='w', library_name='Library', result=SimpleNamespace(
            title='Same title', author='Same author', page='p. 5', edition=edition, verified=verified))
    hits = (hit('First', True), hit('Second', True), hit(None, False), hit('First', True))
    session = SimpleNamespace(workspaces=('w',), hits=hits,
                              groups=(SimpleNamespace(workspace='w', library_name='Library', hits=hits),))
    controller = SimpleNamespace(view=View(), epoch=0, _ready=Mock(), _clear_rows=Mock(),
        selection_changed=Mock(), _report_error=Mock(), _cancelled=lambda: False,
        service=SimpleNamespace(search=Mock(return_value=session)))
    controller._job = lambda title, operation, done, **kwargs: done(operation())
    SearchCenter.search(controller, SearchOptions(('w',), 'test'))
    controller._report_error.assert_not_called()
    assert controller.rows is hits
    assert len(set(map(label_key, controller.view.labels))) == 4
    assert 'Unknown edition' in controller.view.labels[2]
    assert 'Unverified extraction' in controller.view.labels[2]
    assert 'Unverified extraction' in controller.view.messages[-1]
