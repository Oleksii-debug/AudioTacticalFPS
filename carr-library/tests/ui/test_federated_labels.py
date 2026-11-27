"""Source-distinct spoken choices using the actual controller and local library.

Native widget construction uses controlled wx objects; this is not NVDA or
physical-keyboard acceptance. Source search, citation copying and saved evidence
run against the real SQLite-backed application.
"""
from threading import Event
from types import SimpleNamespace as N
from unittest.mock import Mock
import sys
import unicodedata

import pytest

from carr_library.application import LibraryApp
from carr_library.ui import federated_search as module
from carr_library.ui.federated_search import NativeSearchCenter, SearchCenter, SearchOptions


NAMES = (
    ('Café', 'Cafe\u0301'),
    ('Історія', 'Історія '),
    ('Research  methods', 'Research methods'),
    ('Same', 'Same', 'Same [0]'),
    ('Same', 'Same', 'Same (library w; topic 0)'),
    ('Same', 'Same'),
    ('Unique', 'Distinct'),
)


def spoken_key(label):
    return ' '.join(unicodedata.normalize('NFC', label).split())


def assert_distinct(rows, labels):
    assert len(rows) == len(labels) == len(set(map(spoken_key, labels)))
    assert all(label.startswith(row.name) for row, label in zip(rows, labels))


@pytest.mark.parametrize('names', NAMES)
@pytest.mark.parametrize('reverse', [False, True])
def test_topic_choices_keep_identity_order_and_distinct_spoken_names(names, reverse):
    rows = [N(id=str(i), name=name, parent=None) for i, name in enumerate(names)]
    if reverse:
        rows.reverse()
    labels = module._topic_labels(rows, 'w')
    assert_distinct(rows, labels)


def test_topic_paths_preserved_and_cycle_rejected():
    rows = [N(id='root', name='Parent', parent=None),
            N(id='child', name='Child', parent='root')]
    assert module._topic_labels(rows, 'w') == ('Parent', 'Parent / Child')
    rows[0].parent = 'child'
    with pytest.raises(ValueError, match='hierarchy'):
        module._topic_labels(rows, 'w')


class Control(Mock):
    def __init__(self, *args, **kwargs):
        choices = kwargs.pop('choices', ())
        super().__init__()
        self.choices = tuple(choices)
        self.checked = set()
        self.GetSelection.return_value = -1
        self.GetValue.return_value = ''
        self.SetSelection.side_effect = lambda index: setattr(self.GetSelection, 'return_value', index)
        self.Check.side_effect = self.check
        self.IsChecked.side_effect = lambda index: index in self.checked

    def _get_child_mock(self, **kwargs):
        return Mock(**kwargs)

    def check(self, index, value=True):
        if value:
            self.checked.add(index)
        else:
            self.checked.discard(index)


def native_dialog(monkeypatch, rows, active):
    flags = ('VERTICAL HORIZONTAL EXPAND ALL ALIGN_CENTER_VERTICAL LEFT RIGHT '
             'ALIGN_RIGHT TE_PROCESS_ENTER TE_READONLY TE_MULTILINE LB_SINGLE').split()
    events = ('EVT_BUTTON EVT_TEXT_ENTER EVT_LISTBOX EVT_LISTBOX_DCLICK EVT_CHECKLISTBOX '
              'EVT_TEXT EVT_CHOICE EVT_CLOSE EVT_CHAR_HOOK EVT_WINDOW_DESTROY').split()
    wx = N(**{name: 0 for name in flags + events},
           **{name: Control for name in ('Dialog', 'BoxSizer', 'StaticText', 'FlexGridSizer',
                                        'CheckListBox', 'TextCtrl', 'Choice', 'Button', 'ListBox')})
    monkeypatch.setitem(sys.modules, 'wx', wx)
    controller = N(selection_changed=Mock(), search_page=Mock(), next_cursor=None)
    monkeypatch.setattr(module, 'SearchCenter', lambda frame, view: controller)
    view = NativeSearchCenter(N(active=(active, None)), rows)
    return view, controller


@pytest.mark.parametrize('names', NAMES)
@pytest.mark.parametrize('reverse', [False, True])
def test_native_checklist_distinct_labels_preserve_checked_library_ids(monkeypatch, names, reverse):
    rows = [N(id=str(i), name=name) for i, name in enumerate(names)]
    if reverse:
        rows.reverse()
    view, controller = native_dialog(monkeypatch, rows, rows[-1].id)
    assert_distinct(rows, view.libraries_control.choices)
    assert view.libraries_control.checked == {len(rows) - 1}
    view.next_button.Enable.assert_called_with(False)
    view.libraries_control.Check(0)
    view.query.GetValue.return_value = 'railway'
    view._search()
    controller.search_page.assert_called_once()
    options, = controller.search_page.call_args.args
    assert options.workspaces == tuple(row.id for i, row in enumerate(rows)
                                       if i in {0, len(rows) - 1})
    assert options.query == 'railway'


