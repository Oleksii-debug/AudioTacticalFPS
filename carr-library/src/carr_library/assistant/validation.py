"""Pure structural validation for backup/restore; never opens saved paths."""
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
from uuid import NAMESPACE_URL, uuid5

from .models import Step, canonical, text
from .backup_receipt import BackupReceipt
from .local_tools import LOCAL_TOOLS, INDEPENDENT_TOOLS, validate_arguments as validate_local_arguments
from .schedule_model import validate_schedule


_KEYS = {
    'library.create': ({'name'}, {'description'}),
    'library.list': (set(), set()),
    'library.catalog': (set(), {'after', 'limit', 'title', 'author', 'language'}),
    'library.import': ({'path'}, set()),
    'library.import_folder': ({'path'}, set()),
    'library.search': ({'query'}, {'mode', 'limit', 'title', 'author', 'language'}),
    'library.extract': ({'edition', 'name'}, set()),
    'library.backup': ({'name'}, set()),
    'audio.produce': (set(), {'text', 'script', 'voice_ids', 'title', 'format', 'stems', 'language', 'input_kind'}),
}
_KEYS['audio.plan'] = _KEYS['audio.produce']


def validate_arguments(step):
    if step.tool == 'audio.transcribe':
        from .transcription_model import validate_arguments as validate_transcription
        return validate_transcription(step.arguments)
    if step.tool in LOCAL_TOOLS and step.tool != 'audio.plan':
        return validate_local_arguments(step.tool, step.arguments)
    required, optional = _KEYS[step.tool]
    if set(step.arguments) - required - optional or not required <= set(step.arguments):
        raise ValueError(f'Invalid arguments for {step.tool}.')
    for key, value in step.arguments.items():
        if key in ('script',):
            if type(value) is not dict:
                raise ValueError('A structured audio script must be an object.')
        elif key == 'voice_ids':
            if not isinstance(value, (tuple, list)) or not value or len(value) > 100:
                raise ValueError('Choose one or more available voice identities.')
            for voice in value:
                text(voice, 'voice identity', maximum=4096)
        elif key == 'stems':
            if type(value) is not bool:
                raise ValueError('The stems option must be true or false.')
        elif key == 'limit':
            if type(value) is not int or not 1 <= value <= (500 if step.tool == 'library.catalog' else 100):
                raise ValueError('Invalid result page size.')
        else:
            text(value, key, maximum=4 * 1024 * 1024 if key == 'text' else 32768)
    if step.tool in ('audio.produce', 'audio.plan') and (('text' in step.arguments) == ('script' in step.arguments)):
        raise ValueError('Provide either script text or a structured audio script.')
    if 'input_kind' in step.arguments and step.arguments['input_kind'] not in ('narration', 'script'):
        raise ValueError('Choose literal narration or an explicit structured script.')
    if 'mode' in step.arguments and step.arguments['mode'] not in ('word', 'phrase', 'terms', 'thematic', 'semantic'):
        raise ValueError('Unsupported search mode.')


