"""Explicit clipboard snapshots and transient reader integration, without wx I/O."""
import ast
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
import sys
from unittest.mock import Mock

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech import VoiceRegistry
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import clipboard_reader as clipboard_ui
from carr_library.ui import speech_dialog as reader_ui


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


def test_unicode_snapshot_preserves_exact_whitespace_and_releases_clipboard():
    original = '  😀 Український текст.\r\nSecond line.\t '
    clipboard = Clipboard(original)
    assert clipboard_ui.read_clipboard_text(wx_for(clipboard)) == original
    assert clipboard.calls == ['open', 'supported', 'get', 'close']
    assert clipboard.text == original and not clipboard.opened


@pytest.mark.parametrize(('case', 'message'), [
    ('busy', 'busy'), ('unsupported', 'Unicode text'), ('failed', 'could not be read'),
    ('exception', 'transfer failed'), ('empty', 'nonempty'), ('whitespace', 'nonempty'),
    ('oversize', 'at most 2000000'), ('nul', 'NUL'), ('surrogate', 'invalid Unicode'),
])
def test_unusable_snapshot_never_opens_reader_or_changes_existing_drafts(
        tmp_path, monkeypatch, case, message):
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
    monkeypatch.setattr(reader_ui, 'show_speech_reader',
                        lambda *a, **k: pytest.fail('Invalid input reached reader.'))
    reports = []
    clipboard_ui.show_clipboard_reader(SimpleNamespace(report=reports.append))
    assert len(reports) == 1 and message in reports[0]
    assert clipboard.text == original and not clipboard.opened
    assert clipboard.calls == ['open'] if case == 'busy' else clipboard.calls[-1] == 'close'
    assert store.path.read_bytes() == before
    assert not ReaderDraftStore(tmp_path, slot='clipboard').path.exists()


def test_existing_menu_dispatch_is_explicit_and_uses_transient_reader(tmp_path, monkeypatch):
    clipboard = Clipboard('😀 One exact clipboard snapshot.\r\nNext line.')
    wx = wx_for(clipboard)
    class Menu:
        def __init__(self): self.items = []
        def Append(self, identity, label):
            item = SimpleNamespace(label=label)
            self.items.append(item); return item
    wx.Menu, wx.ID_ANY, wx.EVT_MENU = Menu, -1, 'menu'
    wx.Timer = lambda *a, **k: pytest.fail('Installing a menu must not install a watcher.')
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
        assert options['recovery_slot'] is None
        assert ReadingDocument(options['text']).source_anchor is None
    monkeypatch.setattr(reader_ui, 'show_speech_reader', open_existing)
    reader_ui.install_speech_actions(frame)
    assert clipboard.calls == [] and opened == [] and menus[0][1] == '&Voice'
    assert 'Voice reader and reading copy...\tCtrl+Alt+V' in entries
    assert 'Narrate selected book or chapter...\tCtrl+Alt+R' in entries
    item = next(item for item in menus[0][0].items if item.label == 'Read &clipboard...')
    assert item.carr_command_name == 'read clipboard'
    entries['Read &clipboard...'](None)
    assert len(opened) == 1
    assert opened[0]['text'] == clipboard.text and opened[0]['title'] == 'Clipboard reading copy'
    assert callable(opened[0]['clipboard_reader'])
    assert manual.path.read_bytes() == before and not reports
    assert not ReaderDraftStore(tmp_path, slot='clipboard').path.exists()
    clipboard.text = 'Another application changed the clipboard later.'
    assert len(opened) == 1


def test_closing_or_deleted_host_never_reads_clipboard(monkeypatch):
    clipboard, reports = Clipboard('Not admitted'), []
    monkeypatch.setitem(sys.modules, 'wx', wx_for(clipboard))
    monkeypatch.setattr(reader_ui, 'show_speech_reader',
                        lambda *a, **k: pytest.fail('Closing host opened reader.'))
    clipboard_ui.show_clipboard_reader(SimpleNamespace(_closing=True, report=reports.append))
    class Deleted:
        def __bool__(self): return False
    clipboard_ui.show_clipboard_reader(Deleted())
    assert not clipboard.calls and not reports


