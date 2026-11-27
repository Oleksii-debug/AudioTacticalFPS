"""Passive backup validators for the primary reader, studio and assistant.

These validators never resolve external paths, open a credential vault, start
an engine, or execute a saved plan. Restoration preserves declarations and
checkpoint bytes; each producer still checks permissions and assets on use.
"""
from hashlib import sha256
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from uuid import NAMESPACE_URL, UUID, uuid5

ID = r'[A-Za-z0-9_-]{1,80}'
HEX = r'[0-9a-f]{64}'
READER = re.compile(r'state/speech/(' + HEX + r')\.reader\.json\Z')
PROFILE = re.compile(r'state/providers/(' + ID + r')\.profile\.json\Z')
USAGE = re.compile(r'state/(?:providers/usage|translation/provider/usage)/(' + ID + r')\.usage\.json\Z')
TRANSLATION = re.compile(r'state/translation/jobs/([0-9a-f]{32})(\.control)?\.json\Z')
LANGUAGES = re.compile(r'state/translation/languages-(' + ID + r')\.json\Z')
ASSISTANT = re.compile(r'state/assistant/([0-9a-f]{32})(\.control)?\.json\Z')
STUDIO = re.compile(r'state/studio/jobs/(' + ID + r')/(job|control|project|timeline)\.json\Z')
SUBTITLE = re.compile(r'state/studio/jobs/(' + ID + r')/script\.(srt|vtt|lrc)\Z')


MEDIA_RECORD = re.compile(r'state/studio/(?:recordings/[0-9a-f]{32}\.(?:json|wav)|publications/[0-9a-f]{32}\.json|backup-inputs\.json)\Z')
STUDIO_INPUT = re.compile(r'state/studio/inputs/([0-9a-f]{64})\.audio\Z')
RECORDING = re.compile(r'state/studio/recordings/([0-9a-f]{32})\.(json|wav)\Z')
PUBLICATION = re.compile(r'state/studio/publications/([0-9a-f]{32})\.json\Z')
TRANSCRIPTION = re.compile(r'state/transcription/(?P<workspace>[0-9a-f]{64})/jobs/(?P<job>[0-9a-f]{32})/(?P<relative>.+)\Z')
_ASR_DOCUMENT = re.compile(r'(?:job|job_config|job_status|host_status)\.json|receipts/[0-9a-f]{64}\.result\.json|edits/[0-9a-f]{32}\.json')
_ASR_OUTPUT = re.compile(r'(?:results|cache)/[A-Za-z0-9_./ -]+\.(?:json|txt|srt|vtt)')
_ASR_INPUT = re.compile(r'input/[0-9]{3}-[0-9a-f]{32}\.[A-Za-z0-9]{1,8}')
_ASR_CACHE = re.compile(r'logs/cache_v5/([0-9a-f]{2})/([0-9a-f]{64})\.(json|txt)\Z')
_ASR_LOG = re.compile(r'logs/(?:progress|failed)\.log\Z')


def is_media(name):
    match = TRANSCRIPTION.fullmatch(name)
    return bool(STUDIO_INPUT.fullmatch(name)
                or (match and _ASR_INPUT.fullmatch(match['relative']))
                or (RECORDING.fullmatch(name) and name.endswith('.wav')))


def is_text(name):
    match = TRANSCRIPTION.fullmatch(name)
    return bool(SUBTITLE.fullmatch(name) or (match and (
        match['relative'] == 'stop.flag' or _ASR_LOG.fullmatch(match['relative'])
        or (_ASR_CACHE.fullmatch(match['relative']) and name.endswith('.txt'))
        or (_ASR_OUTPUT.fullmatch(match['relative']) and not name.endswith('.json')))))


def allow_empty(name):
    match = TRANSCRIPTION.fullmatch(name)
    return ((is_text(name) and not name.endswith('/stop.flag'))
            or bool(match and _ASR_INPUT.fullmatch(match['relative'])))


