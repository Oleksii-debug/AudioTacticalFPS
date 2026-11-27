"""Typed translation checkpoints on the shared native publication protocol.

This namespace is separate from assistant tool plans. Archived approvals retain
their original library/profile/ledger bindings; loading a backup never rebinds
remote permission. Pure validators are shared with complete backup/restore.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime
from hashlib import sha256
import os
from pathlib import Path, PureWindowsPath
import re

from .models import canonical, local_path, text
from .state import (atomic_json, now, read_bytes, read_json, regular,
                    snapshot_job_guard, state_write_guard, _job_guard, _identity)
from .state import decode_state_json
from ..ingest.writer_guard import exclusive_writer
from ..providers import (GoogleTranslationRequest, JobApproval, ProviderProfile,
                         GOOGLE_TRANSLATION_ENDPOINT, decode_translation_response)

DIRECTORY = 'state/translation'
MAX_JOBS = 1000
MAX_CACHES = 100
MAX_JOB_BYTES = 64 * 1024 * 1024
MAX_CACHE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SOURCE_CHARACTERS = 2_000_000
MAX_OUTPUT_CHARACTERS = 8_000_000
SCHEMA = 'carr-translation-job/1'
CACHE_SCHEMA = 'carr-translation-languages/1'
_JOB = re.compile(r'([0-9a-f]{32})\.json')
_CACHE = re.compile(r'languages-([a-zA-Z0-9_-]{1,100})\.json')
_TRANSIENT = re.compile(
    r'(?:\.state\.lock|[0-9a-f]{32}\.lock|'
    r'\.(?:[0-9a-f]{32}|languages-[a-zA-Z0-9_-]{1,100})\.json\.[0-9a-f]{32}\.tmp)')
_HEX = re.compile(r'[0-9a-f]{64}')
_LANGUAGE = re.compile(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}')


def text_hash(value):
    return sha256(value.encode('utf-8', errors='strict')).hexdigest()


def split_text(value, limit=4500):
    """Bounded Unicode chunks retain every original space and line ending."""
    text(value, 'selected translation text', maximum=MAX_SOURCE_CHARACTERS)
    if type(limit) is not int or not 100 <= limit <= 5000:
        raise ValueError('Invalid translation chunk size.')
    parts, start = [], 0
    while start < len(value):
        end = min(start + limit, len(value))
        if end < len(value):
            boundary = max(value.rfind('\n', start + limit // 2, end),
                           value.rfind(' ', start + limit // 2, end))
            if boundary >= 0:
                end = boundary + 1
        parts.append(value[start:end])
        start = end
    return tuple(parts)


def allocations(parts, budget_micros):
    calls = sum(bool(part.strip()) for part in parts)
    if type(budget_micros) is not int or not 0 <= budget_micros <= 10**12 or not calls:
        raise ValueError('An explicit bounded translation budget is required.')
    each, extra = divmod(budget_micros, calls)
    result, index = [], 0
    for part in parts:
        if part.strip():
            result.append(each + (1 if index < extra else 0))
            index += 1
        else:
            result.append(None)
    return result


def _hash(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError('Invalid translation content hash.')


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Invalid translation checkpoint timestamp.')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValueError('Invalid translation checkpoint timestamp.') from None


def approval_from_dict(value):
    if (type(value) is not dict or set(value) != set(JobApproval.__dataclass_fields__)
            or type(value.get('request_hashes')) is not list):
        raise ValueError('Invalid translation approval.')
    try:
        return JobApproval(**{**value, 'request_hashes': tuple(value['request_hashes'])})
    except (TypeError, ValueError):
        raise ValueError('Invalid translation approval.') from None


def validate_translation_job(value, *, identity=None):
    keys = {'schema', 'id', 'library_root', 'ledger_identity', 'created', 'updated', 'status',
            'profile', 'title', 'source_text', 'source_sha256', 'source_language',
            'target_language', 'language_catalog_sha256', 'approval', 'allocations',
            'receipts', 'pending'}
    if (type(value) is not dict or set(value) != keys or value['schema'] != SCHEMA
            or not isinstance(value['id'], str) or not re.fullmatch('[0-9a-f]{32}', value['id'])
            or (identity is not None and value['id'] != identity)):
        raise ValueError('Invalid typed translation checkpoint.')
    root = text(value['library_root'], 'translation library binding', maximum=8192)
    if not (Path(root).is_absolute() or PureWindowsPath(root).is_absolute()) or '://' in root or root.startswith(('\\\\', '//')):
        raise ValueError('Invalid translation library binding.')
    binding = value['ledger_identity']
    if (type(binding) is not list or len(binding) != 2
            or any(type(part) is not int or not 0 <= part < 2**128 for part in binding)):
        raise ValueError('Invalid translation ledger binding.')
    _stamp(value['created'])
    _stamp(value['updated'])
    if value['status'] not in ('queued', 'running', 'paused', 'attention', 'complete'):
        raise ValueError('Invalid translation checkpoint status.')
    text(value['title'], 'translation title', maximum=512)
    parts = split_text(value['source_text'])
    if value['source_sha256'] != text_hash(value['source_text']):
        raise ValueError('The immutable translation source failed its hash check.')
    _hash(value['language_catalog_sha256'])
    try:
        profile = ProviderProfile.from_dict(value['profile'])
    except (TypeError, ValueError):
        raise ValueError('Invalid translation profile binding.') from None
    if profile.provider != 'google-translation-v2' or not profile.credential_ref:
        raise ValueError('Translation must bind a connected Google Basic v2 profile.')
    requests = [GoogleTranslationRequest(part, value['source_language'], value['target_language'])
                for part in parts if part.strip()]
    approval = approval_from_dict(value['approval'])
    if (approval.job_id != value['id'] or approval.profile_fingerprint != profile.fingerprint
            or approval.request_hashes != tuple(request.fingerprint(profile) for request in requests)
            or approval.max_calls != len(requests) or approval.currency != 'USD'
            or approval.mode != 'APPROVED_REMOTE' or approval.allow_loopback):
        raise ValueError('Translation approval does not match the exact profile, source, languages and call count.')
    if (type(value['allocations']) is not list
            or any(item is not None and type(item) is not int for item in value['allocations'])
            or value['allocations'] != allocations(parts, approval.budget_micros)):
        raise ValueError('Translation reservations do not match the approved integer budget.')
    receipts = value['receipts']
    if type(receipts) is not list or len(receipts) > len(parts):
        raise ValueError('Invalid translation receipt inventory.')
    output_size = 0
    receipt_keys = {'index', 'source_sha256', 'text', 'translation_sha256',
                    'detected_source', 'operation_id', 'request_hash', 'response_sha256'}
    for index, row in enumerate(receipts):
        if type(row) is not dict or set(row) != receipt_keys or type(row['index']) is not int or row['index'] != index:
            raise ValueError('Invalid translation receipt order.')
        translated = text(row['text'], 'accepted translation text', maximum=200000, empty=True)
        if (row['source_sha256'] != text_hash(parts[index])
                or row['translation_sha256'] != text_hash(translated)):
            raise ValueError('An accepted translation chunk failed its content check.')
        output_size += len(translated)
        if output_size > MAX_OUTPUT_CHARACTERS:
            raise ValueError('Translation output exceeds its storage budget.')
        if parts[index].strip():
            expected = GoogleTranslationRequest(parts[index], value['source_language'], value['target_language'])
            if (not translated.strip() or row['operation_id'] != value['id'] + '_' + str(index)
                    or row['request_hash'] != expected.fingerprint(profile)):
                raise ValueError('A translation receipt does not match its approved request.')
            _hash(row['response_sha256'])
            if row['detected_source'] is not None and (
                    not isinstance(row['detected_source'], str) or not _LANGUAGE.fullmatch(row['detected_source'])):
                raise ValueError('Invalid detected source language.')
        elif (translated != parts[index] or any(row[key] is not None for key in (
                'detected_source', 'operation_id', 'request_hash', 'response_sha256'))):
            raise ValueError('Whitespace must be preserved without a provider request.')
    pending = value['pending']
    if pending is not None:
        if (type(pending) is not dict or set(pending) != {'index', 'operation_id'}
                or type(pending['index']) is not int or pending['index'] != len(receipts)
                or len(receipts) >= len(parts) or not parts[len(receipts)].strip()
                or pending['operation_id'] != value['id'] + '_' + str(len(receipts))):
            raise ValueError('Invalid translation request intent.')
    if value['status'] == 'complete' and (len(receipts) != len(parts) or pending is not None):
        raise ValueError('A complete translation needs every accepted chunk.')
    if value['status'] == 'queued' and (receipts or pending is not None):
        raise ValueError('A queued translation cannot contain previous requests.')
    return value


def validate_language_cache(value, *, profile_id=None):
    keys = {'schema', 'profile_id', 'profile_fingerprint', 'endpoint', 'model', 'fetched_at',
            'languages', 'languages_sha256', 'operation_id', 'request_hash', 'response_sha256'}
    if (type(value) is not dict or set(value) != keys or value['schema'] != CACHE_SCHEMA
            or not isinstance(value['profile_id'], str)
            or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', value['profile_id'])
            or (profile_id is not None and value['profile_id'] != profile_id)
            or value['endpoint'] != GOOGLE_TRANSLATION_ENDPOINT or value['model'] != 'nmt'
            or not isinstance(value['operation_id'], str)
            or not re.fullmatch('[0-9a-f]{32}', value['operation_id'])):
        raise ValueError('Invalid translation language cache.')
    _stamp(value['fetched_at'])
    for field in ('profile_fingerprint', 'languages_sha256', 'request_hash', 'response_sha256'):
        _hash(value[field])
    raw = canonical({'data': {'languages': value['languages']}}).encode('utf-8')
    parsed = decode_translation_response(GoogleTranslationRequest(operation='languages'), raw)['languages']
    if parsed != value['languages'] or text_hash(canonical(parsed)) != value['languages_sha256']:
        raise ValueError('Translation language cache failed its content check.')
    return value


def is_state_path(relative):
    if not isinstance(relative, str) or not relative.startswith(DIRECTORY + '/'):
        return False
    name = relative[len(DIRECTORY) + 1:]
    return bool(_JOB.fullmatch(name) or _CACHE.fullmatch(name))


def state_limit(relative):
    if not is_state_path(relative):
        raise ValueError('Unsupported translation backup sidecar.')
    return MAX_CACHE_BYTES if _CACHE.fullmatch(relative.rsplit('/', 1)[-1]) else MAX_JOB_BYTES


def validate_state_document(name, raw):
    if not isinstance(raw, bytes):
        raise ValueError('Translation checkpoint bytes are required.')
    job, cache = _JOB.fullmatch(name), _CACHE.fullmatch(name)
    if not (job or cache) or not 0 < len(raw) <= (MAX_JOB_BYTES if job else MAX_CACHE_BYTES):
        raise ValueError('Invalid translation checkpoint filename or size.')
    value = decode_state_json(raw)
    return (validate_translation_job(value, identity=job[1]) if job
            else validate_language_cache(value, profile_id=cache[1]))


def state_paths(root, *, cancelled=lambda: False, live=True):
    """Read-only inventory under the caller's application/state snapshot guards."""
    root = Path(root)
    folder = root
    for part in DIRECTORY.split('/'):
        folder = folder / part
        if not os.path.lexists(folder):
            return ()
        local_path(folder)
        if not folder.is_dir():
            raise ValueError('Translation state needs ordinary local directories.')
    paths, jobs, caches, size = [], 0, 0, 0
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries, 1):
            if cancelled():
                raise InterruptedError('Translation snapshot cancelled.')
            if count > (MAX_JOBS + MAX_CACHES) * 4 + 16:
                raise ValueError('Translation checkpoint directory exceeds its entry budget.')
            path = Path(entry.path)
            info = regular(path)
            if _TRANSIENT.fullmatch(entry.name):
                if live:
                    continue
                raise ValueError('Transient translation locks and temporary files are not backup data.')
            relative = path.relative_to(root).as_posix()
            if not is_state_path(relative) or not 0 < info.st_size <= state_limit(relative):
                raise ValueError('Unexpected or oversized translation checkpoint.')
            jobs += bool(_JOB.fullmatch(entry.name))
            caches += bool(_CACHE.fullmatch(entry.name))
            size += info.st_size
            if jobs > MAX_JOBS or caches > MAX_CACHES or size > MAX_SNAPSHOT_BYTES:
                raise ValueError('Translation checkpoints exceed their snapshot budget.')
            paths.append(path)
    return tuple(sorted(paths))


