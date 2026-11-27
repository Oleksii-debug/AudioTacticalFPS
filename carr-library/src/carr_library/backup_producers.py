"""Passive backup validators for the primary reader, studio and assistant.

These validators never resolve external paths, open a credential vault, start
an engine, or execute a saved plan. Restoration preserves declarations and
checkpoint bytes; each producer still checks permissions and assets on use.
"""
from hashlib import sha256
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
from uuid import NAMESPACE_URL, uuid5

from . import backup_book_audio

ID = r'[A-Za-z0-9_-]{1,80}'
HEX = r'[0-9a-f]{64}'
READER = re.compile(r'state/speech/(' + HEX + r')\.reader\.json\Z')
PROFILE = re.compile(r'state/providers/(' + ID + r')\.profile\.json\Z')
USAGE = re.compile(r'state/(?:providers/usage|translation/provider/usage)/(' + ID + r')\.usage\.json\Z')
TRANSLATION = re.compile(r'state/translation/jobs/([0-9a-f]{32})(\.control)?\.json\Z')
LANGUAGES = re.compile(r'state/translation/languages-(' + ID + r')\.json\Z')
ASSISTANT = re.compile(r'state/assistant/([0-9a-f]{32})(\.control)?\.json\Z')
TEMPLATE = re.compile(r'state/assistant/templates/([0-9a-f]{32})\.json\Z')
STUDIO = re.compile(r'state/studio/jobs/(' + ID + r')/(job|control|project|timeline)\.json\Z')
SUBTITLE = re.compile(r'state/studio/jobs/(' + ID + r')/script\.(srt|vtt|lrc)\Z')
RECORDING = re.compile(r'state/studio/recordings/([0-9a-f]{32})\.(json|wav)\Z')
MANAGED_INPUT = re.compile(r'state/studio/inputs/(' + HEX + r')\.audio\Z')
PUBLICATION = re.compile(r'state/studio/publications/([0-9a-f]{32})\.json\Z')
MAX_PRIMARY_BYTES = 32 * 1024**3
TRUSTED_CLIENT = frozenset({'state/trusted-client/entitlement.json',
                            'state/trusted-client/entitlement.pending.json'})


def limit(name):
    if maximum := backup_book_audio.limit(name):
        return maximum
    if name == 'state/studio/draft.json':
        return 32 * 1024 * 1024
    if TEMPLATE.fullmatch(name):
        from .assistant.templates import MAX_TEMPLATE_BYTES
        return MAX_TEMPLATE_BYTES
    if name in TRUSTED_CLIENT:
        return 65536
    if name == 'state/operations/resources.json':
        return 32768
    if match := RECORDING.fullmatch(name):
        return 2 * 1024**3 if match[2] == 'wav' else 32 * 1024 * 1024
    if MANAGED_INPUT.fullmatch(name):
        return 8 * 1024**3
    if PUBLICATION.fullmatch(name):
        return 32 * 1024 * 1024
    if READER.fullmatch(name) or STUDIO.fullmatch(name) or SUBTITLE.fullmatch(name):
        return 32 * 1024 * 1024
    if PROFILE.fullmatch(name):
        return 32768
    if LANGUAGES.fullmatch(name):
        return 1024 * 1024
    if USAGE.fullmatch(name) or ASSISTANT.fullmatch(name) or TRANSLATION.fullmatch(name):
        return 64 * 1024 * 1024
    return None


def is_media(name):
    match = RECORDING.fullmatch(name)
    return bool(MANAGED_INPUT.fullmatch(name) or match and match[2] == 'wav')


