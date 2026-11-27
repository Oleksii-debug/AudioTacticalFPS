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


def limit(name):
    if name == 'state/audio-metadata/choices.json':
        from .audio.metadata_review import MAX_BYTES
        return MAX_BYTES
    if name == 'state/assistant/workflow-templates.json':
        from .assistant.templates import MAX_BYTES
        return MAX_BYTES
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


def _studio_job(value, identity):
    from .studio.project import Project
    from .studio.render import _operation_sha256
    fields(value, {'schema', 'id', 'status', 'project', 'fingerprint', 'destination',
                   'format', 'stems', 'nodes', 'artifacts', 'progress', 'created',
                   'updated', 'error', 'runtime'}, {'result', 'resource_plan', 'recovery', 'total_disk_bytes',
                                                   'operation_sha256'})
    if type(value['schema']) is not int or value['schema'] != 1 or value['id'] != identity:
        raise ValueError('Invalid production job identity.')
    project = Project.from_dict(value['project'])
    if value['fingerprint'] != project.fingerprint:
        raise ValueError('Production project fingerprint changed.')
    if value['status'] not in ('pending', 'queued', 'running', 'paused', 'cancelled', 'failed', 'complete', 'attention', 'waiting-input'):
        raise ValueError('Invalid production status.')
    if value['format'] not in ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma'):
        raise ValueError('Invalid production output format.')
    if type(value['stems']) is not bool or type(value['nodes']) is not dict or len(value['nodes']) > 100000:
        raise ValueError('Invalid production options or nodes.')
    if 'operation_sha256' in value:
        digest(value['operation_sha256'])
        if value['operation_sha256'] != _operation_sha256(value):
            raise ValueError('Production operation options changed.')
    if 'recovery' in value:
        _studio_recovery(value['recovery'])
    if value['destination'] == '':
        if value['status'] != 'waiting-input' or value.get('recovery', {}).get('requires_output_directory') is not True:
            raise ValueError('Only an unapproved restored job may have no output directory.')
    else:
        path(value['destination'])
    if 'total_disk_bytes' in value:
        number(value['total_disk_bytes'], 1024**2, 2**63 - 1, integer=True)
    if 'resource_plan' in value:
        _studio_resource_plan(value['resource_plan'])
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
        if project.schema >= 3:
            expected_effects = {
                'master': [effect.to_dict() for effect in project.effects],
                'roles': {role.id: [effect.to_dict() for effect in role.effects] for role in project.roles},
                'scenes': {scene.id: [effect.to_dict() for effect in scene.effects] for scene in project.scenes},
                'clips': {scene.id: {cue.id: [effect.to_dict() for effect in cue.effects]
                          for cue in scene.cues} for scene in project.scenes},
                'stems': 'Track effects are included. Scene and master effects apply only to their mixed buses.'}
            if value['result'].get('effects') != expected_effects:
                raise ValueError('Production effect receipt differs from its typed project.')
        if (value['result']['project'] != project.id or value['result']['revision'] != project.revision
                or value['result']['source_sha256'] != sha256(project.original_script.encode()).hexdigest()):
            raise ValueError('Production result belongs to another project.')


def _timeline(value):
    fields(value, {'schema', 'project', 'revision', 'source_sha256', 'chapters', 'lines',
                   'duration', 'sample_rate', 'channels', 'mode', 'local_only', 'loudness',
                   'music_kind', 'timing_notice'}, {'music_continuity', 'lrc_notice', 'clips',
                   'clip_stems_notice', 'effects', 'book_source_sha256'})
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
    _timeline_extensions(value)
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



def _studio_resource_plan(value):
    from .operations.models import ResourceRequest
    fields(value, {'schema', 'request', 'source_sha256', 'estimated_duration',
                   'cpu_basis', 'memory_basis', 'disk_basis', 'enforcement'})
    if type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Invalid Studio resource plan.')
    request = ResourceRequest.from_dict(value['request'])
    for directory, _size in request.disks:
        path(str(directory))
    if type(value['source_sha256']) is not dict or len(value['source_sha256']) > 100000:
        raise ValueError('Invalid Studio input digest inventory.')
    for source, checksum in value['source_sha256'].items():
        path(source)
        digest(checksum)
    number(value['estimated_duration'], 0, 1e18)
    for key in ('cpu_basis', 'memory_basis', 'disk_basis', 'enforcement'):
        text(value[key], 8192)


def _studio_recovery(value):
    fields(value, {'requires_output_directory', 'inputs_retained', 'previous'})
    if type(value['requires_output_directory']) is not bool or type(value['inputs_retained']) is not bool:
        raise ValueError('Invalid restored Studio permissions.')
    previous = value['previous']
    fields(previous, {'project_fingerprint', 'status', 'destination', 'artifacts'})
    digest(previous['project_fingerprint'])
    if previous['status'] not in ('pending', 'queued', 'running', 'paused', 'cancelled',
                                   'failed', 'complete', 'attention', 'waiting-input'):
        raise ValueError('Invalid previous Studio status.')
    if previous['destination'] != '':
        path(previous['destination'])
    artifacts(previous['artifacts'])


