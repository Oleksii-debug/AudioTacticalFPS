"""Real reader/model/handler contracts through text-only wx control doubles.

These tests do not claim native Windows or NVDA validation. The UTF-16/CRLF
double makes a common Windows caret-offset bug observable without a GUI.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.ui.ebook_model import EbookSession, NOTICE, native_position
from carr_library.ui.ebook_dialog import show_ebook
from carr_library.ui.ebook_actions import EbookActions, install_ebook_actions


def book_data():
    texts = ('First chapter.\n😀 Alpha and alpha.\nEnd.', 'Second chapter.\nNotes without printed pages.')
    chapters = [dict(ordinal=i, title=f'Title {i}', member=f'book/{i}.xhtml', linear=i == 1,
                     characters=len(text), warnings=['A table was flattened.'] if i == 2 else [],
                     image_descriptions=['An author-supplied description of a chart.'] if i == 2 else [])
                for i, text in enumerate(texts, 1)]
    return dict(title='Research EPUB', revision='revision-a', chapters=chapters,
                toc=[dict(title='Part one', ordinal=None, offset=None, depth=0, heading_only=True),
                     dict(title='An exact destination', ordinal=1, offset=texts[0].index('Alpha'), depth=1),
                     dict(title='Notes', ordinal=2, offset=0, depth=0)],
                warnings=['Images may contain text that was not extracted.'],
                metadata_hints={'creators': ['Package creator']}), texts


def callbacks(data=None, texts=None):
    if data is None:
        data, texts = book_data()
    loads = []

    def chapter(ordinal):
        loads.append(ordinal)
        return dict(data['chapters'][ordinal - 1], revision=data['revision'], raw_text=texts[ordinal - 1])

    return lambda: deepcopy(data), chapter, loads


class Control:
    def __init__(self, owner, *, value='', label='', choices=(), **kwargs):
        self.owner = owner
        self.value = value
        self.label = label
        self.name = ''
        self.items = list(choices)
        self.selection = -1
        self.enabled = True
        self.handlers = {}
        self.focused = False
        self.destroyed = False
        self.position = 0
        owner.controls.append(self)

    def SetName(self, name): self.name = name
    def SetMinSize(self, size): pass
    def Bind(self, event, callback, *args): self.handlers[event] = callback
    def ChangeValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetItems(self, items): self.items = list(items)
    def SetSelection(self, value, end=None): self.selection = value if end is None else (value, end)
    def GetSelection(self): return self.selection
    def Clear(self): self.items = []; self.selection = -1
    def Enable(self, value=True): self.enabled = value
    def SetFocus(self):
        for control in self.owner.controls:
            control.focused = False
        self.focused = True
    def SetInsertionPoint(self, value): self.position = value
    def SelectAll(self): self.selection = (0, len(self.value))
    def ShowPosition(self, value): self.position = value
    def SetSizer(self, sizer): pass
    def SetEscapeId(self, value): self.escape = value
    def ShowModal(self): self.owner.interaction(self.owner)
    def Destroy(self): self.destroyed = True

    def XYToPosition(self, column, row):
        # Logical lines; Windows native columns count UTF-16 code units.
        lines = self.value.split('\n')
        if not 0 <= row < len(lines):
            return -1
        count = lambda text: len(text.encode('utf-16-le')) // 2
        if not 0 <= column <= count(lines[row]):
            return -1
        return sum(count(line) + 2 for line in lines[:row]) + column

    def GetLastPosition(self):
        return len(self.value.replace('\n', '\r\n').encode('utf-16-le')) // 2

    def GetRange(self, start, end):
        native = self.value.replace('\n', '\r\n').encode('utf-16-le')
        return native[start * 2:end * 2].decode('utf-16-le').replace('\r\n', '\n')


class WxDouble(SimpleNamespace):
    def __init__(self, interaction):
        super().__init__()
        self.controls = []
        self.interaction = interaction
        for name, value in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                                     'TE_READONLY', 'TE_MULTILINE', 'TE_DONTWRAP', 'TE_PROCESS_ENTER',
                                     'LEFT', 'RIGHT', 'TOP', 'BOTTOM', 'ALL', 'EXPAND', 'ALIGN_RIGHT')):
            setattr(self, value, 1 << name)
        for name in ('EVT_CHOICE', 'EVT_BUTTON', 'EVT_TEXT_ENTER', 'EVT_CHAR_HOOK', 'EVT_MENU'):
            setattr(self, name, object())
        self.ID_CANCEL = 5101
        self.ID_ANY = -1
        self.NOT_FOUND = -1
        self.WXK_F3 = 342
        self.Dialog = lambda *args, **kwargs: Control(self, **kwargs)
        self.StaticText = lambda *args, **kwargs: Control(self, **kwargs)
        self.TextCtrl = lambda *args, **kwargs: Control(self, **kwargs)
        self.Choice = lambda *args, **kwargs: Control(self, **kwargs)
        self.Button = lambda *args, **kwargs: Control(self, **kwargs)
        self.BoxSizer = lambda *args, **kwargs: SimpleNamespace(Add=lambda *a, **kw: None)

    def named(self, name):
        return next(control for control in self.controls if control.name == name)

    def button(self, label):
        return next(control for control in self.controls if control.label == label and not control.name)

    def click(self, label):
        self.button(label).handlers[self.EVT_BUTTON](None)


def run_dialog(monkeypatch, interaction, *, current=lambda: True, initial_ordinal=1,
               custom_contents=None, custom_chapter=None):
    contents, chapter, loads = callbacks()
    wx = WxDouble(interaction)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    show_ebook(None, custom_contents or contents, custom_chapter or chapter,
               still_current=current, initial_ordinal=initial_ordinal)
    assert wx.controls[0].destroyed
    return loads, wx


def test_session_binds_chapter_descriptors_and_reports_unverified_metadata():
    contents, chapter, loads = callbacks()
    session = EbookSession(contents, chapter).open()
    session.visit(2)
    assert session.raw_text.startswith('Second chapter.')
    assert 'Chapter 2 (unpaginated)' in session.chapter_status()
    assert 'supplementary reading' in session.chapter_status()
    assert 'A table was flattened.' in session.chapter_status()
    assert 'author-supplied and unverified' in session.chapter_status()
    assert 'description of a chart' in session.chapter_status()
    assert 'description of a chart' not in session.raw_text
    assert '1 author-supplied image alternative descriptions' in session.report()
    assert NOTICE in session.report()
    assert 'not verified citation metadata' in session.report()
    assert 'Package creator' in session.report()
    assert session.entry(0).heading_only
    assert loads == [2]


def test_canonical_extraction_warnings_are_readable_and_keep_unknown_explanations():
    data, texts = book_data()
    data['warnings'] = ['embedded_contents_ambiguous_spine', 'source_quotations_unverified',
                        'An additional source-specific warning.']
    data['chapters'][0]['warnings'] = ['images_omitted_from_text', 'images_without_text_alternatives']
    contents, chapter, _ = callbacks(data, texts)
    session = EbookSession(contents, chapter).open()
    session.visit(1)
    report = session.report()
    information = session.chapter_status()
    assert 'resource repeated in the reading order' in report
    assert 'Use the chapter selector.' in report
    assert 'Quotations from this extraction have not been independently verified.' in report
    assert 'An additional source-specific warning.' in report
    assert 'Images were omitted from the text.' in information
    assert 'Some images have no text alternative' in information
    assert 'embedded_contents_ambiguous_spine' not in report
    assert 'images_omitted_from_text' not in information


@pytest.mark.parametrize('which,value', [('ordinal', True), ('characters', -1), ('linear', 1)])
def test_invalid_chapter_descriptors_are_rejected(which, value):
    data, texts = book_data()
    data['chapters'][0][which] = value
    contents, chapter, _ = callbacks(data, texts)
    with pytest.raises(ValueError, match='descriptor'):
        EbookSession(contents, chapter).open()


@pytest.mark.parametrize('index', [-1, 2, True])
def test_bad_contents_selection_cannot_wrap_to_last_entry(index):
    data, texts = book_data()
    data['toc'] = data['toc'][:2]
    contents, chapter, _ = callbacks(data, texts)
    session = EbookSession(contents, chapter).open()
    with pytest.raises(ValueError, match='contents list'):
        session.entry(index)


def test_guard_runs_before_and_after_loading_and_revision_mismatch_is_rejected():
    contents, chapter, loads = callbacks()
    state = {'current': True}
    session = EbookSession(contents, chapter, lambda: state['current']).open()
    state['current'] = False
    with pytest.raises(ValueError, match='changed'):
        session.visit(1)
    assert loads == []
    state['current'] = True

    def changed(ordinal):
        result = chapter(ordinal)
        state['current'] = False
        return result

    session.load_chapter = changed
    with pytest.raises(ValueError, match='changed'):
        session.visit(1)
    assert session.raw_text is None
    state['current'] = True
    session.load_chapter = lambda ordinal: dict(chapter(ordinal), revision='another-revision')
    with pytest.raises(ValueError, match='revision changed'):
        session.visit(1)


def test_search_is_literal_case_insensitive_and_uses_original_unicode_positions():
    data, texts = book_data()
    raw = '😀 Straße\nΑλφα Alpha alpha.*'
    data['chapters'][0]['characters'] = len(raw)
    contents, chapter, _ = callbacks(data, (raw, texts[1]))
    session = EbookSession(contents, chapter).open()
    session.visit(1)
    first = session.find('ALPHA')
    second = session.find('ALPHA', next_match=True)
    wrapped = session.find('ALPHA', next_match=True)
    assert raw[first.start:first.end] == 'Alpha'
    assert raw[second.start:second.end] == 'alpha'
    assert first.start == wrapped.start and wrapped.wrapped
    literal = session.find('.*')
    assert raw[literal.start:literal.end] == '.*'
    assert session.find('missing') is None
    with pytest.raises(ValueError, match='Enter text'):
        session.find('')


@pytest.mark.parametrize('raw', ['abc\ndef', '😀a\n😀target\n', 'a😀target'])
def test_native_positions_prove_exact_prefix_with_crlf_and_non_bmp(raw):
    wx = WxDouble(lambda wx: None)
    control = Control(wx, value=raw)
    for offset in range(len(raw) + 1):
        native = native_position(control, raw, offset)
        assert control.GetRange(0, native) == raw[:offset]


def test_native_positions_do_not_select_from_changed_text():
    wx = WxDouble(lambda wx: None)
    control = Control(wx, value='changed')
    with pytest.raises(ValueError, match='changed the extracted text'):
        native_position(control, 'original', 1)


def test_dialog_real_handlers_navigate_find_wrap_and_preserve_group_headings(monkeypatch):
    _, original = book_data()

    def interaction(wx):
        reader = wx.named(NOTICE)
        status = wx.named('Chapter and navigation status')
        contents = wx.named('Book contents (levels and chapter destinations)')
        assert reader.GetValue() == original[0]
        assert status.focused
        assert not wx.button('&Previous chapter').enabled
        assert wx.button('&Next chapter').enabled
        contents.SetSelection(0)
        contents.handlers[wx.EVT_CHOICE](None)
        assert reader.GetValue() == original[0]
        assert 'no destination' in status.GetValue()
        contents.SetSelection(1)
        contents.handlers[wx.EVT_CHOICE](None)
        expected = original[0].index('Alpha')
        assert reader.GetRange(0, reader.selection[0]) == original[0][:expected]
        query = wx.named('Find in this chapter (literal text, ignores case)')
        query.ChangeValue('alpha')
        wx.click('Find from &start')
        assert reader.focused
        assert reader.GetRange(*reader.selection) == 'Alpha'
        wx.click('Find ne&xt (F3)')
        assert reader.GetRange(*reader.selection) == 'alpha'
        wx.click('Find ne&xt (F3)')
        assert 'wrapped' in status.GetValue()
        wx.click('&Next chapter')
        assert reader.GetValue() == original[1]
        assert 'A table was flattened.' in status.GetValue()
        assert not wx.button('&Next chapter').enabled
        wx.click('&Previous chapter')
        assert reader.GetValue() == original[0]

    loads, _ = run_dialog(monkeypatch, interaction)
    assert loads == [1, 1, 2, 1]


def test_dialog_invalid_choice_clears_old_text_and_focuses_readable_error(monkeypatch):
    def interaction(wx):
        selector = wx.named('Chapter (unpaginated)')
        selector.SetSelection(-1)
        selector.handlers[wx.EVT_CHOICE](None)
        assert wx.named(NOTICE).GetValue() == ''
        assert wx.named('EPUB extraction report').GetValue() == ''
        assert not selector.enabled
        assert not wx.button('&Next chapter').enabled
        assert wx.named('Chapter and navigation status').focused
        assert 'Choose a chapter' in wx.named('Chapter and navigation status').GetValue()

    loads, _ = run_dialog(monkeypatch, interaction)
    assert loads == [1]


def test_unavailable_native_position_keeps_chapter_and_explains_fallback(monkeypatch):
    def interaction(wx):
        reader = wx.named(NOTICE)
        reader.XYToPosition = lambda column, row: -1
        reader.GetRange = lambda start, end: ''  # Neither mapping can prove a nonempty prefix.
        contents = wx.named('Book contents (levels and chapter destinations)')
        contents.SetSelection(1)
        contents.handlers[wx.EVT_CHOICE](None)
        message = wx.named('Chapter and navigation status').GetValue()
        assert 'positioning unavailable' in message
        assert 'complete chapter is available from its beginning' in message
        assert 'Tab to read at this destination' not in message
        assert reader.GetValue().startswith('First chapter.')

    run_dialog(monkeypatch, interaction)


def test_truncated_native_chapter_clears_text_and_cannot_claim_complete_fallback(monkeypatch):
    native_write = Control.ChangeValue

    def truncate_reader(control, value):
        native_write(control, value[:12] if control.name == NOTICE and value else value)

    monkeypatch.setattr(Control, 'ChangeValue', truncate_reader)

    def interaction(wx):
        assert wx.named(NOTICE).GetValue() == ''
        assert wx.named('EPUB extraction report').GetValue() == ''
        status = wx.named('Chapter and navigation status')
        assert status.focused
        assert 'could not display the complete extracted chapter' in status.GetValue()
        assert 'complete chapter is available from its beginning' not in status.GetValue()
        for label in ('Chapter (unpaginated)', 'Book contents (levels and chapter destinations)',
                      'Find in this chapter (literal text, ignores case)'):
            assert not wx.named(label).enabled
        assert not wx.button('&Previous chapter').enabled
        assert not wx.button('&Next chapter').enabled

    loads, _ = run_dialog(monkeypatch, interaction)
    assert loads == [1]


def test_dialog_changed_scope_on_find_clears_source_without_new_load(monkeypatch):
    state = {'current': True}

    def interaction(wx):
        state['current'] = False
        wx.named('Find in this chapter (literal text, ignores case)').ChangeValue('alpha')
        wx.click('Find ne&xt (F3)')
        assert wx.named(NOTICE).GetValue() == ''
        assert 'changed' in wx.named('Chapter and navigation status').GetValue()
        assert wx.named('Chapter and navigation status').focused

    loads, _ = run_dialog(monkeypatch, interaction, current=lambda: state['current'])
    assert loads == [1]


def test_dialog_keyboard_find_and_empty_query_keep_text_readable(monkeypatch):
    def interaction(wx):
        query = wx.named('Find in this chapter (literal text, ignores case)')
        key = SimpleNamespace(GetKeyCode=lambda: ord('F'), ControlDown=lambda: True, Skip=Mock())
        wx.controls[0].handlers[wx.EVT_CHAR_HOOK](key)
        assert query.focused
        query.handlers[wx.EVT_TEXT_ENTER](None)
        assert query.focused and 'Enter text' in wx.named('Chapter and navigation status').GetValue()
        query.ChangeValue('alpha')
        key.GetKeyCode = lambda: wx.WXK_F3
        wx.controls[0].handlers[wx.EVT_CHAR_HOOK](key)
        reader = wx.named(NOTICE)
        assert reader.GetRange(*reader.selection) == 'Alpha'
        assert reader.focused

    run_dialog(monkeypatch, interaction)


def test_dialog_initial_chapter_and_callback_failure(monkeypatch):
    def interaction(wx):
        assert wx.named(NOTICE).GetValue().startswith('Second chapter')
        assert wx.named('Chapter (unpaginated)').GetSelection() == 1

    loads, _ = run_dialog(monkeypatch, interaction, initial_ordinal=2)
    assert loads == [2]

    def failed(ordinal):
        raise InterruptedError('Reading was cancelled.')

    def unavailable(wx):
        assert not wx.named(NOTICE).GetValue()
        assert 'cancelled' in wx.named('Chapter and navigation status').GetValue()
        assert wx.named('Chapter and navigation status').focused

    run_dialog(monkeypatch, unavailable, custom_chapter=failed)


def frame_double():
    row = SimpleNamespace(edition_id='edition-a', title='Book')
    panel = SimpleNamespace(workspace='workspace-a', ready=True, rows={'edition-a': row}, books=Mock())
    panel._selection = Mock(return_value=(None, None, 'edition-a'))
    notebook = SimpleNamespace(GetPage=lambda index: panel, GetSelection=lambda: 1)
    frame = SimpleNamespace(library=object(), active=('workspace-a', 'topic-a'), book=notebook,
                            working=False, busy=False, _closing=False, report=Mock(), status=Mock(),
                            cancel_event=SimpleNamespace(is_set=lambda: False), run_job=Mock())
    return frame, panel


@pytest.fixture
def authorized_scope(monkeypatch):
    # Synthetic frame/service tests exercise selection binding. Separate tests
    # below use the real LibraryApp and canonical scope authorization helper.
    monkeypatch.setattr('carr_library.ui.ebook_actions._authorized_scope', lambda *_: True)


def test_action_captures_scope_for_async_service_and_restores_catalog_focus(monkeypatch, authorized_scope):
    frame, panel = frame_double()
    original_library = frame.library
    data, texts = book_data()
    opened, read = Mock(return_value=data), Mock(return_value=dict(data['chapters'][0], raw_text=texts[0]))
    monkeypatch.setattr('carr_library.ui.ebook_actions.open_ebook', opened)
    monkeypatch.setattr('carr_library.ui.ebook_actions.read_chapter', read)

    def reader(parent, load_contents, load_chapter, *, still_current):
        assert still_current()
        assert load_contents() is data
        load_chapter(1)

    actions = EbookActions(frame, reader=reader)
    actions.open()
    _, load, ready = frame.run_job.call_args.args
    ready(load())
    assert opened.call_args.args == (original_library, 'workspace-a', 'edition-a')
    assert opened.call_args.kwargs['authorized_topic'] == 'topic-a'
    assert read.call_args.args == (original_library, 'workspace-a', 'edition-a', 'revision-a', 1)
    assert read.call_args.kwargs['authorized_topic'] == 'topic-a'
    panel.books.SetFocus.assert_called_once()


@pytest.mark.parametrize('changed', ['topic', 'library', 'edition', 'panel'])
def test_action_does_not_open_delayed_result_after_selection_changed(monkeypatch, changed, authorized_scope):
    frame, panel = frame_double()
    data, _ = book_data()
    opened, reader = Mock(return_value=data), Mock()
    monkeypatch.setattr('carr_library.ui.ebook_actions.open_ebook', opened)
    actions = EbookActions(frame, reader=reader)
    original_library = frame.library
    actions.open()
    _, load, ready = frame.run_job.call_args.args
    if changed == 'topic':
        frame.active = ('workspace-a', 'other-topic')
    elif changed == 'library':
        frame.library = object()
    elif changed == 'edition':
        panel._selection.return_value = (None, None, 'other-edition')
    else:
        frame.book = SimpleNamespace(GetPage=lambda index: object())
    contents = load()
    assert opened.call_args.args[0] is original_library
    with pytest.raises(ValueError, match='changed'):
        ready(contents)
    reader.assert_not_called()
    panel.books.SetFocus.assert_not_called()


def test_action_does_not_read_new_scope_or_focus_old_controls_after_modal_switch(monkeypatch, authorized_scope):
    frame, panel = frame_double()
    data, _ = book_data()
    monkeypatch.setattr('carr_library.ui.ebook_actions.open_ebook', Mock(return_value=data))
    read = Mock()
    monkeypatch.setattr('carr_library.ui.ebook_actions.read_chapter', read)

    def reader(parent, load_contents, load_chapter, *, still_current):
        frame.active = ('workspace-b', None)
        assert not still_current()
        with pytest.raises(ValueError, match='changed'):
            load_chapter(1)

    EbookActions(frame, reader=reader).open()
    _, load, ready = frame.run_job.call_args.args
    ready(load())
    read.assert_not_called()
    panel.books.SetFocus.assert_not_called()


def test_action_no_selection_gives_actionable_error_without_background_job():
    frame, panel = frame_double()
    panel._selection.return_value = (None, None, None)
    EbookActions(frame, reader=Mock()).open()
    frame.run_job.assert_not_called()
    assert 'Select an imported EPUB' in frame.report.call_args.args[0]


@pytest.mark.parametrize('archived', ['workspace', 'topic'])
def test_action_reauthorizes_scope_after_worker_finished(monkeypatch, tmp_path, archived):
    from carr_library.application import LibraryApp

    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Scope authorization test')
    topic = library.create_topic(workspace, 'Topic')
    frame, panel = frame_double()
    frame.library = library
    frame.active = workspace, topic
    panel.workspace = workspace
    data, _ = book_data()
    monkeypatch.setattr('carr_library.ui.ebook_actions.open_ebook', Mock(return_value=data))
    reader = Mock()
    actions = EbookActions(frame, reader=reader)
    actions.open()
    _, load, ready = frame.run_job.call_args.args
    contents = load()
    if archived == 'workspace':
        library.archive_workspace(workspace)
    else:
        with library.topic_transaction(workspace) as tree:
            tree.archive(topic)
    # Frame IDs and the selected catalog row still match, but authorization no
    # longer does. A finished worker must not reopen the old scope.
    assert frame.active == (workspace, topic)
    with pytest.raises(ValueError, match='changed'):
        ready(contents)
    reader.assert_not_called()
    panel.books.SetFocus.assert_not_called()


def test_dialog_reauthorizes_scope_after_chapter_callback_before_render(monkeypatch, tmp_path):
    from carr_library.application import LibraryApp

    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Render authorization test')
    topic = library.create_topic(workspace, 'Topic')
    frame, panel = frame_double()
    frame.library = library
    frame.active = workspace, topic
    panel.workspace = workspace
    data, texts = book_data()
    monkeypatch.setattr('carr_library.ui.ebook_actions.open_ebook', Mock(return_value=data))

    def chapter(*args, **kwargs):
        result = dict(data['chapters'][0], revision=data['revision'], raw_text=texts[0])
        with library.topic_transaction(workspace) as tree:
            tree.archive(topic)
        return result

    read = Mock(side_effect=chapter)
    monkeypatch.setattr('carr_library.ui.ebook_actions.read_chapter', read)

    def interaction(wx):
        assert wx.named(NOTICE).GetValue() == ''
        assert wx.named('EPUB extraction report').GetValue() == ''
        assert not wx.named('Chapter (unpaginated)').enabled
        status = wx.named('Chapter and navigation status')
        assert 'changed' in status.GetValue()
        assert status.focused

    wx = WxDouble(interaction)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    EbookActions(frame, reader=show_ebook).open()
    _, load, ready = frame.run_job.call_args.args
    ready(load())
    assert wx.controls[0].destroyed
    read.assert_called_once()
    panel.books.SetFocus.assert_not_called()


def test_menu_installation_is_idempotent_and_binds_keyboard_command(monkeypatch):
    wx = WxDouble(lambda wx: None)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    menu = Mock()
    frame = SimpleNamespace(menu_bar=SimpleNamespace(FindMenu=lambda label: 0,
                                                     GetMenu=lambda index: menu), Bind=Mock())
    first = install_ebook_actions(frame)
    assert install_ebook_actions(frame) is first
    menu.Append.assert_called_once_with(wx.ID_ANY, 'Read electronic book contents\tCtrl+Shift+O')
    assert frame.Bind.call_args.args[1] == first.open



def _recorded_epub_native_probe_handlers():
    """Execute the real diagnostic closures without constructing native windows."""
    import ast
    from hashlib import sha256
    import json
    from pathlib import Path
    import traceback

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_epub_reading_smoke.py'
    module = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    main = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    names = {'remember_failure', 'abort', 'guarded', 'expire', 'trace_highlight'}
    functions = [node for node in main.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names
    events, pending = [], []

    class Dialog:
        def __init__(self, name):
            self.name, self.modal = name, True
        def IsModal(self):
            return self.modal
        def EndModal(self, result):
            assert result == 5101
            events.append(('end', self.name))
            self.modal = False

    class Owner:
        def __init__(self, name):
            self.name, self.closed, self.failure = name, False, None
            self.dialog = Dialog(name)
        def close(self):
            events.append(('close', self.name))
            if self.failure == 'throw':
                raise RuntimeError(self.name + ' cleanup raised')
            if self.failure == 'refuse':
                return
            self.closed = True
            if self.dialog.IsModal():
                self.dialog.EndModal(5101)

    overlay, reader = Owner('overlay'), Owner('reader')
    reader._audio_dialog = overlay
    env = {
        'errors': [], 'finished': False, 'stage': 1, 'key_pending': False,
        'shutdown': {'requested': False, 'reader_returned': False, 'attempted': set()},
        'pressed': set(), 'reader': reader, 'overlay': overlay,
        'wx': SimpleNamespace(ID_CANCEL=5101,
            CallAfter=lambda callback: pending.append((0, callback)),
            CallLater=lambda delay, callback: pending.append((delay, callback))),
        'gui': SimpleNamespace(ExitMainLoop=lambda: events.append(('exit',))),
        'simulator': SimpleNamespace(KeyUp=lambda key: events.append(('key-up', key)) or True),
        'traceback': traceback, 'json': json, 'sha256': sha256,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                 str(script), 'exec'), env)
    return SimpleNamespace(env=env, events=events, pending=pending, overlay=overlay, reader=reader)


def _recorded_epub_native_step(harness):
    assert harness.pending, 'The real modal-unwind closure stopped scheduling cleanup.'
    delay, callback = harness.pending.pop(0)
    assert delay in (0, 25)
    callback()


def test_recorded_epub_failure_is_printed_before_inside_out_modal_unwind(capsys):
    harness = _recorded_epub_native_probe_handlers()
    def fail():
        raise AssertionError('exact native highlight mismatch')
    harness.env['guarded'](fail)()
    assert 'exact native highlight mismatch' in capsys.readouterr().out
    assert len(harness.env['errors']) == 1 and harness.events == []
    harness.env['abort']()
    assert len(harness.pending) == 1, 'Repeated failure must not start another unwind.'
    _recorded_epub_native_step(harness)
    assert harness.events == [('close', 'overlay'), ('end', 'overlay')]
    # EndModal is not proof that the containing ShowModal/finally returned.
    _recorded_epub_native_step(harness)
    assert harness.events == [('close', 'overlay'), ('end', 'overlay')]
    harness.reader._audio_dialog = None
    _recorded_epub_native_step(harness)
    assert harness.events == [('close', 'overlay'), ('end', 'overlay'),
                              ('close', 'reader'), ('end', 'reader')]
    assert ('exit',) not in harness.events
    harness.env['shutdown']['reader_returned'] = True
    _recorded_epub_native_step(harness)
    assert harness.events[-1] == ('exit',)
    assert harness.overlay.closed and harness.reader.closed


@pytest.mark.parametrize('owner_name', ['overlay', 'reader'])
@pytest.mark.parametrize('failure', ['throw', 'refuse'])
def test_recorded_epub_abort_retains_unconfirmed_owner_and_original_failure(capsys, owner_name, failure):
    harness = _recorded_epub_native_probe_handlers()
    failed = getattr(harness, owner_name)
    failed.failure = failure
    def fail():
        raise AssertionError('original selection failure')
    harness.env['guarded'](fail)()
    _recorded_epub_native_step(harness)
    harness.reader._audio_dialog = None
    _recorded_epub_native_step(harness)
    harness.env['shutdown']['reader_returned'] = True
    _recorded_epub_native_step(harness)
    output = capsys.readouterr().out
    assert 'original selection failure' in output and 'Native EPUB owner cleanup failed.' in output
    assert 'original selection failure' in harness.env['errors'][0]
    assert len(harness.env['errors']) == 2
    assert not failed.closed, 'Failure-only modal unwind must not claim resource closure.'
    other = harness.reader if owner_name == 'overlay' else harness.overlay
    assert other.closed
    assert harness.events == [('close', 'overlay'), ('end', 'overlay'),
                              ('close', 'reader'), ('end', 'reader'), ('exit',)]


def test_recorded_epub_deadline_survives_error_suppressed_normal_callbacks(capsys):
    harness = _recorded_epub_native_probe_handlers()
    harness.env['errors'].append('original failure')
    callback = Mock()
    harness.env['guarded'](callback)()
    callback.assert_not_called()
    harness.env['expire']()
    assert harness.env['errors'][0] == 'original failure'
    assert len(harness.env['errors']) == 2
    assert 'deadline at stage 1' in capsys.readouterr().out
    _recorded_epub_native_step(harness)
    assert harness.events[:2] == [('close', 'overlay'), ('end', 'overlay')]


def test_recorded_epub_finished_probe_does_not_restart_deadline_cleanup():
    harness = _recorded_epub_native_probe_handlers()
    harness.env['finished'] = True
    harness.env['expire']()
    assert harness.env['errors'] == [] and harness.pending == [] and harness.events == []


def test_recorded_epub_failed_key_release_does_not_lose_modal_cleanup(capsys):
    harness = _recorded_epub_native_probe_handlers()
    harness.env['pressed'].add(32)
    harness.env['simulator'].KeyUp = lambda key: False
    harness.env['abort']()
    assert harness.env['pressed'] == set()
    assert 'could not release its native key' in capsys.readouterr().out
    _recorded_epub_native_step(harness)
    assert harness.events[:2] == [('close', 'overlay'), ('end', 'overlay')]
    assert len(harness.env['errors']) == 1


def test_recorded_epub_highlight_diagnostic_preserves_exact_bounded_observation(capsys):
    from hashlib import sha256
    import json
    harness = _recorded_epub_native_probe_handlers()
    raw = '😀 line\n' + 'x' * 5000
    selected = raw[:7] + 'wrong'
    control = raw.replace('\n', '\r\n')
    harness.env['expected_page'] = SimpleNamespace(raw_text=raw)
    harness.overlay.busy, harness.overlay.closed = False, False
    harness.overlay.session = SimpleNamespace(page=SimpleNamespace(raw_text=raw),
        chapter=SimpleNamespace(cues=(SimpleNamespace(start=0, end=7),)))
    harness.overlay.text = SimpleNamespace(GetSelection=lambda: (0, 9),
        GetStringSelection=lambda: selected, GetValue=lambda: control)
    harness.overlay.status = SimpleNamespace(GetValue=lambda: 'Mapping unavailable.')
    harness.env['trace_highlight']()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith('EPUB diagnostic: inspecting the unchanged native highlight')
    value = json.loads(lines[1])['epub_highlight_diagnostic']
    assert value['selected']['prefix'] == selected
    assert value['expected_fragment']['prefix'] == raw[:7]
    assert value['selected']['prefix'] != value['expected_fragment']['prefix']
    assert value['control']['sha256'] == sha256(control.encode('utf-8')).hexdigest()
    assert value['expected_source']['sha256'] == sha256(raw.encode('utf-8')).hexdigest()
    assert value['control']['characters'] == len(control) and len(value['control']['prefix']) == 2048
    assert value['native_selection'] == [0, 9] and value['session_source_matches']
    assert value['status']['prefix'] == 'Mapping unavailable.'
    assert harness.env['errors'] == [] and harness.events == []


class _NativeOffsetControl(Control):
    """Native character units and range clamping, without a line-coordinate oracle."""

    def __init__(self, owner, *, value, utf16=True, newline='\r\n'):
        super().__init__(owner, value=value)
        self.utf16, self.newline = utf16, newline
        self.ranges, self.surrogate_splits = [], []

    def native_length(self, text):
        native = text.replace('\n', self.newline)
        return len(native.encode('utf-16-le')) // 2 if self.utf16 else len(native)

    def GetLastPosition(self):
        return self.native_length(self.value)

    def GetRange(self, start, end):
        self.ranges.append((start, end))
        native = self.value.replace('\n', self.newline)
        try:
            value = (native.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
                     if self.utf16 else native[start:end])
        except UnicodeError:
            self.surrogate_splits.append((start, end))
            raise
        # MSW GetRange translates even a trailing CR; slicing also clamps.
        return value.replace('\r\n', '\n').replace('\r', '\n')

    def XYToPosition(self, column, row):
        raise AssertionError('An exact absolute native position must not depend on display rows.')


@pytest.mark.parametrize('utf16,newline', [
    (False, '\n'), (False, '\r\n'), (True, '\n'), (True, '\r\n'),
], ids=['characters-lf', 'characters-crlf', 'utf16-lf', 'utf16-crlf'])
def test_native_positions_prove_all_prefixes_without_logical_row_coordinates(utf16, newline):
    raw = 'Title 😀\n\nExact α 😀 source & words.\nTail 🧪\n'
    control = _NativeOffsetControl(WxDouble(lambda wx: None), value=raw, utf16=utf16, newline=newline)
    for offset in range(len(raw) + 1):
        before = len(control.ranges)
        position = native_position(control, raw, offset)
        assert position == control.native_length(raw[:offset])
        assert 1 <= len(control.ranges) - before <= 4
        assert control.GetRange(0, position) == raw[:offset]
    assert all(0 <= start <= end <= control.GetLastPosition() for start, end in control.ranges)
    assert control.GetValue() == raw


@pytest.mark.parametrize('utf16,newline', [
    (False, '\n'), (False, '\r\n'), (True, '\n'), (True, '\r\n'),
], ids=['characters-lf', 'characters-crlf', 'utf16-lf', 'utf16-crlf'])
def test_native_end_position_cannot_be_proved_by_an_oversized_clamped_range(utf16, newline):
    raw = '😀\n'
    control = _NativeOffsetControl(WxDouble(lambda wx: None), value=raw, utf16=utf16, newline=newline)
    last = control.GetLastPosition()
    assert control.GetRange(0, last + 20) == raw  # The real platform may clamp.
    control.ranges.clear()
    assert native_position(control, raw, len(raw)) == last
    assert control.ranges and all(end <= last for _, end in control.ranges)


def test_native_position_continues_after_a_surrogate_splitting_candidate():
    raw = 'a\n😀tail'
    control = _NativeOffsetControl(WxDouble(lambda wx: None), value=raw, newline='\n')
    assert native_position(control, raw, 2) == 2
    assert control.surrogate_splits == [(0, 3)]
    assert control.GetRange(0, 2) == 'a\n'
    assert control.GetValue() == raw


def test_native_position_uses_complete_crlf_even_if_lone_cr_reads_as_lf():
    raw = 'a\nx'
    control = _NativeOffsetControl(WxDouble(lambda wx: None), value=raw)
    assert control.GetRange(0, 2) == 'a\n'  # Mid-CRLF also normalizes to LF.
    assert native_position(control, raw, 2) == 3
    assert control.GetRange(3, control.GetLastPosition()) == 'x'
    assert control.GetValue() == raw


def test_native_position_retains_only_a_bounded_proven_xy_fallback():
    control = SimpleNamespace(GetValue=lambda: 'abc', GetLastPosition=lambda: 8,
        GetRange=lambda first, last: 'ab' if (first, last) == (0, 7) else '',
        XYToPosition=lambda column, row: 7)
    assert native_position(control, 'abc', 2) == 7
    control.GetLastPosition = lambda: 6
    with pytest.raises(ValueError, match='could not locate'):
        native_position(control, 'abc', 2)


def test_recorded_overlay_highlight_uses_exact_positions_after_soft_wrapped_provenance():
    from carr_library.ui.ebook_audio_dialog import EbookAudioDialog

    fragment = 'Exact α 😀 source & words.'
    raw = ('Title\n\n' + 'Source provenance remains unverified. ' * 18 +
           '\n\nChapter\n\n' + fragment + '\n\nTail.')
    control = _NativeOffsetControl(WxDouble(lambda wx: None), value=raw)
    visual_rows, native_start = [], 0
    for line in raw.split('\n'):
        for column in range(0, max(1, len(line)), 40):
            piece = line[column:column + 40]
            visual_rows.append((native_start + control.native_length(line[:column]),
                                control.native_length(piece)))
        native_start += control.native_length(line) + 2

    def visual_xy(column, row):
        if not 0 <= row < len(visual_rows):
            return -1
        first, length = visual_rows[row]
        return first + column if 0 <= column <= length else -1

    control.XYToPosition = visual_xy
    first, last = raw.index(fragment), raw.index(fragment) + len(fragment)
    prefix = raw[:first]
    old_line_start = visual_xy(0, prefix.count('\n'))
    assert old_line_start >= 0 and control.GetRange(0, old_line_start) != prefix
    owner = SimpleNamespace(text=control, transport=SimpleNamespace(index=0),
        session=SimpleNamespace(page=SimpleNamespace(raw_text=raw),
            chapter=SimpleNamespace(cues=(SimpleNamespace(start=first, end=last),))))
    EbookAudioDialog._highlight(owner)
    assert control.selection == (control.native_length(raw[:first]), control.native_length(raw[:last]))
    assert control.GetRange(*control.selection) == fragment
    assert control.position == control.selection[0]
    assert control.GetValue() == raw


def _recorded_epub_playback_observation():
    from threading import Event

    harness = _recorded_epub_native_probe_handlers()
    raw = 'Exact α 😀 source & words.'
    harness.env.update(stage=2, expected_page=SimpleNamespace(raw_text=raw))
    harness.overlay.busy = False
    harness.overlay.cancelled = Event()
    harness.overlay.session = SimpleNamespace(page=SimpleNamespace(raw_text=raw),
        chapter=SimpleNamespace(cues=(SimpleNamespace(start=0, end=len(raw)),)))
    harness.overlay.text = SimpleNamespace(GetSelection=lambda: (0, 26),
        GetStringSelection=lambda: raw, GetValue=lambda: raw)
    harness.overlay.status = SimpleNamespace(GetValue=lambda: 'Ready. No audio has started.')
    harness.overlay.player = SimpleNamespace(state='stopped')
    harness.overlay.transport = SimpleNamespace(ticket=(0, 1), active=False, closed=False,
                                                 interrupted=False, prepared=None)
    harness.overlay._pending = None
    return harness


@pytest.mark.parametrize('state', ['ready', 'preparation-error', 'deferred-start'])
def test_recorded_epub_deadline_reports_exact_playback_state_before_cleanup(capsys, state):
    import json

    harness = _recorded_epub_playback_observation()
    if state == 'preparation-error':
        harness.overlay.cancelled.set()
        harness.overlay.status.GetValue = lambda: 'The selected audio device refused preparation.'
    elif state == 'deferred-start':
        harness.overlay._pending = object()
        harness.overlay.status.GetValue = lambda: 'Preparing the exact recorded audio interval…'
    expected_status = harness.overlay.status.GetValue()
    expected_pending = harness.overlay._pending
    harness.env['expire']()
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if line.startswith('{"epub_highlight_diagnostic"'))
    diagnostic = json.loads(line)['epub_highlight_diagnostic']
    assert diagnostic['stage'] == 2 and diagnostic['busy'] is False and diagnostic['closed'] is False
    assert diagnostic['status']['prefix'] == expected_status
    assert diagnostic['playback'] == {
        'cancelled': state == 'preparation-error', 'key_pending': False,
        'player_state': 'stopped', 'transport_ticket': [0, 1], 'transport_active': False,
        'transport_closed': False, 'transport_interrupted': False,
        'prepared_present': False, 'pending_present': state == 'deferred-start',
    }
    assert output.index(line) < output.index('FAIL: Native EPUB deadline expired.')
    assert len(harness.env['errors']) == 1 and 'deadline at stage 2' in harness.env['errors'][0]
    assert harness.overlay._pending is expected_pending and harness.events == []
    _recorded_epub_native_step(harness)
    assert harness.events == [('close', 'overlay'), ('end', 'overlay')]


def test_recorded_epub_playback_getter_error_is_bounded_without_hiding_status(capsys):
    import json

    harness = _recorded_epub_playback_observation()

    class UnavailablePlayer:
        @property
        def state(self):
            raise RuntimeError('unavailable-' + 'x' * 3000)

    harness.overlay.player = UnavailablePlayer()
    harness.env['trace_highlight']()
    value = json.loads(capsys.readouterr().out.splitlines()[1])['epub_highlight_diagnostic']
    assert value['playback']['player_state']['unavailable'] == 'RuntimeError'
    assert len(value['playback']['player_state']['reason']) == 256
    assert value['status']['prefix'] == 'Ready. No audio has started.'
    assert harness.events == [] and harness.pending == [] and harness.env['errors'] == []


def test_recorded_epub_failed_failure_state_read_does_not_suppress_deadline_or_cleanup(capsys):
    harness = _recorded_epub_playback_observation()
    harness.overlay.text.GetSelection = Mock(side_effect=RuntimeError('native text peer is unavailable'))
    harness.env['expire']()
    output = capsys.readouterr().out
    assert 'EPUB failure-state observation unavailable: RuntimeError: native text peer is unavailable' in output
    assert 'FAIL: Native EPUB deadline expired.' in output
    assert len(harness.env['errors']) == 1 and 'deadline at stage 2' in harness.env['errors'][0]
    _recorded_epub_native_step(harness)
    assert harness.events == [('close', 'overlay'), ('end', 'overlay')]
