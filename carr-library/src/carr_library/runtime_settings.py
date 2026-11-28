"""Explicit local engine configuration; no installers, remote fallback or secrets."""
from dataclasses import asdict
import math
from pathlib import Path
import re
from time import monotonic

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
    # Reuse the current passive path contract as well: absolute local paths,
    # no network/device components, controls or traversal. This performs no I/O.
    from .speech.dordiai_settings import default_settings, validate_settings
    declaration = default_settings()
    declaration.update(value)
    validate_settings(declaration)
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
    previous = (_normalize_settings(read_json(path))['dordiai'] if path.exists() else None)
    changed = previous != value['dordiai']
    if changed:
        from .speech.dordiai import cancel_active
        cancel_active(Path(state_root).absolute().parent)
    try:
        atomic_json(path, value, replace=path.exists())
    finally:
        if changed:
            # A failed persistence attempt must not silently leave an already
            # requested Disable/selection change running in this library.
            cancel_active(Path(state_root).absolute().parent)


def _selected_audio_tools(settings):
    """Use the sealed installation for defaults; retain explicit selections.

    The existing verifier checks the complete inner/outer runtime identity.
    An invalid declared bundle must fail before ordinary PATH discovery.
    """
    from .audio_runtime import bundled_audio_tools
    bundled = bundled_audio_tools()
    return tuple(settings[name] if settings[name] is not None or bundled is None
                 else bundled[name] for name in ('ffmpeg', 'ffprobe'))


def voice_registry(state_root, *, cancelled=lambda: False, timeout_seconds=60):
    from .speech import VoiceRegistry, PiperProvider, LocalHelperProvider
    from .speech._io import check_cancelled
    from .speech.dordiai import DordiaiProvider, configuration
    if (type(timeout_seconds) not in (float, int) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600):
        raise ValueError('Set a positive voice discovery timeout of at most 3600 seconds.')
    deadline = monotonic() + timeout_seconds
    def check():
        check_cancelled(cancelled)
        if monotonic() >= deadline:
            raise TimeoutError('Local voice discovery exceeded its overall time budget.')
        return False
    check()
    value = load_settings(state_root)
    ffmpeg, _ffprobe = _selected_audio_tools(value)
    library_root = Path(state_root).absolute().parent
    legacy = configuration(library_root)
    selected = value['dordiai']
    if selected and selected['enabled'] and legacy['enabled']:
        raise ValueError('Both canonical and legacy dordiai-voices connections are enabled. Explicitly disable the old connection before choosing a new one.')
    def current_declaration():
        # A selected provider outlives discovery. Do not retain that earlier
        # call's deadline/cancellation in later, independently bounded renders.
        if load_settings(state_root)['dordiai'] != selected or configuration(library_root) != legacy:
            raise ValueError('Dordiai-voices configuration changed. Reload the installed voices.')
    check()
    registry = VoiceRegistry.discover(ffmpeg=ffmpeg, cancelled=check,
        timeout_seconds=max(.001, deadline - monotonic()))
    try:
        from .integrations.dordiai_speech import install_dordiai_provider
        if selected and selected['enabled']:
            install_dordiai_provider(registry, selected, cancelled=check,
                configuration_root=library_root, settings_guard=current_declaration)
        elif legacy['enabled']:
            registry.add_provider(DordiaiProvider(library_root, settings_guard=current_declaration),
                                  cancelled=check, timeout_seconds=max(.001, deadline - monotonic()))
        for item in value['piper']:
            check()
            if set(item) != {'executable', 'models'}:
                raise ValueError('Invalid Piper configuration fields.')
            registry.add_provider(PiperProvider(item['executable'], item['models'], cancelled=check),
                                  cancelled=check, timeout_seconds=max(.001, deadline - monotonic()))
        for item in value['voice_helpers']:
            check()
            if item.get('enabled') is True:
                registry.add_provider(LocalHelperProvider(**item), cancelled=check,
                                      timeout_seconds=max(.001, deadline - monotonic()))
        check()
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
