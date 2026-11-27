"""Drive the actual engine-settings callbacks through supplied native answers.

Controls are synthetic here. Library objects, directories, settings JSON and
its durable writes are real; this is not native wx/NVDA or engine inference.
"""
from hashlib import sha256
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library import runtime_settings
from carr_library.integrations import dordiai_voices
from carr_library.ui import engine_settings


LABELS = {
    'executable': 'Choose FFmpeg executable',
    'registration': 'Register a reviewed local voice helper',
    'dordiai': 'Connect installed &dordiai-voices',
    'disable': 'Disable dordiai-voices',
    'reset': 'Reset overrides',
}


class NativeAnswers:
    def __init__(self, monkeypatch, frame, selected):
        self.frame, self.selected = frame, selected
        self.buttons, self.status_values, self.pickers = {}, [], []
        self.on_open = self.on_picker = self.on_confirmation = lambda: None
        self.confirmations, self.path_reads = 0, 0
        owner = self
        wx = ModuleType('wx')
        for index, name in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'TE_MULTILINE',
                'TE_READONLY', 'EXPAND', 'ALL', 'FD_OPEN', 'FD_FILE_MUST_EXIST', 'DD_DEFAULT_STYLE',
                'DD_DIR_MUST_EXIST', 'YES_NO', 'NO_DEFAULT', 'CLOSE'), 1):
            setattr(wx, name, 1 << index)
        wx.ID_OK, wx.ID_CLOSE, wx.YES = 5100, 5104, 5103
        wx.EVT_BUTTON, wx.EVT_CLOSE, wx.EVT_WINDOW_DESTROY = 'button', 'close', 'destroy'

        class Dialog:
            def __init__(self, *_args, **_kwargs):
                self.handlers = {}
                self.destroyed = False
                owner.dialog = self
            def Bind(self, event, callback, **_kwargs): self.handlers[event] = callback
            def SetSizer(self, *_args): pass
            def SetEscapeId(self, *_args): pass
            def CreateButtonSizer(self, *_args): return object()
            def ShowModal(self):
                owner.on_open()
                return wx.ID_CLOSE
            def EndModal(self, *_args): pass
            def Destroy(self): self.destroyed = True

        class Text:
            def __init__(self, *_args, **_kwargs): pass
            def SetName(self, name): owner.status_name = name
            def ChangeValue(self, value): owner.status_values.append(value)

        class Button:
            def __init__(self, _parent, *, label): self.label = label
            def Bind(self, _event, callback): owner.buttons[self.label] = callback

        class Layout:
            def __init__(self, *_args): pass
            def Add(self, *_args, **_kwargs): pass

        class Picker:
            def __init__(self, *_args, **_kwargs):
                self.destroyed = False
                owner.pickers.append(self)
            def ShowModal(self):
                owner.on_picker()
                return wx.ID_OK
            def GetPath(self):
                owner.path_reads += 1
                return str(owner.selected)
            def Destroy(self): self.destroyed = True

        def confirm(*_args, **_kwargs):
            owner.confirmations += 1
            owner.on_confirmation()
            return wx.YES

        wx.Dialog, wx.TextCtrl, wx.Button = Dialog, Text, Button
        wx.BoxSizer, wx.FileDialog, wx.DirDialog, wx.MessageBox = Layout, Picker, Picker, confirm
        self.wx = wx
        monkeypatch.setitem(sys.modules, 'wx', wx)

    def press(self, action):
        self.buttons[LABELS[action]](SimpleNamespace())


@pytest.fixture
def owner(tmp_path):
    executable = tmp_path / 'installed-helper.fixture'
    executable.write_bytes(b'Public settings boundary; never executed.')
    engine = tmp_path / 'owner-engine'
    engine.mkdir()
    registration = tmp_path / 'registration.json'
    registration.write_text(json.dumps({'executable': str(executable),
        'sha256': sha256(executable.read_bytes()).hexdigest(), 'engine_name': 'Synthetic registration'}),
        encoding='utf-8')
    library = LibraryApp(tmp_path / 'library')
    root = Path(library.root)
    settings = runtime_settings.load_settings(root / 'state')
    settings.update(ffmpeg=str(executable), dordiai={'enabled': True, 'runtime_directory': str(engine)})
    runtime_settings.save_settings(root / 'state', settings)
    path = runtime_settings.settings_path(root / 'state')
    frame = SimpleNamespace(library=library, _closing=False)
    return SimpleNamespace(frame=frame, library=library, root=root, original_root=root,
        before=path.read_bytes(), executable=executable, engine=engine, registration=registration,
        tmp_path=tmp_path)


def invalidate(owner, mode):
    if mode == 'other-library-object':
        owner.frame.library = LibraryApp(owner.root)
    elif mode == 'same-path-replacement':
        moved = owner.root.with_name('original-library')
        owner.root.rename(moved)
        owner.root.mkdir()
        owner.original_root = moved
        assert Path(owner.library.root) == owner.root
    elif mode == 'closing':
        owner.frame._closing = True
    else:
        raise AssertionError(mode)


def unchanged(owner):
    assert runtime_settings.settings_path(owner.original_root / 'state').read_bytes() == owner.before
    if owner.original_root != owner.root:
        assert not runtime_settings.settings_path(owner.root / 'state').exists()


def selected(owner, action):
    return owner.registration if action == 'registration' else owner.engine if action == 'dordiai' else owner.executable