def limit(name):
    if name == 'state/ui/shortcuts.json':
        return 128 * 1024
    if name == 'state/ui/progress.json':
        return 4096
    if is_media(name):
        return 8 * 1024**3
    if name in ('state/studio/backup-inputs.json', 'state/studio/legacy-input-review.json', 'state/studio/draft.json'):
        return 32 * 1024**2
    if RECORDING.fullmatch(name) or PUBLICATION.fullmatch(name):
        return 32 * 1024**2
    if name in ('state/transcription/engine.json', 'state/operations/resources.json'):
        return 64 * 1024
    if match := TRANSCRIPTION.fullmatch(name):
        relative = match['relative']
        if relative == 'stop.flag':
            return 5
        if _ASR_LOG.fullmatch(relative):
            return 16 * 1024**2
        if cache := _ASR_CACHE.fullmatch(relative):
            if cache[1] != cache[2][:2]:
                return None
            return 64 * 1024**2 if cache[3] == 'json' else 16 * 1024**2
        if _ASR_DOCUMENT.fullmatch(relative) or _ASR_OUTPUT.fullmatch(relative):
            return 64 * 1024**2 if relative.endswith('.json') else 16 * 1024**2
    if READER.fullmatch(name) or STUDIO.fullmatch(name) or SUBTITLE.fullmatch(name):
        return 32 * 1024 * 1024
    if PROFILE.fullmatch(name):
        return 32768
    if LANGUAGES.fullmatch(name):
        return 1024 * 1024
    if USAGE.fullmatch(name) or ASSISTANT.fullmatch(name) or TRANSLATION.fullmatch(name):
        return 64 * 1024 * 1024
    return None


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


def _resource_plan(value):
    from .operations.models import ResourceRequest
    if (type(value) is not dict or value.get('schema') != 1 or 'request' not in value
            or len(value) > 10):
        raise ValueError('Invalid saved resource estimate.')
    ResourceRequest.from_dict(value['request'])
    for key, item in value.items():
        if key not in ('schema', 'request'):
            text(key, 80)
            text(item, 8192)


def _studio_recovery(value):
    fields(value, {'requires_output_directory', 'inputs_retained', 'previous'})
    if type(value['requires_output_directory']) is not bool or type(value['inputs_retained']) is not bool:
        raise ValueError('Invalid restored Studio authority.')
    previous = value['previous']
    fields(previous, {'project_fingerprint', 'status', 'destination', 'artifacts'})
    digest(previous['project_fingerprint'])
    text(previous['status'], 80)
    text(previous['destination'], 32768, empty=True)
    artifacts(previous['artifacts'])


def _timeline_clips(values, duration):
    if type(values) is not list or len(values) > 100000:
        raise ValueError('Invalid clip inventory.')
    for row in values:
        fields(row, {'scene', 'id', 'role', 'title', 'source_sha256', 'source_duration',
                     'trim_start', 'duration', 'start', 'end', 'gain_db', 'pan',
                     'fade_in', 'fade_out'})
        for key in ('scene', 'id', 'role', 'title'):
            text(row[key], 8192, empty=key in ('role', 'title'))
        digest(row['source_sha256'])
        number(row['source_duration'], 0, 31_536_000)
        number(row['trim_start'], 0, row['source_duration'])
        number(row['duration'], 0, row['source_duration'])
        number(row['start'], 0, duration + 1)
        number(row['end'], row['start'], duration + 1)
        number(row['gain_db'], -120, 24)
        number(row['pan'], -1, 1)
        for key in ('fade_in', 'fade_out'):
            number(row[key], 0, 31_536_000)


