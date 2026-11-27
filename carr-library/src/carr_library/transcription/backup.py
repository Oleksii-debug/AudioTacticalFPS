"""Typed, streamed ASR backup: retained inputs and accepted derivatives only.

External runtimes/models are never followed. Worker caches, obsolete launch
controls and live resource leases are intentionally outside the durable set.
A live run or source-retention transaction makes snapshot fail as busy.
"""
from contextlib import ExitStack
from hashlib import sha256
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from ..durable_state import atomic_json, encode, local_path, regular, state_lock, sync_directory
from ..operations.models import ResourceLimits
from .models import (AUDIO_EXTENSIONS, FORMATS, MAX_RESULT_BYTES, fingerprint, settings,
                     validated_result, encoded_outputs, validate_effective_settings)
from .runtime import file_identity, stopped, _stamp
from .verified_protocol import read_bytes, decode, relative_name, validate_packages, validate_implementation

KIND = 'carr-transcription-state'
MAX_FILES = 10000
MAX_BYTES = 8 * 1024**3
MAX_JOBS = 1000
MAX_PROFILES = 256
MAX_JSON = 32 * 1024**2
EXCLUDED = ['external-runtimes-and-models', 'worker-caches-and-launch-controls', 'live-resource-leases']
_UUID = re.compile('[0-9a-f]{32}')
_HEX = re.compile('[0-9a-f]{64}')


def _id(value):
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError('Invalid ASR state identity.')
    return value