@contextmanager
def snapshot_guard(root):
    """Caller already holds app writer; never reacquire it from a backup."""
    directory = Path(root) / DIRECTORY
    if not os.path.lexists(directory):
        yield
        return
    local_path(directory)
    with state_write_guard(directory), ExitStack() as stack:
        paths = state_paths(root)
        for path in paths:
            if _JOB.fullmatch(path.name):
                stack.enter_context(snapshot_job_guard(directory, path.stem))
        yield


def _snapshot_inventory(root, *, live):
    paths = state_paths(root, live=live)
    folder = Path(root) / DIRECTORY
    parents = () if not os.path.lexists(folder) else tuple(
        _identity(local_path(path).stat()) for path in (Path(root) / 'state', folder))
    return tuple((path, _identity(regular(path))) for path in paths), parents


def snapshot_files(root, *, live=False):
    """Read typed bytes without writing archive locks or increasing backup limits.

    A live caller holds both the application writer and snapshot_guard until its
    provider-ledger snapshot is also captured. Archived validation takes no lock.
    """
    root = Path(root)
    before = _snapshot_inventory(root, live=live)
    files, total = {}, 0
    for path, _ in before[0]:
        relative = path.relative_to(root).as_posix()
        raw = read_bytes(path, maximum=state_limit(relative))
        total += len(raw)
        if total > MAX_SNAPSHOT_BYTES:
            raise ValueError('Translation checkpoints exceed their snapshot budget.')
        validate_state_document(path.name, raw)
        files[relative] = raw
    if _snapshot_inventory(root, live=live) != before:
        raise ValueError('Translation checkpoints changed during backup; pause the producer and retry.')
    return files