def _studio_job(value, identity):
    from .studio.project import Project
    fields(value, {'schema', 'id', 'status', 'project', 'fingerprint', 'destination',
                   'format', 'stems', 'nodes', 'artifacts', 'progress', 'created',
                   'updated', 'error', 'runtime'}, {'result', 'recovery', 'resource_plan',
                                                    'resource_cleanup_notice', 'total_disk_bytes', 'warnings', 'artifact_version'})
    if type(value['schema']) is not int or value['schema'] != 1 or value['id'] != identity:
        raise ValueError('Invalid production job identity.')
    artifact_version = value.get('artifact_version', 1)
    if type(artifact_version) is not int or artifact_version not in (1, 2):
        raise ValueError('Unsupported production artifact version.')
    project = Project.from_dict(value['project'])
    if value['fingerprint'] != project.fingerprint:
        raise ValueError('Production project fingerprint changed.')
    if value['status'] not in ('pending', 'queued', 'running', 'paused', 'cancelled', 'failed', 'complete', 'waiting-input'):
        raise ValueError('Invalid production status.')
    if value['format'] not in ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma'):
        raise ValueError('Invalid production output format.')
    if type(value['stems']) is not bool or type(value['nodes']) is not dict or len(value['nodes']) > 100000:
        raise ValueError('Invalid production options or nodes.')
    if value['status'] == 'waiting-input':
        if value['destination'] != '':
            raise ValueError('A restored job needs a newly selected output directory.')
    else:
        path(value['destination'])
    if 'resource_plan' in value:
        _resource_plan(value['resource_plan'])
    if 'resource_cleanup_notice' in value:
        text(value['resource_cleanup_notice'], 8192)
    if 'warnings' in value:
        if type(value['warnings']) is not list or len(value['warnings']) > 1000:
            raise ValueError('Invalid bounded production warning list.')
        for warning in value['warnings']:
            text(warning, 1800)
    if 'recovery' in value:
        _studio_recovery(value['recovery'])
    if 'total_disk_bytes' in value:
        number(value['total_disk_bytes'], 1024**2, 2**63 - 1, integer=True)
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
    if 'result' in value:
        _timeline(value['result'])
        if (value['result']['project'] != project.id or value['result']['revision'] != project.revision
                or value['result']['source_sha256'] != sha256(project.original_script.encode()).hexdigest()):
            raise ValueError('Production result belongs to another project.')


def _timeline(value):
    fields(value, {'schema', 'project', 'revision', 'source_sha256', 'chapters', 'lines',
                   'duration', 'sample_rate', 'channels', 'mode', 'local_only', 'loudness',
                   'music_kind', 'timing_notice'}, {'music_continuity', 'clips', 'clip_stems_notice'})
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
    if 'clips' in value:
        _timeline_clips(value['clips'], value['duration'])
        text(value.get('clip_stems_notice'), 8192)
    elif 'clip_stems_notice' in value:
        raise ValueError('Clip provenance needs its clip inventory.')
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
                if key == 'translation_sha256':
                    digest(count)
                else:
                    number(count, integer=True)


def _translation_languages(value, identity):
    from .assistant.models import canonical
    from .assistant.profiles import google_v3_parent
    from .assistant.translation import ENDPOINT
    fields(value, {'schema', 'profile', 'endpoint', 'model', 'fetched_at', 'languages',
                   'languages_sha256', 'qualification'})
    if (value['schema'] != 'carr-translation-languages/1' or value['profile'] != identity
            or value['qualification'] != 'provider-declared; not acoustic or translation-quality verification'):
        raise ValueError('Invalid translation language inventory identity.')
    advanced = value['model'] == 'general/nmt'
    if advanced:
        google_v3_parent(value['endpoint'])
    elif value['endpoint'] != ENDPOINT or value['model'] != 'nmt':
        raise ValueError('Invalid translation model or endpoint.')
    text(value['fetched_at'], 128)
    rows = value['languages']
    if type(rows) is not list or not 1 <= len(rows) <= 1000:
        raise ValueError('Invalid provider language inventory.')
    seen = set()
    for row in rows:
        fields(row, {'language', 'name', 'source', 'target'} if advanced else {'language', 'name'})
        _translation_language(row['language'])
        text(row['name'], 1000)
        if advanced and (type(row['source']) is not bool or type(row['target']) is not bool):
            raise ValueError('Invalid provider-declared language directions.')
        if row['language'] in seen:
            raise ValueError('Duplicate translation language.')
        seen.add(row['language'])
    if sha256(canonical(rows).encode()).hexdigest() != value['languages_sha256']:
        raise ValueError('Translation language inventory checksum changed.')