def test_monitor_default_off_reports_only_changes_and_forgets_owner_on_close():
    source, reads = ['Initial copy'], []
    def read():
        reads.append(source[0])
        return source[0]
    session = clipboard_ui.ClipboardSession(read, source[0])
    source[0] = 'Second copy'
    assert session.poll() is None and reads == []
    session.enable(True)
    assert session.poll() == len(source[0]) and session.poll() is None
    source[0] = 'Third copy'
    assert session.poll() == len(source[0])
    session.enable(False)
    before = list(reads)
    source[0] = 'Fourth copy'
    assert session.poll() is None and reads == before
    assert session.capture() == 'Fourth copy'  # Explicit load still works while watch is off.
    session.close()
    before = list(reads)
    assert session.poll() is None and session.enable(True) is False
    with pytest.raises(ValueError, match='closed'):
        session.capture()
    assert reads == before and session._reader is None and session._last is None


class Control:
    def __init__(self, *args, **kwargs):
        self.value, self.enabled, self.running = kwargs.get('value', False), True, False
        self.name, self.handlers, self.destroyed = '', {}, False
    def native(self): assert not self.destroyed
    def SetName(self, value): self.native(); self.name = value
    def SetValue(self, value): self.native(); self.value = value
    def ChangeValue(self, value): self.SetValue(value)
    def GetValue(self): self.native(); return self.value
    def SetInsertionPoint(self, value): self.native()
    def SetFocus(self): self.native()
    def Add(self, *args): self.native()
    def Bind(self, event, handler, *args): self.native(); self.handlers[event] = handler
    def Start(self, interval): self.native(); self.running = True; self.interval = interval
    def Stop(self): self.native(); self.running = False


def controls_fixture(initial='Initial copy'):
    clipboard, layout, dialog = Clipboard(initial), Control(), Control()
    wx = wx_for(clipboard)
    wx.BoxSizer = wx.CheckBox = wx.Button = wx.TextCtrl = wx.Timer = Control
    wx.HORIZONTAL = wx.ALL = wx.EXPAND = wx.TE_READONLY = wx.TE_MULTILINE = 0
    wx.EVT_TIMER, wx.EVT_CHECKBOX, wx.EVT_BUTTON = 'timer', 'check', 'button'
    applied, alive = [], [True]
    controls = clipboard_ui.ClipboardControls(wx, dialog, layout,
        reader=lambda: clipboard_ui.read_clipboard_text(wx), initial=initial,
        apply=lambda value: applied.append(value) or True, alive=lambda: alive[0])
    return controls, clipboard, applied, alive


def test_native_control_handlers_keep_watch_opt_in_and_never_apply_on_tick():
    controls, clipboard, applied, alive = controls_fixture()
    assert not controls.timer.running and not controls.watch.GetValue() and not clipboard.calls
    clipboard.text = 'Observed without replacing edited text'
    controls.on_tick()
    assert clipboard.calls == [] and applied == []
    controls.watch.SetValue(True)
    controls.on_toggle()
    assert controls.timer.running and controls.timer.interval == 1000
    controls.on_tick()
    assert 'Clipboard text changed' in controls.status.GetValue() and applied == []
    assert controls.on_load() is True and applied == [clipboard.text]
    controls.watch.SetValue(False)
    controls.on_toggle()
    before = list(clipboard.calls)
    controls.on_tick()
    assert clipboard.calls == before and not controls.timer.running
    controls.close()
    for child in (controls.watch, controls.load, controls.status, controls.timer):
        child.destroyed = True
    controls.on_tick(); controls.on_toggle()
    assert controls.on_load() is False
    assert clipboard.calls == before and controls.session._reader is None


def test_destroyed_owner_and_nested_load_stop_late_clipboard_access():
    controls, clipboard, applied, alive = controls_fixture()
    controls.watch.SetValue(True); controls.on_toggle()
    def apply(value):
        controls.on_tick()  # A nested modal loop must not read/replace a newer snapshot.
        alive[0] = False
        return True
    controls.apply = apply
    clipboard.text = 'Selected snapshot'
    assert controls.on_load() is True
    assert clipboard.calls == ['open', 'supported', 'get', 'close']
    assert controls.closed and not controls.timer.running
    controls.on_tick()
    assert clipboard.calls == ['open', 'supported', 'get', 'close']