def validate_state_tree(db, root, *, cancelled=lambda: False):
    """Translation sources are explicit selected text, not database quotations."""
    for path in state_paths(root, cancelled=cancelled):
        relative = path.relative_to(root).as_posix()
        validate_state_document(path.name, read_bytes(path, maximum=state_limit(relative)))


class TranslationStore:
    def __init__(self, root):
        self.root = local_path(root)
        if not self.root.is_dir():
            raise ValueError('Choose an existing local library directory.')
        self.directory = local_path(self.root / DIRECTORY, must_exist=False)
        with exclusive_writer(self.root):
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            local_path(self.directory)

    def path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
            raise ValueError('Invalid translation job identity.')
        return self.directory / (identity + '.json')

    def cache_path(self, profile):
        if not isinstance(profile, ProviderProfile) or profile.provider != 'google-translation-v2':
            raise ValueError('Choose a Google Basic v2 profile.')
        return self.directory / ('languages-' + profile.id + '.json')

    def load(self, identity):
        path = self.path(identity)
        return validate_state_document(path.name, read_bytes(path, maximum=MAX_JOB_BYTES))

    def save(self, value, *, new=False):
        value['updated'] = now()
        validate_translation_job(value)
        self._publish(self.path(value['id']), value, new=new)

    def save_cache(self, profile, value):
        validate_language_cache(value, profile_id=profile.id)
        self._publish(self.cache_path(profile), value)

    def _publish(self, path, value, *, new=False):
        payload = (canonical(value) + '\n').encode('utf-8')
        relative = path.relative_to(self.root).as_posix()
        if len(payload) > state_limit(relative):
            raise ValueError('Translation checkpoint exceeds its file budget.')
        with exclusive_writer(self.root), state_write_guard(self.directory):
            paths = state_paths(self.root)
            if new and path.exists():
                raise FileExistsError('This translation job already exists.')
            new_inventory = [item for item in paths if item != path]
            if sum(item.stat().st_size for item in new_inventory) + len(payload) > MAX_SNAPSHOT_BYTES:
                raise ValueError('Translation checkpoints exceed their snapshot budget.')
            jobs = sum(bool(_JOB.fullmatch(item.name)) for item in new_inventory) + bool(_JOB.fullmatch(path.name))
            caches = sum(bool(_CACHE.fullmatch(item.name)) for item in new_inventory) + bool(_CACHE.fullmatch(path.name))
            if jobs > MAX_JOBS or caches > MAX_CACHES:
                raise ValueError('Too many saved translation checkpoints.')
            atomic_json(path, value)

    def lock(self, identity):
        self.path(identity)
        return _job_guard(self.directory, identity)

    def identities(self):
        with state_write_guard(self.directory):
            return tuple(path.stem for path in state_paths(self.root) if _JOB.fullmatch(path.name))