def validate_primary_collection(root, entries, records, *, cancelled):
    """Validate retained originals on copied paths; never open old outputs."""
    from .assistant.templates import validate_template_collection
    validate_template_collection(entries)
    from .speech.runtime import _wave_info
    from .state_backup import _validate_managed_clip_inputs
    by_name = {entry['filename']: entry for entry in entries}
    for name, entry in by_name.items():
        if cancelled():
            raise InterruptedError('Backup operation cancelled.')
        if match := MANAGED_INPUT.fullmatch(name):
            if entry['sha256'] != match[1]:
                raise ValueError('A managed Studio input differs from its content identity.')
        elif match := RECORDING.fullmatch(name):
            receipt_name = name.rsplit('.', 1)[0] + '.json'
            media_name = name.rsplit('.', 1)[0] + '.wav'
            receipt, media = records.get(receipt_name), by_name.get(media_name)
            if receipt is None or media is None:
                raise ValueError('A retained recording needs both its original WAV and verified receipt.')
            audio = receipt['audio']
            if media['size_bytes'] != audio['size_bytes'] or media['sha256'] != audio['sha256']:
                raise ValueError('The retained recording differs from its verified receipt.')
            if match[2] == 'wav':
                actual = _wave_info(root / media_name, cancelled=cancelled,
                                    max_output_bytes=receipt['limits']['max_bytes'])
                expected = tuple(audio[key] for key in ('sample_rate', 'channels', 'frames', 'sample_width'))
                if actual != expected:
                    raise ValueError('The retained recording PCM differs from its verified receipt.')
    state_entries = [{**entry, 'filename': entry['filename'][6:]}
                     for entry in entries if entry['filename'].startswith('state/')]
    _validate_managed_clip_inputs(root / 'state', state_entries, cancelled=cancelled)
    draft_name = 'state/studio/draft.json'
    if draft_name in by_name:
        draft = records.get(draft_name)
        _studio_draft(draft)
        identity = draft['project_id']
        if identity is not None:
            if cancelled():
                raise InterruptedError('Studio draft backup validation cancelled.')
            from .backup_sidecars import _json, read_regular
            from .durable_state import local_path
            from .studio.project import Project
            name = 'state/studio/projects/' + identity + '/project.json'
            entry = by_name.get(name)
            if entry is None:
                raise ValueError('Studio draft has no matching copied saved project.')
            raw = read_regular(local_path(root / name, root=root, exists=True), 32 * 1024 * 1024)
            if (len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']
                    or Project.from_dict(_json(raw)).id != identity):
                raise ValueError('Studio draft project differs from the copied inventory.')


def validate_translation_collection(db, records, *, cancelled):
    """Recheck copied translation jobs/ledgers and source slices, with no I/O."""
    from types import SimpleNamespace
    from .assistant.translation import TranslationService
    from .backup_sidecars import _validate_original_text
    from .reader.reading_state import ReadingLocation
    for name, job in records.items():
        match = TRANSLATION.fullmatch(name)
        if not match or match[2] is not None:
            continue
        if cancelled():
            raise InterruptedError('Backup operation cancelled.')
        identity = match[1]
        def load_job(wanted):
            if wanted != identity:
                raise ValueError('The copied translation identity changed.')
            return job
        def load_usage(authorization):
            value = records.get('state/translation/provider/usage/' + authorization + '.usage.json')
            if value is None:
                raise ValueError('Accepted translation usage evidence is unavailable.')
            return value
        # Do not call either constructor: both normally create directories and
        # configure runtime gateways. _load uses only these two passive readers.
        service = object.__new__(TranslationService)
        service.jobs = SimpleNamespace(load=load_job)
        service.gateway = SimpleNamespace(ledger=SimpleNamespace(get=load_usage))
        checked = service._load(identity)
        binding = TranslationService._binding(checked)
        if binding is None:
            continue
        anchor = binding['anchor']
        if binding['topic'] is not None and db.execute(
                'SELECT 1 FROM research_topic WHERE workspace=? AND id=?',
                (anchor['workspace'], binding['topic'])).fetchone() is None:
            raise ValueError('Translation topic does not belong to its saved library.')
        for position in anchor['positions']:
            if cancelled():
                raise InterruptedError('Backup operation cancelled.')
            location = ReadingLocation(anchor['workspace'], anchor['edition'], anchor['revision'],
                position['kind'], position['index'], position['text_sha256'], anchor['asset_sha256'])
            _validate_original_text(db, location,
                job['source_text'][position['start']:position['end']])
            row = db.execute('SELECT physical,digest FROM page WHERE revision=? AND ordinal=?',
                             (anchor['revision'], position['ordinal'])).fetchone()
            physical = position['index'] if position['kind'] == 'physical_page' else None
            if row is None or tuple(row) != (physical, position['text_sha256']):
                raise ValueError('Translation source position differs from the copied library.')
            # text_verified_at_capture is historical evidence, not a fresh
            # quote approval. Do not relabel it from a later live attestation.


def fields(value, required, optional=()):
    if (type(value) is not dict or not set(required) <= value.keys()
            or not value.keys() <= set(required) | set(optional)):
        raise ValueError('Unknown or missing checkpoint fields.')


def text(value, maximum=32768, *, empty=False):
    if (type(value) is not str or len(value) > maximum or '\x00' in value
            or (not empty and not value.strip())):
        raise ValueError('Invalid checkpoint text.')
    value.encode('utf-8', 'strict')


def number(value, minimum=0, maximum=1e18, *, integer=False):
    if (type(value) not in ((int,) if integer else (int, float))
            or not minimum <= value <= maximum or not math.isfinite(value)):
        raise ValueError('Invalid checkpoint number.')