def reader_handlers(**overrides):
    """Execute the actual nested replacement/recovery callbacks, without wx."""
    module = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    host = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                and node.name == 'show_speech_reader')
    wanted = {'replace_clipboard', 'recover_saved_draft', 'save_recovery'}
    definitions = [node for node in host.body if isinstance(node, ast.FunctionDef)
                   and node.name in wanted]
    assert {node.name for node in definitions} == wanted
    template = ast.parse("""
def build(values):
    alive, busy, source_loaded = True, False, True
    clipboard_reader = lambda: 'Explicit clipboard'
    recovery_slot, draft_store = None, None
    source_guard, source_loader, recovery_notice = lambda: None, None, ''
    document = values['document']
    recovered, save_path, source_locations, last_recovery_signature = None, values['save_path'], (), None
    editor, live, wx, dialog = values['editor'], values['live'], values['wx'], values['dialog']
    report, sync_document, display_document = values['report'], values['sync'], values['display']
    def placeholder(): pass
    def state():
        return document, save_path, draft_store
    return SimpleNamespace(replace=replace_clipboard, recover=recover_saved_draft,
                           autosave=save_recovery, state=state)
""")
    target = template.body[0].body
    at = next(i for i, node in enumerate(target) if isinstance(node, ast.FunctionDef)
              and node.name == 'placeholder')
    target[at:at+1] = definitions
    original = ReadingDocument('Original clipboard')
    original.set_text('Edited clipboard')
    editor = Control(value=original.text)
    reports, displayed, confirmations = [], [], []
    wx = SimpleNamespace(YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4, YES=8,
                         MessageBox=lambda *args: confirmations.append(args) or 8)
    values = dict(document=original, save_path=Path('previous-explicit-save.json'),
        editor=editor, live=SimpleNamespace(core=SimpleNamespace(busy=False, quiescent=True), stop=Mock()),
        wx=wx, dialog=object(), report=lambda message, **kwargs: reports.append(message),
        sync=lambda: None, display=lambda: displayed.append(True))
    values.update(overrides)
    namespace = dict(SimpleNamespace=SimpleNamespace, ReadingDocument=ReadingDocument,
        ReaderDraftStore=lambda *args, **kwargs: pytest.fail('Transient reader opened a draft store.'))
    exec(compile(ast.fix_missing_locations(template), reader_ui.__file__, 'exec'), namespace)
    handlers = namespace['build'](values)
    return handlers, values, reports, displayed, confirmations


def test_explicit_clipboard_replace_confirms_edits_resets_save_target_and_has_no_source_authority():
    handlers, values, reports, displayed, confirmations = reader_handlers()
    assert handlers.autosave() is False
    handlers.recover()
    assert len(reports) == 1 and 'transient reader' in reports[0]
    assert handlers.replace('New unbound clipboard') is True
    document, save_path, store = handlers.state()
    assert len(confirmations) == 1
    assert document.source_text == document.text == 'New unbound clipboard'
    assert document.source_anchor is None and save_path is None and store is None
    assert displayed == [True] and not values['live'].stop.called
    assert handlers.autosave(force=True) is False


@pytest.mark.parametrize('boundary', ['decline', 'edited-during-confirm', 'playback-pending'])
def test_clipboard_replace_never_silently_discards_edits_or_active_playback(boundary):
    handlers, values, reports, displayed, confirmations = reader_handlers()
    before = handlers.state()
    if boundary == 'decline':
        values['wx'].MessageBox = lambda *args: 0
    elif boundary == 'edited-during-confirm':
        def confirm(*args):
            values['editor'].SetValue('An edit arrived during the modal confirmation.')
            return values['wx'].YES
        values['wx'].MessageBox = confirm
    else:
        values['live'].core.quiescent = False
    assert handlers.replace('New clipboard') is False
    assert handlers.state() == before and displayed == []
    if boundary == 'playback-pending':
        values['live'].stop.assert_called_once_with()
        assert 'playback cleanup' in reports[-1]


def test_transient_draft_initialization_skips_all_recovery_io(tmp_path):
    """Execute the actual initialization block so absence of auto-persistence is tested."""
    tree = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    host = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'show_speech_reader')
    initialization = next(node for node in host.body if isinstance(node, ast.Try)
        and any(isinstance(child, ast.Name) and child.id == 'ReaderDraftStore' for child in ast.walk(node)))
    document = ReadingDocument('Clipboard must not replace a manual draft.')
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Existing manual draft')))
    original = store.path.read_bytes()
    namespace = dict(root=tmp_path, recovery_slot=None, source_loader=None, text=document.text,
        document=document, recovered=None, draft_store=None, source_locations=(),
        recovery_notice='Held in this window.',
        ReaderDraftStore=lambda *args, **kwargs: pytest.fail('Transient clipboard opened recovery storage.'))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[initialization], type_ignores=[])),
                 reader_ui.__file__, 'exec'), namespace)
    assert namespace['draft_store'] is None and namespace['recovered'] is None
    assert namespace['document'] is document and store.path.read_bytes() == original


