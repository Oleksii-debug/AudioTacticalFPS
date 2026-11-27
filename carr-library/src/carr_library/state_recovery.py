"""Validate typed canonical state and revoke execution authority after restore.

Only the verified private stage is changed. Historical opaque state remains
byte-identical; source text, job inputs, receipts and authorization hashes are
never rebound. Credentials and execution approval must be established again.
"""
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re

from .durable_state import atomic_json, read_json


def _hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


def _job_identity(value, path):
    identity = value.get('id')
    if (not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity)
            or path.stem != identity):
        raise ValueError('Restored job identity differs from its filename.')


def _translation(value):
    # Reuse the accepted chunker, profile and authorization contracts. This
    # structural check deliberately creates no Gateway, vault or HTTP client.
    from .assistant.profiles import ProviderProfile
    from .assistant.providers import Authorization, money
    from .assistant.translation import split_text, validate_translation_source_record
    from .assistant.models import canonical
    source = value.get('source_text')
    if (not isinstance(source, str) or value.get('source_sha256') != _hash(source)
            or value.get('chunks') != list(split_text(source))
            or not isinstance(value.get('receipts'), list)
            or len(value['receipts']) > len(value['chunks'])):
        raise ValueError('Invalid restored translation checkpoint.')
    for index, receipt in enumerate(value['receipts']):
        if (type(receipt) is not dict or receipt.get('index') != index
                or receipt.get('source_sha256') != _hash(value['chunks'][index])
                or type(receipt.get('text')) is not str or len(receipt['text']) > 200000
                or receipt.get('translation_sha256') != _hash(receipt['text'])):
            raise ValueError('Invalid restored translation receipt.')
    ProviderProfile(**value['profile'])
    Authorization(**value['authorization'])
    money(value['per_call_cost'])
    marker = value.get('restore_requires_review', False)
    if type(marker) is not bool:
        raise ValueError('Invalid restored translation review marker.')
    prior_profile = value.get('original_profile_sha256')
    if prior_profile is not None and (marker is not True or not isinstance(prior_profile, str)
            or not re.fullmatch('[0-9a-f]{64}', prior_profile)):
        raise ValueError('Invalid historical translation profile binding.')
    validate_translation_source_record(value)
    if not marker:
        value['original_profile_sha256'] = _hash(canonical(value['profile']))
    value['profile'] = {**value['profile'], 'credential_ref': None, 'enabled': False}
    value['restore_requires_review'] = True


def _assistant(value):
    from .assistant.models import Plan, canonical
    plan = value.get('plan')
    if (type(plan) is not dict or value.get('plan_sha256') != _hash(canonical(plan))
            or type(plan.get('scope')) is not dict or type(value.get('steps')) is not list
            or len(value['steps']) != len(plan.get('steps', []))
            or not isinstance(value.get('data_dir'), str)):
        raise ValueError('Invalid restored assistant plan checkpoint.')
    # A restored plan may name paths from another computer. Validate its typed
    # plan/budgets without opening or resolving those original external paths.
    scope = dict(plan['scope'])
    inputs, output = scope.get('input_roots', []), scope.get('output_root')
    if not isinstance(inputs, list) or any(
            not isinstance(path, str) or not path or len(path) > 32768 or '\x00' in path
            for path in [*inputs, *([output] if output is not None else [])]):
        raise ValueError('Invalid restored assistant path declarations.')
    scope.update(input_roots=(), output_root=None)
    Plan.from_dict({**plan, 'scope': scope})
    value['restore_requires_review'] = True


def _publication_path(path):
    return (len(path.parts) == 3 and path.parts[:2] == ('studio', 'publications')
            and path.suffix == '.json')


def _dispatch_path(relative):
    """Classify Windows aliases without renaming any retained state file.

    State manifests already reject case-folded duplicates. Use that same
    portable identity for typed recovery, while reads/writes keep the original
    spelling and all historical record values remain untouched.
    """
    return PurePosixPath(relative.casefold())


def validate_resource_policy(value):
    """Passive saved allowances; constructing no broker, lease or device probe."""
    from .operations.models import ResourceLimits
    keys = {'cpu_slots', 'memory_bytes', 'disk_bytes', 'min_free_disk_bytes', 'gpu_devices', 'max_leases'}
    if (type(value) is not dict or set(value) != {'schema', 'limits'}
            or value['schema'] != 'carr-resource-policy/1'
            or type(value['limits']) is not dict or set(value['limits']) != keys):
        raise ValueError('Invalid restored resource policy.')
    # Every field is explicit: do not use the default CPU-count factory.
    ResourceLimits(**value['limits'])
    return value


