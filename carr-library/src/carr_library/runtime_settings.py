"""Explicit local engine configuration; no installers, remote fallback or secrets."""
from dataclasses import asdict
from copy import deepcopy

_UNREVIEWED_DORDIAI = object()
from pathlib import Path
import re

from .durable_state import atomic_json, local_path, read_json, state_lock


def settings_path(state_root):
    return Path(state_root) / 'audio' / 'engines.json'


def validate_voice_preferences(value):
    """Validate inert references to installed voices, without opening an engine.

    Defaults use exact canonical language tags. A saved voice ID is neither an
    installed model nor proof of a voice's language or acoustic quality.
    """
    from .language_qualification.catalog import canonical_language_tag

    if (type(value) is not dict or set(value) != {'favorites', 'defaults', 'profiles'}
            or type(value['favorites']) is not list or len(value['favorites']) > 10000
            or type(value['defaults']) is not dict or len(value['defaults']) > 512
            or type(value['profiles']) is not dict or len(value['profiles']) > 256):
        raise ValueError('Invalid or excessive saved voice preferences.')

    def text(item, maximum):
        if (type(item) is not str or not item.strip() or len(item) > maximum
                or any(ord(character) < 32 or 127 <= ord(character) < 160 for character in item)):
            raise ValueError('Invalid saved voice preference text.')
        item.encode('utf-8', errors='strict')
        return item

    favorites = [text(item, 2048) for item in value['favorites']]
    if len(favorites) != len(set(favorites)):
        raise ValueError('A favorite voice must not be listed twice.')
    defaults = {}
    for language, identity in value['defaults'].items():
        language = canonical_language_tag(language)
        if language in defaults:
            raise ValueError('A language must have only one default voice.')
        defaults[language] = text(identity, 2048)
    profiles, names = {}, set()
    for name, profile in value['profiles'].items():
        text(name, 80)
        if name != name.strip() or name.casefold() in names:
            raise ValueError('Use distinct, unambiguous voice profile names.')
        names.add(name.casefold())
        if type(profile) is not dict or set(profile) != {'voice_id', 'language', 'rate', 'volume'}:
            raise ValueError('A voice profile needs its exact voice, language, rate and volume.')
        if (type(profile['rate']) is not int or not -10 <= profile['rate'] <= 10
                or type(profile['volume']) is not int or not 0 <= profile['volume'] <= 100):
            raise ValueError('Choose a voice rate from -10 to 10 and volume from 0 to 100.')
        profiles[name] = {**profile, 'voice_id': text(profile['voice_id'], 2048),
                         'language': canonical_language_tag(profile['language'])}
    return {'favorites': favorites, 'defaults': defaults, 'profiles': profiles}


def resolve_voice_preference(voices, preferences, *, language, voice_id='', profile=''):
    """Resolve an explicit choice through the current registry, never a fallback.

    Callers copy this result into their existing immutable reader/book/project
    settings. Saving preferences cannot rewrite an already reviewed job.
    """
    from .language_qualification.catalog import canonical_language_tag

    preferences = validate_voice_preferences(preferences)
    language = canonical_language_tag(language)
    if type(voice_id) is not str or type(profile) is not str:
        raise ValueError('Choose an explicit voice ID or a saved profile name.')
    if profile:
        try:
            selection = dict(preferences['profiles'][profile])
        except KeyError as exc:
            raise ValueError('The selected voice profile is unavailable; reload preferences.') from exc
        if selection['language'] != language or voice_id and voice_id != selection['voice_id']:
            raise ValueError('The selected profile belongs to a different voice or exact language.')
    else:
        identity = voice_id or preferences['defaults'].get(language)
        if not identity:
            raise ValueError('No default is saved for this exact language; choose an installed voice.')
        selection = {'voice_id': identity, 'language': language, 'rate': 0, 'volume': 100}
    available = {voice.id: voice for voice in voices}
    voice = available.get(selection['voice_id'])
    if voice is None:
        raise ValueError('The saved voice is not installed or available. Choose a voice explicitly; no substitute was used.')
    try:
        declared = canonical_language_tag(voice.language, adapter_underscores=True)
    except ValueError as exc:
        raise ValueError('This voice has no usable exact language declaration.') from exc
    if declared != language:
        raise ValueError('The voice does not declare this exact language, script and region; no language fallback was used.')
    if (selection['rate'] != 0 and 'rate' not in voice.capabilities
            or selection['volume'] != 100 and 'volume' not in voice.capabilities):
        raise ValueError('The installed voice does not support the saved profile controls.')
    return selection


