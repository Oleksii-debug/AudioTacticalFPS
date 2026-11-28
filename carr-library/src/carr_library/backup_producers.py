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
USAGE = re.compile(r'state/(?:providers/usage|translation/provider/usage|gateway/usage)/(' + ID + r')\.usage\.json\Z')
TRANSLATION = re.compile(r'state/translation/jobs/([0-9a-f]{32})(\.control)?\.json\Z')
LANGUAGES = re.compile(r'state/translation/languages-(' + ID + r')\.json\Z')
ASSISTANT = re.compile(r'state/assistant/([0-9a-f]{32})(\.control)?\.json\Z')
STUDIO = re.compile(r'state/studio/jobs/(' + ID + r')/(job|control|project|timeline)\.json\Z')
SUBTITLE = re.compile(r'state/studio/jobs/(' + ID + r')/script\.(srt|vtt)\Z')
_CHAPTER_CUE = re.compile(r'state/studio/jobs/' + ID + r'/chapters-([0-9]{4})\.cue\Z')


def generated_chapter_cue(name):
    """Only rebuildable private CUE sheets, never arbitrary editor documents."""
    matched = _CHAPTER_CUE.fullmatch(name)
    return matched is not None and 1 <= int(matched[1]) <= 102


def limit(name):
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
    fields(value, {'schema', 'id', 'status', 'project', 'fingerprint', 'destination',
                   'format', 'stems', 'nodes', 'artifacts', 'progress', 'created',
                   'updated', 'error', 'runtime'}, {'result', 'resource_plan', 'recovery', 'warnings', 'total_disk_bytes'})
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
    recovery = value.get('recovery')
    if recovery is not None:
        fields(recovery, {'requires_output_directory', 'inputs_retained', 'previous'})
        if any(type(recovery[key]) is not bool for key in ('requires_output_directory', 'inputs_retained')):
            raise ValueError('Invalid Studio recovery permission.')
        previous = recovery['previous']
        fields(previous, {'project_fingerprint', 'status', 'destination', 'artifacts'})
        digest(previous['project_fingerprint'])
        text(previous['status'], 80)
        path(previous['destination'])
        artifacts(previous['artifacts'])
    if value['status'] == 'waiting-input':
        if recovery is None or recovery['requires_output_directory'] is not True or value['destination'] != '':
            raise ValueError('A restored Studio job needs a new explicit output directory.')
        if value['artifacts'] or value['nodes'] or value['runtime'] is not None:
            raise ValueError('A restored Studio job cannot inherit output authority.')
    else:
        path(value['destination'])
    if 'total_disk_bytes' in value:
        number(value['total_disk_bytes'], 1024**2, 2**63 - 1, integer=True)
    if 'warnings' in value:
        if type(value['warnings']) is not list or len(value['warnings']) > 10000:
            raise ValueError('Invalid Studio completion warnings.')
        for warning in value['warnings']:
            text(warning, 1800)
    if 'resource_plan' in value:
        from .operations.models import ResourceRequest
        plan = value['resource_plan']
        fields(plan, {'schema', 'request', 'cpu_basis', 'memory_basis',
                      'disk_basis', 'enforcement'}, {'source_sha256', 'estimated_duration'})
        if type(plan['schema']) is not int or plan['schema'] != 1:
            raise ValueError('Invalid Studio resource declaration.')
        # Canonical historical plans predate source hashes and measured clip
        # estimates. Recognize that exact inert shape without inventing either;
        # explicit rendering always computes a new admission plan.
        present = {'source_sha256', 'estimated_duration'} & plan.keys()
        if present and present != {'source_sha256', 'estimated_duration'}:
            raise ValueError('Incomplete Studio source declaration.')
        ResourceRequest.from_dict(plan['request'])
        if plan['request']['disk_path'] is not None:
            path(plan['request']['disk_path'])
        for directory, _ in plan['request']['additional_disks']:
            path(directory)
        if present:
            if type(plan['source_sha256']) is not dict:
                raise ValueError('Invalid Studio source declaration.')
            number(plan['estimated_duration'])
            if len(plan['source_sha256']) > 100000:
                raise ValueError('Studio source declaration exceeds its limit.')
            for source, checksum in plan['source_sha256'].items():
                path(source); digest(checksum)
        for key in ('cpu_basis', 'memory_basis', 'disk_basis', 'enforcement'):
            text(plan[key], 8192)
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
        if 'editor_interchange' in value['result']:
            _validate_editor_interchange(project, value['result'])


