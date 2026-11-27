"""Existing retained-media and reader producers in the common backup inventory.

This adapter reuses each producer's saved-data validator. It never activates a
voice, decoder, ASR runtime, account or restored output directory. External
Studio inputs are copied only by the existing reviewed-input snapshot adapter.
"""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re

from . import backup_extensions, backup_document_batches, backup_transcription
from .durable_state import atomic_json, encode, regular
from .ingest.restore import RestoreError
from .state_backup import relative_name

MAX_BYTES = backup_extensions.MAX_BYTES
MAX_JSON = backup_extensions.MAX_JSON
ROOT_LIMITS = dict(backup_extensions.SIDECARS)
# Recovery bytes only. A backup cannot provide a publisher policy, signature
# verification, protected anchor or installation authority. Keep even a damaged
# bounded token for owner recovery; the normal EntitlementStore still rejects it.
_TRUSTED_RECOVERY = frozenset(('state/trusted-client/entitlement.json',
                               'state/trusted-client/entitlement.pending.json'))
_TRUSTED_RECOVERY_BYTES = 65536
_HEX = r'[0-9a-f]{64}'
_UUID = r'[0-9a-f]{32}'
_LINKS = re.compile(r'state/speech/' + _HEX + r'\.reader-links\.json\Z')
_PUBLICATION = re.compile(r'state/studio/publications/(' + _UUID + r')\.json\Z')
_EFFECT_PRESET = re.compile(r'state/studio/effect-presets/(' + _UUID + r')\.json\Z')
_RECORDING = re.compile(r'state/studio/recordings/(' + _UUID + r')\.(json|wav)\Z')
_INPUT = re.compile(r'state/studio/inputs/' + _HEX + r'\.audio\Z')
_TRANSCRIPTION = re.compile(r'state/transcription/(' + _HEX + r')/(.+)\Z')
_LEGACY = re.compile(r'legacy-backups/(' + _HEX + r')(?:\.json|/(.+))\Z')
_TEXT = re.compile(r'state/studio/jobs/[A-Za-z0-9_-]{1,80}/script\.(srt|vtt|lrc)\Z')


def limit(name):
    if name.startswith('state/'):
        from .studio.backup_inputs import _reserved_names
        _reserved_names((name.removeprefix('state/'),))
    if maximum := backup_document_batches.limit(name):
        return maximum
    if name in ROOT_LIMITS:
        return ROOT_LIMITS[name]
    if name in _TRUSTED_RECOVERY:
        return _TRUSTED_RECOVERY_BYTES
    if name == 'state/operations/resources.json':
        return 65536
    if name == 'state/ui/progress.json':
        return 4096
    if name == 'state/ui/shortcuts.json':
        return 256 * 1024
    if name == 'state/ui/reader-appearance.json':
        return 4096
    if name == 'state/speech/voice-preferences.json':
        from .speech.voice_preferences import MAX_PREFERENCES_BYTES
        return MAX_PREFERENCES_BYTES
    if name == 'state/speech/pronunciations.json':
        return 8_000_000
    if name == 'state/studio/draft.json':
        return 32 * 1024**2
    if _EFFECT_PRESET.fullmatch(name):
        return 64 * 1024
    if (_LINKS.fullmatch(name) or _PUBLICATION.fullmatch(name)
            or name in ('state/studio/backup-inputs.json', 'state/studio/legacy-input-review.json')):
        return MAX_JSON
    if _TEXT.fullmatch(name):
        return 32 * 1024**2
    if match := _RECORDING.fullmatch(name):
        return MAX_JSON if match[2] == 'json' else 32 * 1024**3
    if _INPUT.fullmatch(name):
        return 32 * 1024**3
    if match := _TRANSCRIPTION.fullmatch(name):
        tail = match[2]
        if tail in ('job.json', 'control.json'):
            return MAX_JSON
        from .audio.transcription import AUDIO_EXTENSIONS
        suffixes = '|'.join(re.escape(value) for value in AUDIO_EXTENSIONS)
        if re.fullmatch(r'inputs/' + _HEX + '(?:' + suffixes + ')', tail):
            return 32 * 1024**3
        if re.fullmatch(r'outputs/' + _HEX + '(?:' + suffixes + r')/(?:receipt\.json|transcript\.(?:json|txt|srt|vtt))', tail):
            return MAX_JSON
        raise RestoreError('Unrecognized file in a typed transcription snapshot.')
    if match := _LEGACY.fullmatch(name):
        if match[2] is not None:
            relative_name(match[2])
        return 32 * 1024**3
    return None


