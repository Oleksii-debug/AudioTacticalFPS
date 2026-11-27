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


def validate_arguments(step):
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
    if step.tool == 'audio.produce' and (('text' in step.arguments) == ('script' in step.arguments)):
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


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('Invalid checkpoint checksum.')


def _integer(value, name, minimum=1, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError('Invalid checkpoint ' + name + '.')


def validate_job(value, *, identity=None):
    """Validate a job without resolving paths, importing models or executing tools.

    Restore may set requires_reapproval=True when the original data_dir differs
    from its target. The original plan, scope and path bindings stay unchanged.
    """
    required = {'schema', 'id', 'data_dir', 'plan', 'plan_sha256', 'created', 'updated',
                'status', 'message', 'workspace', 'steps'}
    if (type(value) is not dict or not required <= set(value)
            or set(value) - required - {'requires_reapproval'} or value['schema'] != 'carr-assistant-job/1'):
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
        _path(scope['output_root'])
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
        if (type(checkpoint) is not dict or set(checkpoint) != {'status', 'intent', 'result', 'message'}
                or checkpoint['status'] not in ('pending', 'running', 'complete', 'failed')
                or type(checkpoint['intent']) is not dict):
            raise ValueError('Invalid assistant step checkpoint.')
        text(checkpoint['message'], 'step message', maximum=2000, empty=True)
        result = checkpoint['result']
        if result is None:
            if checkpoint['status'] == 'complete':
                raise ValueError('A complete step needs its result.')
            continue
        if type(result) is not dict or type(result.get('artifacts')) is not list:
            raise ValueError('Invalid completed assistant result.')
        if len(result['artifacts']) > scope['max_files']:
            raise ValueError('Too many assistant artifacts in checkpoint.')
        for artifact in result['artifacts']:
            if type(artifact) is not dict or set(artifact) != {'path', 'size', 'sha256'}:
                raise ValueError('Invalid assistant artifact record.')
            _path(artifact['path'])
            _integer(artifact['size'], 'artifact size', minimum=0)
            _sha(artifact['sha256'])
        if 'backup_receipt' in result:
            BackupReceipt.from_dict(result['backup_receipt'])
            _path(result.get('directory'))
    if creates > 1:
        raise ValueError('Multiple created libraries in one checkpoint are not permitted.')
    actual_workspace = value['workspace']
    if workspace is not None and workspace != actual_workspace:
        raise ValueError('Checkpoint workspace differs from the approved scope.')
    if workspace is None and actual_workspace is not None:
        expected = uuid5(NAMESPACE_URL, 'carr-assistant:' + job_id + ':library').hex
        if not creates or actual_workspace != expected:
            raise ValueError('Checkpoint selected an unapproved workspace.')
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
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=finite)
    return validate_control(value) if matched[2] else validate_job(value, identity=matched[1])
