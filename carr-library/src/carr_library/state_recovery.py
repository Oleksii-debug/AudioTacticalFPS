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
    from .assistant.translation import split_text
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


def _prepared(relative, value):
    """Return a modified typed record, or None for unchanged opaque state."""
    path = _dispatch_path(relative)
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
                or set(value) not in (fields, fields | {'restore_requires_revalidation'})
                or type(value.get('restore_requires_revalidation', False)) is not bool
                or any(not isinstance(value[key], list) or len(value[key]) > 100
                       for key in ('piper', 'voice_helpers', 'music_models'))):
            raise ValueError('Invalid restored local-engine configuration.')
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
        return {**value, 'restore_requires_new_output': True}
    if schema == 'carr.reader-session.v1':
        from .speech.reading import ReadingDocument
        from .speech.session import ReaderResume
        if set(value) != {'schema', 'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds'}:
            raise ValueError('Unsupported restored reading session.')
        ReaderResume(ReadingDocument.from_dict(value['document']), value['voice_id'],
            value['rate'], value['volume'], value['cursor'], value['audio_seconds'])
    return None


def validate_state_documents(root, entries, *, cancelled=lambda: False, restored=False):
    """Check typed state without touching model paths or external output files."""
    for entry in entries:
        if cancelled():
            raise InterruptedError('Application state verification cancelled.')
        relative = _dispatch_path(entry['filename'])
        if relative.suffix != '.json':
            continue
        path = root / entry['filename']
        # Opaque state can contain larger JSON exports. Known application JSON
        # uses the bounded durable-state contract; no arbitrary file is executed.
        if entry['size_bytes'] > 32 * 1024 * 1024:
            if (relative.as_posix().startswith(('assistant/', 'translation/', 'providers/',
                                              'studio/jobs/', 'studio/publications/', 'speech/', 'transcription/'))
                    or relative.name.endswith(('.profile.json', '.usage.json'))
                    or relative == PurePosixPath('audio/engines.json')):
                raise ValueError('Typed application state exceeds its recovery JSON budget.')
            continue
        try:
            value = read_json(path)
        except (UnicodeError, json.JSONDecodeError):
            if (_publication_path(relative)
                    or relative in (PurePosixPath('transcription/engine.json'), PurePosixPath('audio/engines.json'))
                    or len(relative.parts) == 5 and relative.parts[0] == 'transcription'
                    and relative.parts[2] == 'jobs' and relative.name == 'job.json'):
                raise ValueError('Invalid typed transcription recovery JSON.') from None
            # The canonical legacy state component also retained non-JSON
            # diagnostic files with a .json suffix. They cannot be active jobs.
            continue
        updated = _prepared(entry['filename'], value)
        if restored and updated is not None:
            atomic_json(path, updated, replace=True)


def prepare_restored_state(root, entries, *, cancelled=lambda: False):
    validate_state_documents(root, entries, cancelled=cancelled, restored=True)
