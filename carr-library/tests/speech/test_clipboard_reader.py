"""Explicit read-only clipboard dispatch into the existing recoverable reader."""
from threading import get_ident
from types import SimpleNamespace
import sys

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import speech_dialog as ui


class Clipboard:
    def __init__(self, text):
        self.text, self.owner = text, get_ident()
        self.busy, self.supported, self.transfer, self.raise_transfer = False, True, True, False
        self.opened, self.calls = False, []
    def native(self): assert get_ident() == self.owner
    def Open(self):
        self.native(); self.calls.append('open')
        self.opened = not self.busy
        return self.opened
    def IsSupported(self, format):
        self.native(); assert self.opened and format == 'unicode'
        self.calls.append('supported'); return self.supported
    def GetData(self, data):
        self.native(); assert self.opened
        self.calls.append('get')
        if self.raise_transfer: raise RuntimeError('Synthetic clipboard transfer failed')
        data.value = self.text
        return self.transfer
    def Close(self):
        self.native(); assert self.opened
        self.calls.append('close'); self.opened = False
    def Clear(self): pytest.fail('Reading must not clear the clipboard.')
    def SetData(self, data): pytest.fail('Reading must not replace clipboard contents.')
    def Flush(self): pytest.fail('Reading must not change clipboard persistence.')


class TextData:
    def __init__(self): self.value = ''
    def GetText(self): return self.value


def wx_for(clipboard):
    return SimpleNamespace(TheClipboard=clipboard, TextDataObject=TextData,
        DataFormat=lambda value: value, DF_UNICODETEXT='unicode')


def test_explicit_unicode_snapshot_keeps_whitespace_and_releases_clipboard():
    original = '  😀 Український текст.\r\nSecond line.\t '
    clipboard = Clipboard(original)
    assert ui._read_clipboard_text(wx_for(clipboard)) == original
    assert clipboard.calls == ['open', 'supported', 'get', 'close']
    assert clipboard.text == original and not clipboard.opened


@pytest.mark.parametrize(('case', 'message'), [
    ('busy', 'busy'), ('unsupported', 'Unicode text'), ('failed', 'could not be read'),
    ('exception', 'transfer failed'), ('empty', 'nonempty'), ('whitespace', 'nonempty'),
    ('oversize', 'at most 2000000'), ('nul', 'NUL'), ('surrogate', 'invalid Unicode'),
])
def test_unusable_clipboard_never_opens_a_reader_or_changes_existing_drafts(tmp_path, monkeypatch, case, message):
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Existing manual draft')))
    before = store.path.read_bytes()
    clipboard = Clipboard('Original snapshot')
    if case == 'busy': clipboard.busy = True
    elif case == 'unsupported': clipboard.supported = False
    elif case == 'failed': clipboard.transfer = False
    elif case == 'exception': clipboard.raise_transfer = True
    else:
        clipboard.text = {'empty': '', 'whitespace': ' \r\n\t', 'oversize': 'a' * 2_000_001,
                          'nul': 'Before\x00after', 'surrogate': '\ud800'}[case]
    original = clipboard.text
    monkeypatch.setitem(sys.modules, 'wx', wx_for(clipboard))
    monkeypatch.setattr(ui, 'show_speech_reader', lambda *a, **k: pytest.fail('Invalid input reached the reader.'))
    reports = []
    ui.show_clipboard_reader(SimpleNamespace(report=reports.append))
    assert len(reports) == 1 and message in reports[0]
    assert clipboard.text == original and not clipboard.opened
    if case == 'busy':
        assert clipboard.calls == ['open']
    else:
        assert clipboard.calls[-1] == 'close'
    assert store.path.read_bytes() == before
    assert not ReaderDraftStore(tmp_path, slot='clipboard').path.exists()


def test_menu_installation_does_not_read_watch_or_speak_and_explicit_action_uses_separate_slot(
        tmp_path, monkeypatch):
    clipboard = Clipboard('😀 One exact clipboard snapshot.\r\nNext line.')
    wx = wx_for(clipboard)
    class Menu:
        def __init__(self): self.items = []
        def Append(self, identity, label):
            item = SimpleNamespace(label=label)
            self.items.append(item); return item
    wx.Menu, wx.ID_ANY, wx.EVT_MENU = Menu, -1, 'menu'
    wx.Timer = lambda *a, **k: pytest.fail('The clipboard action must not install a watcher.')
    monkeypatch.setitem(sys.modules, 'wx', wx)
    entries, menus, opened, reports = {}, [], [], []
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), report=reports.append,
        Bind=lambda event, handler, item: entries.__setitem__(item.label, handler),
        GetMenuBar=lambda: SimpleNamespace(Append=lambda menu, label: menus.append((menu, label))))
    manual = ReaderDraftStore(tmp_path)
    manual.save(ReaderResume(ReadingDocument('Keep the existing manual reading draft.')))
    before = manual.path.read_bytes()
    def open_existing(parent, **options):
        assert parent is frame and not clipboard.opened
        opened.append(options)
        # Exercise the same existing store/derivative path selected by dispatch.
        document = ReadingDocument(options['text'], title=options['title'])
        assert document.source_anchor is None
        ReaderDraftStore(tmp_path, slot=options['recovery_slot']).save(ReaderResume(document))
    monkeypatch.setattr(ui, 'show_speech_reader', open_existing)
    monkeypatch.setattr(ui.VoiceRegistry, 'synthesize', lambda *a, **k: pytest.fail('Opening must not start speech.'))
    ui.install_speech_actions(frame)
    assert clipboard.calls == [] and opened == [] and menus[0][1] == '&Voice'
    assert 'Voice reader and reading copy...\tCtrl+Alt+V' in entries
    assert 'Narrate selected book or chapter...\tCtrl+Alt+R' in entries
    entries['Read &clipboard...'](None)
    assert opened == [{'text': clipboard.text, 'title': 'Clipboard reading copy', 'recovery_slot': 'clipboard'}]
    retained = ReaderDraftStore(tmp_path, slot='clipboard').load()
    assert retained.document.source_text == retained.document.text == clipboard.text
    assert manual.path.read_bytes() == before and not reports
    clipboard.text = 'Another application changed the clipboard later.'
    assert len(opened) == 1 and retained.document.text != clipboard.text


def test_closing_frame_does_not_access_clipboard(monkeypatch):
    clipboard, reports = Clipboard('Not admitted'), []
    monkeypatch.setitem(sys.modules, 'wx', wx_for(clipboard))
    monkeypatch.setattr(ui, 'show_speech_reader', lambda *a, **k: pytest.fail('Closing frame opened reader.'))
    ui.show_clipboard_reader(SimpleNamespace(_closing=True, report=reports.append))
    assert not clipboard.calls and len(reports) == 1 and 'closing' in reports[0]


def test_reader_open_failure_keeps_snapshot_and_releases_clipboard(monkeypatch):
    clipboard, reports = Clipboard('Unchanged clipboard text'), []
    monkeypatch.setitem(sys.modules, 'wx', wx_for(clipboard))
    def unavailable(*args, **kwargs): raise OSError('Synthetic reader initialization failure')
    monkeypatch.setattr(ui, 'show_speech_reader', unavailable)
    ui.show_clipboard_reader(SimpleNamespace(report=reports.append))
    assert clipboard.text == 'Unchanged clipboard text' and not clipboard.opened
    assert len(reports) == 1 and 'initialization failure' in reports[0]