def _hash(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError('Invalid ASR state hash.')
    return value


def _number(value, maximum=2**63 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('Invalid ASR state count.')
    return value


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**12:
        raise ValueError('Invalid ASR state time.')


def _text(value, maximum=32767, *, empty=False):
    if not isinstance(value, str) or (not value and not empty) or len(value) > maximum or '\x00' in value:
        raise ValueError('Invalid ASR state text.')
    value.encode('utf-8', 'strict')
    return value


def _external_label(value):
    """Check an inert historical path spelling, without resolving or opening it."""
    _text(value)
    if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise ValueError('An ASR historical path must be absolute.')
    if value.startswith(('\\\\', '//')) or '..' in PureWindowsPath(value).parts:
        raise ValueError('Unsupported ASR historical path.')


def _identity(value):
    if type(value) is not dict or set(value) != {'sha256', 'size'}:
        raise ValueError('Invalid ASR file identity.')
    _hash(value['sha256'])
    _number(value['size'], 64 * 1024**3)


def _json(path):
    return decode(read_bytes(path, maximum=MAX_JSON))


def _profile(value, identity):
    metadata = {'id', 'name', 'trusted', 'qualification', 'registered', 'restored'}
    fields = {'schema', 'protocol', 'python_executable', 'python_resolved', 'python_identity',
        'python_configuration', 'python_support', 'runtime_distributions', 'runtime_auxiliary',
        'runtime_capability', 'worker_path', 'worker_identity', 'model_directory', 'model_name',
        'model_files', 'model_bytes', 'model_content_fingerprint', 'fingerprint'} | metadata
    protocol = value.get('protocol') if isinstance(value, dict) else None
    if protocol == 'verified-file-job/1':
        fields |= {'protocol_descriptor', 'verified_packages', 'verified_implementation',
            'verified_site_packages', 'verified_visible_files', 'verified_standard_library', 'verified_markers'}
    elif protocol != 'local-file-job/1':
        raise ValueError('Unsupported ASR runtime protocol.')
    if (set(value) != fields or value['schema'] != 'resonance-transcription-runtime/1'
            or value['id'] != identity or _id(identity) != value['fingerprint'][:32]
            or type(value['trusted']) is not bool or type(value['restored']) is not bool
            or value['qualification'] != 'unqualified'):
        raise ValueError('Invalid ASR runtime profile.')
    _text(value['name'], 512)
    _time(value['registered'])
    _hash(value['fingerprint'])
    manifest = {key: item for key, item in value.items() if key not in metadata | {'fingerprint'}}
    if fingerprint(manifest) != value['fingerprint']:
        raise ValueError('ASR runtime profile fingerprint differs from its saved inventory.')
    for name in ('python_executable', 'python_resolved', 'worker_path', 'model_directory'):
        _external_label(value[name])
    for name in ('python_identity', 'worker_identity'):
        _identity(value[name])
    for name in ('python_configuration', 'python_support', 'runtime_auxiliary'):
        entries = value[name]
        if type(entries) is not dict or len(entries) > 8192:
            raise ValueError('Invalid ASR runtime support inventory.')
        for label, record in entries.items():
            _external_label(label)
            if name == 'python_support' and isinstance(record, dict) and 'resolved' in record:
                _external_label(record['resolved'])
                record = {key: item for key, item in record.items() if key != 'resolved'}
            _identity(record)
    distributions = value['runtime_distributions']
    if type(distributions) is not dict or set(distributions) != {
            'faster-whisper', 'ctranslate2', 'av', 'onnxruntime', 'numpy', 'huggingface-hub', 'tokenizers'}:
        raise ValueError('Invalid legacy ASR distribution inventory.')
    for record in distributions.values():
        if type(record) is not dict or set(record) != {'version', 'file_count', 'installed_files_sha256'}:
            raise ValueError('Invalid ASR distribution record.')
        _text(record['version'], 256)
        _number(record['file_count'], 65536)
        _hash(record['installed_files_sha256'])
    model = value['model_files']
    if type(model) is not dict or not 3 <= len(model) <= 4096:
        raise ValueError('Invalid ASR model inventory.')
    if not {'model.bin', 'config.json', 'tokenizer.json'} <= set(model):
        raise ValueError('Incomplete ASR model inventory.')
    for label, record in model.items():
        relative_name(label)
        _identity(record)
    if (fingerprint(model) != value['model_content_fingerprint']
            or type(value['model_bytes']) is not int
            or value['model_bytes'] != sum(record['size'] for record in model.values())
            or value['model_bytes'] > 64 * 1024**3):
        raise ValueError('Invalid ASR model content binding.')
    _text(value['model_name'], 512)
    _text(value['runtime_capability'], 256)
    if protocol == 'verified-file-job/1':
        validate_packages(value['verified_packages'])
        validate_implementation(value['verified_implementation'])
        if type(value['verified_site_packages']) is not list or len(value['verified_site_packages']) != 1:
            raise ValueError('Invalid isolated ASR import roots.')
        _external_label(value['verified_site_packages'][0])
        for name in ('protocol_descriptor', 'verified_visible_files', 'verified_standard_library', 'verified_markers'):
            if type(value[name]) is not dict:
                raise ValueError('Invalid verified ASR inventory metadata.')
    return value


def _job(value, identity, profiles):
    fields = {'schema', 'id', 'title', 'status', 'profile_id', 'profile_fingerprint', 'sources',
        'settings', 'results', 'edits', 'done', 'total', 'error', 'created', 'updated',
        'recovery_review_required', 'restored', 'plan_fingerprint'}
    optional = {'current', 'worker_plan_sources', 'worker_completion'}
    if (type(value) is not dict or not fields <= set(value) or set(value) - fields - optional
            or value['schema'] != 'resonance-transcription-job/1' or value['id'] != identity):
        raise ValueError('Invalid ASR job schema.')
    _id(identity)
    _text(value['title'], 512)
    _text(value['error'], 65536, empty=True)
    _time(value['created'])
    _time(value['updated'])
    for name in ('recovery_review_required', 'restored'):
        if type(value[name]) is not bool:
            raise ValueError('Invalid ASR recovery state.')
    if value['status'] not in ('pending', 'preparing', 'running', 'pausing', 'cancelling',
                              'paused', 'cancelled', 'interrupted', 'partial', 'failed', 'complete'):
        raise ValueError('Invalid ASR job status.')
    profile = profiles.get(_id(value['profile_id']))
    if profile is None or value['profile_fingerprint'] != profile['fingerprint']:
        raise ValueError('ASR job refers to a missing or different runtime inventory.')
    if settings(value['settings']) != value['settings']:
        raise ValueError('Incomplete ASR settings.')
    sources = value['sources']
    if type(sources) is not list or not 1 <= len(sources) <= 512:
        raise ValueError('Invalid ASR retained-source inventory.')
    ids = set()
    for row in sources:
        if type(row) is not dict or set(row) != {'id', 'original_path', 'name', 'retained_path', 'sha256', 'size'}:
            raise ValueError('Invalid ASR retained source.')
        source_id = _id(row['id'])
        suffix = PurePosixPath(row['retained_path']).suffix
        if (source_id in ids or suffix not in AUDIO_EXTENSIONS
                or row['retained_path'] != f'inputs/{source_id}/source{suffix}'):
            raise ValueError('Invalid or duplicate ASR retained-source path.')
        ids.add(source_id)
        _external_label(row['original_path'])
        _text(row['name'], 512)
        _hash(row['sha256'])
        if not 0 < _number(row['size'], 32 * 1024**3):
            raise ValueError('A retained ASR source is empty.')
    if sum(row['size'] for row in sources) > 64 * 1024**3:
        raise ValueError('ASR retained-source job budget exceeded.')
    plan = {name: value[name] for name in ('id', 'profile_id', 'profile_fingerprint', 'sources', 'settings')}
    if fingerprint(plan) != value['plan_fingerprint']:
        raise ValueError('ASR job plan identity changed.')
    results = value['results']
    if type(results) is not list or len(results) > len(sources):
        raise ValueError('Invalid accepted ASR results.')
    accepted = set()
    for result in results:
        if type(result) is not dict or set(result) != {'source_id', 'receipt', 'sha256', 'formats', 'language', 'duration', 'segments'}:
            raise ValueError('Invalid accepted ASR result record.')
        source_id = result['source_id']
        if source_id not in ids or source_id in accepted:
            raise ValueError('Duplicate or unbound ASR result.')
        accepted.add(source_id)
        pieces = relative_name(result['receipt']).split('/')
        if (len(pieces) != 4 or pieces[0] != 'artifacts' or pieces[1] != source_id
                or not _UUID.fullmatch(pieces[2]) or pieces[3] != 'receipt.json'):
            raise ValueError('Invalid ASR receipt path.')
        _hash(result['sha256'])
        if (type(result['formats']) is not list or not result['formats']
                or len(set(result['formats'])) != len(result['formats'])
                or any(name not in FORMATS for name in result['formats'])):
            raise ValueError('Invalid ASR export format inventory.')
        _text(result['language'], 3)
        _time(result['duration'])
        _number(result['segments'], 250000)
    if type(value['done']) is not int or value['done'] != len(results) or type(value['total']) is not int or value['total'] != len(sources):
        raise ValueError('ASR completion counts disagree with accepted results.')
    if value['status'] == 'complete' and value['done'] != value['total']:
        raise ValueError('Incomplete ASR state claims completion.')
    if type(value['edits']) is not dict or set(value['edits']) - accepted:
        raise ValueError('Unbound edited ASR derivatives.')
    for source_id, record in value['edits'].items():
        if type(record) is not dict or set(record) != {'id', 'path', 'sha256'}:
            raise ValueError('Invalid ASR edit reference.')
        _id(record['id'])
        _hash(record['sha256'])
        if record['path'] != f"edits/{source_id}/{record['id']}.json":
            raise ValueError('Invalid ASR edit path.')
    if 'current' in value:
        _text(value['current'], 2048, empty=True)
    if 'worker_plan_sources' in value:
        planned = value['worker_plan_sources']
        if type(planned) is not list or not planned or len(set(planned)) != len(planned) or set(planned) - ids:
            raise ValueError('Invalid ASR worker source plan.')
    if 'worker_completion' in value:
        completed = value['worker_completion']
        if (type(completed) is not dict or set(completed) != {'run_id', 'receipt', 'source_ids', 'profile_fingerprint'}
                or type(completed['source_ids']) is not list or not completed['source_ids']
                or len(set(completed['source_ids'])) != len(completed['source_ids'])
                or set(completed['source_ids']) - ids):
            raise ValueError('Invalid archived ASR completion reference.')
        _id(completed['run_id'])
        _hash(completed['profile_fingerprint'])
        record = completed['receipt']
        if type(record) is not dict or set(record) != {'path', 'sha256', 'size_bytes'}:
            raise ValueError('Invalid archived ASR worker receipt reference.')
        relative_name(record['path'])
        _hash(record['sha256'])
        _number(record['size_bytes'], MAX_JSON)
    return value


def _provenance(value, source, profiles, options):
    fields = {'derived', 'citation_authority', 'quality', 'source_sha256', 'source_bytes',
        'profile_id', 'profile_fingerprint', 'model_content_fingerprint', 'worker_sha256',
        'python_sha256', 'runtime_identity_sha256', 'worker_checkpoint_sha256', 'settings',
        'effective_settings', 'translation_target', 'diarization'}
    extra = {'worker_export_receipt_sha256', 'worker_protocol', 'text_headers'}
    if (type(value) is not dict or not fields <= set(value) or set(value) - fields - extra
            or value['derived'] is not True or value['citation_authority'] is not False
            or value['quality'] != 'unqualified' or value['diarization'] is not False
            or value['source_sha256'] != source['sha256'] or value['source_bytes'] != source['size']
            or value['settings'] != options):
        raise ValueError('Invalid ASR derivative provenance.')
    profile = profiles.get(value['profile_id'])
    if (profile is None or value['profile_fingerprint'] != profile['fingerprint']
            or value['model_content_fingerprint'] != profile['model_content_fingerprint']
            or value['worker_sha256'] != profile['worker_identity']['sha256']
            or value['python_sha256'] != profile['python_identity']['sha256']):
        raise ValueError('ASR result lost its original runtime provenance.')
    _hash(value['runtime_identity_sha256'])
    _hash(value['worker_checkpoint_sha256'])
    verified = profile['protocol'] == 'verified-file-job/1'
    if verified != ('worker_protocol' in value or 'worker_export_receipt_sha256' in value):
        raise ValueError('ASR result protocol differs from its original runtime provenance.')
    if 'worker_protocol' in value or 'worker_export_receipt_sha256' in value:
        if value.get('worker_protocol') != 'verified-file-job/1':
            raise ValueError('Invalid verified ASR provenance protocol.')
        _hash(value.get('worker_export_receipt_sha256'))
    if 'text_headers' in value and (value['text_headers'] != 'derived-v1' or not options['include_headers']):
        raise ValueError('ASR text headers do not match the requested export option.')
    if value['translation_target'] != ('en' if options['task'] == 'translate' else None):
        raise ValueError('ASR translation target changed.')
    validate_effective_settings(value['effective_settings'], options, profile['model_name'],
                                verified=value.get('worker_protocol') == 'verified-file-job/1')


def _path_kind(name):
    relative_name(name)
    if name == 'resource-policy.json':
        return 'policy'
    parts = name.split('/')
    if len(parts) == 2 and parts[0] == 'profiles' and parts[1].endswith('.json'):
        _id(parts[1][:-5])
        return 'json'
    if len(parts) < 3 or parts[0] != 'jobs':
        raise ValueError('Unknown ASR backup namespace.')
    _id(parts[1])
    if len(parts) == 3 and parts[2] == 'job.json':
        return 'json'
    if len(parts) == 5 and parts[2] == 'inputs':
        _id(parts[3])
        suffix = PurePosixPath(parts[4]).suffix
        if suffix in AUDIO_EXTENSIONS and parts[4] == 'source' + suffix:
            return 'source'
    if len(parts) == 6 and parts[2] == 'artifacts':
        _id(parts[3])
        _id(parts[4])
        if parts[5] == 'receipt.json' or parts[5] in {'transcript.' + fmt for fmt in FORMATS}:
            return 'result'
    if len(parts) == 5 and parts[2] == 'edits' and parts[4].endswith('.json'):
        _id(parts[3])
        _id(parts[4][:-5])
        return 'json'
    raise ValueError('Unknown ASR backup file path.')


def _list(directory, maximum):
    if not os.path.lexists(directory):
        return []
    regular(local_path(directory, exists=True), directory=True)
    paths = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(paths) >= maximum:
                raise ValueError('ASR state inventory limit exceeded.')
            paths.append(Path(entry.path))
    return sorted(paths)


def _policy(value):
    if type(value) is not dict or set(value) != {'schema', 'limits'} or value['schema'] != 'carr-resource-policy/1':
        raise ValueError('Invalid shared resource policy.')
    ResourceLimits.from_dict(value['limits'])


def _inventory(root, profile_ids, job_ids, *, policy=None, cancelled=lambda: False):
    """Validate all selected cross-file references and return exact owned files."""
    result, profiles, total_bytes = {}, {}, 0
    def admit(name, expected=None):
        nonlocal total_bytes
        stopped(cancelled)
        kind = _path_kind(name)
        path = local_path(root / name, root=root, exists=True)
        actual = file_identity(path, cancel=cancelled,
            maximum=32 * 1024**3 if kind == 'source' else MAX_JSON)
        if expected is not None and actual != expected:
            raise ValueError('An ASR backup file differs from its durable journal.')
        if name in result:
            raise ValueError('Duplicate ASR file binding.')
        result[name] = actual
        total_bytes += actual['size']
        if len(result) > MAX_FILES or total_bytes > MAX_BYTES:
            raise ValueError('ASR backup inventory exceeds its supported aggregate limit.')
        return path
    for identity in profile_ids:
        path = admit('profiles/' + identity + '.json')
        profiles[identity] = _profile(_json(path), identity)
    for identity in job_ids:
        base = 'jobs/' + identity + '/'
        state = _job(_json(admit(base + 'job.json')), identity, profiles)
        sources = {row['id']: row for row in state['sources']}
        receipts = {}
        for row in sources.values():
            admit(base + row['retained_path'], {key: row[key] for key in ('sha256', 'size')})
        for record in state['results']:
            receipt_path = admit(base + record['receipt'])
            if result[base + record['receipt']]['sha256'] != record['sha256']:
                raise ValueError('ASR accepted result receipt changed.')
            receipt = _json(receipt_path)
            if (type(receipt) is not dict or set(receipt) != {'schema', 'source_id', 'result', 'provenance', 'files', 'created', 'status'}
                    or receipt['schema'] != 'resonance-transcription-result/1'
                    or receipt['source_id'] != record['source_id'] or receipt['status'] != 'complete'):
                raise ValueError('Invalid ASR result receipt.')
            _time(receipt['created'])
            accepted = receipt['result']
            if (type(accepted) is not dict or set(accepted) != {'language', 'duration', 'segments', 'text'}
                    or validated_result(accepted, accepted['text']) != accepted
                    or accepted['language'] != record['language'] or accepted['duration'] != record['duration']
                    or len(accepted['segments']) != record['segments']):
                raise ValueError('Invalid normalized ASR result.')
            _provenance(receipt['provenance'], sources[record['source_id']], profiles, state['settings'])
            files = receipt['files']
            expected_formats = {name for name in FORMATS if state['settings']['output_' + name]}
            if type(files) is not dict or set(files) != expected_formats or set(record['formats']) != expected_formats:
                raise ValueError('ASR requested export formats are incomplete.')
            payloads = encoded_outputs(accepted, receipt['provenance'])
            parent = PurePosixPath(record['receipt']).parent
            for name, item in files.items():
                if type(item) is not dict or set(item) != {'path', 'sha256', 'size'} or item['path'] != str(parent / ('transcript.' + name)):
                    raise ValueError('Invalid ASR export path.')
                payload = payloads[name]
                expected = {'sha256': sha256(payload).hexdigest(), 'size': len(payload)}
                if {key: item[key] for key in ('sha256', 'size')} != expected:
                    raise ValueError('ASR export identity differs from normalized recognition.')
                admit(base + item['path'], expected)
            receipts[record['source_id']] = receipt
        seen_edits = set()
        for directory in _list(root / base / 'edits', 512):
            source_id = _id(directory.name)
            if source_id not in receipts:
                raise ValueError('Edited ASR history has no accepted original result.')
            regular(local_path(directory, root=root, exists=True), directory=True)
            for path in _list(directory, MAX_FILES):
                name = path.relative_to(root).as_posix()
                value = _json(admit(name))
                if (type(value) is not dict or set(value) != {'id', 'source_id', 'text', 'edited', 'provenance', 'created', 'path'}
                        or value['id'] != path.stem or value['source_id'] != source_id or value['edited'] is not True
                        or value['path'] != path.relative_to(root / base).as_posix()):
                    raise ValueError('Invalid ASR edit history.')
                _id(value['id'])
                _text(value['text'], MAX_RESULT_BYTES, empty=True)
                _time(value['created'])
                expected_provenance = {**receipts[source_id]['provenance'],
                    'revision_kind': 'user-edited-transcript',
                    'original_transcript_sha256': sha256(receipts[source_id]['result']['text'].encode('utf-8')).hexdigest()}
                if value['provenance'] != expected_provenance:
                    raise ValueError('ASR edit is detached from its original recognition.')
                latest = state['edits'].get(source_id)
                if latest is not None and value['id'] == latest['id']:
                    if result[name]['sha256'] != latest['sha256'] or latest['path'] != value['path']:
                        raise ValueError('The latest ASR edit changed.')
                    seen_edits.add(source_id)
        if seen_edits != set(state['edits']):
            raise ValueError('The latest ASR edit is missing.')
    if policy is not None:
        _policy(_json(policy))
        result['resource-policy.json'] = file_identity(policy, cancel=cancelled, maximum=65536)
    return result


def _copy(source, target, identity, *, cancelled):
    regular(local_path(source, exists=True))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    local_path(target.parent, exists=True)
    before = regular(source)
    digest, size = sha256(), 0
    with source.open('rb') as incoming, target.open('xb') as outgoing:
        if _stamp(os.fstat(incoming.fileno())) != _stamp(before):
            raise ValueError('ASR backup source changed while opening.')
        while block := incoming.read(1024 * 1024):
            stopped(cancelled)
            size += len(block)
            if size > identity['size']:
                raise ValueError('ASR backup source grew during snapshot.')
            digest.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        if _stamp(os.fstat(incoming.fileno())) != _stamp(before):
            raise ValueError('ASR backup source changed during snapshot.')
    if (_stamp(regular(source)) != _stamp(before)
            or {'sha256': digest.hexdigest(), 'size': size} != identity):
        raise ValueError('ASR backup source differs from its sealed identity.')
    sync_directory(target.parent)


def validate_manifest(value):
    if (type(value) is not dict or set(value) != {'kind', 'version', 'profiles', 'jobs', 'files', 'restore_mode', 'excluded'}
            or value['kind'] != KIND or type(value['version']) is not int or value['version'] != 1
            or value['restore_mode'] != 'review-required' or value['excluded'] != EXCLUDED):
        raise ValueError('Unsupported ASR backup manifest.')
    for name, maximum in (('profiles', MAX_PROFILES), ('jobs', MAX_JOBS)):
        values = value[name]
        if type(values) is not list or len(values) > maximum or values != sorted(set(values)):
            raise ValueError('Invalid ASR backup object inventory.')
        for identity in values:
            _id(identity)
    if type(value['files']) is not list or len(value['files']) > MAX_FILES:
        raise ValueError('Invalid ASR backup file inventory.')
    names, total = [], 0
    for row in value['files']:
        if type(row) is not dict or set(row) != {'filename', 'sha256', 'size_bytes'}:
            raise ValueError('Invalid ASR backup file record.')
        kind = _path_kind(row['filename'])
        _hash(row['sha256'])
        _number(row['size_bytes'], MAX_BYTES if kind == 'source' else MAX_JSON)
        names.append(row['filename'])
        total += row['size_bytes']
    if names != sorted(set(names)) or total > MAX_BYTES:
        raise ValueError('ASR backup ordering, duplicates or aggregate bytes are invalid.')
    expected_profiles = {'profiles/' + identity + '.json' for identity in value['profiles']}
    expected_jobs = {'jobs/' + identity + '/job.json' for identity in value['jobs']}
    if {name for name in names if name.startswith('profiles/')} != expected_profiles:
        raise ValueError('ASR profile manifest is incomplete.')
    if {name for name in names if name.endswith('/job.json')} != expected_jobs:
        raise ValueError('ASR job manifest is incomplete.')
    if any(name.startswith('jobs/') and name.split('/')[1] not in value['jobs'] for name in names):
        raise ValueError('ASR payload belongs to an undeclared job.')
    return value


def _assert_namespace(root, names):
    expected = set(names)
    directories = {str(parent) for name in names for parent in PurePosixPath(name).parents if str(parent) != '.'}
    pending, seen = [root], set()
    while pending:
        folder = pending.pop()
        regular(local_path(folder, root=root, exists=True), directory=True)
        for path in _list(folder, MAX_FILES + MAX_JOBS * 5):
            relative = path.relative_to(root).as_posix()
            if relative in directories:
                regular(local_path(path, root=root, exists=True), directory=True)
                pending.append(path)
            elif relative in expected:
                regular(local_path(path, root=root, exists=True))
                seen.add(relative)
            else:
                raise ValueError('Unexpected file or directory in typed ASR backup.')
    if seen != expected:
        raise ValueError('ASR backup is incomplete.')


def snapshot(root, stage, *, max_bytes=MAX_BYTES, max_files=MAX_FILES, cancelled=lambda: False):
    root, stage = local_path(root, exists=True), Path(stage)
    policy = root / 'state/operations/resources.json'
    state = root / 'state/transcription'
    stage.mkdir(mode=0o700)
    profiles, jobs, identities = [], [], {}
    with ExitStack() as locks:
        if os.path.lexists(state):
            regular(local_path(state, root=root, exists=True), directory=True)
            locks.enter_context(state_lock(state, 'inventory'))
            locks.enter_context(state_lock(state / 'profiles', 'profiles'))
            for path in _list(state / 'profiles', MAX_PROFILES + 1):
                if path.name == '.profiles.lock':
                    regular(path)
                    continue
                if path.suffix != '.json':
                    raise ValueError('Unexpected ASR profile state.')
                profiles.append(_id(path.stem))
            for path in _list(state / 'jobs', MAX_JOBS):
                jobs.append(_id(path.name))
                regular(local_path(path, root=state, exists=True), directory=True)
                locks.enter_context(state_lock(path, 'run'))
            identities = _inventory(state, sorted(profiles), sorted(jobs), cancelled=cancelled)
        if os.path.lexists(policy):
            regular(local_path(policy, root=root, exists=True))
            _policy(_json(policy))
            identities['resource-policy.json'] = file_identity(policy, cancel=cancelled, maximum=65536)
        if (len(identities) > min(max_files, MAX_FILES)
                or sum(row['size'] for row in identities.values()) > min(max_bytes, MAX_BYTES)):
            raise ValueError('ASR snapshot exceeds the remaining complete-backup budget.')
        for name, identity in sorted(identities.items()):
            source = policy if name == 'resource-policy.json' else state / name
            _copy(source, stage / name, identity, cancelled=cancelled)
    manifest = {'kind': KIND, 'version': 1, 'profiles': sorted(profiles), 'jobs': sorted(jobs),
        'files': [{'filename': name, 'sha256': row['sha256'], 'size_bytes': row['size']}
                  for name, row in sorted(identities.items())],
        'restore_mode': 'review-required', 'excluded': EXCLUDED}
    validate_manifest(manifest)
    _assert_namespace(stage, identities)
    return manifest


def restore(source, stage, manifest, *, cancelled=lambda: False):
    validate_manifest(manifest)
    source = local_path(source, exists=True)
    _assert_namespace(source, [row['filename'] for row in manifest['files']])
    identities = _inventory(source, manifest['profiles'], manifest['jobs'],
        policy=(source / 'resource-policy.json') if (source / 'resource-policy.json').exists() else None,
        cancelled=cancelled)
    expected = {row['filename']: {'sha256': row['sha256'], 'size': row['size_bytes']}
                for row in manifest['files']}
    if identities != expected:
        raise ValueError('Typed ASR backup differs from its complete manifest.')
    destination = Path(stage) / 'state/transcription'
    paths = []
    for name, identity in identities.items():
        target = (Path(stage) / 'state/operations/resources.json' if name == 'resource-policy.json'
                  else destination / name)
        _copy(source / name, target, identity, cancelled=cancelled)
    if manifest['profiles'] or manifest['jobs']:
        from .service import prepare_restored_state
        prepare_restored_state(Path(stage) / 'state')
        paths.append('state/transcription')
    if 'resource-policy.json' in identities:
        paths.append('state/operations')
    return paths