def is_media(name):
    return bool(backup_document_batches.is_media(name) or _INPUT.fullmatch(name) or _LEGACY.fullmatch(name)
                or (match := _RECORDING.fullmatch(name)) and match[2] == 'wav'
                or (match := _TRANSCRIPTION.fullmatch(name)) and match[2].startswith('inputs/'))


def empty_allowed(name):
    return bool(backup_document_batches.empty_allowed(name) or name in _TRUSTED_RECOVERY
                or _TEXT.fullmatch(name) or _TRANSCRIPTION.fullmatch(name)
                and name.endswith(('.txt', '.srt')))


def validate(db, name, raw):
    """Validate passive bytes; cross-file identities are checked as a collection."""
    from .backup_producers import fields, text
    from .backup_sidecars import _json
    if backup_document_batches.limit(name) is not None:
        backup_document_batches.validate_file(name, raw)
        return
    if name in _TRUSTED_RECOVERY:
        if type(raw) is not bytes or len(raw) > _TRUSTED_RECOVERY_BYTES:
            raise ValueError('Trusted-client recovery bytes exceed their bounded allowance.')
        return
    if name in ROOT_LIMITS:
        database = db.execute('PRAGMA database_list').fetchone()[2]
        allowed = [row[0] for row in db.execute('SELECT id FROM workspace')]
        backup_extensions.validate_sidecars(database, {name: raw}, allowed)
        return
    if _TEXT.fullmatch(name):
        value = raw.decode('utf-8')
        if '\x00' in value or name.endswith('.vtt') and not value.startswith('WEBVTT\n'):
            raise ValueError('Invalid generated subtitle text.')
        return
    if match := _TRANSCRIPTION.fullmatch(name):
        tail = match[2]
        if tail == 'job.json':
            from .audio.transcription import validate_saved_job
            record = validate_saved_job(_json(raw))
            if record['id'] != match[1]:
                raise ValueError('Transcription directory differs from its exact job identity.')
        elif tail == 'control.json':
            value = _json(raw)
            fields(value, {'command'})
            if value['command'] not in ('run', 'pause', 'cancel'):
                raise ValueError('Invalid transcription control.')
        # The owning job verifies every included input/output/receipt together.
        return
    value = _json(raw)
    if name == 'state/operations/resources.json':
        from .operations.models import ResourceLimits
        fields(value, {'schema', 'limits'})
        if value['schema'] != 'carr-resource-policy/1':
            raise ValueError('Unsupported saved resource policy.')
        ResourceLimits.from_dict(value['limits'])
    elif name == 'state/ui/progress.json':
        fields(value, {'schema', 'preferences'})
        if type(value['schema']) is not int or value['schema'] != 1:
            raise ValueError('Unsupported legacy progress preferences.')
        preferences = value['preferences']
        fields(preferences, {'mode', 'interval_seconds', 'volume', 'quiet', 'background', 'mute_during_audio'})
        if preferences['mode'] not in ('off', 'beep', 'speech', 'both'):
            raise ValueError('Invalid legacy progress feedback mode.')
        from .backup_producers import number
        number(preferences['interval_seconds'], 1, 120)
        number(preferences['volume'], 0, 1)
        if any(type(preferences[key]) is not bool for key in ('quiet', 'background', 'mute_during_audio')):
            raise ValueError('Invalid legacy progress feedback option.')
    elif name == 'state/ui/shortcuts.json':
        from .ui.keyboard_settings import _settings_value
        _settings_value(raw)
    elif name == 'state/ui/reader-appearance.json':
        from .reader.appearance import _preferences
        _preferences(raw)
    elif name == 'state/speech/voice-preferences.json':
        from .speech.voice_preferences import validate_voice_preferences
        validate_voice_preferences(value)
    elif name == 'state/speech/pronunciations.json':
        from .speech.reading import PronunciationRule
        fields(value, {'schema', 'rules'})
        if value['schema'] != 'carr.pronunciations.v1' or type(value['rules']) is not list or len(value['rules']) > 1000:
            raise ValueError('Invalid saved pronunciation dictionary.')
        for rule in value['rules']:
            PronunciationRule(**rule)
    elif name == 'state/studio/draft.json':
        fields(value, {'schema', 'text', 'title', 'voice_ids', 'project_id'})
        if type(value['schema']) is not int or value['schema'] != 1:
            raise ValueError('Invalid Studio script draft schema.')
        text(value['text'], 8_000_000, empty=True)
        text(value['title'], 512, empty=True)
        if type(value['voice_ids']) is not list or len(value['voice_ids']) > 10000:
            raise ValueError('Invalid Studio draft voice inventory.')
        for identity in value['voice_ids']:
            text(identity, 2048)
        if value['project_id'] is not None:
            text(value['project_id'], 80)
    elif _LINKS.fullmatch(name):
        fields(value, {'schema', 'copies'})
        if (value['schema'] != 'carr.reader-recovery-links.v1' or type(value['copies']) is not list
                or not 1 <= len(value['copies']) <= 500
                or any(type(key) is not str or not re.fullmatch(_HEX, key) for key in value['copies'])
                or len(set(value['copies'])) != len(value['copies'])):
            raise ValueError('Invalid reading recovery-family identities.')
    elif match := _EFFECT_PRESET.fullmatch(name):
        from .audio.effect_presets import EffectPreset
        if EffectPreset.from_dict(value).id != match[1]:
            raise ValueError('The effect preset identity differs from its exact saved filename.')
    elif match := _PUBLICATION.fullmatch(name):
        from .studio.publishing import validate_publication_receipt
        validate_publication_receipt(value, publication_id=match[1])
    elif match := _RECORDING.fullmatch(name):
        from .audio.recording import validate_recording_receipt
        validate_recording_receipt(value, recording_id=match[1])
    elif name == 'state/studio/backup-inputs.json':
        fields(value, {'schema', 'projects', 'inputs', 'external_outputs'})
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['projects']) is not list or type(value['inputs']) is not dict
                or len(value['projects']) + len(value['inputs']) > 100000):
            raise ValueError('Invalid retained Studio input receipt.')
    elif name == 'state/studio/legacy-input-review.json':
        fields(value, {'schema', 'pending', 'approved'})
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['pending']) is not list or type(value['approved']) is not dict
                or len(value['pending']) + len(value['approved']) > 100000):
            raise ValueError('Invalid Studio input review.')
        for path in [*value['pending'], *value['approved']]:
            text(path)
        # Pure byte validation only; path approval stays with the producer.
        for item in value['approved'].values():
            from .backup_producers import digest, number
            fields(item, {'sha256', 'size_bytes'})
            digest(item['sha256']); number(item['size_bytes'], 1, integer=True)
    else:
        raise RestoreError('Unknown current backup producer: ' + name)