def _translation_language(value, *, auto=False):
    if type(value) is not str or not (auto and value == 'auto' or
            re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', value)):
        raise ValueError('Invalid translation language code.')


def _translation_binding(db, value):
    from .assistant.translation import _source_job_id
    from .speech.reading import ReadingDocument
    binding = value.get('source_binding')
    if value['kind'] == 'translation/1':
        if binding is not None or 'source_binding' in value or UUID(value['id']).version != 4:
            raise ValueError('A bound source cannot downgrade to a pasted-text job.')
        return
    if value['kind'] != 'translation/2':
        raise ValueError('Unknown translation source contract.')
    fields(binding, {'schema', 'library_root', 'root_identity', 'topic', 'anchor'})
    if binding['schema'] != 'carr-translation-source/1':
        raise ValueError('Unknown translation source binding.')
    path(binding['library_root'])
    if (type(binding['root_identity']) is not list or len(binding['root_identity']) != 2
            or any(type(part) is not int or part < 0 for part in binding['root_identity'])):
        raise ValueError('Invalid translation origin identity.')
    if binding['topic'] is not None:
        text(binding['topic'], 1024)
    document = ReadingDocument(value['source_text'], title=value['title'],
                               source_anchor=binding['anchor'])
    anchor = document.source_anchor
    if anchor is None or value['id'] != _source_job_id(value):
        raise ValueError('Translation approval or source identity changed.')
    positions = anchor['positions']
    if [row['ordinal'] for row in positions] != list(range(
            positions[0]['ordinal'], positions[-1]['ordinal'] + 1)):
        raise ValueError('Translation source positions are not contiguous.')
    for position in positions:
        row = db.execute("""SELECT p.raw FROM revision r
            JOIN sealed s ON s.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
            JOIN page p ON p.revision=r.id
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?
              AND p.digest=? AND p.ordinal=? AND
              ((?='physical_page' AND p.physical=?) OR
               (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))""",
            (anchor['workspace'], anchor['edition'], anchor['revision'], anchor['asset_sha256'],
             position['text_sha256'], position['ordinal'], position['kind'], position['index'],
             position['kind'], position['index'])).fetchone()
        if row is None or row[0] != value['source_text'][position['start']:position['end']]:
            raise ValueError('Translation original differs from its exact saved source.')


def _translation_job(db, value, identity):
    from .assistant.profiles import ProviderProfile, google_v3_parent
    from .assistant.providers import Authorization
    from .assistant.translation import split_text, _approved_requests
    fields(value, {'schema', 'kind', 'id', 'created', 'updated', 'status', 'profile', 'title',
        'source_text', 'source_sha256', 'source_language', 'target_language', 'chunks', 'receipts',
        'authorization', 'per_call_cost'}, {'source_binding', 'glossary', 'message'})
    if (value['schema'] != 'carr-assistant-job/1' or value['id'] != identity
            or value['status'] not in ('queued', 'running', 'paused', 'attention', 'complete')):
        raise ValueError('Invalid translation checkpoint identity or status.')
    for name in ('created', 'updated'):
        text(value[name], 128)
    if 'message' in value:
        text(value['message'], 8192, empty=True)
    text(value['title'], 512)
    text(value['source_text'], 2_000_000)
    if sha256(value['source_text'].encode()).hexdigest() != value['source_sha256']:
        raise ValueError('Translation original source changed.')
    chunks = split_text(value['source_text'])
    if type(value['chunks']) is not list or value['chunks'] != list(chunks):
        raise ValueError('Translation chunks no longer bind the original.')
    _translation_binding(db, value)
    profile = ProviderProfile(**value['profile'])
    approval = Authorization(**value['authorization'])
    _translation_language(value['source_language'], auto=True)
    _translation_language(value['target_language'])
    if value['source_language'] == value['target_language']:
        raise ValueError('Translation needs different source and target languages.')
    try:
        by_chunk, cost = _approved_requests(profile, chunks, value['source_language'],
            value['target_language'], approval, value['per_call_cost'],
            data_class='selected-script' if value['kind'] == 'translation/2' else 'selected-excerpt',
            glossary=value.get('glossary'))
    except PermissionError as exc:
        raise ValueError('Invalid saved translation approval.') from exc
    receipts = value['receipts']
    if type(receipts) is not list or len(receipts) > len(chunks):
        raise ValueError('Invalid translation receipt inventory.')
    for index, row in enumerate(receipts):
        fields(row, {'index', 'source_sha256', 'text', 'translation_sha256', 'detected_source',
                     'request_sha256', 'response_sha256'}, {'reported_model', 'glossary_config'})
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
            if profile.adapter == 'google-translation-v3':
                glossary = {'glossary': value['glossary'], 'ignoreCase': False} if value.get('glossary') else None
                if (row.get('reported_model') != google_v3_parent(profile.endpoint) + '/models/' + profile.model
                        or row.get('glossary_config') != glossary):
                    raise ValueError('Advanced translation provenance changed.')
        elif (row['response_sha256'] is not None or row['text'] != chunks[index]
              or row['detected_source'] is not None):
            raise ValueError('Whitespace-only translation receipt changed.')
    if value['status'] == 'complete' and len(receipts) != len(chunks):
        raise ValueError('Completed translation is missing accepted chunks.')
    return profile, approval, by_chunk, cost


def _transcription_engine(value):
    from .transcription import _protocol, V5_PROTOCOL, V5_SOURCES
    fields(value, {'version', 'python', 'worker', 'models_dir', 'python_sha256',
                   'worker_sha256', 'network', 'inference_qualified'},
           {'protocol', 'wrapper_sources_sha256', 'retry_policy'})
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Invalid transcription runtime declaration.')
    protocol = _protocol(value.get('protocol', 'receipts-v1'))
    for key in ('python', 'worker', 'models_dir'):
        path(value[key])
    for key in ('python_sha256', 'worker_sha256'):
        digest(value[key])
    if (value['network'] != 'local-files-only; Python network calls denied'
            or value['inference_qualified'] is not False):
        raise ValueError('Transcription configuration cannot assert runtime qualification.')
    if protocol == V5_PROTOCOL:
        fields(value.get('wrapper_sources_sha256'), V5_SOURCES)
        for checksum in value['wrapper_sources_sha256'].values():
            digest(checksum)
        if value['wrapper_sources_sha256']['worker.py'] != value['worker_sha256']:
            raise ValueError('Transcription source identities disagree.')
    if 'retry_policy' in value:
        text(value['retry_policy'], 4096)


def _transcription_job(db, value, match):
    from .transcription import (_protocol, _v5_attempts, settings, MEDIA_SUFFIXES,
                                MAX_FILES, MAX_SOURCE_BYTES, V5_PROTOCOL)
    fields(value, {'version', 'id', 'workspace', 'title', 'created', 'updated',
                   'status', 'sources', 'settings', 'done', 'failed', 'receipts',
                   'artifacts', 'run_context', 'last_error'},
           {'protocol', 'worker_run_id', 'effective_runs', 'resource_plan', 'resource_cleanup_notice'})
    if (type(value['version']) is not int or value['version'] != 1
            or value['id'] != match['job']):
        raise ValueError('Invalid transcription job identity.')
    text(value['workspace'], 128)
    if sha256(value['workspace'].encode()).hexdigest() != match['workspace']:
        raise ValueError('Transcription job belongs to another saved workspace.')
    if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (value['workspace'],)).fetchone() is None:
        raise ValueError('Transcription job belongs to a missing library.')
    protocol = _protocol(value.get('protocol', 'receipts-v1'))
    settings(value['settings'], protocol=protocol)
    if value['status'] not in ('ready', 'running', 'stopped', 'partial', 'failed', 'completed'):
        raise ValueError('Invalid saved transcription status.')
    text(value['title'], 200)
    text(value['last_error'], 8192, empty=True)
    for key in ('created', 'updated'):
        number(value[key])
    if type(value['sources']) is not list or not 1 <= len(value['sources']) <= MAX_FILES:
        raise ValueError('Invalid retained transcription sources.')
    names = set()
    for row in value['sources']:
        fields(row, {'filename', 'original_name', 'size_bytes', 'sha256'})
        name = row['filename']
        if (type(name) is not str or not _ASR_INPUT.fullmatch('input/' + name)
                or PurePosixPath(name).suffix not in MEDIA_SUFFIXES or name in names):
            raise ValueError('Invalid retained transcription source filename.')
        names.add(name)
        text(row['original_name'], 32768)
        number(row['size_bytes'], 0, MAX_SOURCE_BYTES, integer=True)
        digest(row['sha256'])
    for key in ('done', 'failed'):
        number(value[key], 0, len(names), integer=True)
    if (type(value['receipts']) is not list or len(value['receipts']) > len(names)
            or len(set(value['receipts'])) != len(value['receipts'])
            or any(type(item) is not str or not re.fullmatch(
                r'receipts/[0-9a-f]{64}\.result\.json', item) for item in value['receipts'])):
        raise ValueError('Invalid accepted transcription receipt inventory.')
    if type(value['artifacts']) is not list or len(value['artifacts']) > len(names) * 6:
        raise ValueError('Invalid accepted transcription output inventory.')
    for row in value['artifacts']:
        fields(row, {'path', 'size_bytes', 'sha256', 'source', 'format', 'recognition_identity'})
        if (row['source'] not in names or row['format'] not in
                ('txt', 'srt', 'vtt', 'json', 'words.srt', 'words.vtt')):
            raise ValueError('Transcript output belongs to another retained original.')
        from .state_backup import relative_name
        relative_name(row['path'])
        number(row['size_bytes'], 0, 512 * 1024**2, integer=True)
        digest(row['sha256'])
        digest(row['recognition_identity'])
    if value['run_context'] is not None and type(value['run_context']) is not dict:
        raise ValueError('Invalid retained transcription run declaration.')
    if 'worker_run_id' in value and not re.fullmatch(r'[0-9a-f]{32}', value['worker_run_id']):
        raise ValueError('Invalid accepted transcription run identity.')
    if 'effective_runs' in value:
        if type(value['effective_runs']) is not list or len(value['effective_runs']) > len(names):
            raise ValueError('Invalid bounded transcription attempt inventory.')
        for row in value['effective_runs']:
            fields(row, {'source', 'attempt', 'settings'})
            if row['source'] not in names or type(row['settings']) is not dict:
                raise ValueError('Invalid retained transcription attempt.')
            if (protocol != V5_PROTOCOL or type(row['attempt']) is not str
                    or row['attempt'] not in _v5_attempts(value['settings'])):
                raise ValueError('Invalid retained transcription attempt label.')
    if 'resource_plan' in value:
        _resource_plan(value['resource_plan'])
    if 'resource_cleanup_notice' in value:
        text(value['resource_cleanup_notice'], 8192)