def _timeline_extensions(value):
    for key in ('lrc_notice', 'clip_stems_notice'):
        if key in value:
            text(value[key], 8192)
    if 'book_source_sha256' in value:
        digest(value['book_source_sha256'])
    if 'clips' in value:
        from .studio.project import Cue
        clips = value['clips']
        if type(clips) is not list or len(clips) > 100000:
            raise ValueError('Invalid retained clip timeline.')
        for row in clips:
            fields(row, {'scene', 'id', 'role', 'title', 'source_sha256', 'source_duration',
                         'trim_start', 'duration', 'start', 'end', 'gain_db', 'pan',
                         'fade_in', 'fade_out'})
            text(row['scene'], 80)
            digest(row['source_sha256'])
            number(row['source_duration'], 0, 31_536_000)
            number(row['start'], 0, value['duration'] + 1)
            number(row['end'], row['start'], value['duration'] + 1)
            cue = Cue(row['id'], '/inert-backup-source', row['start'], row['gain_db'],
                row['pan'], row['role'], trim_start=row['trim_start'], duration=row['duration'],
                fade_in=row['fade_in'], fade_out=row['fade_out'], title=row['title'])
            if (abs(row['end'] - row['start'] - row['duration']) > 1 / value['sample_rate']
                    or cue.trim_start + cue.duration > row['source_duration'] + 1 / value['sample_rate']):
                raise ValueError('Retained clip timing differs from its source selection.')
    if 'effects' not in value:
        return
    from .audio.effect_presets import read_effect
    from .studio.project import _timeline_effects
    effect_map = value['effects']
    fields(effect_map, {'master', 'roles', 'scenes', 'clips', 'stems'})
    if effect_map['stems'] != 'Track effects are included. Scene and master effects apply only to their mixed buses.':
        raise ValueError('Invalid Studio effect routing notice.')

    def chain(raw):
        if type(raw) is not list or len(raw) > 32:
            raise ValueError('Invalid retained effect chain.')
        _timeline_effects(tuple(read_effect(item) for item in raw), value['sample_rate'])

    def mapping(raw, nested=False):
        if type(raw) is not dict or len(raw) > 100000:
            raise ValueError('Invalid retained effect routing.')
        for identity, item in raw.items():
            if type(identity) is not str or not re.fullmatch(ID, identity):
                raise ValueError('Invalid retained effect target.')
            mapping(item) if nested else chain(item)

    chain(effect_map['master'])
    mapping(effect_map['roles'])
    mapping(effect_map['scenes'])
    mapping(effect_map['clips'], nested=True)

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
                number(count, integer=True)


def _translation_languages(value, identity):
    from .assistant.models import canonical
    from .assistant.translation import ENDPOINT
    from .assistant.profiles import google_v3_parent
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
        if advanced and (type(row['source']) is not bool or type(row['target']) is not bool):
            raise ValueError('Invalid declared translation language capability.')
        _translation_language(row['language'])
        text(row['name'], 1000)
        if row['language'] in seen:
            raise ValueError('Duplicate translation language.')
        seen.add(row['language'])
    if sha256(canonical(rows).encode()).hexdigest() != value['languages_sha256']:
        raise ValueError('Translation language inventory checksum changed.')


