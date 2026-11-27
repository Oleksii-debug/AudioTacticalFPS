"""Explicit local engine configuration; no installers, remote fallback or secrets."""
import os
from pathlib import Path, PureWindowsPath
import re

from .durable_state import atomic_json, read_json


_FIELDS = {'schema', 'ffmpeg', 'ffprobe', 'piper', 'voice_helpers', 'music_models', 'fluidsynth'}
_RESTORE = 'restore_requires_revalidation'


def settings_path(state_root):
    return Path(state_root) / 'audio' / 'engines.json'


def _declared_path(value):
    """Read a saved declaration, including foreign-OS paths, without accessing it."""
    if (not isinstance(value, str) or not value or len(value) > 32768
            or value.startswith(('//', '\\\\')) or '://' in value
            or any(ord(char) < 32 for char in value)):
        raise ValueError('Choose an explicit absolute local engine path.')
    native, windows = Path(value), PureWindowsPath(value)
    if (not native.is_absolute() and not
            (windows.is_absolute() and re.fullmatch('[A-Za-z]:', windows.drive))):
        raise ValueError('Choose an explicit absolute local engine path.')
    if ('..' in native.parts or '..' in windows.parts
            or any(':' in part for part in windows.parts[1:])):
        raise ValueError('Engine paths cannot contain traversal or alternate streams.')


def _settings_document(value):
    if (type(value) is not dict or set(value) - _FIELDS - {_RESTORE}
            or not _FIELDS <= set(value) or type(value['schema']) is not int
            or value['schema'] != 1
            or (_RESTORE in value and type(value[_RESTORE]) is not bool)):
        raise ValueError('Unsupported local engine configuration.')
    for key in ('ffmpeg', 'ffprobe'):
        if value[key] is not None:
            _declared_path(value[key])
    for key in ('piper', 'voice_helpers', 'music_models'):
        if (type(value[key]) is not list or len(value[key]) > 100
                or any(type(item) is not dict for item in value[key])):
            raise ValueError('Invalid local engine declaration list.')
    for item in value['piper']:
        if set(item) != {'executable', 'models'}:
            raise ValueError('Invalid Piper configuration fields.')
        _declared_path(item['executable'])
        if (type(item['models']) is not list or len(item['models']) > 1000
                or any(type(model) is not dict for model in item['models'])):
            raise ValueError('Invalid Piper voice declarations.')
    for item in value['voice_helpers']:
        if (set(item) - {'executable', 'sha256', 'engine_name', 'enabled', 'max_voices'}
                or not {'executable', 'sha256', 'engine_name'} <= set(item)
                or type(item.get('enabled', False)) is not bool):
            raise ValueError('Invalid local voice helper declaration.')
        _declared_path(item['executable'])
    for item in value['music_models']:
        if (set(item) - {'engine', 'python', 'directory', 'manifest', 'device',
                         'private_noncommercial', 'max_chunk_seconds'}
                or not {'engine', 'python', 'directory', 'manifest'} <= set(item)):
            raise ValueError('Invalid local music model declaration.')
        for key in ('python', 'directory', 'manifest'):
            _declared_path(item[key])
    if value['fluidsynth'] is not None:
        item = value['fluidsynth']
        if type(item) is not dict or set(item) != {'binary', 'soundfont', 'sha256', 'license_notice'}:
            raise ValueError('Invalid FluidSynth configuration.')
        _declared_path(item['binary'])
        _declared_path(item['soundfont'])
    return value


