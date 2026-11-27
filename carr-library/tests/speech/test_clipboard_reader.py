"""Explicit read-only clipboard dispatch into the existing recoverable reader."""
from threading import get_ident
from types import SimpleNamespace
import sys

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import clipboard_reader as ui
from carr_library.speech import VoiceRegistry


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
    entries, opened, reports = {}, [], []
    voice_menu = Menu()
    voice_menu.Append(-1, 'Voice reader and reading copy...\tCtrl+Alt+V')
    voice_menu.Append(-1, 'Narrate selected book or chapter...\tCtrl+Alt+R')
    menus = [(Menu(), '&Library'), (voice_menu, '&Voice')]
    bar = SimpleNamespace(GetMenuCount=lambda: len(menus),
        GetMenuLabel=lambda index: menus[index][1], GetMenu=lambda index: menus[index][0])
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), report=reports.append,
        Bind=lambda event, handler, item: entries.__setitem__(item.label, handler),
        GetMenuBar=lambda: bar)
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
    monkeypatch.setattr(VoiceRegistry, 'synthesize', lambda *a, **k: pytest.fail('Opening must not start speech.'))
    ui.install_clipboard_reader(frame)
    assert clipboard.calls == [] and opened == [] and len(menus) == 2
    assert [item.label for item in voice_menu.items[:2]] == [
        'Voice reader and reading copy...\tCtrl+Alt+V',
        'Narrate selected book or chapter...\tCtrl+Alt+R']
    assert set(entries) == {'Read &clipboard...'}
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