def digest(value):
    if type(value) is not str or not re.fullmatch(HEX, value):
        raise ValueError('Invalid checkpoint digest.')


def path(value):
    """Check inert cross-platform references without consulting this machine."""
    text(value)
    normalized = value.replace('\\', '/')
    without_drive = value[2:] if re.match(r'^[A-Za-z]:', value) else value
    if (normalized.startswith('//') or '://' in value or ':' in without_drive
            or any(ord(c) < 32 for c in value)
            or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())):
        raise ValueError('Invalid local checkpoint reference.')


def artifacts(values):
    if type(values) is not list or len(values) > 100000:
        raise ValueError('Invalid artifact inventory.')
    seen = set()
    for row in values:
        fields(row, {'path', 'sha256', 'size'})
        path(row['path'])
        digest(row['sha256'])
        number(row['size'], integer=True)
        if row['path'] in seen:
            raise ValueError('Duplicate artifact path.')
        seen.add(row['path'])


def _studio_draft(value):
    """Retain even an invalid editable script; never parse it or select a voice."""
    from .durable_state import encode
    from .studio.project import _id
    fields(value, {'schema', 'text', 'title', 'voice_ids', 'project_id'})
    if (type(value['schema']) is not int or value['schema'] != 1
            or type(value['text']) is not str or type(value['title']) is not str
            or type(value['voice_ids']) is not list
            or any(type(voice) is not str for voice in value['voice_ids'])):
        raise ValueError('Invalid editable Studio draft fields.')
    if value['project_id'] is not None:
        _id(value['project_id'])
    # Autosave deliberately precedes script validation. Empty/incomplete fields
    # and text exceeding parsed-project limits remain inert recovery data.
    if len(encode(value)) > 32 * 1024 * 1024:
        raise ValueError('Studio draft exceeds the existing JSON recovery budget.')


def _studio_job(value, identity):
    from .studio.project import Project
    fields(value, {'schema', 'id', 'status', 'project', 'fingerprint', 'destination',
                   'format', 'stems', 'nodes', 'artifacts', 'progress', 'created',
                   'updated', 'error', 'runtime'}, {'result', 'total_disk_bytes', 'origin', 'recovery', 'resource_plan'})
    if type(value['schema']) is not int or value['schema'] != 1 or value['id'] != identity:
        raise ValueError('Invalid production job identity.')
    project = Project.from_dict(value['project'])
    if value['fingerprint'] != project.fingerprint:
        raise ValueError('Production project fingerprint changed.')
    if value['status'] not in ('pending', 'queued', 'attention', 'running', 'paused', 'cancelled', 'failed', 'complete'):
        raise ValueError('Invalid production status.')
    if value['format'] not in ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma'):
        raise ValueError('Invalid production output format.')
    if type(value['stems']) is not bool or type(value['nodes']) is not dict or len(value['nodes']) > 100000:
        raise ValueError('Invalid production options or nodes.')
    path(value['destination'])
    for name in ('created', 'updated'):
        number(value[name])
    for name in ('progress', 'error'):
        text(value[name], 8192, empty=True)
    for key, node in value['nodes'].items():
        digest(key)
        fields(node, {'kind', 'sha256', 'reused'})
        text(node['kind'], 128)
        digest(node['sha256'])
        if type(node['reused']) is not bool:
            raise ValueError('Invalid node reuse marker.')
    artifacts(value['artifacts'])
    if value['runtime'] is not None and type(value['runtime']) is not dict:
        raise ValueError('Invalid runtime inventory.')
    if 'total_disk_bytes' in value:
        number(value['total_disk_bytes'], 1024**2, 2**63 - 1, integer=True)
    if 'resource_plan' in value:
        _resource_plan(value['resource_plan'])
    if project.schema == 2:
        _origin(value.get('origin'))
    elif 'origin' in value:
        raise ValueError('Legacy production must not gain portable origin authority.')
    if 'recovery' in value:
        recovery = value['recovery']
        fields(recovery, {'requires_output_directory'}, {'previous', 'output_approved'})
        if project.schema != 2 or type(recovery['requires_output_directory']) is not bool:
            raise ValueError('Invalid portable production recovery state.')
        if ('previous' in recovery) != ('output_approved' in recovery):
            raise ValueError('Incomplete portable output approval history.')
        if 'output_approved' in recovery:
            number(recovery['output_approved'])
        if type(recovery.get('previous', [])) is not list or len(recovery.get('previous', [])) > 10000:
            raise ValueError('Invalid portable production recovery history.')
        for previous in recovery.get('previous', []):
            fields(previous, {'origin', 'destination', 'status', 'nodes', 'artifacts', 'runtime', 'result'})
            _origin(previous['origin'])
            path(previous['destination'])
            if previous['status'] not in ('pending', 'queued', 'attention', 'running', 'paused', 'cancelled', 'failed', 'complete'):
                raise ValueError('Invalid historical production status.')
            if type(previous['nodes']) is not dict or len(previous['nodes']) > 100000:
                raise ValueError('Invalid historical production nodes.')
            artifacts(previous['artifacts'])
            if previous['runtime'] is not None and type(previous['runtime']) is not dict:
                raise ValueError('Invalid historical runtime inventory.')
            if previous['result'] is not None:
                _timeline(previous['result'])
    if 'result' in value:
        _timeline(value['result'])
        if (value['result']['project'] != project.id or value['result']['revision'] != project.revision
                or value['result']['source_sha256'] != sha256(project.original_script.encode()).hexdigest()):
            raise ValueError('Production result belongs to another project.')