def _path(value):
    text(value, 'saved local path', maximum=32768)
    if value.startswith(('\\\\', '//')) or '://' in value or not (
            PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise ValueError('Checkpoint paths must remain absolute local paths.')
    if '..' in PurePosixPath(value).parts or '..' in PureWindowsPath(value).parts:
        raise ValueError('Checkpoint paths cannot contain parent traversal.')
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if isinstance(path, PureWindowsPath) and any(':' in part for part in path.parts[1:]):
        raise ValueError('Checkpoint paths cannot name Windows alternate data streams.')
    return path


def _within(value, root, *, allow_root=False):
    path, base = _path(value), _path(root)
    if (type(path) is not type(base) or not path.is_relative_to(base)
            or (not allow_root and path == base)):
        raise ValueError('Checkpoint path is outside its approved scope.')


def _output_name(value):
    text(value, 'output name', maximum=1024)
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath('.') or '..' in path.parts or ':' in value or '\\' in value:
        raise ValueError('Checkpoint output name is outside its approved scope.')


def decode_state_json(raw):
    """Reject ambiguous or non-finite JSON before interpreting checkpoint fields."""
    if not isinstance(raw, (str, bytes)) or len(raw) > 64 * 1024 * 1024:
        raise ValueError('Assistant backup sidecar exceeds its byte budget.')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate assistant checkpoint field.')
            result[key] = value
        return result

    def finite(value):
        raise ValueError('Non-finite number in assistant checkpoint.')

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            finite(value)
        return parsed

    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        return json.loads(raw, object_pairs_hook=unique, parse_constant=finite, parse_float=number)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError('Invalid assistant checkpoint JSON.') from exc


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('Invalid checkpoint checksum.')


def _integer(value, name, minimum=1, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError('Invalid checkpoint ' + name + '.')


def validate_job(value, *, identity=None):
    """Validate a job without resolving paths, importing models or executing tools.

    An explicit requires_reapproval marker is supported. A restored job is also
    recognized by its unchanged data_dir; restoring never needs to rewrite it.
    """
    required = {'schema', 'id', 'data_dir', 'plan', 'plan_sha256', 'created', 'updated',
                'status', 'message', 'workspace', 'steps'}
    if (type(value) is not dict or not required <= set(value)
            or set(value) - required - {'requires_reapproval', 'schedule'} or value['schema'] != 'carr-assistant-job/1'):
        raise ValueError('Invalid assistant checkpoint fields.')
    job_id = value['id']
    if not isinstance(job_id, str) or re.fullmatch('[0-9a-f]{32}', job_id) is None or (identity is not None and job_id != identity):
        raise ValueError('Invalid assistant checkpoint identity.')
    _path(value['data_dir'])
    if type(value.get('requires_reapproval', False)) is not bool:
        raise ValueError('Invalid assistant reapproval marker.')
    if value['status'] not in ('queued', 'running', 'paused', 'cancelled', 'complete', 'attention', 'failed'):
        raise ValueError('Invalid assistant checkpoint status.')
    text(value['message'], 'checkpoint message', maximum=2000, empty=True)
    for field in ('created', 'updated'):
        text(value[field], 'checkpoint timestamp', maximum=80)
        if datetime.fromisoformat(value[field]).tzinfo is None:
            raise ValueError('Checkpoint timestamps need an explicit timezone.')
    plan = value['plan']
    if type(plan) is not dict or set(plan) != {'schema', 'scope', 'steps', 'summary'} or plan['schema'] != 'carr-assistant-plan/1':
        raise ValueError('Invalid assistant plan in checkpoint.')
    text(plan['summary'], 'plan summary', maximum=16384, empty=True)
    _sha(value['plan_sha256'])
    if sha256(canonical(plan).encode('utf-8')).hexdigest() != value['plan_sha256']:
        raise ValueError('The approved assistant plan checksum changed.')
    scope = plan['scope']
    if type(scope) is not dict or set(scope) != {'workspace_id', 'input_roots', 'output_root', 'allow_create',
            'allow_full_backup', 'max_steps', 'max_files', 'max_seconds', 'max_bytes'}:
        raise ValueError('Invalid assistant scope in checkpoint.')
    workspace = scope['workspace_id']
    if workspace is not None:
        text(workspace, 'workspace identity', maximum=128)
    if type(scope['allow_create']) is not bool or type(scope['allow_full_backup']) is not bool:
        raise ValueError('Invalid saved assistant permissions.')
    _integer(scope['max_steps'], 'step budget', maximum=10000)
    _integer(scope['max_files'], 'file budget', maximum=1000000)
    _integer(scope['max_bytes'], 'byte budget')
    if type(scope['max_seconds']) not in (int, float) or not math.isfinite(scope['max_seconds']) or scope['max_seconds'] <= 0:
        raise ValueError('Invalid checkpoint time budget.')
    if type(scope['input_roots']) is not list or len(scope['input_roots']) > scope['max_files']:
        raise ValueError('Invalid checkpoint input roots.')
    for path in scope['input_roots']:
        _path(path)
    if scope['output_root'] is not None:
        output, data = _path(scope['output_root']), _path(value['data_dir'])
        if type(output) is type(data) and output.is_relative_to(data):
            raise ValueError('Checkpoint output cannot be inside its private library.')
    steps, saved = plan['steps'], value['steps']
    if (type(steps) is not list or not 1 <= len(steps) <= scope['max_steps']
            or type(saved) is not list or len(saved) != len(steps)):
        raise ValueError('Invalid assistant checkpoint step list.')
    creates = 0
    for index, (step, checkpoint) in enumerate(zip(steps, saved)):
        if type(step) is not dict or set(step) != {'tool', 'arguments'}:
            raise ValueError('Invalid assistant step fields.')
        validated = Step(step['tool'], step['arguments'])
        validate_arguments(validated)
        if validated.tool == 'library.create':
            creates += 1
            if index or not scope['allow_create'] or workspace is not None:
                raise ValueError('Invalid saved create-library permission.')
        if validated.tool == 'library.backup' and not scope['allow_full_backup']:
            raise ValueError('A saved backup plan lacks its explicit permission.')
        if validated.tool.startswith('library.import') or validated.tool in ('document.convert', 'player.inspect', 'audio.transcribe'):
            target = _path(validated.arguments['path'])
            if not any(type(target) is type(_path(root)) and target.is_relative_to(_path(root))
                       for root in scope['input_roots']):
                raise ValueError('Checkpoint import is outside its approved input scope.')
        if validated.tool in ('library.extract', 'library.backup', 'audio.produce', 'document.convert', 'audio.transcribe'):
            if scope['output_root'] is None:
                raise ValueError('Checkpoint operation has no approved output scope.')
        if validated.tool in ('library.extract', 'library.backup', 'document.convert', 'audio.transcribe'):
            _output_name(validated.arguments['name'])
        if (validated.tool not in ('library.create', 'library.list', 'library.backup', 'audio.produce', 'audio.transcribe')
                and validated.tool not in INDEPENDENT_TOOLS and workspace is None and creates == 0):
            raise ValueError('Checkpoint operation has no approved library scope.')
        if (type(checkpoint) is not dict or set(checkpoint) != {'status', 'intent', 'result', 'message'}
                or checkpoint['status'] not in ('pending', 'running', 'complete', 'failed')
                or type(checkpoint['intent']) is not dict):
            raise ValueError('Invalid assistant step checkpoint.')
        text(checkpoint['message'], 'step message', maximum=2000, empty=True)
        intent = checkpoint['intent']
        if validated.tool == 'audio.transcribe':
            from .transcription_model import validate_checkpoint
            validate_checkpoint(value, index, validated.arguments, checkpoint)
        permitted = {'transcription', 'definition_sha256', 'transcription_export'} if validated.tool == 'audio.transcribe' else (
            {'batch'} if validated.tool.startswith('library.import') else (
            {'sha256'} if validated.tool == 'library.extract' else set()))
        if set(intent) - permitted:
            raise ValueError('Invalid assistant step intent.')
        if 'batch' in intent and (not isinstance(intent['batch'], str)
                or re.fullmatch('[0-9a-f]{32}', intent['batch']) is None):
            raise ValueError('Invalid saved import batch identity.')
        if 'sha256' in intent:
            _sha(intent['sha256'])
        result = checkpoint['result']
        if result is None:
            if checkpoint['status'] == 'complete':
                raise ValueError('A complete step needs its result.')
            continue
        if type(result) is not dict or type(result.get('artifacts')) is not list:
            raise ValueError('Invalid completed assistant result.')
        if len(result['artifacts']) > scope['max_files']:
            raise ValueError('Too many assistant artifacts in checkpoint.')
        artifact_bytes, artifact_paths = 0, set()
        for artifact in result['artifacts']:
            if type(artifact) is not dict or set(artifact) != {'path', 'size', 'sha256'}:
                raise ValueError('Invalid assistant artifact record.')
            _path(artifact['path'])
            if scope['output_root'] is None:
                raise ValueError('A checkpoint artifact has no approved output scope.')
            _within(artifact['path'], scope['output_root'])
            _integer(artifact['size'], 'artifact size', minimum=0)
            _sha(artifact['sha256'])
            artifact_bytes += artifact['size']
            normalized = _path(artifact['path'])
            if normalized in artifact_paths or artifact_bytes > scope['max_bytes']:
                raise ValueError('Assistant artifacts exceed their approved budget or repeat a path.')
            artifact_paths.add(normalized)
        if 'backup_receipt' in result:
            if validated.tool != 'library.backup':
                raise ValueError('Only a backup step can have a backup receipt.')
            BackupReceipt.from_dict(result['backup_receipt'])
            _within(result.get('directory'), scope['output_root'])
            expected = _path(scope['output_root']) / validated.arguments['name']
            if _path(result['directory']) != expected:
                raise ValueError('Backup result differs from its approved destination.')
    if creates > 1:
        raise ValueError('Multiple created libraries in one checkpoint are not permitted.')
    actual_workspace = value['workspace']
    if workspace is not None and workspace != actual_workspace:
        raise ValueError('Checkpoint workspace differs from the approved scope.')
    if workspace is None and actual_workspace is not None:
        expected = uuid5(NAMESPACE_URL, 'carr-assistant:' + job_id + ':library').hex
        if not creates or actual_workspace != expected:
            raise ValueError('Checkpoint selected an unapproved workspace.')
    if 'schedule' in value:
        validate_schedule(value['schedule'], value)
    try:
        encoded_size = len(canonical(value).encode('utf-8'))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid assistant checkpoint JSON content.') from exc
    if encoded_size > 64 * 1024 * 1024:
        raise ValueError('Assistant checkpoint exceeds its storage budget.')
    return value


def validate_control(value):
    if type(value) is not dict or set(value) != {'command'} or value['command'] not in ('run', 'pause', 'cancel'):
        raise ValueError('Invalid assistant control checkpoint.')
    return value


def validate_state_document(name, raw):
    """Validate one backup sidecar by its basename; locks/tmp are never accepted."""
    matched = re.fullmatch(r'([0-9a-f]{32})(\.control)?\.json', name)
    if matched is None:
        raise ValueError('Invalid assistant backup sidecar filename.')
    if not isinstance(raw, (str, bytes)):
        raise ValueError('Assistant checkpoint must be JSON bytes or text.')
    if matched[2] and len(raw) > 1024:
        raise ValueError('Assistant control exceeds its byte budget.')
    value = decode_state_json(raw)
    return validate_control(value) if matched[2] else validate_job(value, identity=matched[1])
