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
        self.navigation = (False, False)

    def set_navigation(self, previous, next_page):
        self.navigation = previous, next_page

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
    assert view.navigation == (False, False)
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
    session = SimpleNamespace(workspaces=('w',), selected_workspaces=('w',), hits=hits,
        groups=(SimpleNamespace(workspace='w', library_name='Library', hits=hits),),
        has_more=False, next_cursor=None, library_start=1, library_end=1,
        source_ranges=(('w', 1, 1, 0),))
    controller = SimpleNamespace(view=View(), epoch=0, _ready=Mock(), _clear_rows=Mock(),
        selection_changed=Mock(), _refresh_navigation=Mock(), _report_error=Mock(),
        _cancelled=lambda: False,
        service=SimpleNamespace(search_page=Mock(return_value=session)))
    controller._job = lambda title, operation, done, **kwargs: done(operation())
    SearchCenter.search(controller, SearchOptions(('w',), 'test'))
    controller._report_error.assert_not_called()
    assert controller.rows is hits
    assert len(set(map(label_key, controller.view.labels))) == 4
    assert 'Unknown edition' in controller.view.labels[2]
    assert 'Unverified extraction' in controller.view.labels[2]
    assert 'Unverified extraction' in controller.view.messages[-1]