def _transcription_record(db, value, match):
    from .transcription import _identity
    relative = match['relative']
    if relative == 'job.json':
        _transcription_job(db, value, match)
    elif relative.startswith('edits/'):
        fields(value, {'version', 'id', 'job', 'source', 'source_sha256',
                       'recognition_identity', 'original_text_sha256', 'created', 'text', 'kind'})
        if (type(value['version']) is not int or value['version'] != 1
                or value['id'] != PurePosixPath(relative).stem or value['job'] != match['job']
                or value['kind'] != 'user-edited derivative; source and timed recognition remain unchanged'):
            raise ValueError('Invalid transcript edit identity.')
        if not _ASR_INPUT.fullmatch('input/' + value['source']):
            raise ValueError('Invalid transcript edit source.')
        for key in ('source_sha256', 'recognition_identity', 'original_text_sha256'):
            digest(value[key])
        number(value['created'])
        text(value['text'], 16 * 1024**2, empty=True)
    elif relative.startswith('receipts/'):
        if (type(value) is not dict or value.get('version') != 1 or value.get('state') != 'complete'
                or not re.fullmatch(r'[0-9a-f]{32}', value.get('run_id', ''))):
            raise ValueError('Invalid saved transcription receipt.')
        _identity(value.get('identity'))
        _identity(value['identity'].get('model'))
        _identity(value['identity'].get('runtime'))
        if PurePosixPath(relative).name != value['identity']['sha256'] + '.result.json':
            raise ValueError('Transcription receipt filename differs from its identity.')
    elif type(value) is not dict:
        # The two reviewed worker protocols own their status/timed JSON
        # schemas, including the reviewed logs/cache_v5 JSON checkpoints.
        # Backup retains these bounded objects as inert checkpoints;
        # only the normal service may admit them as recognized results.
        raise ValueError('Invalid supplied-worker checkpoint object.')