def load_voice_preferences(state_root):
    """Read passive preferences even if an external engine is no longer present."""
    return _read_settings(state_root)['voice_preferences']


def save_voice_preferences(state_root, value, *, expected, guard=lambda: None):
    """CAS-update only preferences while preserving the latest engine declarations."""
    value = validate_voice_preferences(value)
    expected = validate_voice_preferences(expected)
    guard()
    path = settings_path(state_root)
    with state_lock(path.parent):
        guard()
        current = _read_settings(state_root)
        if current['voice_preferences'] != expected:
            raise ValueError('Voice preferences changed in another window. Reload them before saving.')
        current['voice_preferences'] = value
        guard()
        atomic_json(path, current, replace=path.exists())
    return value


def validate_dordiai_configuration(value):
    """Validate saved declarations without probing a private installation."""
    if value is None:
        return None
    allowed = {'enabled', 'runtime_directory', 'python_executable', 'expected_worker_sha256', 'management'}
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
    if 'management' in value:
        from .audio.voice_settings import validate_management_binding
        value = {**value, 'management': validate_management_binding(value['management'])}
    return dict(value)


def _normalize_settings(value):
    if not isinstance(value, dict):
        raise ValueError('Unsupported local engine configuration.')
    value = dict(value)
    # Existing schema-1 installations remain readable without changing their file.
    value.setdefault('dordiai', None)
    value.setdefault('voice_preferences', {'favorites': [], 'defaults': {}, 'profiles': {}})
    if (set(value) != {'schema','ffmpeg','ffprobe','piper','voice_helpers','music_models','fluidsynth','dordiai','voice_preferences'}
            or type(value['schema']) is not int or value['schema'] != 1
            or any(not isinstance(value[key], list) for key in ('piper','voice_helpers','music_models'))):
        raise ValueError('Unsupported local engine configuration.')
    if any(len(value[key]) > 100 for key in ('piper','voice_helpers','music_models')):
        raise ValueError('Too many local engine configurations.')
    value['dordiai'] = validate_dordiai_configuration(value['dordiai'])
    value['voice_preferences'] = validate_voice_preferences(value['voice_preferences'])
    return value


def _read_settings(state_root):
    path = settings_path(state_root)
    value = read_json(path) if path.exists() else {'schema': 1, 'ffmpeg': None, 'ffprobe': None,
        'piper': [], 'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    return _normalize_settings(value)


def load_settings(state_root):
    value = _read_settings(state_root)
    for key in ('ffmpeg', 'ffprobe'):
        if value[key] is not None:
            local_path(value[key], exists=True)
    return value


def save_settings(state_root, value, *, expected_dordiai=_UNREVIEWED_DORDIAI):
    # Validate all declarations before changing the last good configuration.
    supplied_preferences = isinstance(value, dict) and 'voice_preferences' in value
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
    with state_lock(path.parent):
        if path.exists():
            stored = _read_settings(state_root)
            current = stored['voice_preferences']
            if supplied_preferences and current != value['voice_preferences']:
                raise ValueError('Voice preferences changed. Reload engine settings before saving; use the voice preferences dialog to edit them.')
            value['voice_preferences'] = current
            previous, proposed = stored['dordiai'], value['dordiai']
            old_management = previous.get('management') if previous else None
            new_management = proposed.get('management') if proposed else None
            if (expected_dordiai is not _UNREVIEWED_DORDIAI
                    and expected_dordiai != previous):
                raise ValueError('Voice engine settings changed. Reload engine settings before saving.')
            if old_management is not None or new_management is not None:
                if expected_dordiai is _UNREVIEWED_DORDIAI and proposed != previous:
                    raise ValueError('Managed voice authority changed. Reload engine settings before saving.')
                if new_management is not None and new_management != old_management:
                    raise ValueError('Use the reviewed package manager to change voice authority.')
                if old_management is not None and proposed != previous:
                    # Reset/disconnect cannot erase revocation into an unrestricted
                    # registration. Keep dormant authority until explicit review.
                    dormant = deepcopy(old_management)
                    dormant['restored'] = True
                    if proposed is None:
                        proposed = {**previous, 'enabled': False}
                    value['dordiai'] = {**proposed, 'management': dormant}
        elif value['dordiai'] is not None and 'management' in value['dordiai']:
            raise ValueError('Use explicit package registration to create managed voice authority.')
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
        install_dordiai_provider(registry, value['dordiai'], cancelled=cancelled,
                                 state_root=state_root)
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