def _timeline(value):
    fields(value, {'schema', 'project', 'revision', 'source_sha256', 'chapters', 'lines',
                   'duration', 'sample_rate', 'channels', 'mode', 'local_only', 'loudness',
                   'music_kind', 'timing_notice'}, {'music_continuity', 'clips', 'clip_stems_notice',
                                                      'effects', 'markers', 'marker_notice', 'editor_interchange'})
    if 'editor_interchange' in value:
        _editor_interchange_shape(value['editor_interchange'])
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
    if ('clips' in value) != ('clip_stems_notice' in value):
        raise ValueError('Clip timing needs its explicit measured-timeline notice.')
    if 'clips' in value:
        text(value['clip_stems_notice'], 8192)
        if type(value['clips']) is not list or len(value['clips']) > 100000:
            raise ValueError('Invalid clip timeline inventory.')
        for clip in value['clips']:
            fields(clip, {'scene', 'id', 'role', 'title', 'source_sha256', 'source_duration',
                          'trim_start', 'duration', 'start', 'end', 'gain_db', 'pan', 'fade_in', 'fade_out'})
            for key in ('scene', 'id', 'role', 'title'):
                text(clip[key], 8192, empty=key == 'title')
            digest(clip['source_sha256'])
            number(clip['source_duration'], 0, 31_536_000)
            number(clip['trim_start'], 0, clip['source_duration'])
            number(clip['duration'], 0, clip['source_duration'])
            number(clip['start'], 0, value['duration'] + 1)
            number(clip['end'], clip['start'], value['duration'] + 1)
            number(clip['gain_db'], -120, 120)
            number(clip['pan'], -1, 1)
            for key in ('fade_in', 'fade_out'):
                number(clip[key], 0, clip['duration'])
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


    if any(name in value for name in ('effects', 'markers', 'marker_notice')):
        if not all(name in value for name in ('effects', 'markers', 'marker_notice', 'clips')):
            raise ValueError('Edited Studio timelines require their complete typed annotations.')
        from .audio.effect_presets import read_effect
        from .studio.project import _timeline_effects
        effect_data = value['effects']
        fields(effect_data, {'master', 'roles', 'scenes', 'clips', 'clip_bypass', 'stems'})
        if effect_data['stems'] != ('Track effects are included. Scene and master effects '
                                   'apply only to their mixed buses.'):
            raise ValueError('Unknown effect stem semantics.')
        def identity(key):
            if type(key) is not str or re.fullmatch(ID, key) is None:
                raise ValueError('Invalid effect or marker identity.')
        def mapping(rows):
            if type(rows) is not dict or len(rows) > 100000:
                raise ValueError('Invalid bounded effect inventory.')
            for key in rows:
                identity(key)
            return rows
        def chain(rows):
            if type(rows) is not list or len(rows) > 32:
                raise ValueError('Invalid saved timeline effect chain.')
            _timeline_effects(tuple(read_effect(row) for row in rows), value['sample_rate'])
        chain(effect_data['master'])
        for rows in mapping(effect_data['roles']).values():
            chain(rows)
        chapters = {row['id']: row for row in value['chapters']}
        if len(chapters) != len(value['chapters']):
            raise ValueError('Duplicate annotated chapter identity.')
        for scene in chapters:
            identity(scene)
        if not {row['role'] for row in value['lines']} <= effect_data['roles'].keys():
            raise ValueError('A spoken line has no retained effect-role declaration.')
        scene_effects, clip_effects, bypass = (mapping(effect_data[name])
                                               for name in ('scenes', 'clips', 'clip_bypass'))
        if any(set(rows) != set(chapters) for rows in (scene_effects, clip_effects, bypass)):
            raise ValueError('Effect scenes differ from the retained chapter graph.')
        clips = {scene: set() for scene in chapters}
        for row in value['clips']:
            identity(row['id'])
            if row['scene'] not in clips or row['id'] in clips[row['scene']]:
                raise ValueError('Invalid or duplicate annotated clip identity.')
            clips[row['scene']].add(row['id'])
        for scene in chapters:
            chain(scene_effects[scene])
            effects, flags = mapping(clip_effects[scene]), mapping(bypass[scene])
            if set(effects) != clips[scene] or set(flags) != clips[scene]:
                raise ValueError('Effect clips differ from the measured clip graph.')
            for rows in effects.values():
                chain(rows)
            if any(type(flag) is not bool for flag in flags.values()):
                raise ValueError('Clip bypass must be an explicit boolean.')
        if value['marker_notice'] != 'Authored scene annotations; not transcript alignment or source citations.':
            raise ValueError('Unknown marker provenance semantics.')
        if type(value['markers']) is not list or len(value['markers']) > 100000:
            raise ValueError('Invalid marker inventory.')
        counts, seen = {}, set()
        for row in value['markers']:
            fields(row, {'scene', 'id', 'label', 'position', 'start'})
            identity(row['scene'])
            identity(row['id'])
            if row['scene'] not in chapters or (row['scene'], row['id']) in seen:
                raise ValueError('Unknown scene or duplicate marker identity.')
            seen.add((row['scene'], row['id']))
            counts[row['scene']] = counts.get(row['scene'], 0) + 1
            if counts[row['scene']] > 10000:
                raise ValueError('Scene marker inventory exceeds its producer limit.')
            text(row['label'], 512)
            number(row['position'], 0, 31_536_000)
            number(row['start'], 0, value['duration'] + 31_536_000)
            if (abs(row['position'] * value['sample_rate'] - round(
                    row['position'] * value['sample_rate'])) > .001
                    or abs(row['start'] - chapters[row['scene']]['start'] - row['position']) > 1e-6):
                raise ValueError('Marker position differs from its authored sample-grid annotation.')


