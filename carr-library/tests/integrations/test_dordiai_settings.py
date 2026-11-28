"""Actual persisted configuration and shared-registry lifecycle, using synthetic PCM."""
from hashlib import sha256
import ast
import json
import runpy
from pathlib import Path
import sys

import pytest

from carr_library import runtime_settings as settings
from carr_library.integrations import dordiai_speech
from carr_library.speech import SpeechSettings, VoiceRegistry
# The authored fixture is shared without a test-package or product-path override.
_fixture = runpy.run_path(str(Path(__file__).with_name('test_dordiai_voices.py')))
ACTOR, runtime = _fixture['ACTOR'], _fixture['runtime']


def configured(runtime):
    return {'enabled': True, 'runtime_directory': str(runtime), 'python_executable': str(Path(sys.executable).resolve()),
            'expected_worker_sha256': sha256(ACTOR.encode()).hexdigest()}


def no_platform_voices(monkeypatch):
    monkeypatch.setattr(VoiceRegistry, 'discover', classmethod(lambda cls, **_: cls()))


def test_legacy_settings_are_normalized_without_rewriting_or_mutating_input(tmp_path):
    state = tmp_path / 'state'
    path = settings.settings_path(state)
    legacy = settings.load_settings(state)
    legacy.pop('dordiai')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy), encoding='utf8')
    before = path.read_bytes()
    assert settings.load_settings(state)['dordiai'] is None
    assert path.read_bytes() == before
    settings.save_settings(state, legacy)
    assert 'dordiai' not in legacy
    assert settings.load_settings(state)['dordiai'] is None


@pytest.mark.parametrize('declaration', [None, {'enabled': False},
                                      {'enabled': False, 'runtime_directory': '/missing/private/installation'}])
def test_disabled_saved_engine_never_probes_private_paths(tmp_path, monkeypatch, declaration):
    no_platform_voices(monkeypatch)
    monkeypatch.setattr(dordiai_speech, 'DordiaiSpeechProvider',
                        lambda *_a, **_k: pytest.fail('Disabled configuration launched a provider.'))
    value = settings.load_settings(tmp_path)
    value['dordiai'] = declaration
    settings.save_settings(tmp_path, value)
    registry = settings.voice_registry(tmp_path)
    assert registry.voices() == ()
    registry.close()


@pytest.mark.parametrize('invalid', [True, {}, {'enabled': 1}, {'enabled': True},
    {'enabled': True, 'runtime_directory': '//network/share'},
    {'enabled': True, 'runtime_directory': '\\\\host\\share'},
    {'enabled': True, 'runtime_directory': '/tmp/local', 'expected_worker_sha256': 'unreviewed'},
    {'enabled': True, 'runtime_directory': '/tmp/local', 'expected_worker_sha256': None},
    {'enabled': False, 'remote_url': 'https://invalid.example'},
    {'enabled': True, 'runtime_directory': '/tmp/local', 'python_executable': ''}])
def test_bad_declaration_preserves_last_saved_configuration(tmp_path, invalid):
    value = settings.load_settings(tmp_path)
    settings.save_settings(tmp_path, value)
    before = settings.settings_path(tmp_path).read_bytes()
    value['dordiai'] = invalid
    with pytest.raises(ValueError):
        settings.save_settings(tmp_path, value)
    assert settings.settings_path(tmp_path).read_bytes() == before


