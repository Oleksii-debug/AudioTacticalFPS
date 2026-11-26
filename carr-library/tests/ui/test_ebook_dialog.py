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
        contents = wx.named('Book contents (levels and chapter destinations)')
        contents.SetSelection(1)
        contents.handlers[wx.EVT_CHOICE](None)
        message = wx.named('Chapter and navigation status').GetValue()
        assert 'positioning unavailable' in message
        assert 'complete chapter is available from its beginning' in message
        assert 'Tab to read at this destination' not in message
        assert reader.GetValue().startswith('First chapter.')

    run_dialog(monkeypatch, interaction)


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


def test_action_captures_scope_for_async_service_and_restores_catalog_focus(monkeypatch):
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
def test_action_does_not_open_delayed_result_after_selection_changed(monkeypatch, changed):
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


def test_action_does_not_read_new_scope_or_focus_old_controls_after_modal_switch(monkeypatch):
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