def _editor_interchange_shape(report):
    """Recognize only the current inert, bounded CUE declaration."""
    from .studio.interchange import _NOTICE
    common = {'schema', 'format', 'status', 'notice', 'files', 'editor_import'}
    if type(report) is not dict:
        raise ValueError('Invalid chapter interchange declaration.')
    if report.get('status') == 'unavailable':
        fields(report, common | {'reason'})
        if report['reason'] not in (
                'CUE export supports up to 10000 chapters per production.',
                'Distinct chapter starts cannot be represented by ordered 75 Hz CUE indices.'):
            raise ValueError('Unknown chapter interchange refusal.')
        if type(report['files']) is not list or report['files']:
            raise ValueError('Unavailable chapter interchange cannot declare files.')
    elif report.get('status') == 'ready':
        fields(report, common | {'encoding', 'audio_file', 'timebase', 'rounding',
            'maximum_marker_shift_seconds_exclusive', 'project_title_preserved'})
        if (report['encoding'] != 'UTF-8' or report['audio_file'] != 'master.wav'
                or type(report['timebase']) is not int or report['timebase'] != 75
                or report['rounding'] != 'floor'
                or type(report['maximum_marker_shift_seconds_exclusive']) is not float
                or report['maximum_marker_shift_seconds_exclusive'] != 1 / 75
                or type(report['project_title_preserved']) is not bool
                or type(report['files']) is not list or not 1 <= len(report['files']) <= 102):
            raise ValueError('Invalid chapter interchange bounds.')
        ordinal = 0
        for number_, sheet in enumerate(report['files'], 1):
            fields(sheet, {'file', 'tracks'})
            if (sheet['file'] != f'chapters-{number_:04d}.cue'
                    or type(sheet['tracks']) is not list or not 1 <= len(sheet['tracks']) <= 99):
                raise ValueError('Invalid chapter interchange sheet.')
            for track_number, track in enumerate(sheet['tracks'], 1):
                fields(track, {'track', 'chapter', 'scene', 'index_75hz', 'original_title_preserved'})
                ordinal += 1
                if (type(track['track']) is not int or track['track'] != track_number
                        or type(track['chapter']) is not int or track['chapter'] != ordinal
                        or ordinal > 10000 or type(track['scene']) is not str
                        or not re.fullmatch(ID, track['scene'])
                        or type(track['original_title_preserved']) is not bool):
                    raise ValueError('Invalid chapter interchange source binding.')
                number(track['index_75hz'], 0, 2 ** 31 - 1, integer=True)
    else:
        raise ValueError('Unknown chapter interchange status.')
    if (type(report['schema']) is not int or report['schema'] != 1
            or report['format'] != 'CUE/PCM WAVE' or report['notice'] != _NOTICE
            or report['editor_import'] != 'NOT_RUN'):
        raise ValueError('Unknown chapter interchange schema or qualification.')


