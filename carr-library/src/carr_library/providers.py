"""Opt-in provider profiles, OS-held credentials and exact-request approval.

The core remains LOCAL_ONLY. Keys are never put in project JSON, SQLite,
exports, exception messages or source control. Provider-compatible endpoints
are configured by the owner; creating a profile performs no network operation.
"""
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4
from urllib.parse import quote, urlsplit, urlunsplit
import urllib.error
import urllib.request

from .studio.store import atomic_text


class ProviderError(ValueError):
    pass


def _secret(value):
    try:
        valid = (type(value) is str and bool(value.strip()) and len(value.encode('utf-8')) <= 2560
                 and not any(c in value for c in '\r\n\0'))
    except UnicodeError:
        valid = False
    if not valid:
        raise ProviderError('Enter one nonempty API key within the OS credential size limit.')
    return value


def _endpoint(value):
    message = 'Use an HTTPS endpoint, or an explicit HTTP loopback endpoint without embedded credentials.'
    if type(value) is not str or not value or re.search(r'[\x00-\x20\x7f]', value):
        raise ProviderError(message)
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if (parts.scheme not in ('http', 'https') or not host
                or parts.username is not None or parts.password is not None
                or '?' in value or '#' in value or parts.netloc.endswith(':')
                or (parts.scheme == 'http' and host not in ('localhost', '127.0.0.1', '::1'))):
            raise ProviderError(message)
        if ':' in host:
            import ipaddress
            ipaddress.IPv6Address(host)
            host = '[' + host + ']'
        else:
            host = host.encode('idna').decode('ascii')
            if (len(host) > 253 or any(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) is None
                                      for label in host.rstrip('.').split('.'))):
                raise ProviderError(message)
        netloc = host + (':' + str(port) if port is not None else '')
        return urlunsplit((parts.scheme, netloc, quote(parts.path, safe="/%:@!$&'()*+,;=-._~"), '', ''))
    except (ValueError, UnicodeError) as exc:
        raise ProviderError(message) from None