# Retained public dispatch regressions exercise compatibility entry points.
def test_explicit_unicode_snapshot_keeps_whitespace_and_releases_clipboard():
    original = '  😀 Український текст.\r\nSecond line.\t '
    clipboard = Clipboard(original)
    assert reader_ui._read_clipboard_text(wx_for(clipboard)) == original
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
    monkeypatch.setattr(reader_ui, 'show_speech_reader', lambda *a, **k: pytest.fail('Invalid input reached the reader.'))
    reports = []
    reader_ui.show_clipboard_reader(SimpleNamespace(report=reports.append))
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
    entries, menus, opened, reports, copies = {}, [], [], [], []
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), report=reports.append,
        Bind=lambda event, handler, item: entries.__setitem__(item.label, handler),
        GetMenuBar=lambda: SimpleNamespace(Append=lambda menu, label: menus.append((menu, label))))
    manual = ReaderDraftStore(tmp_path)
    manual.save(ReaderResume(ReadingDocument('Keep the existing manual reading draft.')))
    before = manual.path.read_bytes()
    def open_existing(parent, **options):
        assert parent is frame and not clipboard.opened
        opened.append(options)
        # The retained menu still opens the same derivative reader. Clipboard
        # content now explicitly stays transient until an independent Save As.
        document = ReadingDocument(options['text'], title=options['title'])
        assert document.source_anchor is None
        assert options['recovery_slot'] is None and callable(options['clipboard_reader'])
        copies.append(ReaderResume(document))
    monkeypatch.setattr(reader_ui, 'show_speech_reader', open_existing)
    monkeypatch.setattr(VoiceRegistry, 'synthesize', lambda *a, **k: pytest.fail('Opening must not start speech.'))
    reader_ui.install_speech_actions(frame)
    assert clipboard.calls == [] and opened == [] and menus[0][1] == '&Voice'
    assert 'Voice reader and reading copy...\tCtrl+Alt+V' in entries
    assert 'Narrate selected book or chapter...\tCtrl+Alt+R' in entries
    entries['Read &clipboard...'](None)
    assert len(opened) == 1 and opened[0]['text'] == clipboard.text
    assert opened[0]['title'] == 'Clipboard reading copy' and opened[0]['recovery_slot'] is None
    retained, = copies
    assert not ReaderDraftStore(tmp_path, slot='clipboard').path.exists()
    assert retained.document.source_text == retained.document.text == clipboard.text
    assert manual.path.read_bytes() == before and not reports
    clipboard.text = 'Another application changed the clipboard later.'
    assert len(opened) == 1 and retained.document.text != clipboard.text


def test_closing_frame_does_not_access_clipboard(monkeypatch):
    clipboard, reports = Clipboard('Not admitted'), []
    monkeypatch.setitem(sys.modules, 'wx', wx_for(clipboard))
    monkeypatch.setattr(reader_ui, 'show_speech_reader', lambda *a, **k: pytest.fail('Closing frame opened reader.'))
    reader_ui.show_clipboard_reader(SimpleNamespace(_closing=True, report=reports.append))
    # A closing/deleted native host is not a valid reporting target.
    assert not clipboard.calls and not reports


def test_reader_open_failure_keeps_snapshot_and_releases_clipboard(monkeypatch):
    clipboard, reports = Clipboard('Unchanged clipboard text'), []
    monkeypatch.setitem(sys.modules, 'wx', wx_for(clipboard))
    def unavailable(*args, **kwargs): raise OSError('Synthetic reader initialization failure')
    monkeypatch.setattr(reader_ui, 'show_speech_reader', unavailable)
    reader_ui.show_clipboard_reader(SimpleNamespace(report=reports.append))
    assert clipboard.text == 'Unchanged clipboard text' and not clipboard.opened
    assert len(reports) == 1 and 'initialization failure' in reports[0]