def state_entries(entries):
    return [dict(entry, filename=entry['filename'].removeprefix('state/'))
            for entry in entries if entry['filename'].startswith('state/')]


def legacy_empty_directories(entries):
    return frozenset(name.rsplit('/', 1)[0] + '/originals' for entry in entries
        if re.fullmatch(r'legacy-backups/' + _HEX + r'/manifest\.json', name := entry['filename']))


def validate_collection(db, stage, entries, *, independent=True, cancelled=lambda: False):
    if sum(bool(_EFFECT_PRESET.fullmatch(entry['filename'])) for entry in entries) > 256:
        raise RestoreError('The effect preset inventory exceeds its 256-record allowance.')
    backup_document_batches.validate_collection(stage, entries, cancelled=cancelled)
    from . import backup_recordings
    backup_recordings.validate_collection(stage, entries, cancelled=cancelled)
    canonical = tuple(entry for entry in entries
                      if backup_transcription.limit(entry['filename']) is not None)
    if canonical and not independent:
        raise RestoreError('Independent transcription jobs cannot enter a workspace-scoped backup.')
    if canonical:
        from .backup_sidecars import _check_cancelled, read_regular
        records = {}
        for entry in canonical:
            _check_cancelled(cancelled)
            name = entry['filename']
            if not backup_transcription.is_input(name):
                raw = read_regular(Path(stage) / name, backup_transcription.limit(name),
                                   empty=backup_transcription.allows_empty(name))
                value = backup_transcription.validate(name, raw)
                if value is not None:
                    records[name] = value
        backup_transcription.validate_collection(canonical, records)
    # The existing hash64 producer is authoritative for its own path grammar.
    # A shared schema label cannot make it decode canonical profile/UUID jobs.
    state = state_entries(entry for entry in entries
                          if backup_transcription.limit(entry['filename']) is None)
    database = db.execute('PRAGMA database_list').fetchone()[2]
    allowed = [row[0] for row in db.execute('SELECT id FROM workspace')]
    _media, _required, retained = backup_extensions.inspect_media_state(Path(stage) / 'state', state,
        independent=independent, cancelled=cancelled)
    backup_extensions.inspect_state(Path(stage) / 'state', state, database, allowed,
        independent=independent, cancelled=cancelled, retained_inputs=retained)
    if any(entry['filename'].startswith('legacy-backups/') for entry in entries):
        if not independent:
            raise RestoreError('Independent legacy archives are outside this backup scope.')
        from .backup_compat.archive import verify_archives
        verify_archives(Path(stage) / 'legacy-backups', cancelled=cancelled)


