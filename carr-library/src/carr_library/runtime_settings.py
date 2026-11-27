"""Explicit local engine configuration; no installers, remote fallback or secrets."""
import json
import os
from pathlib import Path, PureWindowsPath
import re

from .durable_state import MAX_JSON_BYTES, atomic_json, read_json, state_lock


_FIELDS = {'schema', 'ffmpeg', 'ffprobe', 'piper', 'voice_helpers', 'music_models', 'fluidsynth'}
_RESTORE = 'restore_requires_revalidation'
_RUNTIME_SEAL = 'audio_runtime_manifest_sha256'


def settings_path(state_root):
    return Path(state_root) / 'audio' / 'engines.json'


def _bounded_preference_json(value):
    # Count bounded encoder chunks before accepting the aggregate. Thousands of
    # short Unicode IDs must not bypass the existing durable JSON byte ceiling.
    size = 1  # the existing atomic writer's terminating newline
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode('utf-8', errors='strict'))
        if size > MAX_JSON_BYTES:
            raise ValueError('Saved voice preferences exceed the existing engine JSON byte budget.')


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
    result = {'favorites': favorites, 'defaults': defaults, 'profiles': profiles}
    _bounded_preference_json(result)
    return result


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


def _voice_preferences(value):
    return validate_voice_preferences(value.get('voice_preferences',
        {'favorites': [], 'defaults': {}, 'profiles': {}}))


def load_voice_preferences(state_root):
    """Read inert exact-ID preferences without resolving or starting an engine."""
    return _voice_preferences(load_settings(state_root))


def save_voice_preferences(state_root, value, *, expected, cancelled=None,
                           guard=lambda: None):
    """CAS-update only passive preferences under the existing engine-state lock.

    A preference save cannot clear a restore marker, change a runtime seal or
    authorize an unavailable engine. All other declarations remain untouched.
    """
    value = validate_voice_preferences(value)
    expected = validate_voice_preferences(expected)
    cancelled = _review_cancellation(cancelled)

    def check():
        _check_review(cancelled)
        guard()

    check()
    path = settings_path(state_root)
    with state_lock(path.parent, 'engines'):
        check()
        current = load_settings(state_root)
        if _voice_preferences(current) != expected:
            raise ValueError('Voice preferences changed in another window. Reload them before saving.')
        candidate = _settings_document({**current, 'voice_preferences': value})
        _bounded_preference_json(candidate)
        check()
        atomic_json(path, candidate, replace=os.path.lexists(path))
    return value


def review_voice_preference(state_root, registry, preferences, *, language,
                            voice_id='', profile='', cancelled=None,
                            guard=lambda: None, registry_factory=None):
    """Review against fresh inventory without replacing the reader's registry.

    The optional factory is an internal dependency, never a saved declaration.
    The review owns that fresh registry; the reader retains its borrowed one.
    A changed/missing voice requires an explicit reader reload, never fallback.
    """
    from .speech import VoiceRegistry

    if not isinstance(registry, VoiceRegistry):
        raise ValueError('Use the reader\'s current local voice registry.')
    cancelled = _review_cancellation(cancelled)

    def check():
        _check_review(cancelled)
        guard()

    check()
    expected = load_settings(state_root)
    if expected.get(_RESTORE, False):
        raise ValueError('Restored local engine settings require explicit revalidation before selecting a voice.')
    _require_supported_voice_declarations(expected)
    factory = registry_factory or (lambda: voice_registry(state_root))
    fresh = factory()
    if fresh is registry:
        raise ValueError('Voice review requires an independently owned fresh inventory.')
    if not isinstance(fresh, VoiceRegistry):
        raise ValueError('Use the existing local voice registry for preference review.')
    try:
        check()
        selection = resolve_voice_preference(fresh.voices(), preferences,
            language=language, voice_id=voice_id, profile=profile)
        # A fresh inventory validates today's registration. Do not hand its
        # selection to a reader still bound to another provider/model snapshot.
        identity = selection['voice_id']
        reviewed = fresh.fingerprint(identity)
        current = registry.fingerprint(identity)
        if reviewed != current:
            raise ValueError('The selected voice changed. Reload reader voices before using its preferences.')
        check()
        if load_settings(state_root) != expected:
            raise ValueError('Local engine settings changed during voice review. Reload before selecting.')
        return selection
    finally:
        fresh.close()


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