def _resource_plan(value):
    """Passive estimates only; never resolve recorded disk/source paths."""
    from .operations.models import ResourceRequest
    fields(value, {'schema', 'request', 'source_sha256', 'estimated_duration',
                   'cpu_basis', 'memory_basis', 'disk_basis', 'enforcement'})
    if type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Invalid recorded resource plan schema.')
    request = ResourceRequest.from_dict(value['request'])
    for selected, _ in request.disks:
        path(str(selected))
    if type(value['source_sha256']) is not dict or len(value['source_sha256']) > 100000:
        raise ValueError('Invalid resource source identity inventory.')
    for selected, checksum in value['source_sha256'].items():
        path(selected)
        digest(checksum)
    number(value['estimated_duration'])
    for name in ('cpu_basis', 'memory_basis', 'disk_basis', 'enforcement'):
        text(value[name], 8192)


def _origin(value):
    fields(value, {'studio_root', 'nonce'})
    path(value['studio_root'])
    digest(value['nonce'])


def _timeline(value):
    fields(value, {'schema', 'project', 'revision', 'source_sha256', 'chapters', 'lines',
                   'duration', 'sample_rate', 'channels', 'mode', 'local_only', 'loudness',
                   'music_kind', 'timing_notice'}, {'music_continuity', 'clips', 'clip_stems_notice', 'lrc_notice', 'book_source_sha256'})
    if type(value['schema']) is not int or value['schema'] != 1 or value['local_only'] is not True:
        raise ValueError('Invalid timeline schema.')
    if type(value['project']) is not str or not re.fullmatch(ID, value['project']):
        raise ValueError('Invalid timeline project identity.')
    digest(value['source_sha256'])
    number(value['revision'], 1, integer=True)
    number(value['duration'], 0, 31_536_000)
    number(value['sample_rate'], 8000, 384000, integer=True)
    number(value['channels'], 1, 2, integer=True)
    if value['mode'] not in ('faithful', 'authored') or type(value['loudness']) is not dict:
        raise ValueError('Invalid timeline mode or loudness.')
    for name in ('music_kind', 'timing_notice'):
        text(value[name], 8192)
    if 'music_continuity' in value:
        text(value['music_continuity'], 8192)
    if 'lrc_notice' in value:
        text(value['lrc_notice'], 8192)
    if 'book_source_sha256' in value:
        digest(value['book_source_sha256'])
    if ('clips' in value) != ('clip_stems_notice' in value):
        raise ValueError('Incomplete clip timeline declaration.')
    if 'clips' in value:
        text(value['clip_stems_notice'], 8192)
        if type(value['clips']) is not list or len(value['clips']) > 100000:
            raise ValueError('Invalid clip timeline inventory.')
        seen = set()
        for row in value['clips']:
            fields(row, {'scene', 'id', 'role', 'title', 'source_sha256', 'source_duration',
                         'trim_start', 'duration', 'start', 'end', 'gain_db', 'pan', 'fade_in', 'fade_out'})
            for key in ('scene', 'id', 'title'):
                text(row[key], 8192, empty=key == 'title')
            if row['role'] not in ('ambience', 'music', 'sfx', 'microphone'):
                raise ValueError('Invalid clip timeline role.')
            identity = row['scene'], row['id']
            if identity in seen:
                raise ValueError('Duplicate clip timeline identity.')
            seen.add(identity)
            digest(row['source_sha256'])
            number(row['source_duration'], 0, 31_536_000)
            number(row['trim_start'], 0, row['source_duration'])
            number(row['duration'], 0, row['source_duration'] + 1)
            number(row['start'], 0, value['duration'] + 1)
            number(row['end'], row['start'], value['duration'] + 1)
            number(row['gain_db'], -96, 24)
            number(row['pan'], -1, 1)
            for key in ('fade_in', 'fade_out'):
                number(row[key], 0, 31_536_000)
    for name, required in [('chapters', {'id', 'title', 'start', 'end'}),
                           ('lines', {'scene', 'line', 'role', 'text', 'start', 'end', 'timing', 'source_ref'})]:
        if type(value[name]) is not list or len(value[name]) > 100000:
            raise ValueError('Invalid timeline inventory.')
        for row in value[name]:
            fields(row, required)
            number(row['start'], 0, value['duration'] + 1)
            number(row['end'], row['start'], value['duration'] + 1)
            for key in required - {'start', 'end'}:
                text(row[key], 100000 if key == 'text' else 8192, empty=key in ('text', 'source_ref'))


