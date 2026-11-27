"""Passive canonical ASR schemas, reused from e69ba240 without its service.

Exact old payloads are validated as historical data only. External paths are
lexical declarations; no interpreter, model, provider or original path is opened.
Current completion remains governed by transcription_receipt.py.
"""
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re

from .legacy_transcription_values import fields, text, number, digest, path
from .ingest.restore import RestoreError
from .legacy_transcription_values import (AUDIO_EXTENSIONS, DEFAULT_SETTINGS, FORMATS,
                                   encoded_outputs, fingerprint, settings,
                                   validated_result)

PREFIX = 'state/transcription/'
MAX_INPUT_BYTES = 64 * 1024**3
MAX_SOURCE_BYTES = 32 * 1024**3
MAX_METADATA_BYTES = 32 * 1024**2
MAX_JOBS = 4096
MAX_PROFILES = 1024
MAX_SOURCES = 512
MAX_HISTORY = 1024
ID = r'[0-9a-f]{32}'
HEX = r'[0-9a-f]{64}'
PROFILE = re.compile(PREFIX + r'profiles/(' + ID + r')\.json\Z')
JOB = re.compile(PREFIX + r'jobs/(' + ID + r')/(job|control)\.json\Z')
INPUT = re.compile(PREFIX + r'jobs/(' + ID + r')/inputs/(' + ID + r')/source(\.[a-z0-9]+)\Z')
ARTIFACT = re.compile(PREFIX + r'jobs/(' + ID + r')/artifacts/(' + ID + r')/(' + ID +
                      r')/(receipt\.json|transcript\.(?:txt|json|srt|vtt))\Z')
EDIT = re.compile(PREFIX + r'jobs/(' + ID + r')/edits/(' + ID + r')/(' + ID + r')\.json\Z')
WORKER = re.compile(PREFIX + r'jobs/(' + ID + r')/(worker|worker-history/' + ID + '-' + ID + r')/(.+)\Z')
CACHE = re.compile(r'logs/cache/(?:verified-v1/)?(?:[0-9a-f]{2}/)?(' + HEX + r')\.(cache\.json|receipt\.json|json|txt)\Z')
RESULT = re.compile(r'results/(' + ID + r')/source\.(txt|json|srt|vtt)\Z')
_PACKAGES = {'faster-whisper', 'ctranslate2', 'av', 'onnxruntime', 'numpy', 'huggingface-hub', 'tokenizers'}
_CONTROL = {'job_config.json', 'settings_live.json', 'job_status.json', 'pause.flag', 'stop.flag'}


def is_input(name):
    match = INPUT.fullmatch(name)
    return bool(match and match[3] in AUDIO_EXTENSIONS)


def is_cache_directory(name):
    match = WORKER.fullmatch(name)
    return bool(match and match[3] == 'logs/cache')


def is_cache_file(name):
    """Exact optional cache subtree of the canonical current/history worker."""
    match = WORKER.fullmatch(name) if isinstance(name, str) else None
    return bool(match and CACHE.fullmatch(match[3]))


def ignored(name):
    if not name.startswith(PREFIX):
        return False
    leaf = PurePosixPath(name).name
    return (leaf in ('.run.lock', '.control.lock', '.profiles.lock')
            or leaf.startswith('.pending-')
            or bool(re.fullmatch(r'\.v43-[A-Za-z0-9_-]+\.tmp', leaf)))


def limit(name):
    if is_input(name):
        return MAX_SOURCE_BYTES
    if PROFILE.fullmatch(name) or JOB.fullmatch(name) or ARTIFACT.fullmatch(name) or EDIT.fullmatch(name):
        return MAX_METADATA_BYTES
    match = WORKER.fullmatch(name)
    if match and (match[3] in _CONTROL or match[3] in ('logs/progress.log', 'logs/failed.log')
                  or RESULT.fullmatch(match[3]) or CACHE.fullmatch(match[3])):
        return MAX_METADATA_BYTES
    return None