def validate_legacy_dordiai_helper(value):
    """Read a canonical helper declaration as data, without probing its assets.

    Relative installation references remain readable for an explicit migration.
    This legacy shape never authorizes the current reviewed voice adapter.
    """
    required = {'adapter', 'runtime_directory', 'rights_notice', 'enabled'}
    optional = {'python_executable', 'expected_worker_sha256'}
    if (type(value) is not dict or not required <= value.keys()
            or value.keys() - required - optional
            or value.get('adapter') != 'dordiai-voices'
            or type(value.get('enabled')) is not bool):
        raise ValueError('Invalid retained dordiai-voices helper declaration.')

    def text(selected, maximum):
        if (type(selected) is not str or not selected.strip()
                or len(selected) > maximum or '\x00' in selected):
            raise ValueError('Invalid retained local voice declaration text.')
        try:
            selected.encode('utf-8', errors='strict')
        except UnicodeError as exc:
            raise ValueError('Invalid retained local voice declaration text.') from exc

    def path(selected):
        text(selected, 32767)
        normalized = selected.replace('\\', '/')
        remainder = selected[2:] if re.match(r'^[A-Za-z]:', selected) else selected
        if (normalized.startswith('//') or '://' in selected or ':' in remainder
                or any(ord(character) < 32 for character in selected)):
            raise ValueError('Retained voice references must be local filesystem declarations.')

    text(value['runtime_directory'], 4096)
    path(value['runtime_directory'])
    text(value['rights_notice'], 4096)
    if value.get('python_executable') is not None:
        path(value['python_executable'])
    expected = value.get('expected_worker_sha256')
    if ('expected_worker_sha256' in value
            and (type(expected) is not str or not re.fullmatch('[0-9a-f]{64}', expected))):
        raise ValueError('A retained voice worker needs a lowercase SHA-256 fingerprint.')
    return dict(value)


def _require_supported_voice_declarations(value):
    # Retain canonical declarations for inspection/backup, but never silently
    # ignore an enabled old engine when another settings edit clears a restore
    # marker. Its assets must be migrated into the existing reviewed registry.
    legacy_enabled = any(item.get('adapter') == 'dordiai-voices' and item.get('enabled') is True
                         for item in value.get('voice_helpers', ()))
    if ((value.get('dordiai') is not None and value['dordiai']['enabled'])
            or legacy_enabled):
        raise ValueError('This retained dordiai-voices declaration requires explicit migration '
                         'into Local voice settings before activation or revalidation.')


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



def validate_audio_runtime_binding(value):
    """Pure optional seal/path validation shared with backup restore admission."""
    if _RUNTIME_SEAL not in value:
        return value
    seal = value[_RUNTIME_SEAL]
    if (not isinstance(seal, str) or not re.fullmatch('[0-9a-f]{64}', seal)
            or value.get('ffmpeg') is None or value.get('ffprobe') is None):
        raise ValueError('A selected complete audio runtime needs both executable paths and its exact manifest SHA256.')
    _declared_path(value['ffmpeg'])
    _declared_path(value['ffprobe'])
    path_type = PureWindowsPath if PureWindowsPath(value['ffmpeg']).drive else Path
    ffmpeg, ffprobe = path_type(value['ffmpeg']), path_type(value['ffprobe'])
    if (ffmpeg.name.casefold() != 'ffmpeg.exe' or ffprobe.name.casefold() != 'ffprobe.exe'
            or ffmpeg.parent != ffprobe.parent or ffmpeg.parent.name.casefold() != 'bin'):
        raise ValueError('A sealed audio runtime must select FFmpeg and FFprobe from the same reviewed bin directory.')
    return value