def _studio_media_record(name, value):
    if match := RECORDING.fullmatch(name):
        from .audio.recording import validate_recording_receipt
        validate_recording_receipt(value, recording_id=match[1])
    elif match := PUBLICATION.fullmatch(name):
        from .studio.publishing import validate_publication_receipt
        validate_publication_receipt(value, publication_id=match[1])
    elif name == 'state/studio/backup-inputs.json':
        fields(value, {'schema', 'projects', 'inputs', 'external_outputs'})
        if (value['schema'] != 1 or type(value['schema']) is not int
                or type(value['projects']) is not list or type(value['inputs']) is not dict
                or value['external_outputs'] != 'not-copied; choose a new output directory after restore'):
            raise ValueError('Invalid retained Studio input receipt.')
    else:
        fields(value, {'schema', 'pending', 'approved'})
        if (value['schema'] != 1 or type(value['schema']) is not int
                or type(value['pending']) is not list or type(value['approved']) is not dict
                or len(value['pending']) + len(value['approved']) > 100000
                or len(set(value['pending'])) != len(value['pending'])
                or set(value['pending']) & set(value['approved'])):
            raise ValueError('Invalid Studio input-review state.')
        for raw in value['pending']:
            path(raw)
        for raw, identity in value['approved'].items():
            path(raw)
            fields(identity, {'size_bytes', 'sha256'})
            number(identity['size_bytes'], 1, integer=True)
            digest(identity['sha256'])