def allows_empty(name):
    return bool(limit(name) and not is_input(name)
                and name.endswith(('.txt', '.srt', '.log', '.flag')))


def _id(value):
    if type(value) is not str or not re.fullmatch(ID, value):
        raise ValueError('Invalid transcription identity.')


def _bool(value):
    if type(value) is not bool:
        raise ValueError('Invalid transcription boolean.')


def _relative(value):
    # Same portable path rules as the archive, without resolving external data.
    from .backup_legacy_state import relative_name
    parsed = relative_name(value)
    if len(value) > 1024 or len(parsed.parts) > (12 if value.startswith(PREFIX) else 8):
        raise ValueError('Excessive historical transcription path depth.')


def _identity(value, *, maximum=MAX_INPUT_BYTES):
    fields(value, {'sha256', 'size'})
    digest(value['sha256'])
    number(value['size'], 0, maximum, integer=True)


def _inventory(value, *, external=False, maximum=100000):
    if type(value) is not dict or len(value) > maximum:
        raise ValueError('Excessive transcription identity inventory.')
    for name, identity in value.items():
        (path if external else _relative)(name)
        _identity(identity)


def _packages(value):
    if type(value) is not dict or set(value) != _PACKAGES:
        raise ValueError('Invalid reviewed transcription distribution inventory.')
    for entry in value.values():
        fields(entry, {'version', 'file_count', 'installed_files_sha256'})
        text(entry['version'], 1024)
        number(entry['file_count'], 1, 100000, integer=True)
        digest(entry['installed_files_sha256'])


def _settings(value, *, worker=False, partial=False):
    if type(value) is not dict:
        raise ValueError('Invalid transcription settings.')
    extras = {'model', 'translation_target'} if worker else set()
    if set(value) - set(DEFAULT_SETTINGS) - extras:
        raise ValueError('Unknown transcription setting.')
    public = {key: item for key, item in value.items() if key in DEFAULT_SETTINGS}
    normalized = settings(public)
    if not partial and normalized != public:
        raise ValueError('Incomplete transcription settings.')
    if worker:
        if not partial and not extras <= value.keys():
            raise ValueError('Missing worker model/task settings.')
        if 'model' in value:
            text(value['model'], 512)
        if 'translation_target' in value and value['translation_target'] != 'en':
            raise ValueError('Unsupported worker translation target.')


def _profile(value, identity):
    fields(value, {'schema', 'protocol', 'python_executable', 'python_resolved', 'python_identity',
        'python_configuration', 'runtime_distributions', 'runtime_auxiliary', 'runtime_capability',
        'worker_path', 'worker_identity', 'model_directory', 'model_name', 'model_files', 'model_bytes',
        'model_content_fingerprint', 'fingerprint', 'id', 'name', 'trusted', 'qualification',
        'registered', 'restored'}, {'python_support'})
    if (value['schema'] != 'resonance-transcription-runtime/1' or value['protocol'] != 'local-file-job/1'
            or value['id'] != identity or identity != value['fingerprint'][:32]
            or value['qualification'] != 'unqualified'
            or value['runtime_capability'] != 'installed-files-reviewed; execution-and-quality-unqualified'):
        raise ValueError('Invalid transcription runtime declaration.')
    for name in ('python_executable', 'python_resolved', 'worker_path', 'model_directory'):
        path(value[name])
    for name in ('python_identity', 'worker_identity'):
        _identity(value[name])
    for name in ('python_configuration', 'runtime_auxiliary', 'python_support'):
        _inventory(value.get(name, {}), external=True)
    _inventory(value['model_files'], maximum=4096)
    if not {'model.bin', 'config.json', 'tokenizer.json'} <= value['model_files'].keys():
        raise ValueError('Incomplete reviewed model declaration.')
    _packages(value['runtime_distributions'])
    for name in ('trusted', 'restored'):
        _bool(value[name])
    for name in ('name', 'model_name'):
        text(value[name], 512)
    number(value['registered'])
    number(value['model_bytes'], 1, MAX_INPUT_BYTES, integer=True)
    if (value['model_bytes'] != sum(item['size'] for item in value['model_files'].values())
            or value['model_content_fingerprint'] != fingerprint(value['model_files'])):
        raise ValueError('Model declaration fingerprint changed.')
    admission = {key: item for key, item in value.items() if key not in
                 {'fingerprint', 'id', 'name', 'trusted', 'qualification', 'registered', 'restored'}}
    if value['fingerprint'] != fingerprint(admission):
        raise ValueError('Reviewed runtime declaration fingerprint changed.')


