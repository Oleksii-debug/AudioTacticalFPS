"""Opt-in capability gateway. No network, credentials or charges at import time.

Non-secret profiles are separate from the OS vault. Every operation is bound to
the exact profile and payload, with a durable reservation before any request.
An uncertain result is never retried automatically. Transport has no proxies,
redirects or executable provider responses. Money is integer millionths of the
approval's currency; reported estimates are never represented as actual bills.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing, contextmanager
from functools import lru_cache
from hashlib import sha256
import ctypes
from ctypes import wintypes
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import stat
import tempfile
from time import monotonic
from typing import Protocol
from urllib.parse import quote, urlsplit
import uuid


MAX_REQUEST = 512 * 1024
MAX_RESPONSE = 128 * 1024 * 1024
PROFILES_FILENAME = 'api-connections.json'
LEDGER_FILENAME = 'api-operations.sqlite'
MAX_PROFILES = 1000
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_ROWS = 100000
_ID = re.compile(r'[a-zA-Z0-9_-]{1,100}')
CAPABILITIES = {'llm', 'tts', 'music', 'sound_effects'}
_LEDGER_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS approval (id TEXT PRIMARY KEY, digest TEXT NOT NULL);'
    'CREATE TABLE IF NOT EXISTS operation (id TEXT PRIMARY KEY, job TEXT NOT NULL, request_hash TEXT NOT NULL,'
    'estimate_micros INTEGER NOT NULL, state TEXT NOT NULL, output_sha256 TEXT);')


class ProviderError(ValueError):
    """Safe to display: never includes keys, transmitted text or server bodies."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value):
    return sha256(_json(value)).hexdigest()


def _plain(path, *, directory=False):
    path = Path(path)
    if (str(path).replace('\\', '/').startswith('//') or '..' in path.parts
            or (os.name == 'nt' and ':' in str(path)[len(path.drive):])):
        raise ProviderError('Connection settings require an ordinary local path.')
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        is_directory = directory or candidate != path
        if (not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode))
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or (not is_directory and info.st_nlink != 1)):
            raise ProviderError('Expected an unlinked regular local settings file or directory.')
    return path


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink)