def _validate_editor_interchange(project, timeline, *, cancelled=lambda: False):
    from .durable_state import encode
    from .studio.interchange import chapter_cues
    _editor_interchange_shape(timeline['editor_interchange'])
    _files, expected = chapter_cues(project, timeline, cancelled=cancelled)
    if encode(timeline['editor_interchange']) != encode(expected):
        raise ValueError('Chapter interchange differs from its exact project and measured timeline.')


def validate_studio_interchange(stage, entries, *, cancelled=lambda: False):
    """Recompute new timeline metadata inside the already verified backup stage.

    Private generated CUE bytes are excluded, like other rebuildable outputs.
    Their retained metadata cannot invent editor acceptance or source bindings.
    Legacy timelines without this optional declaration keep their prior codec.
    """
    from pathlib import Path
    from .backup_sidecars import _check_cancelled, _json, read_regular
    from .studio.project import Project
    names = {entry['filename'] for entry in entries}
    for name in sorted(names):
        _check_cancelled(cancelled)
        match = STUDIO.fullmatch(name)
        if match is None or match[2] != 'timeline':
            continue
        timeline = _json(read_regular(Path(stage) / name, limit(name)))
        if 'editor_interchange' not in timeline:
            continue
        project_name = name.rsplit('/', 1)[0] + '/project.json'
        if project_name not in names:
            raise ValueError('Chapter interchange needs its exact retained project checkpoint.')
        project = Project.from_dict(_json(read_regular(
            Path(stage) / project_name, limit(project_name))))
        _timeline(timeline)
        _validate_editor_interchange(project, timeline, cancelled=cancelled)
    _check_cancelled(cancelled)


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
                if key in ('translation_sha256', 'decoded_translation_sha256'):
                    digest(count)
                else:
                    number(count, integer=True)


def _translation_languages(value, identity):
    from .assistant.models import canonical
    from .assistant.profiles import google_v3_parent
    from .assistant.translation import ENDPOINT
    fields(value, {'schema', 'profile', 'endpoint', 'model', 'fetched_at', 'languages',
                   'languages_sha256', 'qualification'}, {'adapter'})
    if (value['schema'] != 'carr-translation-languages/1' or value['profile'] != identity
            or value['qualification'] != 'provider-declared; not acoustic or translation-quality verification'):
        raise ValueError('Invalid translation language inventory identity.')
    if 'adapter' in value:
        from .assistant.profiles import ProviderProfile
        from .assistant.translation_local import validate_languages
        if value['adapter'] != 'libretranslate-local':
            raise ValueError('Unsupported local translation language adapter.')
        ProviderProfile(identity, 'Retained local translation declaration', value['adapter'],
                        value['endpoint'], value['model'],
                        ('translation.text', 'translation.languages'), local=True, enabled=True)
        text(value['fetched_at'], 128)
        rows = validate_languages(value['languages'])
        if sha256(canonical(rows).encode()).hexdigest() != value['languages_sha256']:
            raise ValueError('Local translation language inventory checksum changed.')
        return
    advanced = value['endpoint'] != ENDPOINT
    if advanced:
        google_v3_parent(value['endpoint'])
        if value['model'] != 'general/nmt':
            raise ValueError('Invalid Advanced translation model declaration.')
    elif value['model'] != 'nmt':
        raise ValueError('Invalid Basic translation model declaration.')
    text(value['fetched_at'], 128)
    rows = value['languages']
    if type(rows) is not list or not 1 <= len(rows) <= 1000:
        raise ValueError('Invalid provider language inventory.')
    seen = set()
    for row in rows:
        fields(row, {'language', 'name', 'source', 'target'} if advanced else {'language', 'name'})
        _translation_language(row['language'])
        text(row['name'], 512 if advanced else 1000, empty=advanced)
        if advanced and (type(row['source']) is not bool or type(row['target']) is not bool):
            raise ValueError('Invalid declared translation language directions.')
        if row['language'] in seen:
            raise ValueError('Duplicate translation language.')
        seen.add(row['language'])
    if sha256(canonical(rows).encode()).hexdigest() != value['languages_sha256']:
        raise ValueError('Translation language inventory checksum changed.')