def _legacy_shortcuts(value):
    """Preserve old menu-path bindings as data; never retarget current IDs."""
    fields(value, {'schema', 'overrides', 'originals'})
    if (type(value['schema']) is not int or value['schema'] != 1
            or type(value['overrides']) is not dict or type(value['originals']) is not dict
            or len(value['overrides']) > 512
            or set(value['overrides']) != set(value['originals'])):
        raise ValueError('Invalid historical keyboard settings schema.')
    used = set()
    for action, shortcut in value['overrides'].items():
        text(action, 32768)
        if (any(ord(character) < 32 for character in action)
                or any(not part.strip() for part in action.split('/'))):
            raise ValueError('Invalid historical menu action.')
        for key in (shortcut, value['originals'][action]):
            text(key, 80, empty=True)
            if not key:
                continue
            parts = key.split('+')
            modifiers, final = parts[:-1], parts[-1]
            ordered = [modifier for modifier in ('Ctrl', 'Alt', 'Shift') if modifier in modifiers]
            if (modifiers != ordered or not re.fullmatch(r'[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])', final)
                    or len(final) == 1 and 'Ctrl' not in modifiers):
                raise ValueError('Invalid historical keyboard shortcut.')
        if shortcut:
            if shortcut in used:
                raise ValueError('Duplicate historical keyboard shortcut.')
            used.add(shortcut)


def _legacy_progress(value):
    """Validate the retained dialog-local schema without starting feedback."""
    fields(value, {'schema', 'preferences'})
    if type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Invalid historical progress settings schema.')
    preferences = value['preferences']
    fields(preferences, {'mode', 'interval_seconds', 'volume', 'quiet', 'background',
                         'mute_during_audio'})
    if preferences['mode'] not in ('off', 'beep', 'speech', 'both'):
        raise ValueError('Invalid historical progress feedback mode.')
    number(preferences['interval_seconds'], 1, 120)
    number(preferences['volume'], 0, 1)
    if any(type(preferences[key]) is not bool for key in ('quiet', 'background', 'mute_during_audio')):
        raise ValueError('Invalid historical progress feedback options.')


def validate(db, name, value):
    if name == 'state/ui/shortcuts.json':
        _legacy_shortcuts(value)
    elif name == 'state/ui/progress.json':
        _legacy_progress(value)
    elif name == 'state/studio/draft.json':
        # Autosave intentionally retains an invalid/unfinished script or title.
        # Check its bounded storage shape, not production readiness.
        fields(value, {'schema', 'text', 'title', 'voice_ids', 'project_id'})
        if type(value['schema']) is not int or value['schema'] != 1:
            raise ValueError('Invalid Studio recovery draft schema.')
        text(value['text'], 32 * 1024**2, empty=True)
        text(value['title'], 32768, empty=True)
        if type(value['voice_ids']) is not list or len(value['voice_ids']) > 10000:
            raise ValueError('Invalid Studio draft voice declarations.')
        for voice in value['voice_ids']:
            text(voice, 4096)
        if value['project_id'] is not None:
            text(value['project_id'], 80)
    elif name == 'state/operations/resources.json':
        from .operations.models import ResourceLimits
        fields(value, {'schema', 'limits'})
        if value['schema'] != 'carr-resource-policy/1':
            raise ValueError('Unknown cooperative resource policy.')
        ResourceLimits.from_dict(value['limits'])
    elif name == 'state/transcription/engine.json':
        _transcription_engine(value)
    elif match := TRANSCRIPTION.fullmatch(name):
        _transcription_record(db, value, match)
    elif (MEDIA_RECORD.fullmatch(name) or name == 'state/studio/legacy-input-review.json'):
        _studio_media_record(name, value)
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
            _translation_job(db, value, match[1])
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