def _job(value, identity):
    fields(value, {'schema', 'id', 'title', 'status', 'profile_id', 'profile_fingerprint', 'sources',
        'settings', 'results', 'edits', 'done', 'total', 'error', 'created', 'updated',
        'recovery_review_required', 'restored', 'plan_fingerprint'}, {'current', 'worker_cache_rejected'})
    if (value['schema'] != 'resonance-transcription-job/1' or value['id'] != identity
            or value['status'] not in ('pending', 'preparing', 'running', 'pausing', 'paused', 'cancelling',
                                      'cancelled', 'interrupted', 'failed', 'partial', 'complete')):
        raise ValueError('Invalid transcription job.')
    _id(value['profile_id'])
    digest(value['profile_fingerprint'])
    text(value['title'], 512)
    text(value['error'], MAX_METADATA_BYTES, empty=True)
    if 'current' in value:
        text(value['current'], 32768, empty=True)
    for name in ('created', 'updated'):
        number(value[name])
    for name in ('recovery_review_required', 'restored', 'worker_cache_rejected'):
        if name in value:
            _bool(value[name])
    _settings(value['settings'])
    sources = value['sources']
    if type(sources) is not list or not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError('Invalid retained-source inventory.')
    seen = set()
    for source in sources:
        fields(source, {'id', 'original_path', 'name', 'retained_path', 'sha256', 'size'})
        _id(source['id'])
        path(source['original_path'])
        text(source['name'], 32768)
        digest(source['sha256'])
        number(source['size'], 1, MAX_SOURCE_BYTES, integer=True)
        name = PREFIX + 'jobs/' + identity + '/' + source['retained_path']
        match = INPUT.fullmatch(name)
        if not is_input(name) or match[2] != source['id'] or source['id'] in seen:
            raise ValueError('Retained source path/identity mismatch.')
        seen.add(source['id'])
    if sum(source['size'] for source in sources) > MAX_INPUT_BYTES:
        raise ValueError('Transcription source batch exceeds its byte budget.')
    if type(value['results']) is not list or len(value['results']) > len(sources):
        raise ValueError('Invalid accepted-result inventory.')
    completed = set()
    for result in value['results']:
        fields(result, {'source_id', 'receipt', 'sha256', 'formats', 'language', 'duration', 'segments'})
        if result['source_id'] not in seen or result['source_id'] in completed:
            raise ValueError('Invalid accepted-result source.')
        completed.add(result['source_id'])
        match = ARTIFACT.fullmatch(PREFIX + 'jobs/' + identity + '/' + result['receipt'])
        if not match or match[2] != result['source_id'] or match[4] != 'receipt.json':
            raise ValueError('Invalid accepted receipt path.')
        digest(result['sha256'])
        if (type(result['formats']) is not list or not result['formats']
                or len(set(result['formats'])) != len(result['formats']) or set(result['formats']) - set(FORMATS)):
            raise ValueError('Invalid accepted export formats.')
        if type(result['language']) is not str or not re.fullmatch('[a-z]{2,3}', result['language']):
            raise ValueError('Invalid accepted language.')
        number(result['duration'], 0, 30 * 86400)
        number(result['segments'], 0, 250000, integer=True)
    if type(value['edits']) is not dict or len(value['edits']) > len(sources):
        raise ValueError('Invalid edited-transcript inventory.')
    for source_id, edit in value['edits'].items():
        fields(edit, {'id', 'path', 'sha256'})
        _id(edit['id'])
        digest(edit['sha256'])
        if source_id not in completed or edit['path'] != f'edits/{source_id}/{edit["id"]}.json':
            raise ValueError('Invalid edited-transcript path.')
    if (type(value['done']) is not int or value['done'] != len(completed)
            or type(value['total']) is not int or value['total'] != len(sources)
            or value['status'] == 'complete' and len(completed) != len(sources)):
        raise ValueError('Transcription completion disagrees with its accepted results.')
    plan = {name: value[name] for name in ('id', 'profile_id', 'profile_fingerprint', 'sources', 'settings')}
    if value['plan_fingerprint'] != fingerprint(plan):
        raise ValueError('Transcription job plan fingerprint changed.')