def _optional_bytes(path, maximum):
    path = _plain(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not 0 < before.st_size <= maximum:
        raise ProviderError('Connection state exceeds its resource budget or is empty.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (_file_identity(opened) != _file_identity(before)
                or getattr(opened, 'st_file_attributes', 0) & 0x400):
            raise ProviderError('Connection state changed while opening.')
        raw = stream.read(maximum + 1)
    if len(raw) != before.st_size or _file_identity(path.lstat()) != _file_identity(before):
        raise ProviderError('Connection state changed while reading.')
    return raw


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_json(path, value):
    path = _plain(path)
    _plain(path.parent, directory=True)
    raw = _json(value)
    if len(raw) > MAX_REQUEST:
        raise ProviderError('Connection settings exceed their resource budget.')
    decode_profiles(raw)
    fd, name = tempfile.mkstemp(prefix='.connections-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    provider: str
    endpoint: str
    model: str
    capabilities: tuple[str, ...]
    credential_ref: str | None = None
    local: bool = False

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ProviderError('Invalid profile identifier.')
        for text in (self.name, self.model):
            if not isinstance(text, str) or not 1 <= len(text) <= 200 or any(ord(c) < 32 for c in text):
                raise ProviderError('Profile name and model must be plain text.')
        if self.provider not in ('elevenlabs', 'openai-compatible'):
            raise ProviderError('This provider adapter is not installed.')
        if (not isinstance(self.capabilities, tuple) or not self.capabilities
                or set(self.capabilities) - CAPABILITIES or len(set(self.capabilities)) != len(self.capabilities)):
            raise ProviderError('Select supported, distinct capabilities.')
        if self.provider == 'openai-compatible' and self.capabilities != ('llm',):
            raise ProviderError('The compatible adapter currently supports text reasoning only.')
        if self.provider == 'elevenlabs' and 'llm' in self.capabilities:
            raise ProviderError('The speech/music adapter does not support text reasoning.')
        if type(self.local) is not bool:
            raise ProviderError('Invalid local-service setting.')
        if not isinstance(self.endpoint, str) or len(self.endpoint) > 2048:
            raise ProviderError('Invalid endpoint.')
        parts = urlsplit(self.endpoint)
        try:
            port = parts.port
        except ValueError as exc:
            raise ProviderError('Invalid endpoint port.') from exc
        if (not parts.hostname or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or '\\' in self.endpoint
                or any(ord(c) < 33 or ord(c) > 126 for c in self.endpoint)
                or not re.fullmatch(r'(/[a-zA-Z0-9_-]+)*/?', parts.path)):
            raise ProviderError('Use a plain API base URL without credentials, query or fragments.')
        if self.local:
            try:
                loopback = ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                loopback = False
            if not loopback or parts.scheme not in ('http', 'https'):
                raise ProviderError('Local services require an explicit loopback IP address.')
        elif parts.scheme != 'https':
            raise ProviderError('Remote providers require HTTPS.')
        if self.provider == 'elevenlabs' and (
                self.local or parts.hostname != 'api.elevenlabs.io' or port not in (None, 443)
                or parts.path.rstrip('/') != '/v1'):
            raise ProviderError('ElevenLabs credentials are restricted to its official API host and /v1 base.')
        if self.credential_ref is not None and not re.fullmatch(r'url-api-[0-9a-f]{32}', self.credential_ref):
            raise ProviderError('Invalid opaque credential reference.')

    @property
    def fingerprint(self):
        return _digest(asdict(self))

    @property
    def credential_binding(self):
        # A restored profile or endpoint change cannot retrieve a credential for
        # a different destination, model, account or capability set.
        data = asdict(self)
        data.pop('credential_ref')
        data.pop('name')
        return _digest(data)

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict or set(data) != set(cls.__dataclass_fields__):
            raise ProviderError('Unsupported profile fields; secrets must never be stored in profile JSON.')
        if type(data['capabilities']) is not list:
            raise ProviderError('Invalid capability list.')
        return cls(**{**data, 'capabilities': tuple(data['capabilities'])})


class CredentialVault(Protocol):
    def put(self, reference: str, binding: str, secret: str) -> None: ...
    def get(self, reference: str, binding: str) -> str: ...
    def delete(self, reference: str, binding: str) -> None: ...


class WindowsCredentialVault:
    """Windows Generic Credentials; no plaintext/non-Windows fallback."""
    def __init__(self):
        if os.name != 'nt':
            raise ProviderError('API key storage requires Windows Credential Manager on this installation.')
        class CREDENTIALW(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        self.Credential = CREDENTIALW
        self.api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        self.api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None

    @staticmethod
    def _target(reference, binding):
        if not re.fullmatch(r'url-api-[0-9a-f]{32}', reference) or not re.fullmatch(r'[0-9a-f]{64}', binding):
            raise ProviderError('Invalid credential identity.')
        return 'UniversalResearchLibrary/' + reference + '/' + binding

    def put(self, reference, binding, secret):
        if not isinstance(secret, str) or not 1 <= len(secret.encode('utf-8')) <= 2400 or any(ord(c) < 33 for c in secret):
            raise ProviderError('API key is empty, too large or contains whitespace.')
        raw = secret.encode('utf-8')
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        record = self.Credential(Type=1, TargetName=self._target(reference, binding),
            CredentialBlobSize=len(raw), CredentialBlob=buffer, Persist=2,
            UserName='Universal Research Library')
        try:
            if not self.api.CredWriteW(ctypes.byref(record), 0):
                raise ProviderError('Windows could not securely save the API key.')
        finally:
            ctypes.memset(buffer, 0, len(raw))

    def get(self, reference, binding):
        pointer = ctypes.POINTER(self.Credential)()
        if not self.api.CredReadW(self._target(reference, binding), 1, 0, ctypes.byref(pointer)):
            raise ProviderError('Reconnect this profile: its API key is unavailable.')
        try:
            return ctypes.string_at(pointer.contents.CredentialBlob,
                                    pointer.contents.CredentialBlobSize).decode('utf-8')
        finally:
            self.api.CredFree(pointer)

    def delete(self, reference, binding):
        if not self.api.CredDeleteW(self._target(reference, binding), 1, 0) and ctypes.get_last_error() != 1168:
            raise ProviderError('Windows could not remove the API key.')


class ProfileStore:
    def __init__(self, path: Path, vault: CredentialVault | None = None):
        self.path, self.vault = _plain(path), vault

    def profiles(self):
        raw = _optional_bytes(self.path, MAX_REQUEST)
        return () if raw is None else decode_profiles(raw)

    def save(self, profile: ProviderProfile, *, secret: str | None = None):
        from .ingest.writer_guard import exclusive_writer
        with exclusive_writer(self.path.parent, timeout=10):
            return self._save_locked(profile, secret=secret)

    def _save_locked(self, profile, *, secret=None):
        profiles = {p.id: p for p in self.profiles()}
        previous = profiles.get(profile.id)
        if secret is not None:
            vault = self.vault or WindowsCredentialVault()
            reference = 'url-api-' + uuid.uuid4().hex
            profile = ProviderProfile(**{**asdict(profile), 'credential_ref': reference})
            vault.put(reference, profile.credential_binding, secret)
        elif profile.credential_ref and (previous is None or previous.credential_ref != profile.credential_ref
                or previous.credential_binding != profile.credential_binding):
            raise ProviderError('Reconnect the key when changing the provider, destination, model or account.')
        profiles[profile.id] = profile
        try:
            _atomic_json(self.path, {'version': 1, 'profiles': [asdict(p) for p in profiles.values()]})
        except BaseException:
            if secret is not None:
                vault.delete(reference, profile.credential_binding)
            raise
        if previous and previous.credential_ref and previous.credential_ref != profile.credential_ref:
            (self.vault or WindowsCredentialVault()).delete(previous.credential_ref, previous.credential_binding)
        return profile

    def remove(self, identity):
        from .ingest.writer_guard import exclusive_writer
        with exclusive_writer(self.path.parent, timeout=10):
            return self._remove_locked(identity)

    def _remove_locked(self, identity):
        profiles = list(self.profiles())
        found = next((p for p in profiles if p.id == identity), None)
        if found is None:
            raise ProviderError('Profile no longer exists.')
        if found.credential_ref:
            (self.vault or WindowsCredentialVault()).delete(found.credential_ref, found.credential_binding)
        _atomic_json(self.path, {'version': 1, 'profiles': [asdict(p) for p in profiles if p.id != identity]})

    def export_profiles(self):
        return {'version': 1, 'profiles': [{**asdict(p), 'credential_ref': None} for p in self.profiles()]}


def _unique_settings(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError('Duplicate connection settings field.')
        result[key] = value
    return result


def decode_profiles(raw, *, disconnected=False):
    """Strict non-secret profile schema; restored profiles cannot name vault keys."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_REQUEST:
        raise ProviderError('Connection settings exceed their resource budget or are empty.')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_settings)
        if (type(data) is not dict or set(data) != {'version', 'profiles'}
                or type(data['version']) is not int or data['version'] != 1
                or type(data['profiles']) is not list or len(data['profiles']) > MAX_PROFILES):
            raise ProviderError('Unsupported connection settings.')
        profiles = tuple(ProviderProfile.from_dict(p) for p in data['profiles'])
        if len({p.id for p in profiles}) != len(profiles):
            raise ProviderError('Duplicate profile identifiers.')
        if disconnected and any(p.credential_ref is not None for p in profiles):
            raise ProviderError('Restored profiles must be disconnected from credentials.')
        return profiles
    except (UnicodeError, ValueError, TypeError, RecursionError):
        # Invalid data can contain secrets; never echo parser or field values.
        raise ProviderError('Invalid or connected provider profiles in backup/settings.') from None


def validate_backup_profiles(raw):
    return decode_profiles(raw, disconnected=True)


@dataclass(frozen=True)
class ProviderRequest:
    capability: str
    text: str
    voice: str | None = None
    duration_seconds: int | None = None

    def __post_init__(self):
        if self.capability not in CAPABILITIES or not isinstance(self.text, str) or not self.text.strip():
            raise ProviderError('Choose a supported task and nonempty text.')
        if len(self.text.encode('utf-8')) > MAX_REQUEST // 2 or '\x00' in self.text:
            raise ProviderError('Task text exceeds its resource budget or contains NUL.')
        if self.capability == 'tts':
            if not isinstance(self.voice, str) or not _ID.fullmatch(self.voice):
                raise ProviderError('Speech generation requires a valid provider voice ID.')
        elif self.voice is not None:
            raise ProviderError('A voice ID only applies to speech generation.')
        if self.capability in ('music', 'sound_effects'):
            maximum = 600 if self.capability == 'music' else 30
            minimum = 3 if self.capability == 'music' else 1
            if type(self.duration_seconds) is not int or not minimum <= self.duration_seconds <= maximum:
                raise ProviderError(f'This provider request requires {minimum} to {maximum} seconds; use chunks for longer jobs.')
            if self.capability == 'music' and len(self.text) > 4100:
                raise ProviderError('This provider accepts at most 4100 prompt characters per music request.')
        elif self.duration_seconds is not None:
            raise ProviderError('Duration does not apply to this task.')

    def fingerprint(self, profile):
        return _digest({'profile': profile.fingerprint, 'request': asdict(self)})


@dataclass(frozen=True)
class JobApproval:
    job_id: str
    profile_fingerprint: str
    request_hashes: tuple[str, ...]
    budget_micros: int = 0
    max_calls: int = 1
    currency: str = 'USD'
    mode: str = 'LOCAL_ONLY'
    allow_loopback: bool = False

    def __post_init__(self):
        if not isinstance(self.job_id, str) or not _ID.fullmatch(self.job_id):
            raise ProviderError('Invalid job identity.')
        if not re.fullmatch('[0-9a-f]{64}', self.profile_fingerprint):
            raise ProviderError('Approval must bind an exact provider profile.')
        if not isinstance(self.request_hashes, tuple) or not self.request_hashes or any(
                not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in self.request_hashes):
            raise ProviderError('Approval must bind the exact permitted task data.')
        if type(self.budget_micros) is not int or not 0 <= self.budget_micros <= 10**12:
            raise ProviderError('Invalid approved budget.')
        if type(self.max_calls) is not int or not 1 <= self.max_calls <= 100000:
            raise ProviderError('Invalid approved call count.')
        if self.mode not in ('LOCAL_ONLY', 'APPROVED_REMOTE', 'HYBRID') or not re.fullmatch('[A-Z]{3}', self.currency):
            raise ProviderError('Invalid network mode or budget currency.')
        if type(self.allow_loopback) is not bool:
            raise ProviderError('Loopback service authorization must be explicit.')


class BudgetLedger:
    """Reservation survives crash/timeout; only explicit reconciliation releases it."""
    def __init__(self, path):
        self.path = _plain(path)
        _plain(self.path.parent, directory=True)
        with self._db() as db:
            db.executescript(_LEDGER_SCHEMA)

    @contextmanager
    def _db(self):
        _ledger_files(self.path)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, identity, approval, request_hash, estimate):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ProviderError('Invalid operation identity.')
        if type(estimate) is not int or estimate < 0:
            raise ProviderError('Approve an explicit estimate before calling this provider.')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            fingerprint = _digest(asdict(approval))
            prior = db.execute('SELECT digest FROM approval WHERE id=?', (approval.job_id,)).fetchone()
            if prior and prior[0] != fingerprint:
                raise ProviderError('Job approval changed. Review a new job before continuing.')
            db.execute('INSERT OR IGNORE INTO approval VALUES(?,?)', (approval.job_id, fingerprint))
            if db.execute('SELECT 1 FROM operation WHERE id=?', (identity,)).fetchone():
                raise ProviderError('Operation already recorded; reconcile its result before any retry.')
            count, total = db.execute("SELECT count(*),coalesce(sum(estimate_micros),0) FROM operation WHERE job=? AND state!='reconciled_no_charge'",
                                      (approval.job_id,)).fetchone()
            if count >= approval.max_calls or total + estimate > approval.budget_micros:
                raise ProviderError('The approved call count or budget would be exceeded.')
            db.execute('INSERT INTO operation VALUES(?,?,?,?,?,NULL)',
                       (identity, approval.job_id, request_hash, estimate, 'reserved'))

    def finish(self, identity, *, output_sha256=None):
        with self._db() as db:
            db.execute('UPDATE operation SET state=?, output_sha256=? WHERE id=?',
                       ('complete' if output_sha256 else 'uncertain', output_sha256, identity))

    def report(self, job_id):
        with self._db() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM operation WHERE job=? ORDER BY rowid', (job_id,))]

    def reconcile_no_charge(self, identity, *, confirmed=False):
        if confirmed is not True:
            raise ProviderError('Explicit reconciliation of provider billing is required.')
        with self._db() as db:
            cursor = db.execute("UPDATE operation SET state='reconciled_no_charge' WHERE id=? AND state IN ('reserved','uncertain')", (identity,))
            if cursor.rowcount != 1:
                raise ProviderError('Only an uncertain operation can be reconciled without charge.')


def _ledger_files(path, *, bounded=False):
    """Check SQLite's adjacent files before opening it; never follow a journal link."""
    path = _plain(path)
    present = path.exists()
    for candidate in (path, *(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            # SQLite normally creates/removes journals during live commits.
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ProviderError('Expected an unlinked regular provider ledger or journal.')
        if not present:
            raise ProviderError('An orphaned provider ledger journal requires recovery first.')
        if bounded and info.st_size > MAX_LEDGER_BYTES * (1 if candidate == path else 2):
            raise ProviderError('Provider ledger exceeds its backup resource budget.')
    return present


def _ledger_schema(db):
    # Exactly two tables and their primary-key indexes are supported. A fifth
    # object is enough to reject an arbitrary schema without materializing it.
    return tuple(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name LIMIT 5'))


@lru_cache(maxsize=1)
def _expected_ledger_schema():
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(_LEDGER_SCHEMA)
        return _ledger_schema(db)


def _is_digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _validate_ledger_database(db):
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
    db.execute('PRAGMA trusted_schema=OFF')
    if (_ledger_schema(db) != _expected_ledger_schema()
            or db.execute('PRAGMA user_version').fetchone()[0] != 0
            or db.execute('PRAGMA application_id').fetchone()[0] != 0
            or db.execute('PRAGMA encoding').fetchone()[0] != 'UTF-8'):
        raise ProviderError('Unsupported provider ledger schema.')
    if tuple(db.execute('PRAGMA integrity_check')) != (('ok',),):
        raise ProviderError('Provider ledger integrity check failed.')
    for table in ('approval', 'operation'):
        if db.execute('SELECT count(*) FROM ' + table).fetchone()[0] > MAX_LEDGER_ROWS:
            raise ProviderError('Provider ledger exceeds its record budget.')
    jobs = set()
    for identity, digest in db.execute('SELECT id,digest FROM approval'):
        if (not isinstance(identity, str) or not _ID.fullmatch(identity)
                or not _is_digest(digest) or identity in jobs):
            raise ProviderError('Invalid provider approval record.')
        jobs.add(identity)
    totals = {}
    operations = set()
    for identity, job, request_hash, estimate, state, output in db.execute(
            'SELECT id,job,request_hash,estimate_micros,state,output_sha256 FROM operation'):
        if (not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in operations
                or not isinstance(job, str) or job not in jobs or not _is_digest(request_hash)
                or type(estimate) is not int or not 0 <= estimate <= 10**12
                or state not in ('reserved', 'uncertain', 'complete', 'reconciled_no_charge')
                or (not _is_digest(output) if state == 'complete' else output is not None)):
            raise ProviderError('Invalid provider operation record.')
        operations.add(identity)
        if state != 'reconciled_no_charge':
            count, cost = totals.get(job, (0, 0))
            count, cost = count + 1, cost + estimate
            if count > 100000 or cost > 10**12:
                raise ProviderError('Provider ledger exceeds an absolute approval budget.')
            totals[job] = count, cost


def validate_ledger_snapshot(raw):
    """Validate a closed portable ledger, without executing any stored SQL.

    Historical approval digests remain opaque. Their original smaller limits
    cannot be reconstructed; only schema, states and absolute bounds are checked.
    """
    if (type(raw) is not bytes or not 512 <= len(raw) <= MAX_LEDGER_BYTES
            or raw[:16] != b'SQLite format 3\x00' or raw[18:20] != b'\x01\x01'):
        raise ProviderError('Invalid or oversized provider ledger snapshot.')
    try:
        with closing(sqlite3.connect(':memory:')) as db:
            db.deserialize(raw)
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
            db.execute('PRAGMA query_only=ON')
            _validate_ledger_database(db)
            size = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
            if size != len(raw) or db.execute('PRAGMA freelist_count').fetchone()[0] != 0:
                raise ProviderError('Provider ledger snapshot contains unaccounted pages or bytes.')
    except sqlite3.Error:
        raise ProviderError('Invalid provider ledger snapshot.') from None


def _snapshot_ledger(path):
    if not _ledger_files(path, bounded=True):
        return None
    before = path.lstat()
    started = monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='carr-provider-snapshot-') as temporary:
            snapshot = Path(temporary) / LEDGER_FILENAME
            with closing(sqlite3.connect(snapshot)) as target:
                # The other library/profile state is frozen by the caller's
                # writer guard. This read transaction supplies a single matching
                # point in time for both approvals and operations, including WAL
                # pages. A finishing paid request need not wait for the entire
                # originals backup to release that guard.
                with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=10)) as source:
                    source.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
                    source.execute('PRAGMA trusted_schema=OFF')
                    source.execute('PRAGMA query_only=ON')
                    source.execute('BEGIN')
                    if _ledger_schema(source) != _expected_ledger_schema():
                        raise ProviderError('Unsupported provider ledger schema.')
                    page_size = source.execute('PRAGMA page_size').fetchone()[0]

                    def progress(status, remaining, total):
                        if total * page_size > MAX_LEDGER_BYTES or monotonic() - started > 15:
                            raise ProviderError('Provider ledger snapshot exceeded its resource budget.')

                    source.backup(target, pages=256, progress=progress)
                _validate_ledger_database(target)
                target.execute('PRAGMA journal_mode=DELETE')
                target.execute('PRAGMA synchronous=FULL')
                # Remove deleted content and unused pages from the portable
                # database; only the checked current ledger is exported.
                target.execute('VACUUM')
            raw = _optional_bytes(snapshot, MAX_LEDGER_BYTES)
            validate_ledger_snapshot(raw)
        _plain(path)
        after = path.lstat()
        # Ordinary concurrent SQLite commits can change size/times; replacing
        # the database inode is never a legitimate ledger transaction.
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ProviderError('Provider ledger was replaced during its snapshot.')
        return raw
    except sqlite3.Error:
        raise ProviderError('Provider ledger could not be snapshotted safely.') from None


def snapshot_provider_files(root):
    """Return sanitized profile/ledger bytes; caller holds exclusive_writer(root).

    Never opens the credential vault, follows audio output paths, or restores
    network permission. Profiles are metadata only; the default job mode remains
    LOCAL_ONLY and every remote task still needs a fresh explicit approval.
    """
    root = _plain(root, directory=True)
    files = {}
    raw = _optional_bytes(root / PROFILES_FILENAME, MAX_REQUEST)
    if raw is not None:
        profiles = decode_profiles(raw)
        exported = _json({'version': 1, 'profiles': [
            {**asdict(profile), 'credential_ref': None} for profile in profiles]})
        validate_backup_profiles(exported)
        files[PROFILES_FILENAME] = exported
    ledger = _snapshot_ledger(root / LEDGER_FILENAME)
    if ledger is not None:
        files[LEDGER_FILENAME] = ledger
    return files


class DirectTransport:
    """DNS-checked and address-pinned HTTP. No proxy, redirect or automatic retry."""
    def post(self, profile, route, payload, headers, *, cancelled=lambda: False):
        parts = urlsplit(profile.endpoint)
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', route):
            raise ProviderError('Invalid adapter route.')
        host = parts.hostname
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
            if not addresses or any(not (ipaddress.ip_address(ip).is_loopback if profile.local
                                        else ipaddress.ip_address(ip).is_global) for ip in addresses):
                raise ProviderError('Endpoint resolved outside its approved network scope.')
            if cancelled():
                raise ProviderError('Provider operation cancelled before transmission.')
            address = addresses[0]
            connection = http.client.HTTPConnection(address, port, timeout=30)
            connection.connect()
            if parts.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=host)
            host_header = f'[{host}]' if ':' in host else host
            if port != (443 if parts.scheme == 'https' else 80):
                host_header += ':' + str(port)
            connection.request('POST', parts.path.rstrip('/') + route, body=payload,
                headers={**headers, 'Host': host_header, 'Content-Type': 'application/json',
                         'Accept': 'application/json' if route.endswith('/completions') else 'audio/mpeg',
                         'Connection': 'close'})
            response = connection.getresponse()
            if response.status != 200:
                raise ProviderError(f'Provider returned HTTP {response.status}; no automatic retry was made.')
            declared = response.getheader('Content-Length')
            if declared and (not declared.isdecimal() or int(declared) > MAX_RESPONSE):
                raise ProviderError('Provider response exceeds its resource budget.')
            data = bytearray()
            while block := response.read(65536):
                if cancelled():
                    raise ProviderError('Provider operation cancelled; reconcile any possible charge before retrying.')
                data.extend(block)
                if len(data) > MAX_RESPONSE:
                    raise ProviderError('Provider response exceeds its resource budget.')
            if not data:
                raise ProviderError('Provider returned an empty result.')
            if declared and len(data) != int(declared):
                raise ProviderError('Provider response was truncated.')
            return bytes(data)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError('Provider request failed; its charge status may be uncertain.') from None
        finally:
            if 'connection' in locals():
                connection.close()


class ProviderGateway:
    def __init__(self, ledger, *, vault=None, transport=None):
        self.ledger, self.vault = ledger, vault
        self.transport = transport or DirectTransport()

    def perform(self, profile, request, *, approval, operation_id, estimated_micros,
                cancelled=lambda: False):
        if not isinstance(profile, ProviderProfile) or not isinstance(request, ProviderRequest) or not isinstance(approval, JobApproval):
            raise ProviderError('A typed profile, request and explicit job approval are required.')
        if approval.profile_fingerprint != profile.fingerprint or request.fingerprint(profile) not in approval.request_hashes:
            raise ProviderError('This provider, model or task data is outside the approved job.')
        if request.capability not in profile.capabilities:
            raise ProviderError('This profile does not provide the requested capability.')
        if profile.local:
            if not approval.allow_loopback:
                raise ProviderError('Authorize this exact local service before sending it task data.')
        elif approval.mode == 'LOCAL_ONLY':
            raise ProviderError('LOCAL ONLY blocks every remote provider request.')
        if cancelled():
            raise ProviderError('Provider operation cancelled before transmission.')
        if profile.provider == 'openai-compatible':
            route = '/chat/completions'
            body = {'model': profile.model, 'messages': [{'role': 'user', 'content': request.text}], 'stream': False}
        elif request.capability == 'tts':
            route = '/text-to-speech/' + quote(request.voice, safe='')
            body = {'text': request.text, 'model_id': profile.model}
        elif request.capability == 'music':
            route = '/music'
            body = {'prompt': request.text, 'model_id': profile.model,
                    'music_length_ms': request.duration_seconds * 1000, 'force_instrumental': True}
        else:
            route = '/sound-generation'
            body = {'text': request.text, 'model_id': profile.model, 'duration_seconds': request.duration_seconds}
        raw = _json(body)
        if len(raw) > MAX_REQUEST:
            raise ProviderError('Request exceeds its resource budget.')
        headers = {}
        if profile.credential_ref:
            try:
                secret = (self.vault or WindowsCredentialVault()).get(profile.credential_ref, profile.credential_binding)
            except Exception:
                raise ProviderError('Reconnect this profile: its securely stored credential could not be read.') from None
            if not isinstance(secret, str) or not secret or any(ord(c) < 33 for c in secret):
                raise ProviderError('Reconnect this profile: the stored credential is invalid.')
            headers['xi-api-key' if profile.provider == 'elevenlabs' else 'Authorization'] = (
                secret if profile.provider == 'elevenlabs' else 'Bearer ' + secret)
        elif not profile.local:
            raise ProviderError('Reconnect this profile before using an external provider.')
        self.ledger.reserve(operation_id, approval, request.fingerprint(profile), estimated_micros)
        try:
            output = self.transport.post(profile, route, raw, headers, cancelled=cancelled)
            if not isinstance(output, bytes) or not 0 < len(output) <= MAX_RESPONSE:
                raise ProviderError('Invalid or oversized provider result.')
            if request.capability == 'llm':
                try:
                    decoded = json.loads(output)
                    message = decoded['choices'][0]['message']
                    if message.get('tool_calls') or not isinstance(message['content'], str):
                        raise ValueError
                except (ValueError, TypeError, KeyError, IndexError):
                    raise ProviderError('Unsupported model response; no returned commands were executed.') from None
            self.ledger.finish(operation_id, output_sha256=sha256(output).hexdigest())
            return output
        except BaseException as exc:
            self.ledger.finish(operation_id)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # Transport implementations may include raw request/key material in
            # exceptions. No third-party exception is displayed or chained.
            raise ProviderError('Provider operation did not complete. Review its uncertain charge status before retrying.') from None
        finally:
            headers.clear()