def _assistant_job(db, value, identity):
    from .assistant.models import Step, canonical
    from .assistant.service import _check_arguments
    fields(value, {'schema', 'id', 'data_dir', 'plan', 'plan_sha256', 'created', 'updated',
                   'status', 'message', 'workspace', 'steps'})
    if value['schema'] != 'carr-assistant-job/1' or value['id'] != identity:
        raise ValueError('Invalid assistant identity.')
    path(value['data_dir'])
    plan = value['plan']
    fields(plan, {'schema', 'scope', 'steps', 'summary'})
    if plan['schema'] != 'carr-assistant-plan/1':
        raise ValueError('Invalid assistant plan schema.')
    if value['plan_sha256'] != sha256(canonical(plan).encode('utf-8')).hexdigest():
        raise ValueError('Approved assistant plan fingerprint changed.')
    scope = plan['scope']
    fields(scope, {'workspace_id', 'input_roots', 'output_root', 'allow_create', 'allow_full_backup',
                   'max_steps', 'max_files', 'max_seconds', 'max_bytes'})
    for name in ('allow_create', 'allow_full_backup'):
        if type(scope[name]) is not bool:
            raise ValueError('Invalid assistant permission.')
    number(scope['max_steps'], 1, 10000, integer=True)
    number(scope['max_files'], 1, 1000000, integer=True)
    number(scope['max_bytes'], 1, integer=True)
    number(scope['max_seconds'], 1e-12)
    if type(scope['input_roots']) is not list or len(scope['input_roots']) > 100000:
        raise ValueError('Invalid input permissions.')
    for item in scope['input_roots']:
        path(item)
    if scope['output_root'] is not None:
        path(scope['output_root'])
    if (type(plan['steps']) is not list or not 1 <= len(plan['steps']) <= scope['max_steps']
            or type(value['steps']) is not list or len(value['steps']) != len(plan['steps'])):
        raise ValueError('Invalid assistant checkpoint step inventory.')
    text(plan['summary'], 16384, empty=True)
    creates = 0
    for index, (raw, checkpoint) in enumerate(zip(plan['steps'], value['steps'])):
        fields(raw, {'tool', 'arguments'})
        step = Step(**raw)
        _check_arguments(step)
        if step.tool == 'library.create':
            creates += 1
            if index or not scope['allow_create'] or scope['workspace_id'] is not None:
                raise ValueError('Invalid create-library permission.')
        if step.tool == 'library.backup' and not scope['allow_full_backup']:
            raise ValueError('Full backup lacks its saved permission.')
        fields(checkpoint, {'status', 'intent', 'result', 'message'})
        if checkpoint['status'] not in ('pending', 'running', 'complete', 'failed'):
            raise ValueError('Invalid assistant step status.')
        if type(checkpoint['intent']) is not dict or (checkpoint['result'] is not None and type(checkpoint['result']) is not dict):
            raise ValueError('Invalid typed assistant result.')
        if checkpoint['result'] is not None:
            artifacts(checkpoint['result'].get('artifacts', []))
        text(checkpoint['message'], 8192, empty=True)
    if creates > 1:
        raise ValueError('Multiple library creation steps.')
    workspace = value['workspace']
    if scope['workspace_id'] is not None:
        text(scope['workspace_id'], 128)
        if workspace != scope['workspace_id']:
            raise ValueError('Assistant workspace permission changed.')
    elif workspace is not None and (not scope['allow_create'] or workspace != uuid5(
            NAMESPACE_URL, 'carr-assistant:' + identity + ':library').hex):
        raise ValueError('Invalid created workspace checkpoint.')
    if workspace is not None and db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (workspace,)).fetchone() is None:
        raise ValueError('Assistant checkpoint belongs to a missing library.')
    if value['status'] not in ('queued', 'running', 'paused', 'cancelled', 'complete', 'attention', 'failed'):
        raise ValueError('Invalid assistant job status.')
    for name in ('created', 'updated'):
        text(value[name], 128)
    text(value['message'], 8192, empty=True)