def _prepared(relative, value):
    """Return a modified typed record, or None for unchanged opaque state."""
    path = _dispatch_path(relative)
    if path == PurePosixPath('studio/draft.json'):
        from .studio.project import validate_studio_draft
        validate_studio_draft(value)
        return None
    if path == PurePosixPath('audio-metadata/choices.json'):
        from .audio.metadata_review import validate_choices
        validate_choices(value)
        return None
    if path == PurePosixPath('speech/playback-queue.json'):
        from .speech.playlist import SavedPlaylist
        SavedPlaylist.from_dict(value)
        return None
    if path == PurePosixPath('speech/presentation.json'):
        from .speech.presentation import ReadingAppearance
        ReadingAppearance.from_dict(value)
        return None
    if path == PurePosixPath('operations/resources.json'):
        validate_resource_policy(value)
        return None
    if _publication_path(path):
        from .studio.publishing import validate_publication_document
        validate_publication_document(value)
        _job_identity(value, path)
        return {**value, 'restore_requires_output': True}
    if type(value) is not dict:
        if (path in (PurePosixPath('transcription/engine.json'), PurePosixPath('audio/engines.json'))
                or len(path.parts) == 5 and path.parts[0] == 'transcription'
                and path.parts[2] == 'jobs' and path.name == 'job.json'):
            raise ValueError('Invalid typed transcription recovery object.')
        return None
    schema = value.get('schema')
    if path == PurePosixPath('transcription/engine.json'):
        from .transcription_contract import validate_engine_document
        validate_engine_document(value)
        return {**value, 'restore_requires_revalidation': True}
    if (len(path.parts) == 5 and path.parts[0] == 'transcription'
            and path.parts[2] == 'jobs' and path.name == 'job.json'):
        from .transcription_contract import validate_job_document
        validate_job_document(value, identity=path.parts[3])
        if _hash(value['workspace']) != path.parts[1]:
            raise ValueError('Restored transcription job belongs to another workspace.')
        # Preserve original input roots, hashes and receipts. New execution
        # requires explicit review; completed text and exports remain readable.
        return {**value, 'restore_requires_review': True}
    if path == PurePosixPath('audio/engines.json'):
        fields = {'schema', 'ffmpeg', 'ffprobe', 'piper', 'voice_helpers', 'music_models', 'fluidsynth'}
        if (type(schema) is not int or schema != 1
                or not fields <= set(value)
                or set(value) - fields - {'restore_requires_revalidation', 'audio_runtime_manifest_sha256', 'dordiai', 'voice_preferences'}
                or type(value.get('restore_requires_revalidation', False)) is not bool
                or any(not isinstance(value[key], list) or len(value[key]) > 100
                       for key in ('piper', 'voice_helpers', 'music_models'))):
            raise ValueError('Invalid restored local-engine configuration.')
        from .runtime_settings import validate_audio_runtime_binding, validate_dordiai_configuration
        validate_audio_runtime_binding(value)
        from .runtime_settings import validate_helper_script_configuration
        for helper in value['voice_helpers']:
            validate_helper_script_configuration(helper)
        if 'dordiai' in value:
            validate_dordiai_configuration(value['dordiai'])
        if 'voice_preferences' in value:
            from .runtime_settings import validate_voice_preferences
            validate_voice_preferences(value['voice_preferences'])
        # Keep original paths/hashes as evidence; do not resolve an executable
        # or model on the previous machine during backup or restoration.
        return {**value, 'restore_requires_revalidation': True}
    if schema == 'carr-provider-profile/1' and path.suffixes[-2:] == ['.profile', '.json']:
        from .assistant.profiles import ProviderProfile
        profile = ProviderProfile(**{key: item for key, item in value.items() if key != 'schema'})
        if path.name != (profile.id + '.profile.json').casefold():
            raise ValueError('Restored provider profile identity differs from its filename.')
        return {**value, 'credential_ref': None, 'enabled': False}
    if schema == 'carr-provider-usage/1' and path.name.endswith('.usage.json'):
        from .assistant.providers import validate_usage_ledger_document
        validate_usage_ledger_document(value)
        return {**value, 'restored': True}
    if schema == 'carr-assistant-job/1':
        _job_identity(value, path)
        if value.get('kind') == 'translation/1':
            _translation(value)
        else:
            _assistant(value)
        return value
    if (schema == 1 and len(path.parts) == 4
            and path.parts[:2] == ('studio', 'jobs') and path.name == 'job.json'):
        if type(schema) is not int:
            raise ValueError('Invalid restored production schema type.')
        from .studio.project import Project
        project = Project.from_dict(value['project'])
        if value.get('id') != path.parts[2] or value.get('fingerprint') != project.fingerprint:
            raise ValueError('Restored production plan identity changed.')
        if type(value.get('destination')) is not str:
            raise ValueError('Invalid restored production destination.')
        if 'operation_sha256' in value:
            from .studio.render import _operation_sha256
            if (value.get('format') not in ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma')
                    or type(value.get('stems')) is not bool
                    or value['operation_sha256'] != _operation_sha256(value)):
                raise ValueError('Production operation options changed.')
        return {**value, 'restore_requires_new_output': True}
    if schema == 'carr.reader-session.v1':
        from .speech.reading import ReadingDocument
        from .speech.session import ReaderResume
        if set(value) != {'schema', 'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds'}:
            raise ValueError('Unsupported restored reading session.')
        ReaderResume(ReadingDocument.from_dict(value['document']), value['voice_id'],
            value['rate'], value['volume'], value['cursor'], value['audio_seconds'])
    return None