def _translation_language(value, *, auto=False):
    if type(value) is not str or not (auto and value == 'auto' or
            re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', value)):
        raise ValueError('Invalid translation language code.')


def _translation_job(db, value, identity):
    from .assistant.profiles import ProviderProfile, google_v3_parent
    from .assistant.providers import Authorization
    from .assistant.translation import split_text, _approved_requests
    from .assistant.translation_source import source_context

    fields(value, {'schema', 'kind', 'id', 'created', 'updated', 'status', 'profile', 'title',
        'source_text', 'source_sha256', 'source_language', 'target_language', 'chunks', 'receipts',
        'authorization', 'per_call_cost'},
        {'glossary', 'source_anchor', 'source_metadata', 'source_topic'})
    if (value['schema'] != 'carr-assistant-job/1' or value['kind'] != 'translation/1'
            or value['id'] != identity or value['status'] not in ('queued', 'running', 'paused', 'attention', 'complete')):
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
    profile = ProviderProfile(**value['profile'])
    _translation_language(value['source_language'], auto=True)
    _translation_language(value['target_language'])
    if value['source_language'] == value['target_language']:
        raise ValueError('Translation needs different source and target languages.')
    context = source_context(value['source_text'], value.get('source_anchor'),
                             value.get('source_metadata'), value.get('source_topic'))
    if context is not None:
        from .backup_studio import validate_source_snapshot
        if validate_source_snapshot(db, context['source_anchor']) != value['source_text']:
            raise ValueError('Translation no longer matches its retained source positions.')
    approval = Authorization(**value['authorization'])
    # The same pure request reconstruction is used for submission and resume.
    # It does not open a gateway, renew approval or perform a provider request.
    try:
        by_chunk, _ = _approved_requests(profile, chunks, value['source_language'],
            value['target_language'], approval, value['per_call_cost'], value.get('glossary'),
            **(context or {}))
    except PermissionError as exc:
        raise ValueError('Translation checkpoint no longer matches its approved scope.') from exc
    receipts = value['receipts']
    if type(receipts) is not list or len(receipts) > len(chunks):
        raise ValueError('Invalid translation receipt inventory.')
    for index, row in enumerate(receipts):
        advanced = profile.adapter == 'google-translation-v3' and by_chunk[index] is not None
        required = {'index', 'source_sha256', 'text', 'translation_sha256', 'detected_source',
                    'request_sha256', 'response_sha256'}
        if advanced:
            required |= {'reported_model', 'glossary_config'}
        fields(row, required)
        if (type(row['index']) is not int or row['index'] != index
                or row['source_sha256'] != sha256(chunks[index].encode()).hexdigest()
                or row['request_sha256'] != by_chunk[index]):
            raise ValueError('Translation receipt belongs to another source chunk.')
        text(row['text'], 200000, empty=by_chunk[index] is None)
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
            glossary = {'glossary': value['glossary'], 'ignoreCase': False} if value.get('glossary') else None
            if (row['reported_model'] != google_v3_parent(profile.endpoint) + '/models/' + profile.model
                    or row['glossary_config'] != glossary):
                raise ValueError('Advanced translation receipt lost its model or glossary binding.')
    if value['status'] == 'complete' and len(receipts) != len(chunks):
        raise ValueError('Completed translation is missing accepted chunks.')


def _translation_batch(db, value, identity):
    from .assistant.translation_batch import validate_batch
    manifest = validate_batch(value, identity)
    for member in manifest['members']:
        job = member['job']
        # Templates have no execution state. Reuse the same passive source,
        # provider/request and quota validation as a normal unstarted child.
        _translation_job(db, {**job, 'updated': job['created']}, job['id'])


def validate_collection(db, stage, entries, *, cancelled):
    """Bind batch templates, accepted chunks and their preserved usage evidence."""
    from pathlib import Path
    from .durable_state import read_json
    from .assistant.models import canonical
    from .assistant.providers import Authorization, money

    inventory = {entry['filename'] for entry in entries}
    for name in sorted(inventory):
        if cancelled():
            raise InterruptedError('Producer checkpoint verification cancelled.')
        match = TRANSLATION.fullmatch(name)
        if match is None or match[2]:
            continue
        job = read_json(Path(stage) / name)
        if job.get('kind') == 'translation-batch/1':
            from .assistant.translation_batch import validate_batch, validate_member
            manifest = validate_batch(job, match[1])
            for index, member in enumerate(manifest['members']):
                if cancelled():
                    raise InterruptedError('Translation batch verification cancelled.')
                child_name = 'state/translation/jobs/' + member['job']['id'] + '.json'
                if child_name in inventory:
                    validate_member(manifest, index, read_json(Path(stage) / child_name))
                # Missing children are an unfinished local preparation. The
                # exact validated templates remain; restore grants no replay.
            continue
        accepted = [row for row in job['receipts'] if row['request_sha256'] is not None]
        if not accepted:
            continue
        approval = Authorization(**job['authorization'])
        names = ('state/translation/provider/usage/' + approval.id + '.usage.json',
                 'state/providers/usage/' + approval.id + '.usage.json')
        candidates = [item for item in names if item in inventory]
        if len(candidates) != 1:
            raise ValueError('Translation has no unique retained usage evidence.')
        usage = read_json(Path(stage) / candidates[0])
        if (usage['authorization'] != sha256(canonical(job['authorization']).encode()).hexdigest()
                or usage['currency'] != approval.currency):
            raise ValueError('Translation authority differs from its retained usage evidence.')
        profile = job['profile']
        profile_hash = sha256(canonical(profile).encode()).hexdigest()
        for row in accepted:
            recorded = usage['operations'].get(job['id'] + '_' + str(row['index']))
            if (type(recorded) is not dict or recorded.get('status') != 'complete'
                    or recorded.get('profile') != profile['id'] or recorded.get('model') != profile['model']
                    or recorded.get('profile_sha256') != profile_hash
                    or recorded.get('operation') != 'translation.text'
                    or recorded.get('request_sha256') != row['request_sha256']
                    or recorded.get('output_sha256') != row['response_sha256']
                    or money(recorded.get('reserved_cost')) != money(job['per_call_cost'])):
                raise ValueError('Translation receipt differs from its accepted provider usage.')


def validate(db, name, value):
    if name == 'state/audio-metadata/choices.json':
        from .audio.metadata_review import validate_choices
        validate_choices(value)
    elif name == 'state/assistant/workflow-templates.json':
        from .assistant.templates import validate_catalog
        validate_catalog(value)
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
        elif type(value) is dict and value.get('kind') == 'translation-batch/1':
            _translation_batch(db, value, match[1])
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
