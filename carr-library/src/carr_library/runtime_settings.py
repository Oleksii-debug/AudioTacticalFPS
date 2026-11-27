"""Explicit local engine configuration; no installers, remote fallback or secrets."""
from dataclasses import asdict
from pathlib import Path
import re

from .durable_state import atomic_json, local_path, read_json


def settings_path(state_root):
    return Path(state_root) / 'audio' / 'engines.json'


def validate_dordiai_configuration(value):
    """Validate saved declarations without probing a private installation."""
    if value is None:
        return None
    allowed = {'enabled', 'runtime_directory', 'python_executable', 'expected_worker_sha256'}
    if (not isinstance(value, dict) or not set(value) <= allowed
            or type(value.get('enabled')) is not bool):
        raise ValueError('A dordiai-voices configuration needs an explicit enabled boolean.')
    for name in ('runtime_directory', 'python_executable'):
        selected = value.get(name)
        if selected is None and (name == 'python_executable' or not value['enabled']):
            continue
        if (not isinstance(selected, str) or not 1 <= len(selected) <= 4096
                or '\x00' in selected or selected.startswith(('//', '\\\\'))):
            raise ValueError('Choose a local installed dordiai-voices runtime and interpreter.')
    expected = value.get('expected_worker_sha256')
    if 'expected_worker_sha256' in value and (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
        raise ValueError('The reviewed dordiai-voices worker fingerprint must be a lowercase SHA-256.')
    return dict(value)


def _normalize_settings(value):
    if not isinstance(value, dict):
        raise ValueError('Unsupported local engine configuration.')
    value = dict(value)
    # Existing schema-1 installations remain readable without changing their file.
    value.setdefault('dordiai', None)
    if (set(value) != {'schema','ffmpeg','ffprobe','piper','voice_helpers','music_models','fluidsynth','dordiai'}
            or type(value['schema']) is not int or value['schema'] != 1
            or any(not isinstance(value[key], list) for key in ('piper','voice_helpers','music_models'))):
        raise ValueError('Unsupported local engine configuration.')
    if any(len(value[key]) > 100 for key in ('piper','voice_helpers','music_models')):
        raise ValueError('Too many local engine configurations.')
    value['dordiai'] = validate_dordiai_configuration(value['dordiai'])
    return value


def load_settings(state_root):
    path = settings_path(state_root)
    value = read_json(path) if path.exists() else {'schema': 1, 'ffmpeg': None, 'ffprobe': None,
        'piper': [], 'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    value = _normalize_settings(value)
    for key in ('ffmpeg', 'ffprobe'):
        if value[key] is not None:
            local_path(value[key], exists=True)
    return value


def save_settings(state_root, value):
    # Validate all declarations before changing the last good configuration.
    value = _normalize_settings(value)
    for key in ('ffmpeg', 'ffprobe'):
        if value.get(key) is not None:
            path = local_path(value[key], exists=True)
            if not path.is_file():
                raise ValueError('Choose a local executable file.')
    from .audio.models import LocalModelConfig
    for model in value['music_models']:
        if not isinstance(model, dict):
            raise ValueError('Invalid local model declaration.')
        LocalModelConfig(**model)
    for collection in ('piper', 'voice_helpers'):
        if not isinstance(value[collection], list) or len(value[collection]) > 100:
            raise ValueError('Invalid local voice configuration.')
        for item in value[collection]:
            if not isinstance(item, dict):
                raise ValueError('Invalid local voice declaration.')
            local_path(item.get('executable', ''), exists=True)
    path = settings_path(state_root)
    atomic_json(path, value, replace=path.exists())


def _selected_audio_tools(settings):
    """Use the sealed installation for defaults; retain explicit selections.

    The existing verifier checks the complete inner/outer runtime identity.
    An invalid declared bundle must fail before ordinary PATH discovery.
    """
    from .audio_runtime import bundled_audio_tools
    bundled = bundled_audio_tools()
    return tuple(settings[name] if settings[name] is not None or bundled is None
                 else bundled[name] for name in ('ffmpeg', 'ffprobe'))


def voice_registry(state_root, *, cancelled=lambda: False):
    from .speech import VoiceRegistry, PiperProvider, LocalHelperProvider
    value = load_settings(state_root)
    ffmpeg, _ffprobe = _selected_audio_tools(value)
    registry = VoiceRegistry.discover(ffmpeg=ffmpeg, cancelled=cancelled)
    try:
        from .integrations.dordiai_speech import install_dordiai_provider
        install_dordiai_provider(registry, value['dordiai'], cancelled=cancelled)
        for item in value['piper']:
            if set(item) != {'executable', 'models'}:
                raise ValueError('Invalid Piper configuration fields.')
            registry.add_provider(PiperProvider(item['executable'], item['models']), cancelled=cancelled)
        for item in value['voice_helpers']:
            if item.get('enabled') is True:
                registry.add_provider(LocalHelperProvider(**item), cancelled=cancelled)
        return registry
    except BaseException:
        registry.close()
        raise


def audio_runtime(directory, *, state_root, input_roots=(), budget=None):
    from .audio import AudioRuntime
    from .audio.models import LocalModelConfig
    value = load_settings(state_root)
    ffmpeg, ffprobe = _selected_audio_tools(value)
    runtime = AudioRuntime(directory, ffmpeg=ffmpeg, ffprobe=ffprobe,
                           input_roots=input_roots, budget=budget)
    for item in value['music_models']:
        runtime.configure_music_model(LocalModelConfig(**item))
    if value['fluidsynth'] is not None:
        item = value['fluidsynth']
        if not isinstance(item, dict) or set(item) != {'binary','soundfont','sha256','license_notice'}:
            raise ValueError('Invalid FluidSynth configuration.')
        runtime.configure_fluidsynth(**item)
    return runtime