def _provenance(value, *, edited=False):
    required = {'citation_authority', 'derived', 'diarization', 'effective_settings',
        'model_content_fingerprint', 'profile_fingerprint', 'profile_id', 'python_sha256', 'quality',
        'runtime_identity_sha256', 'settings', 'source_bytes', 'source_sha256', 'translation_target',
        'worker_checkpoint_sha256', 'worker_sha256'}
    if edited:
        required |= {'revision_kind', 'original_transcript_sha256'}
    fields(value, required)
    if (value['citation_authority'] is not False or value['derived'] is not True
            or value['diarization'] is not False or value['quality'] != 'unqualified'):
        raise ValueError('A derived transcript cannot acquire source/quality authority.')
    for name in required:
        if name.endswith(('_sha256', '_fingerprint')):
            digest(value[name])
    _id(value['profile_id'])
    number(value['source_bytes'], 1, MAX_SOURCE_BYTES, integer=True)
    _settings(value['settings'])
    _settings(value['effective_settings'], worker=True)
    if (value['effective_settings']['language'] != value['settings']['language']
            or value['effective_settings']['task'] != value['settings']['task']
            or value['translation_target'] != ('en' if value['settings']['task'] == 'translate' else None)):
        raise ValueError('Transcription provenance changes the selected language/task.')
    if edited and value['revision_kind'] != 'user-edited-transcript':
        raise ValueError('Invalid transcript revision kind.')


def _result(value):
    fields(value, {'language', 'duration', 'segments', 'text'})
    if validated_result(value, value['text']) != value:
        raise ValueError('Transcript result is not normalized.')


def _receipt(value, match):
    fields(value, {'schema', 'source_id', 'result', 'provenance', 'files', 'created', 'status'})
    if (value['schema'] != 'resonance-transcription-result/1' or value['status'] != 'complete'
            or value['source_id'] != match[2]):
        raise ValueError('Invalid accepted transcript receipt.')
    _result(value['result'])
    _provenance(value['provenance'])
    number(value['created'])
    if type(value['files']) is not dict or not value['files'] or set(value['files']) - set(FORMATS):
        raise ValueError('Invalid accepted transcript files.')
    expected = encoded_outputs(value['result'], value['provenance'])
    for extension, record in value['files'].items():
        fields(record, {'path', 'sha256', 'size'})
        _identity({key: record[key] for key in ('sha256', 'size')}, maximum=MAX_METADATA_BYTES)
        if (record['path'] != f'artifacts/{match[2]}/{match[3]}/transcript.{extension}'
                or record['size'] != len(expected[extension])
                or record['sha256'] != sha256(expected[extension]).hexdigest()):
            raise ValueError('Accepted export bytes disagree with their typed result.')