def _usage(value):
    from .assistant.credentials import validate_profile_id
    from .assistant.profiles import CAPABILITIES
    from .assistant.providers import money
    fields(value, {'schema', 'authorization', 'currency', 'operations'})
    if value['schema'] != 'carr-provider-usage/1' or not re.fullmatch('[A-Z]{3}', value['currency']):
        raise ValueError('Invalid provider ledger schema.')
    digest(value['authorization'])
    if type(value['operations']) is not dict or len(value['operations']) > 100000:
        raise ValueError('Invalid provider operation inventory.')
    for identity, row in value['operations'].items():
        validate_profile_id(identity)
        fields(row, {'profile', 'model', 'operation', 'request_sha256', 'reserved_cost', 'cost_kind',
                     'actual_cost', 'status', 'output_sha256', 'usage'}, {'profile_sha256'})
        if 'profile_sha256' in row:
            digest(row['profile_sha256'])
        validate_profile_id(row['profile'])
        text(row['model'], 256)
        digest(row['request_sha256'])
        money(row['reserved_cost'])
        if (row['operation'] not in CAPABILITIES or row['status'] not in ('reserved', 'complete', 'uncertain', 'not_sent')
                or row['cost_kind'] != 'user-approved upper bound' or row['actual_cost'] is not None):
            raise ValueError('Invalid provider reservation.')
        if row['output_sha256'] is not None:
            digest(row['output_sha256'])
        if row['usage'] is not None:
            if type(row['usage']) is not dict or len(row['usage']) > 100:
                raise ValueError('Invalid provider usage.')
            for key, count in row['usage'].items():
                text(key, 128)
                if (key in {'translation_sha256', 'decoded_translation_sha256'}
                        and row['operation'] == 'translation.text'):
                    digest(count)
                else:
                    number(count, integer=True)


def _translation_languages(value, identity):
    from .assistant.models import canonical
    from .assistant.profiles import google_v3_parent
    from .assistant.translation import ENDPOINT
    fields(value, {'schema', 'profile', 'endpoint', 'model', 'fetched_at', 'languages',
                   'languages_sha256', 'qualification'})
    advanced = value['endpoint'] != ENDPOINT
    if (value['schema'] != 'carr-translation-languages/1' or value['profile'] != identity
            or value['model'] != ('general/nmt' if advanced else 'nmt')
            or value['qualification'] != 'provider-declared; not acoustic or translation-quality verification'):
        raise ValueError('Invalid translation language inventory identity.')
    if advanced:
        google_v3_parent(value['endpoint'])
    text(value['fetched_at'], 128)
    rows = value['languages']
    if type(rows) is not list or not 1 <= len(rows) <= 1000:
        raise ValueError('Invalid provider language inventory.')
    seen = set()
    for row in rows:
        fields(row, {'language', 'name', 'source', 'target'} if advanced else {'language', 'name'})
        _translation_language(row['language'])
        text(row['name'], 1000)
        if advanced and (type(row['source']) is not bool or type(row['target']) is not bool
                         or not (row['source'] or row['target'])):
            raise ValueError('Invalid provider language capability.')
        if row['language'] in seen:
            raise ValueError('Duplicate translation language.')
        seen.add(row['language'])
    if sha256(canonical(rows).encode()).hexdigest() != value['languages_sha256']:
        raise ValueError('Translation language inventory checksum changed.')


