"""Opt-in capability gateway. No network, credentials or charges at import time.

Non-secret profiles are separate from the OS vault. Every operation is bound to
the exact profile and payload, with a durable reservation before any request.
An uncertain result is never retried automatically. Transport has no proxies,
redirects or executable provider responses. Money is integer millionths of the
approval's currency; reported estimates are never represented as actual bills.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from contextlib import contextmanager
from hashlib import sha256
from html import unescape
import ctypes
from ctypes import wintypes
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import tempfile
from time import monotonic, time
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit
import uuid


MAX_REQUEST = 512 * 1024
MAX_RESPONSE = 128 * 1024 * 1024
_ID = re.compile(r'[a-zA-Z0-9_-]{1,100}')
CAPABILITIES = {'llm', 'tts', 'music', 'sound_effects'}
GOOGLE_TRANSLATION_ENDPOINT = 'https://translation.googleapis.com/language/translate/v2'
MAX_TRANSLATION_RESPONSE = 1024 * 1024
_LANGUAGE = re.compile(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}')
_ADVANCED_LANGUAGE = re.compile(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}')
GOOGLE_TRANSLATION_PROVIDERS = ('google-translation-v2', 'google-translation-v3')


class ProviderError(ValueError):
    """Safe to display: never includes keys, transmitted text or server bodies."""



def google_v3_parent(endpoint):
    """Fixed Google authority; numeric projects make model receipts unambiguous."""
    if (not isinstance(endpoint, str) or not re.fullmatch(
            r'https://translation\.googleapis\.com(?::443)?/v3/projects/'
            r'[1-9][0-9]{0,19}/locations/[a-z][a-z0-9-]{0,62}', endpoint)):
        raise ProviderError('Advanced v3 requires the official HTTPS endpoint, an explicit numeric project and a location.')
    return urlsplit(endpoint).path[len('/v3/'):]


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON field.')
        value[key] = item
    return value


def google_oauth_credential(token, expires, endpoint):
    """Make an endpoint-bound IAM/ADC access-token record for the OS vault only.

    The caller supplies the real expiry. No ADC files, account discovery,
    subprocess, refresh token or token-exchange network call is used here.
    """
    google_v3_parent(endpoint)
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{8,2200}', token)
            or token.startswith('AIza')):
        raise ProviderError('Enter an OAuth access token, not an API key, JSON key file or refresh token.')
    if isinstance(expires, str):
        try:
            stamp = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError
            expires = stamp.timestamp()
        except (ValueError, OverflowError, OSError):
            raise ProviderError('Enter the actual OAuth expiry with a UTC offset, such as 2026-11-27T15:30:00Z.') from None
    current = time()
    if (type(expires) not in (int, float) or not math.isfinite(expires)
            or not current < expires <= current + 12 * 3600):
        raise ProviderError('Enter the real future OAuth expiry, at most twelve hours from now.')
    result = _json({'schema': 'carr-google-oauth/1', 'access_token': token,
                    'expires_at': expires, 'endpoint': endpoint})
    if len(result) > 2400:
        raise ProviderError('The OAuth credential record exceeds the Windows vault byte limit.')
    return result.decode('utf-8')


def google_oauth_token(record, endpoint):
    """Validate only the explicitly supplied, bounded vault record."""
    try:
        if not isinstance(record, str) or not 1 <= len(record) <= 2400:
            raise ValueError
        value = json.loads(record, object_pairs_hook=_unique_json,
                           parse_constant=lambda item: (_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) != {'schema', 'access_token', 'expires_at', 'endpoint'}
                or value['schema'] != 'carr-google-oauth/1' or value['endpoint'] != endpoint):
            raise ValueError
        google_oauth_credential(value['access_token'], value['expires_at'], endpoint)
        return value['access_token'], value['expires_at']
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        raise ProviderError('Advanced v3 needs a current endpoint-bound OAuth token and its real expiry. No automatic refresh was attempted.') from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value):
    return sha256(_json(value)).hexdigest()


def translation_receipt_sha256(value, detected_source):
    """Bind accepted decoded text and language independently of its wire hash."""
    return _digest({'text': value, 'detected_source': detected_source})


def _plain(path, *, directory=False):
    path = Path(path)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ProviderError('Symbolic links are not accepted for connection settings.')
    if path.exists() and not (path.is_dir() if directory else path.is_file()):
        raise ProviderError('Expected a regular local settings file or directory.')
    return path


def _atomic_json(path, value):
    path = _plain(path)
    _plain(path.parent, directory=True)
    raw = _json(value)
    if len(raw) > MAX_REQUEST:
        raise ProviderError('Connection settings exceed their resource budget.')
    fd, name = tempfile.mkstemp(prefix='.connections-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
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
        if self.provider not in ('elevenlabs', 'openai-compatible', *GOOGLE_TRANSLATION_PROVIDERS):
            raise ProviderError('This provider adapter is not installed.')
        if (not isinstance(self.capabilities, tuple) or not self.capabilities
                or set(self.capabilities) - (CAPABILITIES | {'translation'}) or len(set(self.capabilities)) != len(self.capabilities)):
            raise ProviderError('Select supported, distinct capabilities.')
        if self.provider == 'openai-compatible' and self.capabilities != ('llm',):
            raise ProviderError('The compatible adapter currently supports text reasoning only.')
        if self.provider == 'elevenlabs' and set(self.capabilities) - {'tts', 'music', 'sound_effects'}:
            raise ProviderError('The speech/music adapter only supports speech, music and sound effects.')
        if self.provider == 'google-translation-v2' and (
                self.capabilities != ('translation',) or self.model != 'nmt'
                or self.local or self.endpoint != GOOGLE_TRANSLATION_ENDPOINT):
            raise ProviderError('Google Basic v2 requires the official Translation endpoint, model nmt and translation capability.')
        if self.provider == 'google-translation-v3':
            google_v3_parent(self.endpoint)
            if self.capabilities != ('translation',) or self.model != 'general/nmt' or self.local:
                raise ProviderError('Google Advanced v3 requires the NMT model, translation capability and an IAM/ADC OAuth token.')
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
        self.api = ctypes.WinDLL('Advapi32.dll', use_last_error=True, winmode=0x00000800)
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
            record = pointer.contents
            if (record.Type != 1 or record.TargetName != self._target(reference, binding)
                    or not 1 <= record.CredentialBlobSize <= 2400 or not record.CredentialBlob):
                raise ProviderError('Reconnect this profile: its saved credential has an invalid format.')
            try:
                return ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize).decode('utf-8')
            except UnicodeError:
                raise ProviderError('Reconnect this profile: its saved credential has an invalid format.') from None
        finally:
            if pointer:
                record = pointer.contents
                if record.CredentialBlob and 0 < record.CredentialBlobSize <= 2400:
                    ctypes.memset(record.CredentialBlob, 0, record.CredentialBlobSize)
            self.api.CredFree(pointer)

    def delete(self, reference, binding):
        if not self.api.CredDeleteW(self._target(reference, binding), 1, 0) and ctypes.get_last_error() != 1168:
            raise ProviderError('Windows could not remove the API key.')


class ProfileStore:
    def __init__(self, path: Path, vault: CredentialVault | None = None):
        self.path, self.vault = _plain(path), vault
        self.warning = ''

    def _discard_key(self, profile):
        try:
            (self.vault or WindowsCredentialVault()).delete(profile.credential_ref, profile.credential_binding)
        except Exception:
            self.warning = ('Profile settings were saved, but an unused Windows credential could not be removed. '
                            'Review this application\'s entries in Windows Credential Manager.')

    def profiles(self):
        _plain(self.path)
        if not self.path.exists():
            return ()
        if self.path.stat().st_size > MAX_REQUEST:
            raise ProviderError('Connection settings exceed their resource budget.')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if type(data) is not dict or set(data) != {'version', 'profiles'} or data['version'] != 1 or type(data['profiles']) is not list:
            raise ProviderError('Unsupported connection settings.')
        profiles = tuple(ProviderProfile.from_dict(p) for p in data['profiles'])
        if len({p.id for p in profiles}) != len(profiles):
            raise ProviderError('Duplicate profile identifiers.')
        return profiles

    def save(self, profile: ProviderProfile, *, secret: str | None = None):
        from .ingest.writer_guard import exclusive_writer
        self.warning = ''
        with exclusive_writer(self.path.parent, timeout=10):
            return self._save_locked(profile, secret=secret)

    def save_google_token(self, profile, token, expires, *, renew_same_account=False):
        """Only explicit same-account renewal preserves an approved profile hash.

        An opaque token cannot prove its principal locally. The caller must
        separately confirm the same account; every other replacement receives
        a fresh credential reference and requires fresh job approval.
        """
        from .ingest.writer_guard import exclusive_writer
        if (not isinstance(profile, ProviderProfile) or profile.provider != 'google-translation-v3'
                or type(renew_same_account) is not bool):
            raise ProviderError('Choose an Advanced v3 profile and an explicit renewal decision.')
        record = google_oauth_credential(token, expires, profile.endpoint)
        self.warning = ''
        with exclusive_writer(self.path.parent, timeout=10):
            if not renew_same_account:
                return self._save_locked(profile, secret=record)
            previous = next((item for item in self.profiles() if item.id == profile.id), None)
            if (previous is None or previous.fingerprint != profile.fingerprint
                    or not previous.credential_ref):
                raise ProviderError('Same-account renewal requires the exact unchanged connected profile.')
            try:
                (self.vault or WindowsCredentialVault()).put(
                    previous.credential_ref, previous.credential_binding, record)
            except Exception:
                # Never delete the existing reference after an ambiguous vault
                # failure. No profile write follows the atomic credential update.
                raise ProviderError('The OAuth credential could not be renewed. Review it before resuming this profile.') from None
            return previous

    def _save_locked(self, profile, *, secret=None):
        profiles = {p.id: p for p in self.profiles()}
        previous = profiles.get(profile.id)
        if secret is not None:
            if profile.provider == 'google-translation-v3':
                google_oauth_token(secret, profile.endpoint)
            vault = self.vault or WindowsCredentialVault()
            reference = 'url-api-' + uuid.uuid4().hex
            profile = ProviderProfile(**{**asdict(profile), 'credential_ref': reference})
            try:
                vault.put(reference, profile.credential_binding, secret)
            except Exception:
                self._discard_key(profile)
                raise ProviderError('The key could not be saved securely. The profile was not changed.') from None
        elif profile.credential_ref and (previous is None or previous.credential_ref != profile.credential_ref
                or previous.credential_binding != profile.credential_binding):
            raise ProviderError('Reconnect the key when changing the provider, destination, model or account.')
        profiles[profile.id] = profile
        try:
            _atomic_json(self.path, {'version': 1, 'profiles': [asdict(p) for p in profiles.values()]})
        except BaseException:
            if secret is not None:
                self._discard_key(profile)
            raise
        if previous and previous.credential_ref and previous.credential_ref != profile.credential_ref:
            self._discard_key(previous)
        return profile

    def remove(self, identity):
        from .ingest.writer_guard import exclusive_writer
        self.warning = ''
        with exclusive_writer(self.path.parent, timeout=10):
            return self._remove_locked(identity)

    def _remove_locked(self, identity):
        profiles = list(self.profiles())
        found = next((p for p in profiles if p.id == identity), None)
        if found is None:
            raise ProviderError('Profile no longer exists.')
        _atomic_json(self.path, {'version': 1, 'profiles': [asdict(p) for p in profiles if p.id != identity]})
        if found.credential_ref:
            self._discard_key(found)

    def export_profiles(self):
        return {'version': 1, 'profiles': [{**asdict(p), 'credential_ref': None} for p in self.profiles()]}


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
class GoogleTranslationRequest:
    """A separate request type keeps every existing audio/LLM approval hash stable."""
    text: str = ''
    source: str = 'auto'
    target: str = 'en'
    operation: str = 'text'

    def __post_init__(self):
        if self.operation not in ('text', 'languages'):
            raise ProviderError('Unsupported Google Basic v2 operation.')
        if not isinstance(self.target, str) or not _LANGUAGE.fullmatch(self.target):
            raise ProviderError('Choose a supported target language code.')
        if self.source != 'auto' and (
                not isinstance(self.source, str) or not _LANGUAGE.fullmatch(self.source)):
            raise ProviderError('Choose a supported source language code or auto detection.')
        if self.operation == 'languages':
            if self.text != '' or self.source != 'auto' or self.target != 'en':
                raise ProviderError('Language discovery sends only the fixed NMT language-list query.')
        elif (not isinstance(self.text, str) or not self.text.strip()
              or len(self.text) > 5000 or '\x00' in self.text or self.source == self.target):
            raise ProviderError('Translate a nonempty plain-text chunk of at most 5000 characters to a different language.')
        try:
            self.text.encode('utf-8', errors='strict')
        except UnicodeError:
            raise ProviderError('Translation text contains invalid Unicode.') from None

    @property
    def capability(self):
        return 'translation'

    def fingerprint(self, profile):
        return _digest({'profile': profile.fingerprint, 'request': asdict(self)})



@dataclass(frozen=True)
class GoogleAdvancedTranslationRequest(GoogleTranslationRequest):
    """Distinct payload keeps every existing Basic v2 approval hash unchanged."""
    glossary: str = ''

    def __post_init__(self):
        if self.operation not in ('text', 'languages'):
            raise ProviderError('Unsupported Google Advanced v3 operation.')
        if not isinstance(self.target, str) or not _ADVANCED_LANGUAGE.fullmatch(self.target):
            raise ProviderError('Choose a supported target language code.')
        if self.source != 'auto' and (
                not isinstance(self.source, str) or not _ADVANCED_LANGUAGE.fullmatch(self.source)):
            raise ProviderError('Choose a supported source language code or auto detection.')
        if not isinstance(self.glossary, str) or len(self.glossary) > 512:
            raise ProviderError('Choose a bounded glossary resource name or leave it empty.')
        if self.operation == 'languages':
            if self.text != '' or self.source != 'auto' or self.target != 'en' or self.glossary:
                raise ProviderError('Language discovery sends only the fixed NMT language-list query.')
        elif (not isinstance(self.text, str) or not self.text.strip()
              or len(self.text) > 5000 or '\x00' in self.text or self.source == self.target):
            raise ProviderError('Translate a nonempty plain-text chunk of at most 5000 characters to a different language.')
        try:
            self.text.encode('utf-8', errors='strict')
        except UnicodeError:
            raise ProviderError('Translation text contains invalid Unicode.') from None


def google_glossary(profile, value, source):
    parent = google_v3_parent(profile.endpoint)
    if not isinstance(value, str):
        raise ProviderError('Choose a glossary resource name or leave it empty.')
    if not value:
        return ''
    prefix = parent + '/glossaries/'
    if (source == 'auto' or parent.endswith('/locations/global')
            or not value.startswith(prefix) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value[len(prefix):])):
        raise ProviderError('A glossary needs an explicit source language and the same non-global project/location as this profile.')
    return value


def translation_request(profile, text='', source='auto', target='en', operation='text', *, glossary=''):
    """One version-aware factory for gateway, approvals and durable checkpoints."""
    if not isinstance(profile, ProviderProfile):
        raise ProviderError('Choose a typed Google Translation profile.')
    if profile.provider == 'google-translation-v2':
        if not isinstance(glossary, str) or glossary:
            raise ProviderError('Glossaries require an Advanced v3 profile.')
        return GoogleTranslationRequest(text, source, target, operation)
    if profile.provider != 'google-translation-v3':
        raise ProviderError('Choose a Google Basic v2 or Advanced v3 translation profile.')
    request = GoogleAdvancedTranslationRequest(text, source, target, operation, glossary)
    google_glossary(profile, request.glossary, request.source)
    return request


def _advanced_wire(profile, request):
    if profile.provider != 'google-translation-v3' or type(request) is not GoogleAdvancedTranslationRequest:
        raise ProviderError('Use the typed request for the selected Advanced v3 adapter.')
    parent = google_v3_parent(profile.endpoint)
    model = parent + '/models/' + profile.model
    glossary = google_glossary(profile, request.glossary, request.source)
    if request.operation == 'languages':
        body = {'displayLanguageCode': 'en', 'model': model}
        return '/supportedLanguages?' + urlencode(body), body
    body = {'contents': [request.text], 'targetLanguageCode': request.target,
            'mimeType': 'text/plain', 'model': model}
    if request.source != 'auto':
        body['sourceLanguageCode'] = request.source
    if glossary:
        body['glossaryConfig'] = {'glossary': glossary, 'ignoreCase': False}
    return ':translateText', body


def _decode_advanced_translation_response(profile, request, raw, *, secret=None):
    """Validate requested model/glossary receipts; keep text/plain entities literal."""
    _advanced_wire(profile, request)
    try:
        decoded = json.loads(raw, object_pairs_hook=_unique_json,
                             parse_constant=lambda item: (_ for _ in ()).throw(ValueError()))
        if type(decoded) is not dict:
            raise ValueError
        # Inspect every returned string, including an unselected base translation,
        # after JSON decoding. HTML decoding is used only for secret detection.
        if secret:
            probe = _json(decoded).decode('utf-8')
            for _ in range(5):
                if secret in probe:
                    raise ProviderError('Provider returned credential material; no response was saved.')
                following = unescape(probe)
                if following == probe:
                    break
                probe = following
            else:
                raise ProviderError('Unsupported nested response escapes; no response was saved.')

        def plain(value, maximum, *, empty=False):
            if (not isinstance(value, str) or len(value) > maximum or '\x00' in value
                    or (not empty and not value.strip())):
                raise ValueError
            value.encode('utf-8', errors='strict')
            return value

        if request.operation == 'languages':
            if set(decoded) != {'languages'} or type(decoded['languages']) is not list or not 1 <= len(decoded['languages']) <= 1000:
                raise ValueError
            rows, codes = [], set()
            for row in decoded['languages']:
                if (type(row) is not dict or 'languageCode' not in row
                        or set(row) - {'languageCode', 'displayName', 'supportSource', 'supportTarget'}):
                    raise ValueError
                code = plain(row['languageCode'], 35)
                name = plain(row.get('displayName', ''), 200, empty=True)
                source, target = row.get('supportSource', False), row.get('supportTarget', False)
                if (not _ADVANCED_LANGUAGE.fullmatch(code) or code in codes or any(ord(c) < 32 for c in name)
                        or type(source) is not bool or type(target) is not bool or not (source or target)):
                    raise ValueError
                codes.add(code)
                rows.append({'language': code, 'name': name, 'source': source, 'target': target})
            return {'languages': rows}
        if not decoded or set(decoded) - {'translations', 'glossaryTranslations'}:
            raise ValueError
        chosen = 'glossaryTranslations' if request.glossary else 'translations'
        if chosen not in decoded or (not request.glossary and 'glossaryTranslations' in decoded):
            raise ValueError
        expected_model = google_v3_parent(profile.endpoint) + '/models/' + profile.model
        result = None
        for field, translations in decoded.items():
            if type(translations) is not list or len(translations) != 1:
                raise ValueError
            row = translations[0]
            if (type(row) is not dict or not {'translatedText', 'model'} <= set(row)
                    or set(row) - {'translatedText', 'model', 'detectedLanguageCode', 'glossaryConfig'}):
                raise ValueError
            translated = plain(row['translatedText'], 200000)
            if plain(row['model'], 300) != expected_model:
                raise ValueError
            detected = row.get('detectedLanguageCode', '')
            if not isinstance(detected, str):
                raise ValueError
            detected = detected or None
            if detected is not None and not _ADVANCED_LANGUAGE.fullmatch(plain(detected, 35)):
                raise ValueError
            if field == 'glossaryTranslations':
                observed = row.get('glossaryConfig')
                if (type(observed) is not dict or 'glossary' not in observed
                        or set(observed) - {'glossary', 'ignoreCase'}
                        or observed['glossary'] != request.glossary
                        or type(observed.get('ignoreCase', False)) is not bool
                        or observed.get('ignoreCase', False)):
                    raise ValueError
            elif 'glossaryConfig' in row:
                raise ValueError
            if field == chosen:
                result = {'text': translated, 'detected_source': detected,
                          'reported_model': expected_model, 'glossary': request.glossary}
        if result is None:
            raise ValueError
        return result
    except ProviderError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        raise ProviderError('Unsupported Google Advanced v3 response; no requested model/glossary result was accepted.') from None


def decode_translation_response(request, raw, *, secret=None, profile=None):
    """Validate one bounded response before it can become a charged receipt.

    JSON and HTML escapes are decoded before credential-reflection checks. No
    provider error body, unknown command, credential or response URL is returned.
    """
    if not isinstance(request, GoogleTranslationRequest) or not isinstance(raw, bytes):
        raise ProviderError('Invalid translation response.')
    if not 0 < len(raw) <= MAX_TRANSLATION_RESPONSE:
        raise ProviderError('Translation response exceeds its resource budget.')
    if isinstance(request, GoogleAdvancedTranslationRequest):
        if not isinstance(profile, ProviderProfile):
            raise ProviderError('Advanced v3 response verification requires the selected profile.')
        return _decode_advanced_translation_response(profile, request, raw, secret=secret)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def plain(value, maximum):
        if (not isinstance(value, str) or not value.strip()
                or len(value) > maximum or '\x00' in value):
            raise ValueError
        value.encode('utf-8', errors='strict')
        result, probe = unescape(value), value
        if not result.strip() or '\x00' in result:
            raise ValueError
        result.encode('utf-8', errors='strict')
        for _ in range(5):
            if secret and secret in probe:
                raise ProviderError('Provider returned credential material; no response was saved.')
            decoded = unescape(probe)
            if decoded == probe:
                break
            probe = decoded
        else:
            raise ProviderError('Unsupported nested response escapes; no response was saved.')
        if secret and secret in result:
            raise ProviderError('Provider returned credential material; no response was saved.')
        return result

    try:
        decoded = json.loads(raw, object_pairs_hook=unique,
                             parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if type(decoded) is not dict or set(decoded) != {'data'} or type(decoded['data']) is not dict:
            raise ValueError
        data = decoded['data']
        if request.operation == 'languages':
            if set(data) != {'languages'} or type(data['languages']) is not list or not 1 <= len(data['languages']) <= 1000:
                raise ValueError
            rows, codes = [], set()
            for row in data['languages']:
                if type(row) is not dict or set(row) != {'language', 'name'}:
                    raise ValueError
                code, name = plain(row['language'], 35), plain(row['name'], 200)
                if not _LANGUAGE.fullmatch(code) or code in codes or any(ord(c) < 32 for c in name):
                    raise ValueError
                codes.add(code)
                rows.append({'language': code, 'name': name})
            return {'languages': rows}
        if set(data) != {'translations'} or type(data['translations']) is not list or len(data['translations']) != 1:
            raise ValueError
        row = data['translations'][0]
        if (type(row) is not dict or not {'translatedText'} <= set(row)
                or set(row) - {'translatedText', 'detectedSourceLanguage', 'model'}):
            raise ValueError
        result = plain(row['translatedText'], 200000)
        detected = row.get('detectedSourceLanguage')
        if detected is not None and not _LANGUAGE.fullmatch(plain(detected, 35)):
            raise ValueError
        if 'model' in row and plain(row['model'], 200) != 'nmt':
            raise ValueError
        return {'text': result, 'detected_source': detected}
    except ProviderError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        raise ProviderError('Unsupported Google Basic v2 response; no result was accepted.') from None


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
            # Serialize first-open migration; historical rows stay NULL because
            # their wire hashes cannot prove a decoded output after the fact.
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS approval (id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS operation (id TEXT PRIMARY KEY, job TEXT NOT NULL, request_hash TEXT NOT NULL,'
                       'estimate_micros INTEGER NOT NULL, state TEXT NOT NULL, output_sha256 TEXT, decoded_translation_sha256 TEXT)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(operation)')}
            if 'decoded_translation_sha256' not in columns:
                db.execute('ALTER TABLE operation ADD COLUMN decoded_translation_sha256 TEXT')

    @contextmanager
    def _db(self):
        _plain(self.path)
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
            existing = db.execute(
                'SELECT job,request_hash,estimate_micros,state FROM operation WHERE id=?', (identity,)).fetchone()
            if existing and existing != (approval.job_id, request_hash, estimate, 'cancelled_before_send'):
                raise ProviderError('Operation already recorded; reconcile its result before any retry.')
            count, total = db.execute(
                "SELECT count(*),coalesce(sum(estimate_micros),0) FROM operation "
                "WHERE job=? AND state NOT IN ('reconciled_no_charge','cancelled_before_send')",
                (approval.job_id,)).fetchone()
            if count >= approval.max_calls or total + estimate > approval.budget_micros:
                raise ProviderError('The approved call count or budget would be exceeded.')
            if db.execute("SELECT 1 FROM operation WHERE job=? AND state IN ('reserved','uncertain')",
                          (approval.job_id,)).fetchone():
                raise ProviderError('Reconcile the previous uncertain or interrupted request before continuing this job.')
            if existing:
                # An unsent operation may reuse only its exact original approval,
                # request and reservation. Sent/unknown/completed rows never retry.
                db.execute("UPDATE operation SET state='reserved' WHERE id=?", (identity,))
            else:
                db.execute('INSERT INTO operation (id,job,request_hash,estimate_micros,state,output_sha256,decoded_translation_sha256) VALUES(?,?,?,?,?,NULL,NULL)',
                           (identity, approval.job_id, request_hash, estimate, 'reserved'))

    def cancel_before_send(self, identity):
        with self._db() as db:
            cursor = db.execute("UPDATE operation SET state='cancelled_before_send' WHERE id=? AND state='reserved'", (identity,))
            if cursor.rowcount != 1:
                raise ProviderError('Only an unsent reserved operation can be cancelled without charge.')

    def finish(self, identity, *, output_sha256=None, decoded_translation_sha256=None):
        if decoded_translation_sha256 is not None and (
                output_sha256 is None or not isinstance(decoded_translation_sha256, str)
                or not re.fullmatch('[0-9a-f]{64}', decoded_translation_sha256)):
            raise ProviderError('Invalid decoded translation receipt.')
        with self._db() as db:
            db.execute('UPDATE operation SET state=?, output_sha256=?, decoded_translation_sha256=? WHERE id=?',
                       ('complete' if output_sha256 else 'uncertain', output_sha256,
                        decoded_translation_sha256, identity))

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


class DirectTransport:
    """DNS-checked and address-pinned HTTP. No proxy, redirect or automatic retry."""
    def post(self, profile, route, payload, headers, *, cancelled=lambda: False, credential_expires_at=None):
        return self._send('POST', profile, route, payload, headers, cancelled=cancelled,
                          credential_expires_at=credential_expires_at)

    def get(self, profile, route, headers, *, cancelled=lambda: False, credential_expires_at=None):
        return self._send('GET', profile, route, None, headers, cancelled=cancelled,
                          credential_expires_at=credential_expires_at)

    def _send(self, method, profile, route, payload, headers, *, cancelled, credential_expires_at=None):
        parts = urlsplit(profile.endpoint)
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        google = profile.provider in GOOGLE_TRANSLATION_PROVIDERS
        advanced = profile.provider == 'google-translation-v3'
        if advanced:
            expected = (':translateText' if method == 'POST' else
                        _advanced_wire(profile, GoogleAdvancedTranslationRequest(operation='languages'))[0])
            if (method not in ('GET', 'POST') or route != expected or (method == 'GET' and payload is not None)
                    or type(credential_expires_at) not in (int, float) or not math.isfinite(credential_expires_at)):
                raise ProviderError('Invalid Google Advanced v3 adapter route or OAuth expiry.')
        elif google:
            expected = '' if method == 'POST' else '/languages?target=en&model=nmt'
            if method not in ('GET', 'POST') or route != expected or (method == 'GET' and payload is not None):
                raise ProviderError('Invalid Google Basic v2 adapter route.')
        elif method != 'POST' or not re.fullmatch(r'/[a-zA-Z0-9_/-]+', route):
            raise ProviderError('Invalid adapter route.')
        maximum = MAX_TRANSLATION_RESPONSE if google else MAX_RESPONSE
        host = parts.hostname
        deadline = monotonic() + 120
        sent = False

        def active(connection=None):
            if advanced and not sent and time() >= credential_expires_at:
                raise ProviderError('The OAuth token expired during this request. Renew it explicitly before another request.')
            if cancelled():
                raise ProviderError('Provider operation cancelled; reconcile any possible charge before retrying.')
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ProviderError('Provider request exceeded its total elapsed-time budget.')
            if connection is not None and connection.sock is not None:
                connection.sock.settimeout(min(30, remaining))
            return min(30, remaining)

        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
            if not addresses or any(not (ipaddress.ip_address(ip).is_loopback if profile.local
                                        else ipaddress.ip_address(ip).is_global) for ip in addresses):
                raise ProviderError('Endpoint resolved outside its approved network scope.')
            if cancelled():
                raise ProviderError('Provider operation cancelled before transmission.')
            address = addresses[0]
            connection = http.client.HTTPConnection(address, port, timeout=active())
            connection.connect()
            active(connection)
            if parts.scheme == 'https':
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_default_certs()
                context.set_alpn_protocols(['http/1.1'])
                connection.sock = context.wrap_socket(connection.sock, server_hostname=host)
            host_header = f'[{host}]' if ':' in host else host
            if port != (443 if parts.scheme == 'https' else 80):
                host_header += ':' + str(port)
            if cancelled():
                raise ProviderError('Provider operation cancelled before transmission.')
            active(connection)
            sent = True  # Expiry gates transmission, not an already accepted response body.
            connection.request(method, parts.path.rstrip('/') + route, body=payload,
                headers={**headers, 'Host': host_header, 'Content-Type': 'application/json',
                         'Accept': 'application/json' if google or route.endswith('/completions') else 'audio/mpeg',
                         'Connection': 'close'})
            active(connection)
            response = connection.getresponse()
            if response.status != 200:
                raise ProviderError(f'Provider returned HTTP {response.status}; no automatic retry was made.')
            if advanced and response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                raise ProviderError('The Advanced v3 provider returned an unexpected content type.')
            declared = response.getheader('Content-Length')
            if declared and (not declared.isdecimal() or int(declared) > maximum):
                raise ProviderError('Provider response exceeds its resource budget.')
            data = bytearray()
            while True:
                active(connection)
                block = response.read(min(65536, maximum + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > maximum:
                    raise ProviderError('Provider response exceeds its resource budget.')
            active(connection)
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
                try:
                    connection.close()
                except Exception:
                    pass


class ProviderGateway:
    def __init__(self, ledger, *, vault=None, transport=None):
        self.ledger, self.vault = ledger, vault
        self.transport = transport or DirectTransport()

    def perform(self, profile, request, *, approval, operation_id, estimated_micros,
                cancelled=lambda: False):
        if not isinstance(profile, ProviderProfile) or not isinstance(request, (ProviderRequest, GoogleTranslationRequest)) or not isinstance(approval, JobApproval):
            raise ProviderError('A typed profile, request and explicit job approval are required.')
        if approval.profile_fingerprint != profile.fingerprint or request.fingerprint(profile) not in approval.request_hashes:
            raise ProviderError('This provider, model or task data is outside the approved job.')
        if request.capability not in profile.capabilities:
            raise ProviderError('This profile does not provide the requested capability.')
        if ((profile.provider == 'google-translation-v2' and type(request) is not GoogleTranslationRequest)
                or (profile.provider == 'google-translation-v3' and type(request) is not GoogleAdvancedTranslationRequest)
                or (profile.provider not in GOOGLE_TRANSLATION_PROVIDERS and isinstance(request, GoogleTranslationRequest))):
            raise ProviderError('Use the typed request for the selected provider adapter.')
        if profile.local:
            if not approval.allow_loopback:
                raise ProviderError('Authorize this exact local service before sending it task data.')
        elif approval.mode == 'LOCAL_ONLY':
            raise ProviderError('LOCAL ONLY blocks every remote provider request.')
        if cancelled():
            raise ProviderError('Provider operation cancelled before transmission.')
        if isinstance(request, GoogleAdvancedTranslationRequest):
            route, body = _advanced_wire(profile, request)
        elif isinstance(request, GoogleTranslationRequest):
            route = '/languages?target=en&model=nmt' if request.operation == 'languages' else ''
            body = {'target': 'en', 'model': 'nmt'} if request.operation == 'languages' else {
                'q': request.text, 'target': request.target, 'format': 'text', 'model': 'nmt'}
            if request.operation == 'text' and request.source != 'auto':
                body['source'] = request.source
        elif profile.provider == 'openai-compatible':
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
        self.ledger.reserve(operation_id, approval, request.fingerprint(profile), estimated_micros)
        submitted, secret, expires_at = False, None, None
        try:
            if profile.credential_ref:
                try:
                    secret = (self.vault or WindowsCredentialVault()).get(profile.credential_ref, profile.credential_binding)
                except Exception:
                    raise ProviderError('Reconnect this profile: its securely stored credential could not be read.') from None
                if profile.provider == 'google-translation-v3':
                    secret, expires_at = google_oauth_token(secret, profile.endpoint)
                    headers['X-goog-user-project'] = google_v3_parent(profile.endpoint).split('/')[1]
                if not isinstance(secret, str) or not secret or any(not 33 <= ord(c) <= 126 for c in secret):
                    raise ProviderError('Reconnect this profile: the stored credential is invalid.')
                if profile.provider == 'google-translation-v2':
                    headers['X-goog-api-key'] = secret
                else:
                    headers['xi-api-key' if profile.provider == 'elevenlabs' else 'Authorization'] = (
                        secret if profile.provider == 'elevenlabs' else 'Bearer ' + secret)
            elif not profile.local:
                raise ProviderError('Reconnect this profile before using an external provider.')
            if cancelled():
                raise ProviderError('Provider operation cancelled before transmission.')
            if expires_at is not None and time() >= expires_at:
                raise ProviderError('The OAuth token expired before transmission.')
            transport_options = {'cancelled': cancelled}
            if expires_at is not None:
                transport_options['credential_expires_at'] = expires_at
            submitted = True
            if isinstance(request, GoogleTranslationRequest) and request.operation == 'languages':
                output = self.transport.get(profile, route, headers, **transport_options)
            else:
                output = self.transport.post(profile, route, raw, headers, **transport_options)
            if not isinstance(output, bytes) or not 0 < len(output) <= MAX_RESPONSE:
                raise ProviderError('Invalid or oversized provider result.')
            decoded_translation_sha256 = None
            if isinstance(request, GoogleTranslationRequest):
                decoded = decode_translation_response(request, output, secret=secret, profile=profile)
                if request.operation == 'text':
                    decoded_translation_sha256 = translation_receipt_sha256(
                        decoded['text'], decoded['detected_source'])
            elif request.capability == 'llm':
                try:
                    decoded = json.loads(output)
                    message = decoded['choices'][0]['message']
                    if message.get('tool_calls') or not isinstance(message['content'], str):
                        raise ValueError
                except (ValueError, TypeError, KeyError, IndexError):
                    raise ProviderError('Unsupported model response; no returned commands were executed.') from None
            receipt = {'output_sha256': sha256(output).hexdigest()}
            if decoded_translation_sha256 is not None:
                receipt['decoded_translation_sha256'] = decoded_translation_sha256
            self.ledger.finish(operation_id, **receipt)
            return output
        except BaseException as exc:
            if submitted:
                self.ledger.finish(operation_id)
            else:
                self.ledger.cancel_before_send(operation_id)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # Transport implementations may include raw request/key material in
            # exceptions. No third-party exception is displayed or chained.
            if not submitted:
                raise ProviderError('Provider operation stopped before transmission. No request was sent.') from None
            raise ProviderError('Provider operation did not complete. Review its uncertain charge status before retrying.') from None
        finally:
            headers.clear()
            secret = None
