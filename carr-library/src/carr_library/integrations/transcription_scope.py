"""Local execution authority for canonical transcription data.

Portable job/profile records never grant execution authority by themselves.
The origin nonce is local-only; restore revokes copied bindings, including
same-path restore. Historical result validation never opens external engines.
"""
from pathlib import Path
import re
import time
from uuid import uuid4

from ..durable_state import atomic_json, local_path, regular, state_lock

BINDING_SCHEMA = 'resonance-transcription-workspace/1'
ORIGIN_SCHEMA = 'resonance-transcription-origin/1'
ORIGIN_NAME = '.local-origin.json'


def validate_binding(value, identity):
    required = {'schema', 'job_id', 'workspace', 'origin', 'bound_at',
                'review_required', 'reviewed_plan_fingerprint'}
    if type(value) is not dict or set(value) != required:
        raise ValueError('Invalid transcription workspace binding.')
    if (value['schema'] != BINDING_SCHEMA or value['job_id'] != identity
            or type(identity) is not str or not re.fullmatch('[0-9a-f]{32}', identity)
            or type(value['workspace']) is not str or not 1 <= len(value['workspace']) <= 200
            or '\x00' in value['workspace']
            or type(value['review_required']) is not bool
            or type(value['bound_at']) not in (int, float)
            or not 0 <= value['bound_at'] < 10**12
            or type(value['reviewed_plan_fingerprint']) is not str
            or not re.fullmatch('[0-9a-f]{64}', value['reviewed_plan_fingerprint'])):
        raise ValueError('Invalid transcription workspace authority.')
    origin = value['origin']
    if origin is None:
        if not value['review_required']:
            raise ValueError('A transcription binding without local origin requires review.')
    elif (type(origin) is not dict or set(origin) != {'root', 'nonce'}
          or type(origin['root']) is not str or not origin['root']
          or '\x00' in origin['root'] or len(origin['root']) > 32768
          or type(origin['nonce']) is not str or not re.fullmatch('[0-9a-f]{64}', origin['nonce'])):
        raise ValueError('Invalid transcription local-origin binding.')
    return value


def _bounded_json(path, maximum):
    # Reuse the existing regular-handle/size check and strict inert JSON
    # parser. No selected runtime or producer implementation is executed.
    from ..backup_sidecars import read_regular, _json
    return _json(read_regular(path, maximum))


def local_origin(host, *, create=False):
    host._authorize()
    root = local_path(host.root)
    target = root / ORIGIN_NAME
    if not target.exists():
        if not create:
            return None
        with state_lock(root, 'origin'):
            if not target.exists():
                atomic_json(target, {'schema': ORIGIN_SCHEMA, 'nonce': uuid4().hex + uuid4().hex})
    if regular(local_path(target, root=root, exists=True)).st_size > 4096:
        raise ValueError('The local transcription authority exceeds its byte budget.')
    value = _bounded_json(target, 4096)
    if (type(value) is not dict or set(value) != {'schema', 'nonce'}
            or value['schema'] != ORIGIN_SCHEMA or type(value['nonce']) is not str
            or not re.fullmatch('[0-9a-f]{64}', value['nonce'])):
        raise ValueError('Invalid local transcription execution authority.')
    host._authorize()
    return {'root': str(Path(host.app.root)), 'nonce': value['nonce']}


def read_binding(host, job_root, identity):
    host._authorize()
    path = local_path(Path(job_root) / 'workspace.json', root=host.root)
    if not path.exists():
        return None
    if regular(path).st_size > 65536:
        raise ValueError('The transcription workspace binding exceeds its byte budget.')
    return validate_binding(_bounded_json(path, 65536), identity)


def check_scope(host, job_root, state, *, execution=False):
    host._authorize()
    binding = read_binding(host, job_root, state['id'])
    if binding is not None and binding['workspace'] != host.workspace:
        raise PermissionError('This transcription job is bound to another workspace.')
    if execution and (binding is None or binding['review_required']
                      or binding['origin'] != local_origin(host)
                      or binding['reviewed_plan_fingerprint'] != state['plan_fingerprint']):
        raise PermissionError('Review and explicitly bind this retained job to the current workspace before continuing.')
    return binding


def write_binding(host, job_root, state):
    host._authorize()
    value = {'schema': BINDING_SCHEMA, 'job_id': state['id'],
             'workspace': host.workspace, 'origin': local_origin(host, create=True),
             'bound_at': time.time(), 'review_required': False,
             'reviewed_plan_fingerprint': state['plan_fingerprint']}
    validate_binding(value, state['id'])
    host._authorize()
    target = local_path(Path(job_root) / 'workspace.json', root=host.root)
    atomic_json(target, value, replace=target.exists())
    return value