def test_saved_engine_reaches_actual_shared_render_and_releases_worker(tmp_path, runtime, monkeypatch):
    no_platform_voices(monkeypatch)
    state = tmp_path / 'library-state'
    value = settings.load_settings(state)
    value['dordiai'] = configured(runtime)
    settings.save_settings(state, value)
    registry = settings.voice_registry(state)
    provider = registry._providers[0]
    try:
        voice = next(item for item in registry.voices() if item.language == 'uk')
        rendered = registry.synthesize('Saved configuration fixture.', voice.id, tmp_path / 'actual.wav',
                                       settings=SpeechSettings(volume=65, rate=-1, pitch=54))
        assert (rendered.sample_rate, rendered.frames, rendered.channels) == (24000, 6, 1)
        process = provider.adapter._worker.process
        assert process.poll() is None
        request = json.loads((runtime / 'requests.jsonl').read_text().splitlines()[0])
        assert request['controls'] == {'volume': 65, 'pitch': 54, 'timbre': 50}
        assert request['speed'] == 2 ** (-1 / 10)
        assert settings.load_settings(state)['dordiai'] == configured(runtime)
    finally:
        registry.close()
    assert process.poll() is not None
    assert provider.adapter._closed
    assert not registry.voices()
    registry.close()  # Idempotent cleanup of a closed dialog.


def test_failed_later_provider_releases_already_registered_private_engine(tmp_path, runtime, monkeypatch):
    no_platform_voices(monkeypatch)
    owned = []
    original = dordiai_speech.DordiaiSpeechProvider
    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        owned.append(value)
        return value
    monkeypatch.setattr(dordiai_speech, 'DordiaiSpeechProvider', capture)
    value = settings.load_settings(tmp_path)
    value['dordiai'] = configured(runtime)
    marker = tmp_path / 'synthetic-piper-runtime'
    marker.write_bytes(b'Synthetic local path marker; never executed.')
    value['piper'] = [{'executable': str(marker), 'models': {}}]
    settings.save_settings(tmp_path, value)
    # Distinguish a broken later configuration from a silent successful registry.
    from carr_library import speech
    def fail(*_args, **_kwargs):
        raise ValueError('Synthetic later-provider configuration failure.')
    monkeypatch.setattr(speech, 'PiperProvider', fail)
    with pytest.raises(ValueError, match='later-provider'):
        settings.voice_registry(tmp_path)
    assert len(owned) == 1 and owned[0].adapter._closed


def test_registry_closes_remaining_workers_after_one_cleanup_error():
    calls = []
    blocked = [True]
    class Provider:
        def __init__(self, name):
            self.name = name
        def voices(self):
            return ()
        def close(self):
            calls.append(self.name)
            if self.name == 'first' and blocked[0]:
                raise OSError('Synthetic close failure.')
    registry = VoiceRegistry([Provider('first'), Provider('second')])
    assert registry.close() is False
    assert calls == ['first', 'second']
    assert len(registry.notices) == 1 and 'first' in registry.notices[0]
    assert len(registry._providers) == 1 and registry._providers[0].name == 'first'
    blocked[0] = False
    assert registry.close() is True
    assert calls == ['first', 'second', 'first']
    assert not registry._providers
    assert registry.close() is True
    assert calls == ['first', 'second', 'first']