@pytest.mark.parametrize('action', ['executable', 'registration', 'dordiai'])
@pytest.mark.parametrize('change', ['other-library-object', 'same-path-replacement', 'closing'])
def test_picker_return_cannot_write_or_probe_a_stale_library(owner, monkeypatch, action, change):
    answers = NativeAnswers(monkeypatch, owner.frame, selected(owner, action))
    adapter_calls = []
    monkeypatch.setattr(dordiai_voices, 'DordiaiVoicesAdapter',
        lambda *_a, **_k: adapter_calls.append(True))
    answers.on_picker = lambda: invalidate(owner, change)
    answers.on_open = lambda: answers.press(action)
    engine_settings.show_engine_settings(owner.frame)
    unchanged(owner)
    assert answers.path_reads == 0 and answers.confirmations == 0 and adapter_calls == []
    assert len(answers.status_values) == 1
    assert answers.dialog.destroyed and all(item.destroyed for item in answers.pickers)


@pytest.mark.parametrize('action', ['registration', 'dordiai', 'reset'])
@pytest.mark.parametrize('change', ['other-library-object', 'same-path-replacement', 'closing'])
def test_confirmation_return_rechecks_original_owner_before_side_effects(owner, monkeypatch, action, change):
    answers = NativeAnswers(monkeypatch, owner.frame, selected(owner, action))
    adapter_calls = []
    monkeypatch.setattr(dordiai_voices, 'DordiaiVoicesAdapter',
        lambda *_a, **_k: adapter_calls.append(True))
    answers.on_confirmation = lambda: invalidate(owner, change)
    answers.on_open = lambda: answers.press(action)
    engine_settings.show_engine_settings(owner.frame)
    unchanged(owner)
    assert answers.confirmations == 1 and adapter_calls == []
    assert len(answers.status_values) == 1


@pytest.mark.parametrize('action', ['registration', 'dordiai'])
def test_external_metadata_return_cannot_commit_to_a_replaced_original_root(owner, monkeypatch, action):
    answers = NativeAnswers(monkeypatch, owner.frame, selected(owner, action))
    releases = []
    if action == 'registration':
        original = engine_settings.read_json
        def read(path):
            result = original(path)
            invalidate(owner, 'same-path-replacement')
            return result
        monkeypatch.setattr(engine_settings, 'read_json', read)
    else:
        class Adapter:
            def __init__(self, path):
                assert path == str(owner.engine)
            def close(self):
                releases.append(True)
                invalidate(owner, 'same-path-replacement')
        monkeypatch.setattr(dordiai_voices, 'DordiaiVoicesAdapter', Adapter)
    answers.on_open = lambda: answers.press(action)
    engine_settings.show_engine_settings(owner.frame)
    unchanged(owner)
    assert len(answers.status_values) == 1
    assert releases == ([True] if action == 'dordiai' else [])
    assert answers.confirmations == (1 if action == 'dordiai' else 0)


@pytest.mark.parametrize('event', ['close', 'destroy'])
@pytest.mark.parametrize('action', ['executable', 'registration', 'dordiai', 'disable'])
def test_late_callback_after_dialog_lifetime_has_no_reads_writes_or_control_updates(owner, monkeypatch, event, action):
    answers = NativeAnswers(monkeypatch, owner.frame, selected(owner, action))
    skipped = []
    def on_open():
        callback = answers.dialog.handlers[event]
        callback(SimpleNamespace(GetEventObject=lambda: answers.dialog, Skip=lambda: skipped.append(True)))
        answers.press(action)
    answers.on_open = on_open
    engine_settings.show_engine_settings(owner.frame)
    unchanged(owner)
    assert answers.pickers == [] and answers.confirmations == 0 and answers.path_reads == 0
    assert len(answers.status_values) == 1
    assert skipped == ([True] if event == 'destroy' else [])


@pytest.mark.parametrize('action', ['executable', 'registration', 'dordiai', 'disable', 'reset'])
def test_current_explicit_choice_still_persists_through_existing_shared_settings(owner, monkeypatch, action):
    new_executable = owner.tmp_path / 'new-helper.fixture'
    new_executable.write_bytes(b'Explicit public path; never executed.')
    chosen = new_executable if action == 'executable' else selected(owner, action)
    answers = NativeAnswers(monkeypatch, owner.frame, chosen)
    adapter_calls = []
    class Adapter:
        def __init__(self, path):
            adapter_calls.append(('construct', path))
        def close(self):
            adapter_calls.append(('close',))
    monkeypatch.setattr(dordiai_voices, 'DordiaiVoicesAdapter', Adapter)
    answers.on_open = lambda: answers.press(action)
    engine_settings.show_engine_settings(owner.frame)
    result = runtime_settings.load_settings(owner.root / 'state')
    if action == 'executable':
        assert result['ffmpeg'] == str(new_executable)
    elif action == 'registration':
        assert result['voice_helpers'] == [{**json.loads(owner.registration.read_text(encoding='utf-8')), 'enabled': True}]
    elif action == 'dordiai':
        assert result['dordiai'] == {'enabled': True, 'runtime_directory': str(owner.engine)}
        assert adapter_calls == [('construct', str(owner.engine)), ('close',)]
    elif action == 'disable':
        assert result['dordiai'] is None and owner.engine.is_dir()
    else:
        assert result == {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
                          'voice_helpers': [], 'music_models': [], 'fluidsynth': None, 'dordiai': None}
        assert owner.executable.exists() and owner.engine.is_dir()
    assert len(answers.status_values) == 2
    assert answers.status_name == 'Local engine configuration and availability'
    assert answers.dialog.destroyed and all(item.destroyed for item in answers.pickers)