def validate_historical_state(db, stage, entries, *, cancelled):
    """Keep historical opaque state inert while validating every known producer.

    Old state envelopes intentionally admitted unrelated bounded owner data.
    A historical version number must not bypass an installed producer's exact
    source, receipt, plan or credential-field validator.
    """
    from . import backup_producers
    from .backup_sidecars import _STUDIO, read_regular, validate_sidecar, _check_cancelled
    common = [dict(entry, filename='state/' + entry['filename']) for entry in entries]
    for entry in common:
        _check_cancelled(cancelled)
        name = entry['filename']
        maximum = limit(name) or backup_transcription.limit(name) or backup_producers.limit(name)
        if name == 'state/audio/engines.json' or _STUDIO.fullmatch(name):
            from .backup_sidecars import sidecar_limit
            maximum = sidecar_limit(name)
        if maximum is not None and not is_media(name) and not backup_transcription.is_input(name):
            raw = read_regular(Path(stage) / name, maximum,
                               empty=empty_allowed(name) or backup_transcription.allows_empty(name))
            validate_sidecar(db, name, raw)
    validate_collection(db, stage, common, cancelled=cancelled)


def retain_inputs(stage, entries, *, max_bytes, max_files, cancelled):
    from .studio.backup_inputs import snapshot_inputs
    state = state_entries(entries)
    if not state:
        return entries
    non_state = [entry for entry in entries if not entry['filename'].startswith('state/')]
    retained = snapshot_inputs(Path(stage) / 'state', state,
        max_bytes=max_bytes - sum(entry['size_bytes'] for entry in non_state),
        max_files=max_files - len(non_state), cancelled=cancelled)
    return sorted([*non_state, *(dict(entry, filename='state/' + entry['filename']) for entry in retained)],
                  key=lambda entry: entry['filename'])


def project_restored(db, stage, destination, inventory, *, cancelled):
    """Rebase only verified staged inputs; all declared external outputs stay inert."""
    from .backup_sidecars import _capture, _check_cancelled, Inventory
    from .studio.backup_inputs import restore_inputs
    stage, destination = Path(stage), Path(destination)
    state = state_entries(inventory.entries)
    if state:
        restore_inputs(stage / 'state', destination / 'state', state, cancelled=cancelled)
    for entry in inventory.entries:
        _check_cancelled(cancelled)
        name = entry['filename']
        if (match := _TRANSCRIPTION.fullmatch(name)) and match[2] == 'job.json':
            from .audio.transcription import restored_job
            from .durable_state import read_json
            path = stage / name
            atomic_json(path, restored_job(read_json(path)), replace=True)
            atomic_json(path.parent / 'control.json', {'command': 'pause'}, replace=True)
        elif name in (backup_extensions.DORDIAI, backup_extensions.ASR_SETTINGS):
            transform = (backup_extensions.restored_settings_bytes if name == backup_extensions.DORDIAI
                         else backup_extensions.restored_asr_settings)
            raw = transform((stage / name).read_bytes())
            from .durable_state import atomic_bytes
            atomic_bytes(stage / name, raw, replace=True)
    # Projection may rename a project revision and removes the old input receipt.
    # The entire private stage has already passed exact inventory/content checks.
    # Re-enumeration uses the same bounded registered namespace, never a path
    # supplied as cleanup authority by an external receipt.
    files, _ = _capture(stage, include_generated=inventory.generated_audio_included, cancelled=cancelled)
    entries = tuple(dict(filename=name) for name in sorted(files))
    return replace(inventory, entries=entries,
                   empty_roots=inventory.empty_roots | legacy_empty_directories(entries))