def _worker_identity(value):
    fields(value, {'schema', 'source', 'group', 'relative', 'model', 'runtime', 'settings'})
    if value['schema'] not in ('whisper-v43-local-integrity-1', 'synthetic-file-job/1'):
        raise ValueError('Unsupported inert worker checkpoint schema.')
    fields(value['source'], {'path', 'sha256', 'size'})
    path(value['source']['path'])
    _identity({key: value['source'][key] for key in ('sha256', 'size')})
    text(value['group'], 512)
    _relative(value['relative'])
    fields(value['model'], {'name', 'root', 'files'})
    text(value['model']['name'], 512)
    path(value['model']['root'])
    _inventory(value['model']['files'], maximum=4096)
    runtime = value['runtime']
    fields(runtime, {'worker', 'executable', 'packages'}, {'python', 'implementation', 'platform', 'machine', 'fixture'})
    _identity(runtime['worker'])
    _identity(runtime['executable'])
    _packages(runtime['packages'])
    for name in ('python', 'implementation', 'platform', 'machine'):
        if name in runtime:
            text(runtime[name], 4096)
    if 'fixture' in runtime:
        _bool(runtime['fixture'])
    _settings(value['settings'], worker=True)


def _worker_meta(value):
    fields(value, {'text', 'duration', 'segments'}, {'language', 'detected_language', 'settings',
        'effective_settings', 'created', 'group', 'local_integrity', 'relative', 'requested_settings',
        'selected_language', 'source', 'v43_attempt', 'v43_attempt_no'})
    validated_result(value, value['text'])
    for key in ('settings', 'effective_settings', 'requested_settings'):
        if key in value:
            _settings(value[key], worker=True)
    for key in ('source',):
        if key in value:
            path(value[key])
    for key in ('group', 'selected_language', 'v43_attempt'):
        if key in value:
            text(value[key], 4096)
    if 'relative' in value:
        _relative(value['relative'])
    if 'created' in value:
        if isinstance(value['created'], str):
            text(value['created'], 128)
        else:
            number(value['created'])
    if 'v43_attempt_no' in value:
        number(value['v43_attempt_no'], 0, 1000000, integer=True)
    if 'local_integrity' in value:
        fields(value['local_integrity'], {'schema', 'identity_sha256', 'selected_backend'})
        text(value['local_integrity']['schema'], 128)
        digest(value['local_integrity']['identity_sha256'])
        backend = value['local_integrity']['selected_backend']
        fields(backend, {'device', 'compute_type', 'num_workers'})
        text(backend['device'], 128)
        text(backend['compute_type'], 128)
        number(backend['num_workers'], 1, 4, integer=True)