def controller_for(app, workspace, *, topic=None):
    frame = N(library=app, active=(workspace, topic), working=False, busy=False,
              _closing=False, cancel_event=Event(), draft='Retain this unsaved note.')

    def run_job(title, operation, done, failed):
        frame.working = True
        try:
            value = operation()
        except Exception as exc:
            frame.working = False
            failed(exc)
        else:
            frame.working = False
            done(value)

    frame.run_job = run_job
    view = Mock()
    view.selection.return_value = 0
    view.select.side_effect = lambda index: setattr(view.selection, 'return_value', index)
    dialogs = N(choose=Mock(return_value=1), text=Mock(return_value='A source note'), show=Mock())
    return SearchCenter(frame, view, dialogs=dialogs), view, frame, dialogs


@pytest.mark.parametrize('names', NAMES[:3] + (('Same', 'Same'),))
@pytest.mark.parametrize('reverse', [False, True])
def test_real_search_labels_counts_and_citations_identify_source_with_empty_group(tmp_path, names, reverse):
    app = LibraryApp(tmp_path / 'library')
    first = app.create_workspace(names[0])
    empty = app.create_workspace(names[0])
    second = app.create_workspace(names[1])
    for workspace, edition in ((first, 'First edition'), (second, 'Second edition')):
        app.add_text(workspace, 'Same title', 'Railway history.',
                     authors=('Same author',), edition=edition)
    ordered = (first, empty, second)
    if reverse:
        ordered = tuple(reversed(ordered))
    controller, view, frame, _ = controller_for(app, first)
    controller.search(SearchOptions(ordered, 'Railway'))
    assert len(controller.rows) == 2, view.report.call_args
    labels, = view.show_results.call_args.args
    assert len(set(map(spoken_key, labels))) == 2
    report = view.report.call_args.args[0]
    assert f'library {empty}; library only): 0' in report
    for index, (hit, label) in enumerate(zip(controller.rows, labels)):
        assert f'library {hit.workspace};' in label
        assert hit.result.edition in label
        assert label.startswith(f'{index + 1}. ')
        view.selection.return_value = index
        controller.copy('citation')
        view.copy_text.assert_called_with(hit.result.citation)
    assert frame.active == (first, None) and frame.draft == 'Retain this unsaved note.'
    assert tuple(hit.workspace for hit in controller.rows) == tuple(w for w in ordered if w != empty)


def test_repeated_page_results_and_missing_edition_have_distinct_rows(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    app.add_text(workspace, 'Title', 'Railway archives. Railway museums.', authors=('Author',))
    controller, view, _, _ = controller_for(app, workspace)
    controller.search(SearchOptions((workspace,), 'Railway'))
    assert len(controller.rows) == 2, view.report.call_args
    labels, = view.show_results.call_args.args
    assert len(set(labels)) == 2
    assert all('Unknown edition' in label for label in labels)
    assert labels[0].startswith('1. ') and labels[1].startswith('2. ')
    assert controller.rows[0].result.page == controller.rows[1].result.page


def test_real_save_uses_original_topic_id_after_label_disambiguation(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    current = app.create_workspace('Research')
    source = app.create_workspace('Research')
    app.create_topic(source, 'Café')
    app.create_topic(source, 'Cafe\u0301')
    app.add_text(source, 'Title', 'Railway history.', edition='First')
    controller, view, frame, dialogs = controller_for(app, current)
    controller.search(SearchOptions((current, source), 'Railway'))
    hit, = controller.rows
    original = app.topics(source)
    controller.save()
    labels = dialogs.choose.call_args.args[-1]
    assert len(set(map(spoken_key, labels))) == 2
    assert all(f'topic {topic.id}' in label for topic, label in zip(original, labels))
    assert app.evidence(source, original[0].id) == ()
    saved, = app.evidence(source, original[1].id)
    assert saved.anchor.identity.workspace == source
    assert saved.anchor.quote == hit.result.quote
    assert saved.note == 'A source note'
    assert frame.active == (current, None) and frame.draft == 'Retain this unsaved note.'


def test_unverified_result_never_gets_verified_label():
    hit = N(library_name='Research', result=N(title='Title', author='Author',
                page='p. 5', edition=None, verified=False))
    label = module.result_label(hit)
    assert 'Unknown edition' in label and 'Unverified extraction' in label
