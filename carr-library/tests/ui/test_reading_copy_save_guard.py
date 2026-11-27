"""Execute the real save callback around a modal authorization change."""
import ast
from concurrent.futures import Future
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech.reading import ReadingDocument


def _host_callbacks(template, names):
    path = Path(find_spec('carr_library.ui.speech_dialog').origin)
    source = ast.parse(path.read_text(encoding='utf-8'))
    host = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    factory = ast.parse(template)
    callbacks = {node.name: node for node in host.body if isinstance(node, ast.FunctionDef)}
    factory.body[0].body[-1:-1] = [callbacks[name] for name in names]
    environment = {'__package__': 'carr_library.ui', 'with_recovery_warning': lambda message: message,
                   'Event': Event, 'SimpleNamespace': SimpleNamespace, 'Path': Path,
                   'ReadingDocument': ReadingDocument}
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), environment)
    return environment['build']


@pytest.mark.parametrize('revocation', ['before_dialog', 'inside_dialog', 'none'])
def test_reading_copy_save_as_reauthorizes_after_native_chooser(tmp_path, revocation):
    source = Path(find_spec('carr_library.ui.speech_dialog').origin)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    host = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    save = next(node for node in host.body if isinstance(node, ast.FunctionDef) and node.name == 'save_project')
    factory = ast.parse('''
def build(source_guard, sync_document, draft_store, save_recovery, document, wx, dialog, Path, report):
    save_path, alive = None, True
    return save_project
''')
    factory.body[0].body.insert(1, save)
    environment = {'with_recovery_warning': lambda message: message}
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    authorized, dialogs, reports = [revocation != 'before_dialog'], [], []
    destination = tmp_path / 'reading.reading.json'
    document = ReadingDocument('Source 🚂.\r\n')
    document.replace(0, 6, 'Reading copy')

    def guard():
        if not authorized[0]:
            raise ValueError('The source authorization changed while choosing a destination.')

    class Chooser:
        def __init__(self, *args, **kwargs): dialogs.append(self)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def Destroy(self): pass
        def ShowModal(self):
            if revocation == 'inside_dialog':
                authorized[0] = False
            return 1
        def GetPath(self): return str(destination)

    wx = SimpleNamespace(FileDialog=Chooser, ID_OK=1, FD_SAVE=2)
    callback = environment['build'](guard, lambda: None, None, lambda **kwargs: None,
        document, wx, object(), Path, lambda text, **kwargs: reports.append(text))
    result = callback(choose=True)
    if revocation == 'none':
        assert result is True and ReadingDocument.load(destination).source_text == document.source_text
    else:
        assert result is False and not destination.exists()
        assert any('authorization changed' in report for report in reports)
        assert len(dialogs) == int(revocation == 'inside_dialog')


@pytest.mark.parametrize('late_change', ['stop', 'revoke'])
def test_wav_completion_reports_real_publication_before_late_stop_or_revoke(tmp_path, monkeypatch, late_change):
    from carr_library.reader.live_reading import DocumentSequence
    from carr_library.reader import live_export
    from carr_library.speech.runtime import SpeechResult

    cancelled, queued, reports, authorized = Event(), [], [], [True]
    document = ReadingDocument('Actual synthetic PCM publication. 🚂\r\n')
    target = tmp_path / 'completed.wav'

    def guard():
        if not authorized[0]:
            raise ValueError('Source authorization revoked after publication.')

    def render(request, path, **kwargs):
        assert not kwargs['cancelled']()
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x00\x00' * 800)
        return SpeechResult(Path(path), sha256(Path(path).read_bytes()).hexdigest(), .1,
            8000, 1, 800, request.voice_id, sha256(request.text.encode()).hexdigest(), 1)

    actual_export = live_export.export_document_speech
    monkeypatch.setattr(live_export, 'export_document_speech',
        lambda *args, **kwargs: actual_export(*args, render=render, **kwargs))

    class Executor:
        def submit(self, work):
            future = Future()
            try:
                future.set_result(work())
            except Exception as exc:
                future.set_exception(exc)
            return future

    class Chooser:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def Destroy(self): pass
        def ShowModal(self): return 1
        def GetPath(self): return str(target)

    build = _host_callbacks('''
def build(live, report, cancel, wx, executor, source_guard, current, editor,
          reading_range, root, document, source_locations, DocumentSequence, Path, dialog):
    busy, alive = False, True
    return save_audio
''', ['run', 'save_audio'])
    live = SimpleNamespace(core=SimpleNamespace(busy=False), _voice_settings=lambda: ('fixture:pcm', 1., 1.))
    callback = build(live, lambda message, **kwargs: reports.append(message), cancelled,
        SimpleNamespace(FileDialog=Chooser, ID_OK=1, FD_SAVE=2, CallAfter=queued.append),
        Executor(), guard, lambda: (1, document.text), SimpleNamespace(GetSelection=lambda: (0, 0)),
        lambda editor, raw, mode: (0, len(raw)), tmp_path, document, (), DocumentSequence, Path, object())
    callback()
    assert target.is_file() and len(queued) == 1
    before = target.read_bytes()
    if late_change == 'stop':
        cancelled.set()
    else:
        authorized[0] = False
    queued.pop()()
    assert target.read_bytes() == before
    assert reports[-1].startswith(f'WAV saved: {target}')
    assert not any('did not complete' in report for report in reports)
    with wave.open(str(target), 'rb') as audio:
        assert audio.getnframes() == 800