def validate(name, raw):
    """Validate one declared file; return JSON only for collection checks."""
    from .ingest.bundle import _unique_object
    if not limit(name) or is_input(name):
        raise ValueError('Unsupported transcription metadata path.')
    if not name.endswith('.json'):
        value = raw.decode('utf-8')
        if '\x00' in value or name.endswith('.vtt') and not value.startswith('WEBVTT\n'):
            raise ValueError('Invalid inert transcript/log text.')
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite historical value.')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid historical transcription JSON.') from exc
    if match := PROFILE.fullmatch(name):
        _profile(value, match[1])
    elif match := JOB.fullmatch(name):
        if match[2] == 'job':
            _job(value, match[1])
        else:
            fields(value, {'action'})
            if value['action'] not in ('run', 'pause', 'cancel'):
                raise ValueError('Invalid transcription control.')
    elif match := ARTIFACT.fullmatch(name):
        if match[4] == 'receipt.json':
            _receipt(value, match)
        else:
            fields(value, {'schema', 'language', 'duration', 'segments', 'text', 'provenance'})
            if value['schema'] != 'resonance-transcript/1':
                raise ValueError('Invalid transcript export schema.')
            _result({key: value[key] for key in ('language', 'duration', 'segments', 'text')})
            _provenance(value['provenance'])
    elif match := EDIT.fullmatch(name):
        fields(value, {'id', 'source_id', 'text', 'edited', 'provenance', 'created', 'path'})
        if (value['id'] != match[3] or value['source_id'] != match[2] or value['edited'] is not True
                or value['path'] != f'edits/{match[2]}/{match[3]}.json'):
            raise ValueError('Invalid edited transcript identity.')
        text(value['text'], MAX_METADATA_BYTES, empty=True)
        _provenance(value['provenance'], edited=True)
        number(value['created'])
    else:
        relative = WORKER.fullmatch(name)[3]
        if relative == 'job_config.json':
            fields(value, {'folders', 'job_name', 'models_dir', 'results_root', 'settings'})
            if type(value['folders']) is not list or not 1 <= len(value['folders']) <= MAX_SOURCES:
                raise ValueError('Invalid worker input-folder declarations.')
            for folder in value['folders']:
                path(folder)
            path(value['models_dir'])
            path(value['results_root'])
            text(value['job_name'], 512)
            _settings(value['settings'], worker=True)
        elif relative == 'settings_live.json':
            _settings(value, worker=True, partial=True)
        elif relative == 'job_status.json':
            fields(value, {'state', 'done', 'failed', 'total'}, {'active_count', 'active_files', 'current',
                   'job_name', 'results_dir', 'updated'})
            text(value['state'], 128)
            for key in ('done', 'failed', 'total', 'active_count'):
                if key in value:
                    number(value[key], 0, MAX_SOURCES, integer=True)
            for key in ('current', 'job_name', 'results_dir'):
                if key in value:
                    text(value[key], 32768, empty=True)
            if 'updated' in value:
                if type(value['updated']) is str:
                    text(value['updated'], 128)
                else:
                    number(value['updated'])
            if 'active_files' in value:
                active = value['active_files']
                if type(active) not in (list, dict) or len(active) > MAX_SOURCES:
                    raise ValueError('Invalid worker active-file inventory.')
                if isinstance(active, dict):
                    for key in active:
                        if not re.fullmatch(r'[0-9]{1,3}', key) or not 1 <= int(key) <= MAX_SOURCES:
                            raise ValueError('Invalid worker active-file index.')
                for item in (active.values() if isinstance(active, dict) else active):
                    if type(item) is str:
                        text(item, 32768)
                    else:
                        fields(item, {'index', 'total', 'group', 'relative', 'source', 'phase', 'percent'})
                        for key in ('index', 'total'):
                            number(item[key], 1, MAX_SOURCES, integer=True)
                        number(item['percent'], 0, 100)
                        for key in ('group', 'phase'):
                            text(item[key], 1024)
                        _relative(item['relative'])
                        path(item['source'])
        elif (cache := CACHE.fullmatch(relative)) and cache[2] in ('cache.json', 'receipt.json'):
            if cache[2] == 'cache.json':
                fields(value, {'identity', 'text', 'meta'})
                _identity(value['text'], maximum=MAX_METADATA_BYTES)
                _identity(value['meta'], maximum=MAX_METADATA_BYTES)
            else:
                fields(value, {'schema', 'identity', 'state', 'outputs', 'required_outputs', 'errors'})
                if value['schema'] != 'whisper-v43-local-integrity-1':
                    raise ValueError('Invalid inert worker receipt schema.')
                text(value['state'], 128)
                _inventory(value['outputs'], external=True, maximum=MAX_SOURCES * 4)
                for key in ('required_outputs', 'errors'):
                    if type(value[key]) is not list or len(value[key]) > MAX_SOURCES * 4:
                        raise ValueError('Excessive inert worker receipt records.')
                    for item in value[key]:
                        text(item, 32768)
            _worker_identity(value['identity'])
            encoded = json.dumps(value['identity'], ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':'), allow_nan=False).encode('utf-8')
            if sha256(encoded).hexdigest() != cache[1]:
                raise ValueError('Worker checkpoint filename disagrees with its identity.')
        else:
            _worker_meta(value)
    return value