def _settings_document(value):
    if (type(value) is not dict or set(value) - _FIELDS - {_RESTORE, _RUNTIME_SEAL, 'dordiai', 'voice_preferences'}
            or not _FIELDS <= set(value) or type(value['schema']) is not int
            or value['schema'] != 1
            or (_RESTORE in value and type(value[_RESTORE]) is not bool)):
        raise ValueError('Unsupported local engine configuration.')
    if 'dordiai' in value:
        validate_dordiai_configuration(value['dordiai'])
    if 'voice_preferences' in value:
        validate_voice_preferences(value['voice_preferences'])
    for key in ('ffmpeg', 'ffprobe'):
        if value[key] is not None:
            _declared_path(value[key])
    validate_audio_runtime_binding(value)
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
        if 'adapter' in item:
            validate_legacy_dordiai_helper(item)
            continue
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


def _enabled_settings(state_root, *, cancelled=None):
    value = load_settings(state_root)
    if value.get(_RESTORE, False):
        raise ValueError('Restored local engine settings require explicit revalidation in '
                         'Local engine settings before any voice or music activation.')
    _require_supported_voice_declarations(value)
    verify_configured_audio_runtime(value, cancelled=cancelled)
    from .audio.musicgen_worker import plain_local
    for key in ('ffmpeg', 'ffprobe'):
        if value[key] is not None:
            plain_local(value[key])
    return value



def configured_audio_runtime_root(value):
    """Resolve no files; restored and sealed identity checks precede activation."""
    value = _settings_document(value)
    if value.get(_RESTORE, False):
        raise ValueError('Restored local engine settings require explicit revalidation before audio activation.')
    if _RUNTIME_SEAL not in value:
        return None
    root = Path(value['ffmpeg']).parent.parent
    if not root.is_absolute():
        raise ValueError('This reviewed audio runtime belongs to another operating system; select local files again.')
    return root


def verify_configured_audio_runtime(value, *, cancelled=None, progress=None):
    root = configured_audio_runtime_root(value)
    if root is None:
        return None
    from .audio.runtime import verify_audio_runtime
    from .audio.render import _require_local_volume
    _require_local_volume(root)
    return verify_audio_runtime(root, value[_RUNTIME_SEAL], cancelled=cancelled, progress=progress)


def _review_cancellation(cancelled):
    callback = getattr(cancelled, 'is_set', cancelled) if cancelled is not None else lambda: False
    if not callable(callback):
        raise ValueError('Cancellation must be a callback or event.')
    return callback


def _check_review(cancelled):
    if cancelled():
        raise InterruptedError('Local engine review was cancelled; no new settings were saved.')


def _configured_voice_providers(value, *, cancelled=None):
    _require_supported_voice_declarations(value)
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
    try:
        for provider in providers:
            registry.add_provider(provider)
        return registry
    except BaseException:
        # This factory owns the newly discovered registry until it returns.
        # Fatal configured-provider revalidation must not leak its earlier
        # providers. A borrowed caller's add_provider semantics stay unchanged.
        registry.close()
        raise


def save_settings(state_root, value, *, cancelled=None, expected_settings=None):
    """Explicitly review configured assets before clearing a restore barrier."""
    cancelled = _review_cancellation(cancelled)
    _check_review(cancelled)
    value = _settings_document(value)
    previous = load_settings(state_root) if expected_settings is None else _settings_document(expected_settings)
    if ('voice_preferences' in value
            and _voice_preferences(value) != _voice_preferences(previous)):
        raise ValueError('Voice preferences changed. Reload engine settings before saving; '
                         'edit preferences in the voice preferences dialog.')
    if 'voice_preferences' in previous:
        value = {**value, 'voice_preferences': _voice_preferences(previous)}
    _require_supported_voice_declarations(value)
    from .audio.models import LocalModelConfig
    from .audio.music import validate_model
    from .audio.musicgen_worker import plain_local
    from .audio.render import run_process
    # Explicit revalidation may clear the restore marker, but only after the
    # same sealed inventory has been checked; never silently erase its digest.
    review_value = {**value, _RESTORE: False}
    verify_configured_audio_runtime(review_value, cancelled=cancelled)
    for key in ('ffmpeg', 'ffprobe'):
        _check_review(cancelled)
        if value[key] is not None:
            # Review the explicit candidate, even when another runtime remains
            # active. Production invocation still uses the active-runtime guard.
            binary = str(plain_local(value[key]))
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
    with state_lock(path.parent, 'engines'):
        _check_review(cancelled)
        if load_settings(state_root) != previous:
            raise ValueError('Local engine settings changed during review. Reopen the dialog before saving.')
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
