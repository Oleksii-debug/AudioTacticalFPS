"""Validate saved assistant data without opening inputs or executing a plan.

Restored jobs retain their original data-directory and path permissions. The
executor therefore requires a new review when they move to another library.
"""
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath
import re

from .models import SCHEMA, Scope, Step, canonical, text


MAX_BYTES = 64 * 1024 * 1024
_NAME = re.compile(r'state/assistant/([0-9a-f]{32})(\.control)?\.json\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_STATUS = {'queued', 'running', 'paused', 'cancelled', 'complete', 'attention', 'failed'}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Repeated assistant checkpoint field.')
        result[key] = value
    return result


def _path(value):
    text(value, 'saved local path', maximum=32768)
    if value.startswith(('\\\\', '//')) or '://' in value or '\x00' in value:
        raise ValueError('Assistant backup contains a non-local path.')


def validate_state_bytes(name, raw):
    """Return referenced workspace IDs after pure structural validation."""
    match = _NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None or type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Invalid assistant state filename or size.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number.')))
    if match[2]:
        if type(value) is not dict or set(value) != {'command'} or value['command'] not in ('run', 'pause', 'cancel'):
            raise ValueError('Invalid saved assistant control.')
        return ()
    fields = {'schema', 'id', 'data_dir', 'plan', 'plan_sha256', 'created', 'updated',
              'status', 'message', 'workspace', 'steps'}
    if (type(value) is not dict or set(value) != fields
            or value['schema'] != 'carr-assistant-job/1' or value['id'] != match[1]
            or value['status'] not in _STATUS):
        raise ValueError('Invalid assistant job checkpoint.')
    _path(value['data_dir'])
    for field in ('created', 'updated', 'message'):
        text(value[field], field, maximum=2000, empty=field == 'message')
    plan = value['plan']
    if (type(plan) is not dict or set(plan) != {'schema', 'scope', 'steps', 'summary'}
            or plan['schema'] != SCHEMA or not isinstance(value['plan_sha256'], str)
            or not _HASH.fullmatch(value['plan_sha256'])
            or sha256(canonical(plan).encode('utf-8')).hexdigest() != value['plan_sha256']):
        raise ValueError('The reviewed assistant plan checksum differs.')
    text(plan['summary'], 'plan summary', maximum=16384, empty=True)
    scope = plan['scope']
    if type(scope) is not dict or set(scope) != set(Scope.__dataclass_fields__):
        raise ValueError('Invalid saved assistant scope.')
    for field in ('allow_create', 'allow_full_backup'):
        if type(scope[field]) is not bool:
            raise ValueError('Invalid saved assistant permission.')
    for field, maximum in (('max_steps', 10000), ('max_files', 1000000), ('max_bytes', None)):
        number = scope[field]
        if type(number) is not int or number < 1 or (maximum is not None and number > maximum):
            raise ValueError('Invalid saved assistant resource budget.')
    if type(scope['max_seconds']) not in (int, float) or not math.isfinite(scope['max_seconds']) or scope['max_seconds'] <= 0:
        raise ValueError('Invalid saved assistant time budget.')
    if type(scope['input_roots']) is not list or len(scope['input_roots']) > scope['max_files']:
        raise ValueError('Invalid saved assistant input folders.')
    for path in scope['input_roots']:
        _path(path)
    if scope['output_root'] is not None:
        _path(scope['output_root'])
    workspaces = []
    for workspace in (scope['workspace_id'], value['workspace']):
        if workspace is not None:
            text(workspace, 'workspace identity', maximum=128)
            workspaces.append(workspace)
    steps, checkpoints = plan['steps'], value['steps']
    if (type(steps) is not list or not 1 <= len(steps) <= scope['max_steps']
            or type(checkpoints) is not list or len(steps) != len(checkpoints)):
        raise ValueError('Invalid saved assistant step inventory.')
    from .service import _check_arguments
    creates = used_files = used_bytes = 0
    for index, (step, checkpoint) in enumerate(zip(steps, checkpoints)):
        if type(step) is not dict or set(step) != {'tool', 'arguments'}:
            raise ValueError('Invalid saved assistant tool.')
        typed = Step(step['tool'], step['arguments'])
        _check_arguments(typed)
        if typed.tool == 'library.create':
            creates += 1
            if index or not scope['allow_create'] or scope['workspace_id'] is not None:
                raise ValueError('Invalid saved create-library permission.')
        if (type(checkpoint) is not dict or set(checkpoint) != {'status', 'intent', 'result', 'message'}
                or checkpoint['status'] not in ('pending', 'running', 'complete', 'failed')
                or type(checkpoint['intent']) is not dict
                or set(checkpoint['intent']) - {'batch', 'sha256'}):
            raise ValueError('Invalid saved assistant step checkpoint.')
        text(checkpoint['message'], 'step message', maximum=2000, empty=True)
        if checkpoint['status'] == 'complete' and type(checkpoint['result']) is not dict:
            raise ValueError('A completed assistant step has no result.')
        if checkpoint['status'] == 'complete':
            result = checkpoint['result']
            artifacts = result.get('artifacts')
            if type(artifacts) is not list:
                raise ValueError('A completed assistant step has no artifact inventory.')
            for artifact in artifacts:
                if (type(artifact) is not dict or set(artifact) != {'path', 'size', 'sha256'}
                        or type(artifact['size']) is not int or artifact['size'] < 0
                        or not isinstance(artifact['sha256'], str) or not _HASH.fullmatch(artifact['sha256'])):
                    raise ValueError('Invalid saved assistant artifact.')
                _path(artifact['path'])
            if typed.tool == 'library.backup':
                from .backup_receipt import BackupReceipt
                receipt = BackupReceipt.from_dict(result.get('backup_receipt'))
                _path(result.get('directory'))
                if (result.get('manifest_sha256') != receipt.manifest_sha256 or len(artifacts) != 1
                        or artifacts[0]['sha256'] != receipt.manifest_sha256
                        or artifacts[0]['size'] > receipt.total_bytes):
                    raise ValueError('Saved assistant backup receipt and manifest differ.')
                used_files += receipt.files
                used_bytes += receipt.total_bytes
            else:
                used_files += len(artifacts)
                used_bytes += sum(artifact['size'] for artifact in artifacts)
            if used_files > scope['max_files'] or used_bytes > scope['max_bytes']:
                raise ValueError('Saved results exceed the approved whole-job file or byte budget.')
    if creates > 1:
        raise ValueError('An assistant plan may create at most one library.')
    return tuple(dict.fromkeys(workspaces))