def validate_collection(entries, records):
    """Bind old sources/results without retaining every decoded transcript in RAM."""
    inventory = {entry['filename']: entry for entry in entries if entry['filename'].startswith(PREFIX)}
    profiles, jobs, by_job, history = {}, set(), {}, {}
    for name in records:
        if match := PROFILE.fullmatch(name):
            profiles[match[1]] = records[name]['fingerprint']
        elif (match := JOB.fullmatch(name)) and match[2] == 'job':
            jobs.add(match[1])
        if name.startswith(PREFIX + 'jobs/'):
            by_job.setdefault(name.split('/')[3], []).append(name)
    if len(profiles) > MAX_PROFILES or len(jobs) > MAX_JOBS:
        raise RestoreError('Transcription backup exceeds its profile/job budget.')
    def exact(name, identity):
        entry = inventory.get(name)
        if entry is None or entry['sha256'] != identity['sha256'] or ('size' in identity and entry['size_bytes'] != identity['size']):
            raise RestoreError('Missing or changed transcription source/result.')
    for name in inventory:
        if name.startswith(PREFIX + 'jobs/') and name.split('/')[3] not in jobs:
            raise RestoreError('Transcription data has no owning job.')
        if (match := WORKER.fullmatch(name)) and match[2].startswith('worker-history/'):
            history.setdefault(match[1], set()).add(match[2])
    if any(len(values) > MAX_HISTORY for values in history.values()):
        raise RestoreError('Transcription backup exceeds its worker-history budget.')
    for identity in sorted(jobs):
        base = PREFIX + 'jobs/' + identity + '/'
        job = records[base + 'job.json']
        if profiles.get(job['profile_id']) != job['profile_fingerprint']:
            raise RestoreError('Transcription job has no matching runtime declaration.')
        sources = {source['id']: source for source in job['sources']}
        for source in sources.values():
            exact(base + source['retained_path'], source)
        accepted = {}
        for result in job['results']:
            exact(base + result['receipt'], result)
            receipt = records[base + result['receipt']]
            accepted[result['source_id']] = sha256(receipt['result']['text'].encode('utf-8')).hexdigest()
            if (set(result['formats']) != set(receipt['files']) or result['language'] != receipt['result']['language']
                    or result['duration'] != receipt['result']['duration']
                    or result['segments'] != len(receipt['result']['segments'])):
                raise RestoreError('Accepted transcript summary changed.')
        for name in by_job.get(identity, ()):
            if (ARTIFACT.fullmatch(name) and name.endswith('/receipt.json')) or EDIT.fullmatch(name):
                value = records[name]
                source, provenance = sources.get(value['source_id']), value['provenance']
                if source is None or (provenance['source_sha256'], provenance['source_bytes']) != (source['sha256'], source['size']):
                    raise RestoreError('Transcript provenance disagrees with its retained source.')
                if name.endswith('/receipt.json'):
                    for record in value['files'].values():
                        exact(base + record['path'], record)
                elif value['source_id'] in accepted and provenance['original_transcript_sha256'] != accepted[value['source_id']]:
                    raise RestoreError('Edited transcript lost its original result identity.')
        for edit in job['edits'].values():
            exact(base + edit['path'], edit)
    for name in records:
        if name.endswith('.cache.json'):
            checkpoint = records[name]
            for kind, suffix in (('text', '.txt'), ('meta', '.json')):
                exact(name[:-len('.cache.json')] + suffix, checkpoint[kind])


def canonical_path(name):
    """Recognize only this historical producer, not sibling scoped ASR jobs."""
    return isinstance(name, str) and name.startswith((PREFIX + 'profiles/', PREFIX + 'jobs/'))


def namespace_family(name):
    """Disjoint direct children shared with the retained workspace ASR family."""
    if name in ('profiles', 'jobs'):
        return 'canonical'
    if name in ('engine.json', '.engine.lock') or re.fullmatch('[a-f0-9]{64}', name):
        return 'scoped'
    return None