def _diagnostic_probe(monkeypatch):
    import importlib.util
    from pathlib import Path
    from unittest.mock import Mock

    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_clipboard_reader_smoke.py'
    spec = importlib.util.spec_from_file_location('clipboard_diagnostic_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # These controls test durable diagnostic reporting only. The actual probe
    # retains its independent native watchdog and every clipboard assertion.
    module.faulthandler = SimpleNamespace(enable=Mock(), dump_traceback_later=Mock(),
                                         cancel_dump_traceback_later=Mock())
    return module


@pytest.mark.parametrize('error', [ImportError, OSError], ids=['import', 'native-call'])
def test_native_probe_persists_initial_and_failure_receipt_before_native_success(tmp_path, monkeypatch, error):
    import json

    module = _diagnostic_probe(monkeypatch)
    report = tmp_path / 'receipt.json'
    monkeypatch.setattr(sys, 'argv', ['probe', '--expect-installed', '--report', str(report)])
    def failing(args, value):
        initial = json.loads(report.read_bytes())
        assert initial['status'] == 'FAIL' and initial['phase'] == 'starting'
        assert initial['checks'] == [] and initial['clipboard_content'] == 'NOT_RECORDED'
        assert initial['python'] == sys.version
        module._phase(args, value, 'exact-native-boundary')
        raise error('Synthetic bounded diagnostic failure.')
    monkeypatch.setattr(module, '_run_probe', failing)
    with pytest.raises(error, match='Synthetic'):
        module.main()
    saved = json.loads(report.read_bytes())
    assert saved['status'] == 'FAIL' and saved['phase'] == 'exact-native-boundary'
    assert saved['failure'] == {'type': error.__name__, 'message': 'Synthetic bounded diagnostic failure.'}
    assert saved['checks'] == [] and saved['installed'] is True
    assert not report.with_name('receipt.json.partial').exists()
    module.faulthandler.dump_traceback_later.assert_called_once_with(90, exit=True)
    module.faulthandler.cancel_dump_traceback_later.assert_called_once_with()


def test_native_probe_pass_receipt_requires_completed_probe_result(tmp_path, monkeypatch):
    import json

    module = _diagnostic_probe(monkeypatch)
    report = tmp_path / 'receipt.json'
    monkeypatch.setattr(sys, 'argv', ['probe', '--source', str(tmp_path), '--report', str(report)])
    def completed(args, value):
        value['checks'].append('Purpose-written completed control')
        value['cleanup_complete'] = True
        return {'status': 'PASS', 'checks': value['checks'], 'module_sha256': 'a' * 64}
    monkeypatch.setattr(module, '_run_probe', completed)
    assert module.main() == 0
    saved = json.loads(report.read_bytes())
    assert saved['status'] == 'PASS' and saved['phase'] == 'completed'
    assert saved['checks'] == ['Purpose-written completed control']
    assert saved['module_sha256'] == 'a' * 64 and saved['cleanup_complete'] is True
    assert saved['clipboard_content'] == 'NOT_RECORDED' and saved['installed'] is False


def test_native_probe_interrupt_stays_failed_and_cancels_watchdog(tmp_path, monkeypatch):
    import json

    module = _diagnostic_probe(monkeypatch)
    report = tmp_path / 'receipt.json'
    monkeypatch.setattr(sys, 'argv', ['probe', '--expect-installed', '--report', str(report)])
    def interrupted(args, value):
        raise KeyboardInterrupt('Synthetic interrupted native probe.')
    monkeypatch.setattr(module, '_run_probe', interrupted)
    with pytest.raises(KeyboardInterrupt):
        module.main()
    saved = json.loads(report.read_bytes())
    assert saved['status'] == 'FAIL' and saved['failure']['type'] == 'KeyboardInterrupt'
    module.faulthandler.cancel_dump_traceback_later.assert_called_once_with()


def test_native_probe_atomic_receipt_does_not_replace_prior_evidence_on_publish_failure(tmp_path, monkeypatch):
    module = _diagnostic_probe(monkeypatch)
    report = tmp_path / 'receipt.json'
    module._checkpoint(report, {'status': 'FAIL', 'phase': 'before-native-call'})
    original = report.read_bytes()
    def failed_publish(source, destination):
        raise OSError('Synthetic receipt publication failure.')
    monkeypatch.setattr(module.os, 'replace', failed_publish)
    with pytest.raises(OSError, match='publication failure'):
        module._checkpoint(report, {'status': 'PASS', 'phase': 'completed'})
    assert report.read_bytes() == original



@pytest.mark.parametrize('mode', ['source', 'installed'])
def test_native_clipboard_probe_requires_exact_loaded_dependency_bytes(tmp_path, monkeypatch, mode):
    from pathlib import Path

    module = _diagnostic_probe(monkeypatch)
    checkout = tmp_path / 'checkout'
    expected = checkout / 'src' / 'carr_library'
    package = expected if mode == 'source' else tmp_path / 'installed' / 'carr_library'
    relative = 'ui/clipboard_reader.py'
    for root in {expected, package}:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'purpose-written module bytes\n')
    loaded = SimpleNamespace(__file__=str(package / relative))
    distribution = SimpleNamespace(files=[Path('carr_library') / relative],
        locate_file=lambda item: package.parent / item)
    values = module._module_evidence(checkout, package, [(relative, loaded)],
        source_mode=mode == 'source', distribution=distribution)
    assert values[relative]['path'] == str((package / relative).resolve())
    if mode == 'installed':
        (package / relative).write_bytes(b'changed loaded dependency\n')
        with pytest.raises(RuntimeError, match='bytes differ'):
            module._module_evidence(checkout, package, [(relative, loaded)],
                source_mode=False, distribution=distribution)
        (package / relative).write_bytes((expected / relative).read_bytes())
        distribution.files = []
        with pytest.raises(RuntimeError, match='wheel RECORD'):
            module._module_evidence(checkout, package, [(relative, loaded)],
                source_mode=False, distribution=distribution)
    else:
        with pytest.raises(RuntimeError, match='source checkout'):
            module._module_evidence(checkout, package, [(relative, loaded)],
                source_mode=False, distribution=distribution)


def test_native_clipboard_probe_rejects_foreign_module_root_before_byte_read(tmp_path, monkeypatch):
    module = _diagnostic_probe(monkeypatch)
    checkout = tmp_path / 'checkout'
    package = checkout / 'src' / 'carr_library'
    (package / 'ui').mkdir(parents=True)
    (package / 'ui' / 'clipboard_reader.py').write_bytes(b'exact')
    foreign = tmp_path / 'foreign.py'
    foreign.write_bytes(b'exact')
    monkeypatch.setattr(module, '_bounded_sha256',
        lambda path: pytest.fail('A foreign module reached the byte-proof reader.'))
    with pytest.raises(RuntimeError, match='package root'):
        module._module_evidence(checkout, package,
            [('ui/clipboard_reader.py', SimpleNamespace(__file__=str(foreign)))],
            source_mode=True)


def test_native_clipboard_probe_source_proof_rejects_oversize_before_hash(tmp_path, monkeypatch):
    module = _diagnostic_probe(monkeypatch)
    path = tmp_path / 'oversize.py'
    with path.open('wb') as stream:
        stream.truncate(2 * 1024 * 1024 + 1)
    monkeypatch.setattr(module, 'sha256',
        lambda value: pytest.fail('An oversized module reached hashing.'))
    with pytest.raises(RuntimeError, match='source-proof bound'):
        module._bounded_sha256(path)