def _reading_tab_rows(index, inventory, *, cancelled):
    """Preflight referenced byte totals before any retained history is replayed."""
    from .speech import tabs
    selected, total = [], 0
    for tab in index.tabs:
        if cancelled():
            raise InterruptedError('Reading-tab backup verification cancelled.')
        name = tab.filename.removeprefix('state/').casefold()
        entry = inventory.get(name)
        if entry is None:
            raise ValueError('Reading-tab recovery references a missing retained draft.')
        size = entry['size_bytes']
        if type(size) is not int or size <= 0:
            raise ValueError('Invalid retained reading-tab draft size.')
        total += size
        if total > tabs.MAX_TOTAL_DRAFT_BYTES:
            raise ValueError('Reading tabs exceed their aggregate recovery byte budget.')
        selected.append((name, entry))
    return selected


def _validate_reading_tabs(root, entries, *, cancelled):
    """Validate passive membership against the exact copied state inventory.

    Use the existing portable case identity without renaming any user file.
    No ReaderDraftStore is constructed and no saved voice is activated.
    """
    from .speech import tabs
    from .speech.session import ReaderDraftStore
    from .durable_state import read_json_snapshot

    index_name = tabs.NAME.removeprefix('state/').casefold()
    inventory = {}
    for entry in entries:
        if cancelled():
            raise InterruptedError('Reading-tab backup verification cancelled.')
        name = entry['filename'].casefold()
        if name in inventory:
            raise ValueError('Ambiguous reading-tab state inventory.')
        inventory[name] = entry
    index_entry = inventory.get(index_name)
    if index_entry is None:
        return frozenset()
    if (type(index_entry['size_bytes']) is not int
            or not 0 < index_entry['size_bytes'] <= tabs.MAX_BYTES):
        raise ValueError('Reading-tab membership exceeds its recovery byte budget.')
    if cancelled():
        raise InterruptedError('Reading-tab backup verification cancelled.')
    value, _ = read_json_snapshot(root / index_entry['filename'],
                                 max_bytes=index_entry['size_bytes'])
    index = tabs.ReadingTabs.from_dict(value)
    selected = _reading_tab_rows(index, inventory, cancelled=cancelled)
    captured, characters = [], 0
    for name, entry in selected:
        if cancelled():
            raise InterruptedError('Reading-tab backup verification cancelled.')
        record, _ = read_json_snapshot(root / entry['filename'], max_bytes=entry['size_bytes'])
        characters += tabs._history_characters(record)
        if characters > tabs.MAX_RETAINED_CHARACTERS:
            raise ValueError('Reading tabs exceed their aggregate retained-history budget.')
        captured.append((name, record))
    for _, record in captured:
        if cancelled():
            raise InterruptedError('Reading-tab backup verification cancelled.')
        ReaderDraftStore.decode(record)
    if cancelled():
        raise InterruptedError('Reading-tab backup verification cancelled.')
    return frozenset((index_name, *(name for name, _ in selected)))