def test_native_connect_writes_only_canonical_settings_and_preserves_legacy_until_explicit_disable(
        tmp_path, runtime, monkeypatch):
    from carr_library.speech import dordiai, dordiai_settings
    from carr_library.ui.dordiai_settings import connect_runtime, disable_runtime
    no_platform_voices(monkeypatch)
    root = tmp_path / 'library'
    root.mkdir()
    legacy = dordiai_settings.default_settings()
    legacy.update(configured(runtime))
    dordiai_settings.save_settings(root, legacy)
    raw = (root / dordiai_settings.FILENAME).read_bytes()
    old = dordiai.DordiaiProvider(root)
    try:
        old_ids = {voice.id for voice in old.voices()}
        with pytest.raises(ValueError, match='Explicitly disable the legacy'):
            connect_runtime(root, str(runtime),
                            python_executable=str(Path(sys.executable).resolve()),
                            expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
        assert (root / dordiai_settings.FILENAME).read_bytes() == raw
        assert not settings.settings_path(root / 'state').exists()
        assert disable_runtime(root) == {**legacy, 'enabled': False}
        assert dordiai_settings.load_settings(root) == {**legacy, 'enabled': False}
        disabled_raw = (root / dordiai_settings.FILENAME).read_bytes()
        saved = connect_runtime(root, str(runtime),
                                python_executable=str(Path(sys.executable).resolve()),
                                expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
        assert (root / dordiai_settings.FILENAME).read_bytes() == disabled_raw
        assert settings.load_settings(root / 'state')['dordiai'] == {
            key: value for key, value in saved.items() if key != 'version'}
        current = settings.voice_registry(root / 'state')
        try:
            assert len(current.voices()) == len(old_ids) == 5
            assert old_ids.isdisjoint(voice.id for voice in current.voices())
            for identity in old_ids:
                with pytest.raises(ValueError, match='available installed voice'):
                    current.get(identity)
        finally:
            assert current.close() is True
    finally:
        old.close()


def test_two_enabled_declarations_fail_before_discovery_or_runtime_activation(
        tmp_path, runtime, monkeypatch):
    from carr_library.speech import dordiai_settings
    root = tmp_path / 'library'
    root.mkdir()
    legacy = dordiai_settings.default_settings()
    legacy.update(configured(runtime))
    dordiai_settings.save_settings(root, legacy)
    value = settings.load_settings(root / 'state')
    value['dordiai'] = configured(runtime)
    settings.save_settings(root / 'state', value)
    canonical = settings.settings_path(root / 'state').read_bytes()
    historical = (root / dordiai_settings.FILENAME).read_bytes()
    monkeypatch.setattr(VoiceRegistry, 'discover',
        lambda *args, **kwargs: pytest.fail('Ambiguous declarations started discovery.'))
    with pytest.raises(ValueError, match='Both canonical and legacy'):
        settings.voice_registry(root / 'state')
    assert settings.settings_path(root / 'state').read_bytes() == canonical
    assert (root / dordiai_settings.FILENAME).read_bytes() == historical


def test_canonical_disable_signals_only_owned_library_even_when_save_fails(tmp_path, runtime, monkeypatch):
    from carr_library.speech import dordiai
    from carr_library.ui import dordiai_settings as controls
    root, other = tmp_path / 'library', tmp_path / 'unrelated-library'
    root.mkdir()
    other.mkdir()
    controls.connect_runtime(root, str(runtime),
        python_executable=str(Path(sys.executable).resolve()),
        expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    before = settings.settings_path(root / 'state').read_bytes()
    def blocked(*args, **kwargs):
        raise PermissionError('Synthetic canonical preference failure')
    monkeypatch.setattr(settings, 'save_settings', blocked)
    with dordiai._cancellation(root) as owned, dordiai._cancellation(other) as unrelated:
        with pytest.raises(PermissionError, match='canonical preference'):
            controls.disable_runtime(root)
        with pytest.raises(InterruptedError):
            owned()
        assert unrelated() is False
    assert settings.settings_path(root / 'state').read_bytes() == before


def test_canonical_disable_missing_runtime_never_probes_external_paths(tmp_path, monkeypatch):
    from carr_library.ui import dordiai_settings as controls
    root = tmp_path / 'library'
    root.mkdir()
    value = settings.load_settings(root / 'state')
    declaration = {'enabled': True, 'runtime_directory': str(root / 'missing-selected-runtime'),
                   'python_executable': str(root / 'missing-selected-python'),
                   'expected_worker_sha256': 'a' * 64}
    value['dordiai'] = declaration
    settings.save_settings(root / 'state', value)
    monkeypatch.setattr(dordiai_speech, 'DordiaiSpeechProvider',
        lambda *args, **kwargs: pytest.fail('Disable probed a missing runtime.'))
    result = controls.disable_runtime(root)
    assert result['enabled'] is False
    assert settings.load_settings(root / 'state')['dordiai'] == {**declaration, 'enabled': False}
    assert not (root / 'dordiai-voices.json').exists()


@pytest.mark.parametrize('legacy', [False, True], ids=['canonical', 'retained-legacy'])
def test_selected_provider_outlives_completed_discovery_deadline(
        tmp_path, runtime, monkeypatch, legacy):
    from carr_library.speech import dordiai_settings
    no_platform_voices(monkeypatch)
    root = tmp_path / 'library'
    root.mkdir()
    clock = [0.0]
    monkeypatch.setattr(settings, 'monotonic', lambda: clock[0])
    if legacy:
        declaration = dordiai_settings.default_settings()
        declaration.update(configured(runtime))
        dordiai_settings.save_settings(root, declaration)
    else:
        value = settings.load_settings(root / 'state')
        value['dordiai'] = configured(runtime)
        settings.save_settings(root / 'state', value)
    registry = settings.voice_registry(root / 'state', timeout_seconds=1)
    try:
        clock[0] = 600.0
        voice = next(item for item in registry.voices() if item.language == 'uk')
        result = registry.synthesize('A later explicitly requested utterance.', voice.id,
                                     tmp_path / 'later.wav')
        assert result.frames == 6 and result.path.is_file()
    finally:
        assert registry.close() is True


@pytest.mark.parametrize('path', ['/inactive/foreign/runtime', r'C:\Inactive\foreign\runtime'])
def test_inert_dordiai_declarations_do_not_depend_on_the_host_path_class(path, monkeypatch):
    from carr_library.speech import dordiai_settings
    declaration = {'enabled': False, 'runtime_directory': path,
                   'python_executable': None, 'expected_worker_sha256': 'a' * 64}
    def forbidden(*args, **kwargs):
        pytest.fail('Passive declaration validation consulted the host filesystem path class.')
    monkeypatch.setattr(dordiai_settings, 'Path', forbidden)
    assert settings.validate_dordiai_configuration(declaration) == declaration


@pytest.mark.parametrize('owner_alive', [False, True], ids=['owner-deleted', 'picker-only-deleted'])
@pytest.mark.parametrize('surface,callback,argument', [
    ('dordiai', 'choose_directory', None), ('dordiai', 'choose_python', None),
    ('engines', 'executable', 'ffmpeg'), ('engines', 'register', 'piper')])
def test_settings_picker_return_does_not_read_or_destroy_a_deleted_peer(
        tmp_path, owner_alive, surface, callback, argument):
    from types import SimpleNamespace
    from carr_library.ui import dordiai_settings as dordiai_ui, engine_settings
    module = dordiai_ui if surface == 'dordiai' else engine_settings
    source = Path(module.__file__).read_text(encoding='utf-8')
    outer_name = 'show_dordiai_settings' if surface == 'dordiai' else 'show_engine_settings'
    outer = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name == outer_name)
    selected = next(node for node in outer.body
                    if isinstance(node, ast.FunctionDef) and node.name == callback)
    forbidden_access, owner_live = [], [True]
    class Picker:
        alive = True
        def __bool__(self):
            return self.alive
        def ShowModal(self):
            self.alive = False
            owner_live[0] = owner_alive
            return 1
        def GetPath(self):
            forbidden_access.append('deleted picker path')
            raise RuntimeError('deleted native picker')
        def Destroy(self):
            forbidden_access.append('deleted picker destruction')
            raise RuntimeError('deleted native picker')
    picker = Picker()
    def forbidden(*args, **kwargs):
        forbidden_access.append('late view or settings write')
        raise AssertionError('A deleted picker cannot approve settings.')
    wx = SimpleNamespace(ID_OK=1, FD_OPEN=1, FD_FILE_MUST_EXIST=2,
        DD_DEFAULT_STYLE=0, DD_DIR_MUST_EXIST=1,
        DirDialog=lambda *args, **kwargs: picker,
        FileDialog=lambda *args, **kwargs: picker)
    namespace = dict(wx=wx, dialog=object(), active=lambda: owner_live[0],
        settings={}, state_root=tmp_path, save_settings=forbidden,
        describe=forbidden, status=SimpleNamespace(ChangeValue=forbidden),
        read_json=forbidden, Path=Path)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[selected], type_ignores=[])),
                 module.__file__, 'exec'), namespace)
    control = SimpleNamespace(GetValue=lambda: '', ChangeValue=forbidden)
    namespace[callback](control if argument is None else argument)
    assert forbidden_access == []