def _translation_language(value, *, auto=False):
    if type(value) is not str or not (auto and value == 'auto' or
            re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', value)):
        raise ValueError('Invalid translation language code.')


def _translation_job(value, identity, db):
    from uuid import UUID
    from .assistant.profiles import ProviderProfile, google_v3_parent
    from .assistant.providers import translation_authorization_type, translation_receipt_sha256
    from .assistant.translation import split_text, _approved_requests, _source_job_id
    fields(value, {'schema', 'kind', 'id', 'created', 'updated', 'status', 'profile', 'title',
        'source_text', 'source_sha256', 'source_language', 'target_language', 'chunks', 'receipts',
        'authorization', 'per_call_cost'}, {'glossary', 'source_binding'})
    if (value['schema'] != 'carr-assistant-job/1' or value['kind'] not in ('translation/1', 'translation/2')
            or value['id'] != identity or value['status'] not in ('queued', 'running', 'paused', 'attention', 'complete')):
        raise ValueError('Invalid translation checkpoint identity or status.')
    for name in ('created', 'updated'):
        text(value[name], 128)
    text(value['title'], 512)
    text(value['source_text'], 2_000_000)
    if sha256(value['source_text'].encode()).hexdigest() != value['source_sha256']:
        raise ValueError('Translation original source changed.')
    bound = value['kind'] == 'translation/2'
    if bound:
        from .speech.reading import ReadingDocument
        from .speech.session import document_source_locations
        from .backup_extensions import _source_text
        binding = value.get('source_binding')
        fields(binding, {'schema', 'library_root', 'root_identity', 'topic', 'anchor'})
        if binding['schema'] != 'carr-translation-source/1':
            raise ValueError('Invalid translation source binding schema.')
        path(binding['library_root'])
        if '..' in binding['library_root'].replace('\\', '/').split('/'):
            raise ValueError('Invalid translation original root declaration.')
        if (type(binding['root_identity']) is not list or len(binding['root_identity']) != 2
                or any(type(part) is not int or part < 0 for part in binding['root_identity'])):
            raise ValueError('Invalid translation original root identity.')
        if binding['topic'] is not None:
            text(binding['topic'], 1024)
        document = ReadingDocument(value['source_text'], title=value['title'], source_anchor=binding['anchor'])
        anchor = document.source_anchor
        if anchor is None:
            raise ValueError('A source translation requires canonical lineage.')
        positions = anchor['positions']
        if [row['ordinal'] for row in positions] != list(range(positions[0]['ordinal'], positions[-1]['ordinal'] + 1)):
            raise ValueError('The translation source range is not contiguous.')
        if value['id'] != _source_job_id(value):
            raise ValueError('Translation source identity differs from its approved checkpoint.')
        for row, location in zip(positions, document_source_locations(document)):
            if _source_text(db, location) != document.source_text[row['start']:row['end']]:
                raise ValueError('Translation source differs from the exact copied catalog.')
    elif 'source_binding' in value or UUID(value['id']).version != 4:
        raise ValueError('A source translation cannot be downgraded to an independent job.')
    chunks = split_text(value['source_text'])
    if type(value['chunks']) is not list or value['chunks'] != list(chunks):
        raise ValueError('Translation chunks no longer bind the original.')
    profile = ProviderProfile(**value['profile'])
    if profile.adapter not in ('google-translation-v2', 'google-translation-v3', 'libretranslate-local'):
        raise ValueError('Unsupported translation provider declaration.')
    advanced = profile.adapter == 'google-translation-v3'
    _translation_language(value['source_language'], auto=True)
    _translation_language(value['target_language'])
    if value['source_language'] == value['target_language']:
        raise ValueError('Translation needs different source and target languages.')
    approval = translation_authorization_type(profile)(**value['authorization'])
    try:
        by_chunk, _cost = _approved_requests(profile, chunks, value['source_language'],
            value['target_language'], approval, value['per_call_cost'], glossary=value.get('glossary'),
            data_class='selected-script' if bound else 'selected-excerpt')
    except PermissionError as exc:
        raise ValueError('Translation checkpoint no longer matches its approved scope.') from exc
    receipts = value['receipts']
    if type(receipts) is not list or len(receipts) > len(chunks):
        raise ValueError('Invalid translation receipt inventory.')
    for index, row in enumerate(receipts):
        optional = {'decoded_translation_sha256'}
        if advanced and chunks[index].strip():
            optional.update(('reported_model', 'glossary_config'))
        fields(row, {'index', 'source_sha256', 'text', 'translation_sha256', 'detected_source',
                     'request_sha256', 'response_sha256'}, optional)
        if (type(row['index']) is not int or row['index'] != index
                or row['source_sha256'] != sha256(chunks[index].encode()).hexdigest()
                or row['request_sha256'] != by_chunk[index]):
            raise ValueError('Translation receipt belongs to another source chunk.')
        text(row['text'], 200000, empty=by_chunk[index] is None)
        if row['translation_sha256'] != sha256(row['text'].encode()).hexdigest():
            raise ValueError('Accepted translation text changed.')
        if row['detected_source'] is not None:
            _translation_language(row['detected_source'])
        if by_chunk[index] is None:
            if (row['response_sha256'] is not None or row['text'] != chunks[index]
                    or row['detected_source'] is not None or 'decoded_translation_sha256' in row):
                raise ValueError('Whitespace-only translation receipt changed.')
            continue
        digest(row['response_sha256'])
        if profile.adapter == 'libretranslate-local':
            from .assistant.translation_local import language
            if value['source_language'] == 'auto':
                language(row['detected_source'])
            elif row['detected_source'] is not None:
                raise ValueError('Local explicit-source translation has unrequested detection.')
        if advanced:
            glossary = {'glossary': value['glossary'], 'ignoreCase': False} if value.get('glossary') else None
            observed_glossary = row.get('glossary_config')
            if (row.get('reported_model') != google_v3_parent(profile.endpoint) + '/models/' + profile.model
                    or 'glossary_config' not in row or observed_glossary != glossary
                    or (glossary is not None and (type(observed_glossary) is not dict
                                                 or observed_glossary.get('ignoreCase') is not False))):
                raise ValueError('Advanced translation provenance differs from its approved model or glossary.')
        if bound or profile.adapter == 'libretranslate-local' or 'decoded_translation_sha256' in row:
            if row.get('decoded_translation_sha256') != translation_receipt_sha256(row['text'], row['detected_source']):
                raise ValueError('Decoded translation receipt changed.')
        # This local consistency check grants no independent receipt authority.
        # The restored service still verifies the paired saved usage ledger.
    if value['status'] == 'complete' and len(receipts) != len(chunks):
        raise ValueError('Completed translation is missing accepted chunks.')


def validate(db, name, value):
    if READER.fullmatch(name):
        from .speech.session import decode_reader_resume
        decode_reader_resume(value)
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
            _translation_job(value, match[1], db)
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