def load_settings(state_root):
    """Allow inspection of restored declarations without resolving old asset paths."""
    path = settings_path(state_root)
    value = read_json(path) if os.path.lexists(path) else {
        'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
        'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    return _settings_document(value)


def _enabled_settings(state_root):
    value = load_settings(state_root)
    if value.get(_RESTORE, False):
        raise ValueError('Restored local engine settings require explicit revalidation in '
                         'Local engine settings before any voice or music activation.')
    from .audio.musicgen_worker import plain_local
    for key in ('ffmpeg', 'ffprobe'):
        if value[key] is not None:
            plain_local(value[key])
    return value


def _review_cancellation(cancelled):
    callback = getattr(cancelled, 'is_set', cancelled) if cancelled is not None else lambda: False
    if not callable(callback):
        raise ValueError('Cancellation must be a callback or event.')
    return callback


def _check_review(cancelled):
    if cancelled():
        raise InterruptedError('Local engine review was cancelled; no new settings were saved.')


def _configured_voice_providers(value, *, cancelled=None):
    from .speech import PiperProvider, LocalHelperProvider
    from .audio.musicgen_worker import plain_local
    providers = []
    cancelled = _review_cancellation(cancelled)
    for item in value['piper']:
        _check_review(cancelled)
        plain_local(item['executable'])
        for model in item['models']:
            _check_review(cancelled)
            if not isinstance(model.get('path'), str):
                raise ValueError('Piper voices require an explicit model path.')
            plain_local(model['path'])
            plain_local(model.get('config', model['path'] + '.json'))
        provider = PiperProvider(item['executable'], item['models'], cancelled=cancelled)
        provider.failure_is_fatal = True
        providers.append(provider)
    for item in value['voice_helpers']:
        _check_review(cancelled)
        if item.get('enabled') is True:
            plain_local(item['executable'])
            provider = LocalHelperProvider(**item, cancelled=cancelled)
            provider.failure_is_fatal = True
            providers.append(provider)
    return providers


def _review_fluidsynth(item, *, cancelled=None):
    from .audio.musicgen_worker import plain_local
    from .audio.render import file_sha256, tool_path
    notice = item['license_notice']
    expected = item['sha256']
    if (not isinstance(notice, str) or not notice.strip() or len(notice) > 4000
            or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
        raise ValueError('Supply the reviewed SoundFont hash and license notice.')
    binary = tool_path('fluidsynth', item['binary'])
    plain_local(binary)
    soundfont = plain_local(item['soundfont'])
    if soundfont.suffix.lower() not in ('.sf2', '.sf3') or file_sha256(soundfont, cancelled) != expected:
        raise ValueError('SoundFont identity does not match the reviewed installation.')
    return binary, soundfont


def _registry(state_root, value, *, discover=True):
    from .speech import VoiceRegistry
    from .audio.voice_settings import VoiceSettingsStore
    from .speech.studio_voice_adapter import StudioVoiceProvider
    library_root = Path(state_root).parent
    registration = VoiceSettingsStore(library_root)
    providers = _configured_voice_providers(value)
    if os.path.lexists(registration.path):
        # Once registered, a missing/changed/restored installation is an error.
        # Do not silently return a newly discovered substitute voice catalog.
        provider = StudioVoiceProvider(library_root)
        return VoiceRegistry([provider, *providers])
    registry = VoiceRegistry.discover(ffmpeg=value['ffmpeg']) if discover else VoiceRegistry()
    for provider in providers:
        registry.add_provider(provider)
    return registry


def save_settings(state_root, value, *, cancelled=None):
    """Explicitly review configured assets before clearing a restore barrier."""
    cancelled = _review_cancellation(cancelled)
    _check_review(cancelled)
    value = _settings_document(value)
    from .audio.models import LocalModelConfig
    from .audio.music import validate_model
    from .audio.musicgen_worker import plain_local
    from .audio.render import run_process, tool_path
    for key in ('ffmpeg', 'ffprobe'):
        _check_review(cancelled)
        if value[key] is not None:
            binary = tool_path(key, value[key])
            plain_local(binary)
            run_process([binary, '-version'], timeout_seconds=20, cancelled=cancelled)
    # Enumerate enabled helpers and check complete pinned Piper assets. Merely
    # parsing their paths cannot authorize execution after a restore.
    from .speech import Voice
    seen = set()
    for provider in _configured_voice_providers(value, cancelled=cancelled):
        voices = provider.voices(cancelled=cancelled)
        if len(voices) > 10000:
            raise ValueError('The configured voice provider returned too many voices.')
        for voice in voices:
            _check_review(cancelled)
            if not isinstance(voice, Voice) or voice.id in seen:
                raise ValueError('Duplicate or invalid configured voice identity.')
            seen.add(voice.id)
    for model in value['music_models']:
        validate_model(LocalModelConfig(**model), cancelled=cancelled)
    if value['fluidsynth'] is not None:
        binary, _ = _review_fluidsynth(value['fluidsynth'], cancelled=cancelled)
        run_process([binary, '--version'], timeout_seconds=20, cancelled=cancelled)
    candidate = {**value}
    if _RESTORE in candidate:
        candidate[_RESTORE] = False
    path = settings_path(state_root)
    _check_review(cancelled)
    atomic_json(path, candidate, replace=os.path.lexists(path))
    return candidate


def voice_registry(state_root):
    return _registry(state_root, _enabled_settings(state_root))


def audio_runtime(directory, *, state_root, input_roots=(), budget=None):
    from .audio import AudioRuntime
    from .audio.models import LocalModelConfig
    value = _enabled_settings(state_root)
    runtime = AudioRuntime(directory, ffmpeg=value['ffmpeg'], ffprobe=value['ffprobe'],
                           input_roots=input_roots, budget=budget)
    for item in value['music_models']:
        runtime.configure_music_model(LocalModelConfig(**item))
    if value['fluidsynth'] is not None:
        # This is the explicitly registered global SoundFont, not an arbitrary
        # project input. Verify it before assigning this one approved asset.
        binary, soundfont = _review_fluidsynth(value['fluidsynth'])
        runtime.fluidsynth = binary
        runtime.soundfont = soundfont
        runtime.soundfont_sha256 = value['fluidsynth']['sha256']
        runtime.soundfont_license = value['fluidsynth']['license_notice']
    return runtime