def _translation_language(value, *, auto=False):
    if type(value) is not str or not (auto and value == 'auto' or
            re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', value)):
        raise ValueError('Invalid translation language code.')


def _translation_job(value, identity):
    """Passive v1/v2 translation validation; never open the original library."""
    from .assistant.profiles import ProviderProfile, google_v3_parent
    from .assistant.providers import Authorization
    from .assistant.translation import TranslationService, split_text, _approved_requests
    fields(value, {'schema', 'kind', 'id', 'created', 'updated', 'status', 'profile', 'title',
        'source_text', 'source_sha256', 'source_language', 'target_language', 'chunks', 'receipts',
        'authorization', 'per_call_cost'}, {'source_binding', 'glossary'})
    if (value['schema'] != 'carr-assistant-job/1'
            or value['kind'] not in ('translation/1', 'translation/2')
            or value['id'] != identity
            or value['status'] not in ('queued', 'running', 'paused', 'attention', 'complete')):
        raise ValueError('Invalid translation checkpoint identity or status.')
    for name in ('created', 'updated'):
        text(value[name], 128)
    text(value['title'], 512)
    text(value['source_text'], 2_000_000)
    if sha256(value['source_text'].encode()).hexdigest() != value['source_sha256']:
        raise ValueError('Translation original source changed.')
    chunks = split_text(value['source_text'])
    if type(value['chunks']) is not list or value['chunks'] != list(chunks):
        raise ValueError('Translation chunks no longer bind the original.')
    # These producer checks are pure: source lineage and UUID bind the saved
    # content/approval, but grant no access to the old absolute library path.
    TranslationService._binding(value)
    TranslationService._admission_budget(value)
    TranslationService._output_budget(value)
    profile = ProviderProfile(**value['profile'])
    _translation_language(value['source_language'], auto=True)
    _translation_language(value['target_language'])
    if value['source_language'] == value['target_language']:
        raise ValueError('Translation needs different source and target languages.')
    approval = Authorization(**value['authorization'])
    try:
        by_chunk, _ = _approved_requests(profile, chunks, value['source_language'],
            value['target_language'], approval, value['per_call_cost'],
            data_class='selected-script' if value['kind'] == 'translation/2' else 'selected-excerpt',
            glossary=value.get('glossary'))
    except PermissionError as exc:
        raise ValueError('Translation checkpoint no longer matches its approved scope.') from exc
    receipts = value['receipts']
    if type(receipts) is not list or len(receipts) > len(chunks):
        raise ValueError('Invalid translation receipt inventory.')
    for index, row in enumerate(receipts):
        required = {'index', 'source_sha256', 'text', 'translation_sha256', 'detected_source',
                    'request_sha256', 'response_sha256'}
        advanced = profile.adapter == 'google-translation-v3' and by_chunk[index] is not None
        if advanced:
            required |= {'reported_model', 'glossary_config'}
        fields(row, required, {'decoded_translation_sha256'} if by_chunk[index] is not None else set())
        if 'decoded_translation_sha256' in row:
            digest(row['decoded_translation_sha256'])
        if (type(row['index']) is not int or row['index'] != index
                or row['source_sha256'] != sha256(chunks[index].encode()).hexdigest()
                or row['request_sha256'] != by_chunk[index]):
            raise ValueError('Translation receipt belongs to another source chunk.')
        text(row['text'], 200000, empty=True)
        if row['translation_sha256'] != sha256(row['text'].encode()).hexdigest():
            raise ValueError('Accepted translation text changed.')
        if row['detected_source'] is not None:
            _translation_language(row['detected_source'])
        if by_chunk[index] is not None:
            digest(row['response_sha256'])
        elif (row['response_sha256'] is not None or row['text'] != chunks[index]
                or row['detected_source'] is not None):
            raise ValueError('Whitespace-only translation receipt changed.')
        if advanced:
            expected = ({'glossary': value['glossary'], 'ignoreCase': False}
                        if value.get('glossary') else None)
            if (row['reported_model'] != google_v3_parent(profile.endpoint) + '/models/' + profile.model
                    or row['glossary_config'] != expected):
                raise ValueError('Advanced translation model/glossary evidence changed.')
    if value['status'] == 'complete' and len(receipts) != len(chunks):
        raise ValueError('Completed translation is missing accepted chunks.')


def _public_entitlement(value):
    """Keep only a bounded public envelope, without importing its authority.

    Backup has no independently trusted publisher key or OS anchor. These
    structural checks preserve exact signed bytes as inert recovery data; only
    EntitlementStore can authenticate them against the owner's live policy and
    protected monotonic state. Secrets, keys and anchor records have no adapter.
    """
    from .security._files import json_value
    from .security.entitlements import _b64, _identity, _integer
    fields(value, {'version', 'kind', 'key_id', 'payload', 'signature'})
    if type(value['version']) is not int or value['version'] != 1 or value['kind'] != 'carr-entitlement':
        raise ValueError('Unsupported public entitlement envelope.')
    digest(value['key_id'])
    payload = _b64(value['payload'])
    _b64(value['signature'], 64)
    item = json_value(payload, maximum=32768)
    fields(item, {'schema', 'product', 'entitlement_id', 'subject', 'device', 'sequence', 'profile',
        'issued_at', 'not_before', 'expires_at', 'offline_until', 'refresh_interval',
        'policy_version', 'terms_sha256', 'features', 'revoked', 'request_nonce'})
    if type(item['schema']) is not int or item['schema'] != 1:
        raise ValueError('Unsupported public entitlement payload.')
    for key in ('product', 'entitlement_id', 'subject', 'policy_version'):
        _identity(item[key])
    if item['device'] is not None:
        _identity(item['device'])
    _integer(item['sequence'], minimum=1, maximum=2**53 - 1)
    for key in ('issued_at', 'not_before'):
        _integer(item[key], minimum=1)
    digest(item['terms_sha256'])
    if item['profile'] not in ('perpetual', 'renewable', 'evaluation') or type(item['revoked']) is not bool:
        raise ValueError('Invalid public entitlement profile.')
    if item['profile'] == 'perpetual':
        if any(item[key] is not None for key in ('expires_at', 'offline_until', 'refresh_interval')):
            raise ValueError('A perpetual grant cannot require periodic renewal.')
    else:
        for key in ('expires_at', 'offline_until'):
            _integer(item[key], minimum=1)
        if (item['issued_at'] > item['expires_at']
                or not item['not_before'] < item['expires_at'] <= item['offline_until']):
            raise ValueError('Invalid public entitlement validity interval.')
        if item['refresh_interval'] is not None:
            _integer(item['refresh_interval'], minimum=60, maximum=30 * 86400)
    features = item['features']
    if (type(features) is not list or len(features) > 128
            or any(type(feature) is not str for feature in features)
            or len(set(features)) != len(features)):
        raise ValueError('Invalid public entitlement feature inventory.')
    for feature in features:
        _identity(feature)
    nonce = item['request_nonce']
    if nonce is not None and (type(nonce) is not str or not re.fullmatch('[0-9a-f]{32}', nonce)):
        raise ValueError('Invalid public entitlement renewal nonce.')


def validate(db, name, value):
    if backup_book_audio.limit(name) is not None:
        backup_book_audio.validate_record(name, value)
    elif name == 'state/studio/draft.json':
        _studio_draft(value)
    elif name in TRUSTED_CLIENT:
        _public_entitlement(value)
    elif name == 'state/operations/resources.json':
        from .operations.models import ResourceLimits
        fields(value, {'schema', 'limits'})
        if value['schema'] != 'carr-resource-policy/1':
            raise ValueError('Invalid shared resource policy schema.')
        ResourceLimits.from_dict(value['limits'])
    elif match := RECORDING.fullmatch(name):
        if match[2] != 'json':
            raise ValueError('Recording WAV needs the streaming media adapter.')
        from .audio.recording import validate_recording_receipt
        validate_recording_receipt(value, recording_id=match[1])
    elif match := PUBLICATION.fullmatch(name):
        from .studio.publishing import validate_publication_receipt
        validate_publication_receipt(value, publication_id=match[1])
    elif READER.fullmatch(name):
        from .speech.reading import ReadingDocument
        from .speech.session import ReaderResume
        fields(value, {'schema', 'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds'})
        if value['schema'] != 'carr.reader-session.v1':
            raise ValueError('Invalid reader recovery schema.')
        ReaderResume(ReadingDocument.from_dict(value['document']), value['voice_id'], value['rate'],
                     value['volume'], value['cursor'], value['audio_seconds'])
    elif match := PROFILE.fullmatch(name):
        from .assistant.profiles import ProviderProfile
        if type(value) is not dict or value.get('schema') != 'carr-provider-profile/1':
            raise ValueError('Invalid provider profile schema.')
        profile = ProviderProfile(**{k: v for k, v in value.items() if k != 'schema'})
        if profile.id != match[1]:
            raise ValueError('Provider profile filename changed.')
    elif USAGE.fullmatch(name):
        _usage(value)
    elif match := LANGUAGES.fullmatch(name):
        _translation_languages(value, match[1])
    elif match := TRANSLATION.fullmatch(name):
        if match[2]:
            fields(value, {'command'})
            if value['command'] not in ('run', 'pause', 'cancel'):
                raise ValueError('Invalid translation control.')
        else:
            _translation_job(value, match[1])
    elif TEMPLATE.fullmatch(name):
        from .assistant.templates import validate_saved_template
        validate_saved_template(name, value)
    elif match := ASSISTANT.fullmatch(name):
        if match[2]:
            fields(value, {'command'})
            if value['command'] not in ('run', 'pause', 'cancel'):
                raise ValueError('Invalid assistant control.')
        else:
            _assistant_job(db, value, match[1])
    elif match := STUDIO.fullmatch(name):
        if match[2] == 'job':
            _studio_job(value, match[1])
        elif match[2] == 'control':
            fields(value, {'action'})
            if value['action'] not in ('pause', 'cancel'):
                raise ValueError('Invalid production control.')
        elif match[2] == 'project':
            from .studio.project import Project
            Project.from_dict(value)
        else:
            _timeline(value)
    else:
        raise ValueError('Unknown producer checkpoint.')
