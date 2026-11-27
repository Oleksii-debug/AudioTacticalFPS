"""Explicit local engine configuration; no installers, remote fallback or secrets."""
from dataclasses import asdict
from pathlib import Path

from .durable_state import atomic_json, local_path, read_json


def settings_path(state_root):
    return Path(state_root) / 'audio' / 'engines.json'


def load_settings(state_root):
    path = settings_path(state_root)
    value = read_json(path) if path.exists() else {'schema': 1, 'ffmpeg': None, 'ffprobe': None,
        'piper': [], 'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    if (not isinstance(value, dict) or set(value) != {'schema','ffmpeg','ffprobe','piper','voice_helpers','music_models','fluidsynth'}
            or value['schema'] != 1 or any(not isinstance(value[key], list) for key in ('piper','voice_helpers','music_models'))):
        raise ValueError('Unsupported local engine configuration.')
    if any(len(value[key]) > 100 for key in ('piper','voice_helpers','music_models')):
        raise ValueError('Too many local engine configurations.')
    for key in ('ffmpeg', 'ffprobe'):
        if value[key] is not None:
            local_path(value[key], exists=True)
    return value


def save_settings(state_root, value):
    # Validate all declarations before changing the last good configuration.
    if not isinstance(value, dict) or value.get('schema') != 1:
        raise ValueError('Local engine schema 1 is required.')
    for key in ('ffmpeg', 'ffprobe'):
        if value.get(key) is not None:
            path = local_path(value[key], exists=True)
            if not path.is_file():
                raise ValueError('Choose a local executable file.')
    if set(value) != {'schema','ffmpeg','ffprobe','piper','voice_helpers','music_models','fluidsynth'}:
        raise ValueError('Unknown local engine setting.')
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


def voice_registry(state_root):
    from .speech import VoiceRegistry, PiperProvider, LocalHelperProvider
    value = load_settings(state_root)
    registry = VoiceRegistry.discover(ffmpeg=value['ffmpeg'])
    for item in value['piper']:
        if set(item) != {'executable', 'models'}:
            raise ValueError('Invalid Piper configuration fields.')
        registry.add_provider(PiperProvider(item['executable'], item['models']))
    for item in value['voice_helpers']:
        if item.get('enabled') is True:
            registry.add_provider(LocalHelperProvider(**item))
    return registry


def audio_runtime(directory, *, state_root, input_roots=(), budget=None):
    from .audio import AudioRuntime
    from .audio.models import LocalModelConfig
    value = load_settings(state_root)
    runtime = AudioRuntime(directory, ffmpeg=value['ffmpeg'], ffprobe=value['ffprobe'],
                           input_roots=input_roots, budget=budget)
    for item in value['music_models']:
        runtime.configure_music_model(LocalModelConfig(**item))
    if value['fluidsynth'] is not None:
        item = value['fluidsynth']
        if not isinstance(item, dict) or set(item) != {'binary','soundfont','sha256','license_notice'}:
            raise ValueError('Invalid FluidSynth configuration.')
        runtime.configure_fluidsynth(**item)
    return runtime