def validate_state_documents(root, entries, *, cancelled=lambda: False, restored=False,
                             destination_state_root=None):
    """Check typed state without touching model paths or external output files."""
    from . import recording_backup, backup_transcription
    from .studio import backup_inputs
    entries = backup_transcription.validate_state(root, entries, cancelled=cancelled, restored=restored)
    recording_backup.validate_state_entries(entries, cancelled=cancelled)
    clip_receipt = backup_inputs.validate_state(root, entries, cancelled=cancelled)
    validated_tabs = _validate_reading_tabs(root, entries, cancelled=cancelled)
    from .studio import book_backup
    validated_books = book_backup.validate_state(root, entries, cancelled=cancelled)
    if any(entry['filename'].casefold() == 'speech/playback-queue.json' for entry in entries):
        from .speech.playlist import validate_state_collection
        validate_state_collection(root, entries)
    for entry in entries:
        if cancelled():
            raise InterruptedError('Application state verification cancelled.')
        relative = _dispatch_path(entry['filename'])
        if relative.as_posix() in validated_tabs or relative.as_posix() in validated_books:
            continue
        if relative == PurePosixPath('audio-metadata/choices.json'):
            from .audio.metadata_review import MAX_BYTES
            if not 0 < entry['size_bytes'] <= MAX_BYTES:
                raise ValueError('Saved audio metadata choices exceed their recovery size limit.')
        if relative == PurePosixPath('speech/playback-queue.json'):
            from .speech.playlist import MAX_STATE_BYTES
            if not 0 < entry['size_bytes'] <= MAX_STATE_BYTES:
                raise ValueError('Saved playback queue exceeds its recovery size limit.')
        if relative == PurePosixPath('speech/presentation.json'):
            from .speech.presentation import MAX_BYTES
            if not 0 < entry['size_bytes'] <= MAX_BYTES:
                raise ValueError('Reading appearance settings exceed their recovery size limit.')
        if backup_inputs.namespace('state/' + entry['filename']):
            continue
        if backup_transcription.namespace('state/' + entry['filename']):
            continue
        if relative.as_posix().startswith(recording_backup.STATE_PREFIX):
            # The dedicated copied-tree verifier below parses only receipts,
            # streams actual PCM and validates the complete retake graph.
            # An unreceipted final WAV remains inert and is never decoded.
            continue
        if relative.suffix != '.json':
            continue
        if relative == PurePosixPath('operations/resources.json') and entry['size_bytes'] > 32768:
            raise ValueError('Resource policy exceeds its bounded state record size.')
        path = root / entry['filename']
        # Opaque state can contain larger JSON exports. Known application JSON
        # uses the bounded durable-state contract; no arbitrary file is executed.
        if entry['size_bytes'] > 32 * 1024 * 1024:
            if (relative.as_posix().startswith(('assistant/', 'translation/', 'providers/',
                                              'studio/jobs/', 'studio/publications/', 'speech/', 'transcription/'))
                    or relative.name.endswith(('.profile.json', '.usage.json'))
                    or relative in (PurePosixPath('audio/engines.json'), PurePosixPath('operations/resources.json'),
                                    PurePosixPath('studio/draft.json'))):
                raise ValueError('Typed application state exceeds its recovery JSON budget.')
            continue
        try:
            value = read_json(path)
        except (UnicodeError, json.JSONDecodeError):
            if (_publication_path(relative)
                    or relative in (PurePosixPath('studio/draft.json'),
                                    PurePosixPath('audio-metadata/choices.json'),
                                    PurePosixPath('speech/presentation.json'),
                                    PurePosixPath('transcription/engine.json'), PurePosixPath('audio/engines.json'),
                                    PurePosixPath('operations/resources.json'))
                    or len(relative.parts) == 5 and relative.parts[0] == 'transcription'
                    and relative.parts[2] == 'jobs' and relative.name == 'job.json'):
                raise ValueError('Invalid typed transcription recovery JSON.') from None
            # The canonical legacy state component also retained non-JSON
            # diagnostic files with a .json suffix. They cannot be active jobs.
            continue
        updated = _prepared(entry['filename'], value)
        if restored and updated is not None:
            atomic_json(path, updated, replace=True)
    recording_backup.validate_state_root(root, cancelled=cancelled)
    if restored:
        entries = backup_inputs.restore_inputs(root, entries, clip_receipt,
            destination_state_root=destination_state_root, cancelled=cancelled)
    return entries


def prepare_restored_state(root, entries, *, cancelled=lambda: False, destination_state_root=None):
    return validate_state_documents(root, entries, cancelled=cancelled, restored=True,
                                    destination_state_root=destination_state_root)