@pytest.mark.parametrize('authorized', [False, True])
def test_recovery_write_rechecks_source_scope_before_modifying_saved_draft(tmp_path, authorized):
    from carr_library.speech.session import ReaderDraftStore, ReaderResume

    document = ReadingDocument('Original text')
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(document))
    before = store.path.read_bytes()
    document.replace(0, 8, 'Edited')

    def guard():
        if not authorized:
            raise ValueError('Source scope is no longer authorized.')

    build = _host_callbacks('''
def build(draft_store, source_guard, sync_document, live, reading_range, editor,
          document, source_locations, ReaderResume):
    alive, source_loaded, last_recovery_signature = True, True, None
    recovered, report = None, lambda message: None
    return save_recovery
''', ['save_recovery'])
    live = SimpleNamespace(voice=SimpleNamespace(GetSelection=lambda: -1), _voices=(),
        rate=SimpleNamespace(GetValue=lambda: 0), volume=SimpleNamespace(GetValue=lambda: 80))
    callback = build(store, guard, lambda: None, live, lambda *args: (0, 0), object(),
                     document, (), ReaderResume)
    if authorized:
        assert callback(force=True)
        assert store.load().document.text == 'Edited text'
    else:
        with pytest.raises(ValueError, match='no longer authorized'):
            callback(force=True)
        assert store.path.read_bytes() == before


def test_saved_project_is_reported_when_later_recovery_update_fails(tmp_path):
    document = ReadingDocument('Original retained in a committed reading project')
    target = tmp_path / 'published.reading.json'
    reports = []

    class Chooser:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def Destroy(self): pass
        def ShowModal(self): return 1
        def GetPath(self): return str(target)

    def unavailable(**kwargs):
        raise OSError('Recovery destination is unavailable.')

    build = _host_callbacks('''
def build(source_guard, sync_document, draft_store, save_recovery, document, wx, dialog, Path, report):
    save_path, alive = None, True
    return save_project
''', ['save_project'])
    callback = build(lambda: None, lambda: None, object(), unavailable, document,
        SimpleNamespace(FileDialog=Chooser, ID_OK=1, FD_SAVE=2), object(), Path, reports.append)
    assert callback(choose=True) is True
    assert ReadingDocument.load(target).source_text == document.source_text
    assert reports[-1].startswith(f'Reading copy and undo history saved: {target}')
    assert 'The recovery draft could not be updated' in reports[-1]


@pytest.mark.parametrize('action', ['save_project', 'open_project', 'save_audio'])
@pytest.mark.parametrize('boundary', ['modal_return', 'chooser_destruction'])
def test_reader_file_chooser_cannot_resume_into_deleted_owner(tmp_path, action, boundary):
    """Run actual save/open/export handlers through a successful stale modal."""
    forbidden, reports, paths = [], [], []
    state = SimpleNamespace(dead=False, detach=None)
    target = tmp_path / 'must-not-publish.json'
    document = ReadingDocument('Immutable original for a detached modal.')
    original = tmp_path / 'original.reading.json'
    document.save(original)
    original_bytes = original.read_bytes()

    def touch(label):
        if state.dead:
            forbidden.append(label)
            raise RuntimeError('Deleted native control: ' + label)

    def detach():
        state.detach()
        state.dead = True

    class Chooser:
        def __init__(self, *args, **kwargs):
            touch('construct chooser')
            self.dead = False
        def __bool__(self):
            return not self.dead
        def ShowModal(self):
            touch('ShowModal')
            if boundary == 'modal_return':
                detach()
                self.dead = True  # Native parent deletion already removed it.
            return 1
        def GetPath(self):
            touch('GetPath')
            paths.append(True)
            return str(target)
        def Destroy(self):
            touch('Destroy')
            assert not self.dead
            if boundary == 'chooser_destruction':
                detach()
            self.dead = True

    build = _host_callbacks("""
def build(document, wx, state, touch, reports, root):
    alive, busy, source_loaded = True, False, True
    appearance = None
    save_path, source_loader, draft_store = None, None, None
    source_locations, recovery_notice = (), ''
    cancel = Event()
    dialog = object()
    source_guard = lambda: touch('source guard')
    sync_document = lambda: touch('editor sync')
    save_recovery = lambda **kwargs: touch('recovery write')
    display_document = lambda: touch('editor display')
    current = lambda: (1, document.text)
    editor = SimpleNamespace(GetSelection=lambda: (0, 0), SetFocus=lambda: touch('editor focus'))
    live = SimpleNamespace(core=SimpleNamespace(busy=False),
        stop=lambda: touch('live stop'), _voice_settings=lambda: ('fixture:voice', 1., 1.))
    reading_range = lambda editor, raw, mode: (0, len(raw))
    def report(message, **kwargs):
        if alive:
            reports.append(message)
    def run(*args, **kwargs):
        touch('submit after modal')
        raise AssertionError('Detached modal submitted new work.')
    return SimpleNamespace(save_project=save_project, open_project=open_project,
        save_audio=save_audio, detach=detach_reader)
""", ['detach_reader', 'save_project', 'open_project', 'save_audio'])
    wx = SimpleNamespace(FileDialog=Chooser, ID_OK=1, FD_SAVE=2, FD_OPEN=4,
                         FD_FILE_MUST_EXIST=8)
    handlers = build(document, wx, state, touch, reports, tmp_path)
    state.detach = handlers.detach
    if action == 'save_project':
        assert handlers.save_project(choose=True) is False
    else:
        getattr(handlers, action)()
    assert state.dead and not forbidden and not reports
    assert paths == ([True] if boundary == 'chooser_destruction' else [])
    assert not target.exists() and original.read_bytes() == original_bytes
    assert document.text == document.source_text and document.revision == 0