def validate_collection(db, root, entries, *, cancelled):
    """Cross-bind saved data without opening external paths or running jobs."""
    from dataclasses import asdict
    from .backup_sidecars import _json, read_regular
    from .assistant.models import canonical
    from .assistant.providers import money
    from .ingest.restore import RestoreError
    files = {entry['filename']: entry for entry in entries}
    for name in sorted(files):
        if cancelled():
            raise InterruptedError('Backup validation cancelled.')
        if match := STUDIO_INPUT.fullmatch(name):
            if files[name]['sha256'] != match[1]:
                raise RestoreError('A retained Studio input differs from its content-addressed filename.')
        translation = TRANSLATION.fullmatch(name)
        if translation and not translation[2]:
            value = _json(read_regular(root / name, limit(name)))
            profile, approval, requests, cost = _translation_job(db, value, translation[1])
            accepted = [index for index in range(len(value['receipts'])) if requests[index] is not None]
            if accepted:
                ledger_name = 'state/translation/provider/usage/' + approval.id + '.usage.json'
                if ledger_name not in files:
                    raise RestoreError('Accepted translation has no retained usage evidence.')
                usage = _json(read_regular(root / ledger_name, limit(ledger_name)))
                if (usage['authorization'] != sha256(canonical(asdict(approval)).encode()).hexdigest()
                        or usage['currency'] != approval.currency):
                    raise RestoreError('Translation usage belongs to another approval.')
                for index in accepted:
                    receipt = value['receipts'][index]
                    row = usage['operations'].get(value['id'] + '_' + str(index))
                    profile_hash = sha256(canonical(profile.to_dict()).encode()).hexdigest()
                    if (type(row) is not dict or row.get('status') != 'complete'
                            or row.get('profile') != profile.id or row.get('model') != profile.model
                            or row.get('profile_sha256') != profile_hash
                            or row.get('operation') != 'translation.text'
                            or row.get('request_sha256') != requests[index]
                            or row.get('output_sha256') != receipt['response_sha256']
                            or money(row.get('reserved_cost')) != cost):
                        raise RestoreError('Accepted translation differs from its usage receipt.')
                    proof = row.get('usage')
                    if ((value['kind'] == 'translation/2' or profile.adapter == 'google-translation-v3'
                         or type(proof) is dict and 'translation_sha256' in proof)
                            and (type(proof) is not dict
                                 or proof.get('translation_sha256') != receipt['translation_sha256'])):
                        raise RestoreError('Accepted translation text differs from its decoded-output proof.')
        match = TRANSCRIPTION.fullmatch(name)
        if match and match['relative'] == 'job.json':
            value = _json(read_regular(root / name, limit(name)))
            base = name[:-len('job.json')]
            expected = {base + 'input/' + row['filename']: row for row in value['sources']}
            actual = {item for item in files if item.startswith(base + 'input/')}
            if actual != set(expected):
                raise RestoreError('A saved transcription original is missing or unlisted.')
            for item, row in expected.items():
                if any(files[item][key] != row[key] for key in ('size_bytes', 'sha256')):
                    raise RestoreError('Retained transcription original differs from its host manifest.')
            for relative in value['receipts']:
                if base + relative not in files:
                    raise RestoreError('An accepted transcription receipt is missing.')
            for artifact in value['artifacts']:
                output = files.get(base + 'results/' + artifact['path'])
                if output is None or any(output[key] != artifact[key] for key in ('size_bytes', 'sha256')):
                    raise RestoreError('Accepted transcription output differs from its saved bytes.')
        elif match:
            job_name = name[:-len(match['relative'])] + 'job.json'
            if job_name not in files:
                raise RestoreError('Transcription state has no matching durable host job.')