class CredentialVault:
    """Windows generic credentials; optional secure keyring on other systems."""
    service = 'Universal Research Library providers'

    @staticmethod
    def _windows():
        import ctypes
        from ctypes import wintypes
        class CREDENTIAL(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        return ctypes, CREDENTIAL, api

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise ProviderError('The OS credential vault is unavailable. Install a supported secure keyring; no plaintext key file was created.') from exc
        backend = keyring.get_keyring()
        name = type(backend).__module__.lower()
        if not any(part in name for part in ('secretservice', 'libsecret', 'kwallet', 'macos', 'windows')):
            raise ProviderError('Select a supported OS credential vault. Plaintext or unknown keyring backends are not accepted.')
        return keyring

    def put(self, identity, secret):
        _secret(secret)
        if os.name != 'nt':
            self._keyring().set_password(self.service, identity, secret)
            return
        ctypes, record, api = self._windows()
        data = secret.encode('utf-8')
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        value = record(Type=1, TargetName=self.service + '/' + identity,
            CredentialBlobSize=len(data), CredentialBlob=buffer, Persist=2,
            UserName=identity, Comment='User-selected optional provider; no automatic remote calls')
        if not api.CredWriteW(ctypes.byref(value), 0):
            raise ProviderError('The Windows credential vault could not save the key.')

    def get(self, identity):
        if os.name != 'nt':
            return self._keyring().get_password(self.service, identity)
        ctypes, record, api = self._windows()
        value = ctypes.POINTER(record)()
        if not api.CredReadW(self.service + '/' + identity, 1, 0, ctypes.byref(value)):
            if ctypes.get_last_error() == 1168:
                return None
            raise ProviderError('The Windows credential vault could not read the selected key.')
        try:
            return ctypes.string_at(value.contents.CredentialBlob, value.contents.CredentialBlobSize).decode('utf-8')
        finally:
            api.CredFree(value)

    def delete(self, identity):
        if os.name != 'nt':
            self._keyring().delete_password(self.service, identity)
            return
        ctypes, _, api = self._windows()
        if not api.CredDeleteW(self.service + '/' + identity, 1, 0) and ctypes.get_last_error() != 1168:
            raise ProviderError('The Windows credential vault could not remove the selected key.')


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    task: str
    endpoint: str
    model: str
    enabled: bool = False
    requires_key: bool = True
    request_budget_cents: int = 0
    total_budget_cents: int = 0
    max_input_bytes: int = 1024 * 1024
    max_output_bytes: int = 32 * 1024 * 1024
    timeout_seconds: int = 60

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}', self.id):
            raise ProviderError('Provider ID must contain letters, digits, underscores or hyphens.')
        for text in (self.name, self.model):
            if type(text) is not str or not text.strip() or '\0' in text:
                raise ProviderError('Provider name and model must contain text.')
        if self.task not in ('llm', 'tts', 'music', 'transcription'):
            raise ProviderError('Choose an LLM, speech, music, or transcription provider task.')
        object.__setattr__(self, 'endpoint', _endpoint(self.endpoint))
        if type(self.enabled) is not bool or type(self.requires_key) is not bool:
            raise ProviderError('Provider flags must be true or false.')
        for name in ('request_budget_cents', 'total_budget_cents'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ProviderError('Provider budget must be nonnegative integer cents.')
        if self.request_budget_cents > self.total_budget_cents:
            raise ProviderError('Per-request budget cannot exceed total authorized budget.')
        if type(self.max_input_bytes) is not int or not 1 <= self.max_input_bytes <= 64 * 1024**2:
            raise ProviderError('Input byte budget is invalid.')
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= 512 * 1024**2:
            raise ProviderError('Output byte budget is invalid.')
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 3600:
            raise ProviderError('Request timeout is invalid.')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ProviderError('Provider redirects are disabled; no credentials were forwarded.')


class ProviderManager:
    def __init__(self, root, *, vault=None):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ProviderError('Provider settings cannot use a symbolic-link folder.')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.credential_namespace = sha256(str(self.root.resolve()).encode('utf-8')).hexdigest()
        self.database = self.root / 'providers.sqlite'
        if self.database.is_symlink():
            raise ProviderError('Provider database cannot be a symbolic link.')
        self.vault = vault or CredentialVault()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS policy(id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT NOT NULL);
                INSERT OR IGNORE INTO policy VALUES(1,'LOCAL_ONLY');
                CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,document TEXT NOT NULL,spent_cents INTEGER NOT NULL DEFAULT 0,credential_ref TEXT);
                CREATE TABLE IF NOT EXISTS requests(digest TEXT PRIMARY KEY,profile TEXT NOT NULL,state TEXT NOT NULL,
                    reserved_cents INTEGER NOT NULL,response_sha256 TEXT,updated REAL NOT NULL);
            ''')
            if 'credential_ref' not in {row[1] for row in db.execute('PRAGMA table_info(profiles)')}:
                db.execute('ALTER TABLE profiles ADD COLUMN credential_ref TEXT')

    @property
    def mode(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            return db.execute('SELECT mode FROM policy WHERE id=1').fetchone()[0]

    def set_mode(self, mode):
        if mode not in ('LOCAL_ONLY', 'HYBRID'):
            raise ProviderError('Choose LOCAL_ONLY or explicitly enabled HYBRID mode.')
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE policy SET mode=? WHERE id=1', (mode,))

    def save(self, profile, *, key=None):
        if not isinstance(profile, ProviderProfile):
            raise ProviderError('Expected a validated provider profile.')
        if key is not None:
            _secret(key)
        raw = json.dumps(asdict(profile), ensure_ascii=False, sort_keys=True)
        new_reference = self.credential_namespace + '/' + profile.id + '/' + uuid4().hex if key is not None else None
        previous_reference = None
        try:
            # New credential IDs keep the previous working key intact if the
            # settings transaction fails after the OS accepted the replacement.
            if new_reference is not None:
                try:
                    self.vault.put(new_reference, key)
                except Exception:
                    raise ProviderError('The OS credential vault could not save the selected key.') from None
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                previous = db.execute('SELECT credential_ref FROM profiles WHERE id=?', (profile.id,)).fetchone()
                previous_reference = self._credential_reference(profile.id, previous[0] if previous else None)
                reference = new_reference or previous_reference
                db.execute('INSERT INTO profiles(id,document,credential_ref) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document,credential_ref=excluded.credential_ref',
                           (profile.id, raw, reference))
        except BaseException:
            if new_reference is not None:
                try:
                    self.vault.delete(new_reference)
                except Exception:
                    pass
            raise
        if new_reference is not None and previous_reference:
            try:
                self.vault.delete(previous_reference)
            except Exception:
                # An unreferenced OS-held old key must never make a committed
                # replacement look like a failed save or expose the key value.
                pass

    def profiles(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            return tuple(ProviderProfile(**json.loads(row[0])) for row in db.execute('SELECT document FROM profiles ORDER BY id'))

    def disable(self, identity):
        """Reversibly disable a profile while retaining its key and audit data."""
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT document,credential_ref FROM profiles WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ProviderError('Unknown provider profile.')
            profile = ProviderProfile(**json.loads(row[0]))
            if profile.id != identity:
                raise ProviderError('The saved provider profile identity is inconsistent.')
            self._credential_reference(identity, row[1])
            disabled = replace(profile, enabled=False)
            db.execute('UPDATE profiles SET document=? WHERE id=?',
                       (json.dumps(asdict(disabled), ensure_ascii=False, sort_keys=True), identity))
        return disabled

    def remove(self, identity):
        """Remove one configuration and its own key; retain request receipts.

        Configuration removal commits before OS cleanup so a failed vault
        operation cannot leave an enabled profile behind. The result explicitly
        reports a key that still needs removal in the operating-system vault.
        """
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT document,credential_ref FROM profiles WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ProviderError('Unknown provider profile.')
            if ProviderProfile(**json.loads(row[0])).id != identity:
                raise ProviderError('The saved provider profile identity is inconsistent.')
            reference = self._credential_reference(identity, row[1])
            db.execute('DELETE FROM profiles WHERE id=?', (identity,))
        credential_removed = True
        if reference is not None:
            try:
                self.vault.delete(reference)
            except Exception:
                credential_removed = False
        return {'profile_removed': True, 'credential_removed': credential_removed,
                'request_receipts_retained': True}

    def _credential_reference(self, identity, reference):
        if reference is None:
            return None
        prefix = self.credential_namespace + '/' + identity + '/'
        if (type(reference) is not str or not reference.startswith(prefix)
                or re.fullmatch(r'[0-9a-f]{32}', reference[len(prefix):]) is None):
            raise ProviderError('The credential reference is outside this provider settings scope. Save a new profile and key.')
        return reference

    def _snapshot(self, identity):
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute('SELECT document,credential_ref FROM profiles WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ProviderError('Unknown provider profile.')
        profile = ProviderProfile(**json.loads(row[0]))
        if profile.id != identity:
            raise ProviderError('The saved provider profile identity is inconsistent.')
        return profile, self._credential_reference(identity, row[1])

    def _profile(self, identity):
        return self._snapshot(identity)[0]

    def _review(self, profile, reference, payload, maximum_cost_cents):
        if type(payload) is not dict:
            raise ProviderError('Provider input must be a JSON object.')
        # Credentials belong in their vault even when a nested options object
        # or array accidentally includes one. The walk also checks nested keys.
        pending, seen = [payload], set()
        while pending:
            item = pending.pop()
            if type(item) not in (dict, list):
                continue
            if id(item) in seen:
                continue
            seen.add(id(item))
            if type(item) is dict:
                if any(type(k) is not str for k in item):
                    raise ProviderError('Provider JSON object keys must be text.')
                if any(k.strip().lower() in ('api_key', 'apikey', 'authorization', 'secret', 'password') for k in item):
                    raise ProviderError('Credentials belong in the OS vault, not request data.')
                pending.extend(item.values())
            else:
                pending.extend(item)
        try:
            # This single immutable byte snapshot is both approved and sent.
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ProviderError('Provider input must contain finite, valid UTF-8 JSON data.') from None
        if len(raw) > profile.max_input_bytes:
            raise ProviderError('Request exceeds the selected input budget.')
        if type(maximum_cost_cents) is not int or not 0 <= maximum_cost_cents <= profile.request_budget_cents:
            raise ProviderError('This request exceeds the authorized per-request budget.')
        credential_revision = sha256((reference or '').encode('utf-8')).hexdigest()
        descriptor = dict(profile=asdict(profile), input_sha256=sha256(raw).hexdigest(),
                          maximum_cost_cents=maximum_cost_cents, credential_revision=credential_revision)
        digest = sha256(json.dumps(descriptor, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        return {'approval_sha256': digest, 'profile': profile.name, 'task': profile.task,
                'destination': profile.endpoint, 'input_bytes': len(raw), 'input_sha256': descriptor['input_sha256'],
                'maximum_cost_cents': maximum_cost_cents, 'mode': self.mode,
                'credential_revision': credential_revision,
                'billing_note': 'A local reservation is not a provider price quote or a verified final charge.'}, raw

    def preview(self, identity, payload, *, maximum_cost_cents=0):
        profile, reference = self._snapshot(identity)
        return self._review(profile, reference, payload, maximum_cost_cents)[0]

    def request(self, identity, payload, *, approval_sha256, maximum_cost_cents=0):
        # Fail before looking up any secret or opening a socket in LOCAL_ONLY.
        if self.mode != 'HYBRID':
            raise ProviderError('LOCAL_ONLY mode denies all provider HTTP calls. Enable a provider explicitly to use it.')
        profile, reference = self._snapshot(identity)
        if not profile.enabled:
            raise ProviderError('The selected provider is disabled.')
        # Do not fetch the profile again via preview: an A -> B -> A concurrent
        # edit could approve B's destination but send using the first A record.
        review, raw = self._review(profile, reference, payload, maximum_cost_cents)
        if approval_sha256 != review['approval_sha256']:
            raise ProviderError('Approve this exact destination, request and budget before sending.')
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            # Recheck the policy and profile within the same reservation lease.
            if db.execute('SELECT mode FROM policy WHERE id=1').fetchone()[0] != 'HYBRID':
                raise ProviderError('Provider access was revoked before sending.')
            row = db.execute('SELECT document,spent_cents,credential_ref FROM profiles WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ProviderError('Provider settings changed. Review the request again.')
            saved, spent, key_reference = row
            if (ProviderProfile(**json.loads(saved)) != profile
                    or key_reference != reference):
                raise ProviderError('Provider settings changed. Review the request again.')
            if spent + maximum_cost_cents > profile.total_budget_cents:
                raise ProviderError('This request exceeds the remaining total provider budget.')
            if db.execute('SELECT 1 FROM requests WHERE digest=?', (approval_sha256,)).fetchone():
                raise ProviderError('This approved request was already attempted; it will not be silently sent or billed twice.')
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)', (approval_sha256, identity, 'reserved', maximum_cost_cents, None, time.time()))
            db.execute('UPDATE profiles SET spent_cents=spent_cents+? WHERE id=?', (maximum_cost_cents, identity))
        try:
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json,audio/*'}
            if profile.requires_key:
                try:
                    key = self.vault.get(reference) if reference else None
                except Exception:
                    raise ProviderError('The OS credential vault could not read the selected key.') from None
                if not key:
                    raise ProviderError('The selected OS-vault key is unavailable.')
                headers['Authorization'] = 'Bearer ' + _secret(key)
            # A vault prompt may take time. Honor revocation or edits that
            # completed while it was open before constructing any HTTP opener.
            with closing(sqlite3.connect(self.database)) as db, db:
                current_mode = db.execute('SELECT mode FROM policy WHERE id=1').fetchone()[0]
                current = db.execute('SELECT document,credential_ref FROM profiles WHERE id=?', (identity,)).fetchone()
            if (current_mode != 'HYBRID' or current is None
                    or ProviderProfile(**json.loads(current[0])) != profile or current[1] != reference):
                raise ProviderError('Provider access or settings changed before sending. Review the request again.')
            request = urllib.request.Request(profile.endpoint, data=raw, headers=headers, method='POST')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=profile.timeout_seconds) as response:
                content = response.read(profile.max_output_bytes + 1)
                if len(content) > profile.max_output_bytes:
                    raise ProviderError('Provider output exceeds the approved response budget.')
                content_type = response.headers.get('Content-Type', 'application/octet-stream')
            digest = sha256(content).hexdigest()
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('UPDATE requests SET state=?,response_sha256=?,updated=? WHERE digest=?',
                           ('complete', digest, time.time(), approval_sha256))
            return {'body': content, 'content_type': content_type, 'sha256': digest, 'request_sha256': approval_sha256}
        except BaseException as exc:
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('UPDATE requests SET state=?,updated=? WHERE digest=?',
                           ('needs_review', time.time(), approval_sha256))
            if isinstance(exc, (KeyboardInterrupt, SystemExit, ProviderError)):
                raise
            if isinstance(exc, urllib.error.HTTPError):
                raise ProviderError(f'The provider returned HTTP {exc.code}. The request will not be retried automatically.') from None
            raise ProviderError('The provider request failed. No request content or credentials were logged; review its status before retrying.') from None
