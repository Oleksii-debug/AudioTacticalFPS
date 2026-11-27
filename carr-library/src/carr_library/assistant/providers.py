"""Opt-in provider profiles and a bounded, destination-bound API gateway.

All authorization is a host-created PlanApproval. Imported documents, scripts
and model responses cannot create it. LOCAL_ONLY is the default and denies
every remote connection. Only explicitly selected literal loopback services
are allowed locally. Remote requests require reviewed profiles, data scope,
finite per-request cost reservation and a plan-wide budget. No automatic retry.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
import errno
from hashlib import sha256
import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4
import wave

from filelock import FileLock

from ..audio.projects import atomic_json, canonical, directory, plain, read_json
from ..audio.voices import _destination
from ..data_paths import guard_data_directory
from .credentials import WindowsCredentialVault, validate_reference, validate_secret


_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')
_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z')
_VOICE = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
MAX_PROFILES = 128
MAX_REQUEST_BYTES = 512 * 1024
MAX_CHAT_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 64 * 1024 * 1024


class ProviderError(RuntimeError):
    """Public safe error: response bodies and secrets never enter diagnostics."""


def _id(value, label='identity'):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f'Invalid {label}.')
    return value


def _label(value, label, maximum=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError(f'Invalid {label}.')
    return value


def endpoint(value, *, local):
    """Canonical endpoint, with no embedded credentials, query or redirect."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(ord(ch) < 33 for ch in value):
        raise ValueError('Invalid provider endpoint.')
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError('Invalid provider endpoint.') from None
    if (not host or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or '\\' in value or '%' in value
            or parts.path not in ('', '/', '/v1', '/v1/')):
        raise ValueError('Use a provider base URL with an optional /v1 path and no credentials or query.')
    host = host.lower()
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Invalid provider port.')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if local:
        if parts.scheme not in ('http', 'https') or address is None or not address.is_loopback:
            raise ValueError('A local provider must use an explicit 127.x.x.x or ::1 loopback address.')
    else:
        if parts.scheme != 'https':
            raise ValueError('Remote providers require HTTPS.')
        if address is not None and not address.is_global:
            raise ValueError('Remote provider addresses must be public; configure loopback services explicitly.')
        if address is None:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host) or '.' not in host:
                raise ValueError('Use a public DNS hostname for a remote provider.')
            if any(not part or part.startswith('-') or part.endswith('-') for part in host.split('.')):
                raise ValueError('Invalid provider hostname.')
    visible_host = '[' + host + ']' if ':' in host else host
    authority = visible_host + (f':{port}' if port is not None else '')
    return f'{parts.scheme}://{authority}{parts.path.rstrip("/") or "/v1"}'


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    provider: str
    endpoint: str
    model: str
    capability: str
    credential_ref: str | None = None
    local: bool = False
    voice_id: str | None = None

    def __post_init__(self):
        _id(self.id, 'profile identity')
        _label(self.name, 'profile name')
        if type(self.local) is not bool:
            raise ValueError('Provider locality must be explicitly true or false.')
        normalized = endpoint(self.endpoint, local=self.local)
        if self.endpoint != normalized:
            raise ValueError('Use the normalized provider base URL: ' + normalized)
        if not isinstance(self.model, str) or not _TOKEN.fullmatch(self.model):
            raise ValueError('Invalid provider model identity.')
        if self.provider == 'openai-compatible':
            if self.capability != 'chat' or self.voice_id is not None:
                raise ValueError('This compatible adapter supports text chat only.')
        elif self.provider == 'elevenlabs':
            if self.local or self.endpoint != 'https://api.elevenlabs.io/v1':
                raise ValueError('ElevenLabs credentials can only go to its official HTTPS API origin.')
            if self.capability != 'tts' or not isinstance(self.voice_id, str) or not _VOICE.fullmatch(self.voice_id):
                raise ValueError('ElevenLabs speech requires an explicit valid voice identity.')
        else:
            raise ValueError('This provider adapter is not implemented.')
        if self.credential_ref is not None:
            validate_reference(self.credential_ref)

    @property
    def fingerprint(self):
        return sha256(canonical(asdict(self)).encode()).hexdigest()


def validate_profiles_document(value):
    if (type(value) is not dict or set(value) != {'version', 'kind', 'profiles'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'provider-connections' or type(value['profiles']) is not list
            or len(value['profiles']) > MAX_PROFILES):
        raise ValueError('Invalid non-secret provider profile document.')
    fields = set(ProviderProfile.__dataclass_fields__)
    if any(type(row) is not dict or set(row) != fields for row in value['profiles']):
        raise ValueError('Provider configuration contains unknown or secret fields.')
    profiles = tuple(ProviderProfile(**row) for row in value['profiles'])
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError('Duplicate provider account identity.')
    return profiles


class ConnectionStore:
    def __init__(self, root):
        self.root = guard_data_directory(root)
        directory(self.root)
        self.path = self.root / 'connections.json'

    @contextmanager
    def _lock(self):
        lock = self.root / '.connections.lock'
        plain(lock, missing=True)
        with FileLock(lock, mode=0o600).acquire(timeout=5):
            yield

    def list_profiles(self):
        if not self.path.exists():
            return ()
        return validate_profiles_document(read_json(self.path, 256 * 1024))

    def get(self, profile_id):
        _id(profile_id, 'profile identity')
        for profile in self.list_profiles():
            if profile.id == profile_id:
                return profile
        raise ValueError('Provider profile not found.')

    def save_profile(self, profile, *, secret=None, vault=None, expected_fingerprint=None):
        if type(profile) is not ProviderProfile:
            raise ValueError('Save a validated provider profile.')
        if secret is not None:
            validate_secret(secret)
            if secret in canonical(asdict(profile)):
                raise ValueError('An API key must not appear in non-secret profile fields.')
        created_reference = None
        with self._lock():
            profiles = list(self.list_profiles())
            previous = next((row for row in profiles if row.id == profile.id), None)
            if previous is not None and expected_fingerprint != previous.fingerprint:
                raise ValueError('The provider profile changed; reopen it before saving.')
            if previous is None and len(profiles) >= MAX_PROFILES:
                raise ValueError('Provider profile storage limit reached.')
            if previous and previous.endpoint != profile.endpoint and profile.credential_ref == previous.credential_ref:
                profile = replace(profile, credential_ref=None)
            if secret is not None:
                vault = vault or WindowsCredentialVault()
                created_reference = vault.store(profile.id, profile.endpoint, secret)
                profile = replace(profile, credential_ref=created_reference)
            try:
                profiles = [row for row in profiles if row.id != profile.id] + [profile]
                document = {'version': 1, 'kind': 'provider-connections',
                            'profiles': [asdict(row) for row in profiles]}
                validate_profiles_document(document)
                atomic_json(self.path, document)
            except BaseException:
                if created_reference is not None:
                    vault.delete(created_reference)
                raise
        return profile

    def remove_profile(self, profile_id, *, vault=None, delete_credential=True, expected_fingerprint=None):
        _id(profile_id, 'profile identity')
        with self._lock():
            profiles = list(self.list_profiles())
            old = next((profile for profile in profiles if profile.id == profile_id), None)
            if old is None:
                raise ValueError('Provider profile not found.')
            if expected_fingerprint is not None and old.fingerprint != expected_fingerprint:
                raise ValueError('The provider profile changed; reopen it before deleting.')
            if delete_credential and old.credential_ref:
                (vault or WindowsCredentialVault()).delete(old.credential_ref)
            atomic_json(self.path, {'version': 1, 'kind': 'provider-connections',
                                   'profiles': [asdict(row) for row in profiles if row.id != profile_id]})


@dataclass(frozen=True)
class PlanApproval:
    plan_id: str = field(default_factory=lambda: uuid4().hex)
    mode: str = 'LOCAL_ONLY'
    profiles: tuple[tuple[str, str], ...] = ()
    data_classes: tuple[str, ...] = ('user_text',)
    max_budget_microunits: int = 0
    max_calls: int = 100
    currency: str = 'USD'

    def __post_init__(self):
        _id(self.plan_id, 'provider plan identity')
        if self.mode not in ('LOCAL_ONLY', 'APPROVED_REMOTE', 'HYBRID'):
            raise ValueError('Unsupported provider mode.')
        if not isinstance(self.profiles, tuple) or len(self.profiles) > MAX_PROFILES:
            raise ValueError('Invalid approved provider set.')
        seen = set()
        for row in self.profiles:
            if not isinstance(row, tuple) or len(row) != 2:
                raise ValueError('Invalid approved provider binding.')
            _id(row[0], 'approved profile identity')
            if not isinstance(row[1], str) or not re.fullmatch('[0-9a-f]{64}', row[1]) or row[0] in seen:
                raise ValueError('Invalid or duplicate approved provider binding.')
            seen.add(row[0])
        if not isinstance(self.data_classes, tuple) or not self.data_classes or len(self.data_classes) > 32:
            raise ValueError('Invalid approved data scope.')
        for value in self.data_classes:
            _id(value, 'data class')
        if (type(self.max_budget_microunits) is not int or not 0 <= self.max_budget_microunits <= 10**12
                or type(self.max_calls) is not int or not 1 <= self.max_calls <= 10000
                or self.currency not in ('USD', 'EUR', 'GBP')):
            raise ValueError('Invalid provider budget, currency or call limit.')

    @classmethod
    def for_profiles(cls, profiles, **kwargs):
        """Called by trusted host UI after the user approves a concrete plan."""
        profiles = tuple(profiles)
        if any(type(profile) is not ProviderProfile for profile in profiles):
            raise ValueError('Approve validated provider profiles only.')
        return cls(profiles=tuple((profile.id, profile.fingerprint) for profile in profiles), **kwargs)

    @property
    def fingerprint(self):
        return sha256(canonical(asdict(self)).encode()).hexdigest()


def validate_ledger_document(value, *, plan_id):
    """Strict secret-free backup decoder; restored history cannot resume calls.

    Restore must set ``restored`` to True. A backup can predate a later charge,
    so its counters cannot safely authorize more requests against the old plan.
    """
    _id(plan_id, 'provider plan identity')
    fields = {'version', 'kind', 'plan_id', 'restored', 'approval_sha256', 'currency', 'operations'}
    if (type(value) is not dict or set(value) != fields
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'provider-usage' or value['plan_id'] != plan_id
            or type(value['restored']) is not bool or value['currency'] not in ('USD', 'EUR', 'GBP')
            or not isinstance(value['approval_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', value['approval_sha256'])
            or type(value['operations']) is not dict or len(value['operations']) > 10000):
        raise ValueError('Invalid provider usage journal.')
    for key, row in value['operations'].items():
        _id(key, 'provider operation identity')
        if (type(row) is not dict or set(row) != {'profile_id', 'request_sha256', 'state',
                'reserved_microunits', 'billed_microunits', 'data_class'}
                or row['state'] not in ('reserved', 'received', 'complete', 'uncertain',
                    'not_sent', 'invalid_response', 'result_not_saved')
                or type(row['reserved_microunits']) is not int
                or not 0 <= row['reserved_microunits'] <= 10**12
                or row['billed_microunits'] is not None
                or not isinstance(row['request_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', row['request_sha256'])):
            raise ValueError('Invalid provider usage operation.')
        _id(row['profile_id'], 'provider profile identity')
        _id(row['data_class'], 'provider data class')
        if row['state'] == 'not_sent' and row['reserved_microunits'] != 0:
            raise ValueError('An unsent provider operation cannot consume a reservation.')
    return value


def _cancel(cancelled):
    if cancelled is not None:
        callback = cancelled.is_set if hasattr(cancelled, 'is_set') else cancelled
        if not callable(callback):
            raise ValueError('Invalid provider cancellation callback.')
        if callback():
            raise InterruptedError('Provider operation cancelled; reconcile any uncertain charge before retrying.')


def _addresses(host, port, *, local, timeout, cancelled):
    # DNS timeout is independent of libc's resolver timeout; a stuck resolver
    # cannot hold the caller. A late daemon result has no authority to connect.
    answers = queue.Queue(maxsize=1)
    def resolve():
        try:
            answers.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            answers.put(None)
    threading.Thread(target=resolve, daemon=True).start()
    started = time.monotonic()
    while True:
        _cancel(cancelled)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ProviderError('Provider address lookup exceeded the request time budget.')
        try:
            records = answers.get(timeout=min(0.05, remaining))
            break
        except queue.Empty:
            pass
    if not records or len(records) > 64:
        raise ProviderError('The provider address could not be resolved.')
    result = []
    for family, kind, protocol, _, address in records:
        ip = ipaddress.ip_address(address[0])
        permitted = ip.is_loopback if local else ip.is_global
        if not permitted or ip.is_multicast or ip.is_unspecified:
            raise ProviderError('The provider resolved to a disallowed network destination.')
        if address not in [entry[3] for entry in result]:
            result.append((family, kind, protocol, address))
    return result


@dataclass(frozen=True)
class HTTPResult:
    content_type: str
    body: bytes
    status: int = 200


class _ResponseReader(io.RawIOBase):
    """Keep http.client's parser/buffer while polling before every socket read."""
    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        channel.readers += 1

    def readable(self):
        return True

    def readinto(self, target):
        return self.channel.operation(self.channel.sock.recv_into, target)

    def close(self):
        if not self.closed:
            try:
                super().close()
            finally:
                self.channel.readers -= 1
                if self.channel.closing and not self.channel.readers:
                    self.channel.sock.close()


class _RequestSocket:
    """One nonblocking connection; no reader thread or cross-thread shutdown.

    select supports Windows sockets. A timed-out socket.makefile cannot safely
    resume a partial HTTP header, so only the raw socket is nonblocking and the
    normal http.client parser receives a blocking BufferedReader facade. Every
    wait returns to the request's own cancellation/deadline check within 50 ms.
    """
    def __init__(self, sock, *, deadline, cancelled):
        self.sock, self.deadline, self.cancelled = sock, deadline, cancelled
        self.readers, self.closing = 0, False
        self.sock.setblocking(False)

    def remaining(self):
        _cancel(self.cancelled)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Provider request exceeded its time budget.')
        return remaining

    def wait(self, *, writing=False):
        while True:
            remaining = self.remaining()
            readable, writable, exceptional = select.select(
                [] if writing else [self.sock], [self.sock] if writing else [],
                [self.sock], min(0.05, remaining))
            self.remaining()
            if readable or writable or exceptional:
                return

    def operation(self, call, *arguments, writing=False):
        while True:
            self.remaining()
            try:
                return call(*arguments)
            except ssl.SSLWantReadError:
                self.wait()
            except ssl.SSLWantWriteError:
                self.wait(writing=True)
            except BlockingIOError:
                self.wait(writing=writing)

    def connect(self, address):
        self.remaining()
        error = self.sock.connect_ex(address)
        pending = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                   errno.EINTR, 10035, 10036, 10037}  # Winsock pending codes.
        while error not in (0, errno.EISCONN, 10056):
            if error not in pending:
                raise OSError(error, 'Provider connection failed.')
            self.wait(writing=True)
            error = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        self.remaining()

    def secure(self, hostname):
        self.remaining()
        self.sock = ssl.create_default_context().wrap_socket(
            self.sock, server_hostname=hostname, do_handshake_on_connect=False)
        self.operation(self.sock.do_handshake)

    def sendall(self, data):
        remaining = memoryview(data)
        while remaining:
            count = self.operation(self.sock.send, remaining, writing=True)
            if count <= 0:
                raise OSError('Provider connection closed while sending.')
            remaining = remaining[count:]

    def makefile(self, mode):
        if mode != 'rb':
            raise ValueError('Provider responses require a binary read stream.')
        return io.BufferedReader(_ResponseReader(self))

    def close(self):
        # HTTP/1.0 can release HTTPConnection.sock before its response is read.
        # Keep that response's reader alive until it closes, as socket.makefile
        # does, and always force-close the underlying socket in request.finally.
        self.closing = True
        if not self.readers:
            self.sock.close()


class HTTPTransport:
    """Direct HTTP(S), pinned resolved IP, normal TLS verification, no proxies.

    Redirects are returned as errors. The connection uses the reviewed hostname
    for TLS and Host while connecting once to a validated IP; it never performs
    a second hostname lookup that could change the credential destination.
    """
    def request(self, profile, path, payload, headers, *, maximum_bytes, timeout_seconds, cancelled=None,
                method='POST'):
        if method not in ('POST', 'GET') or (method == 'GET' and payload is not None):
            raise ValueError('The provider transport permits POST or body-free GET only.')
        _cancel(cancelled)
        parts = urlsplit(profile.endpoint)
        host, port = parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)
        started = time.monotonic()
        addresses = _addresses(host, port, local=profile.local,
                               timeout=timeout_seconds, cancelled=cancelled)
        _cancel(cancelled)
        family, kind, protocol, address = addresses[0]
        connection = http.client.HTTPConnection(host, port, timeout=timeout_seconds)
        raw_socket = socket.socket(family, kind, protocol)
        channel, response = None, None
        try:
            channel = _RequestSocket(raw_socket, deadline=started + timeout_seconds, cancelled=cancelled)
            channel.connect(address)
            if parts.scheme == 'https':
                channel.secure(host)
            connection.sock = channel
            connection.auto_open = 0
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            if 300 <= response.status <= 399:
                raise ProviderError('Provider redirect refused; credentials were not forwarded.')
            if not 200 <= response.status <= 299:
                raise ProviderError(f'The provider refused the request (HTTP {response.status}); review account and usage.')
            declared = response.getheader('Content-Length')
            if declared is not None and (not declared.isdecimal() or int(declared) > maximum_bytes):
                raise ProviderError('Provider response exceeds the allowed byte budget.')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ProviderError('Unexpected encoded provider response.')
            body = bytearray()
            while True:
                _cancel(cancelled)
                chunk = response.read(min(65536, maximum_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > maximum_bytes:
                    raise ProviderError('Provider response exceeds the allowed byte budget.')
            if declared is not None and len(body) != int(declared):
                raise ProviderError('Provider response was truncated.')
            channel.remaining()
            return HTTPResult(response.getheader('Content-Type', '')[:256].lower(), bytes(body), response.status)
        except (InterruptedError, TimeoutError):
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError, UnicodeError):
            raise ProviderError('The provider connection failed; charge state may be uncertain. No automatic retry was made.') from None
        finally:
            try:
                if response is not None:
                    response.close()
                connection.close()
            finally:
                if channel is not None:
                    channel.sock.close()
                raw_socket.close()


class ProviderGateway:
    def __init__(self, store, *, vault=None, approval=None, _transport=None):
        if type(store) is not ConnectionStore:
            raise ValueError('A validated provider profile store is required.')
        self.store = store
        self.vault = vault or WindowsCredentialVault()
        self.approval = approval or PlanApproval()
        if type(self.approval) is not PlanApproval:
            raise ValueError('The host must provide a validated provider plan approval.')
        self.transport = _transport or HTTPTransport()
        self.ledger_root = directory(store.root / 'provider-jobs')
        self.ledger_path = self.ledger_root / (self.approval.plan_id + '.json')

    @contextmanager
    def _lock(self):
        path = self.ledger_root / (self.approval.plan_id + '.lock')
        plain(path, missing=True)
        with FileLock(path, mode=0o600).acquire(timeout=5):
            yield

    def _ledger(self):
        if not self.ledger_path.exists():
            return {'version': 1, 'kind': 'provider-usage', 'approval_sha256': self.approval.fingerprint,
                    'plan_id': self.approval.plan_id, 'restored': False,
                    'currency': self.approval.currency, 'operations': {}}
        try:
            value = validate_ledger_document(read_json(self.ledger_path, 4 * 1024 * 1024),
                                            plan_id=self.approval.plan_id)
        except ValueError:
            raise ProviderError('Provider usage journal contains invalid operation state.') from None
        if value['restored']:
            raise ProviderError('Restored provider history is read-only. Review a new plan before making another request.')
        if (value['approval_sha256'] != self.approval.fingerprint
                or value['currency'] != self.approval.currency
                or len(value['operations']) > self.approval.max_calls
                or sum(row['reserved_microunits'] for row in value['operations'].values()) > self.approval.max_budget_microunits):
            raise ProviderError('Provider usage journal does not match this approval.')
        approved = dict(self.approval.profiles)
        for row in value['operations'].values():
            if row['profile_id'] not in approved or row['data_class'] not in self.approval.data_classes:
                raise ProviderError('Provider usage journal exceeds the reviewed profile or data scope.')
        return value

    def _state(self, operation_id, state):
        with self._lock():
            ledger = self._ledger()
            ledger['operations'][operation_id]['state'] = state
            atomic_json(self.ledger_path, ledger)

    def usage(self):
        with self._lock():
            ledger = self._ledger()
        return {'currency': ledger['currency'],
                'reserved_or_consumed_microunits': sum(row['reserved_microunits'] for row in ledger['operations'].values()),
                'billed_cost': 'unknown; reservations are estimates, not provider invoices',
                'operations': ledger['operations']}

    def _authorize(self, profile, data_class, estimated_cost_microunits):
        approved = dict(self.approval.profiles)
        if approved.get(profile.id) != profile.fingerprint:
            raise ProviderError('This exact provider profile is not approved for the plan.')
        if data_class not in self.approval.data_classes:
            raise ProviderError('This data class is outside the approved provider scope.')
        if not profile.local and self.approval.mode == 'LOCAL_ONLY':
            raise ProviderError('LOCAL_ONLY blocks remote provider connections.')
        if type(estimated_cost_microunits) is not int or estimated_cost_microunits < 0:
            raise ProviderError('A finite reviewed per-request cost reservation is required before a provider call.')
        if profile.local and estimated_cost_microunits != 0:
            raise ProviderError('Local provider calls must not declare remote billing.')
        return estimated_cost_microunits

    def _call(self, profile_id, capability, suffix, body, *, data_class, operation_id,
              estimated_cost_microunits, maximum_bytes, timeout_seconds=60, cancelled=None):
        _cancel(cancelled)
        _id(operation_id, 'provider operation identity')
        _id(data_class, 'provider data class')
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 600:
            raise ValueError('Provider timeout must be between 0.1 and 600 seconds.')
        profile = self.store.get(profile_id)
        if profile.capability != capability:
            raise ValueError('The selected profile does not support this operation.')
        reservation = self._authorize(profile, data_class, estimated_cost_microunits)
        payload = canonical(body).encode('utf-8')
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError('Provider input exceeds this explicitly selected request budget.')
        request_hash = sha256(canonical({'profile':profile.fingerprint,'suffix':suffix,'body':body}).encode()).hexdigest()
        with self._lock():
            ledger = self._ledger()
            if operation_id in ledger['operations']:
                raise ProviderError('This operation was already attempted. Review its result or uncertain charge before authorizing another call.')
            if len(ledger['operations']) >= self.approval.max_calls:
                raise ProviderError('The approved plan call limit is exhausted.')
            used = sum(row['reserved_microunits'] for row in ledger['operations'].values())
            if used + reservation > self.approval.max_budget_microunits:
                raise ProviderError('This request would exceed the approved plan budget.')
            ledger['operations'][operation_id] = {'profile_id':profile.id,'request_sha256':request_hash,
                'state':'reserved','reserved_microunits':reservation,'billed_microunits':None,'data_class':data_class}
            atomic_json(self.ledger_path, ledger)
        sent = False
        secret = None
        try:
            headers = {'Content-Type':'application/json','Accept-Encoding':'identity',
                       'Accept':'application/json' if capability == 'chat' else 'audio/pcm',
                       'User-Agent':'UniversalResearchLibrary/0.2'}
            if profile.credential_ref is not None:
                secret = self.vault.resolve(profile.credential_ref, profile.id, profile.endpoint)
                validate_secret(secret)
                if secret.encode() in payload:
                    raise ProviderError('The request body contains the configured API secret; transmission was refused.')
                _reject_decoded_credential(body, secret.encode('utf-8'))
                if profile.provider == 'elevenlabs':
                    headers['xi-api-key'] = secret
                else:
                    headers['Authorization'] = 'Bearer ' + secret
            elif not profile.local:
                raise ProviderError('Reconnect this remote profile with a native-vault API credential.')
            _cancel(cancelled)
            sent = True  # A connection failure after here has uncertain charge state.
            result = self.transport.request(profile, urlsplit(profile.endpoint).path + suffix,
                payload, headers, maximum_bytes=maximum_bytes, timeout_seconds=timeout_seconds, cancelled=cancelled)
            if type(result) is not HTTPResult or type(result.body) is not bytes or len(result.body) > maximum_bytes:
                raise ProviderError('Provider returned an invalid or oversized result.')
            if secret is not None:
                if secret.encode('utf-8') in result.body:
                    raise ProviderError('Provider response contained credential material; it was discarded.')
                if result.content_type.startswith('application/json'):
                    try:
                        decoded = json.loads(result.body)
                    except (ValueError, UnicodeError):
                        pass
                    else:
                        _reject_decoded_credential(decoded, secret.encode('utf-8'))
            self._state(operation_id, 'received')
            return profile, result, request_hash
        except BaseException:
            with self._lock():
                ledger = self._ledger()
                row = ledger['operations'][operation_id]
                row['state'] = 'uncertain' if sent else 'not_sent'
                if not sent:
                    row['reserved_microunits'] = 0
                atomic_json(self.ledger_path, ledger)
            raise
        finally:
            secret = None

    def chat(self, profile_id, messages, *, data_class='user_text', operation_id,
             estimated_cost_microunits=None, max_output_tokens=1024, timeout_seconds=60, cancelled=None):
        if type(messages) not in (list, tuple) or not 1 <= len(messages) <= 128:
            raise ValueError('A chat request needs between 1 and 128 selected messages.')
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32768:
            raise ValueError('Invalid chat output token budget.')
        for message in messages:
            if (type(message) is not dict or set(message) != {'role','content'}
                    or message['role'] not in ('system','user','assistant')
                    or not isinstance(message['content'], str) or len(message['content']) > MAX_REQUEST_BYTES
                    or '\x00' in message['content']):
                raise ValueError('Chat messages must contain selected plain text and explicit roles only.')
        profile = self.store.get(profile_id)
        profile, result, request_hash = self._call(profile_id, 'chat', '/chat/completions',
            {'model':profile.model,'messages':list(messages),'max_tokens':max_output_tokens,'stream':False},
            data_class=data_class, operation_id=operation_id, estimated_cost_microunits=estimated_cost_microunits,
            maximum_bytes=MAX_CHAT_BYTES, timeout_seconds=timeout_seconds, cancelled=cancelled)
        if not result.content_type.startswith('application/json'):
            self._state(operation_id, 'invalid_response')
            raise ProviderError('Provider chat result is not JSON.')
        try:
            value = json.loads(result.body.decode('utf-8'))
            message = value['choices'][0]['message']
            if type(message) is not dict or message.get('tool_calls'):
                raise ValueError
            content = message['content']
            usage = value.get('usage', {})
            if not isinstance(content, str) or len(content) > MAX_CHAT_BYTES or '\x00' in content:
                raise ValueError
            # Retain numeric token counters only; never trust arbitrary provider
            # fields as ledger entries, costs, tool calls or executable plans.
            usage = {key: usage[key] for key in ('prompt_tokens','completion_tokens','total_tokens')
                     if type(usage) is dict and type(usage.get(key)) is int and usage[key] >= 0}
        except (ValueError, UnicodeError, KeyError, IndexError, TypeError):
            self._state(operation_id, 'invalid_response')
            raise ProviderError('Provider chat response has an unsupported schema.') from None
        self._state(operation_id, 'complete')
        return {'text':content,'profile_id':profile.id,'model':profile.model,'request_sha256':request_hash,
                'operation_id':operation_id,'usage':usage,'billed_microunits':None,
                'trust':'untrusted model text; validate every proposed tool operation separately'}

    def tts(self, profile_id, text, output, *, data_class='selected_narration', operation_id,
            estimated_cost_microunits=None, timeout_seconds=120, cancelled=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 32000 or '\x00' in text:
            raise ValueError('Provide a selected speech segment of at most 32000 characters.')
        destination = _destination(output)
        profile = self.store.get(profile_id)
        if profile.provider != 'elevenlabs':
            raise ValueError('This speech adapter requires an ElevenLabs profile.')
        profile, result, request_hash = self._call(profile_id, 'tts',
            '/text-to-speech/' + profile.voice_id + '?output_format=pcm_24000',
            {'text':text,'model_id':profile.model}, data_class=data_class, operation_id=operation_id,
            estimated_cost_microunits=estimated_cost_microunits, maximum_bytes=MAX_AUDIO_BYTES,
            timeout_seconds=timeout_seconds, cancelled=cancelled)
        if (not result.content_type.startswith(('audio/pcm','audio/x-pcm','application/octet-stream'))
                or not result.body or len(result.body) % 2):
            self._state(operation_id, 'invalid_response')
            raise ProviderError('Provider did not return the requested 16-bit raw PCM waveform.')
        temporary = destination.with_name('.' + uuid4().hex + '.wav')
        try:
            with temporary.open('xb') as stream:
                with wave.open(stream, 'wb') as writer:
                    writer.setparams((1,2,24000,0,'NONE','not compressed'))
                    for start in range(0, len(result.body), 65536):
                        _cancel(cancelled)
                        writer.writeframesraw(result.body[start:start + 65536])
                stream.flush()
                os.fsync(stream.fileno())
            _cancel(cancelled)
            checksum = sha256(temporary.read_bytes()).hexdigest()
            os.link(temporary, destination)
        except BaseException:
            self._state(operation_id, 'result_not_saved')
            raise
        finally:
            temporary.unlink(missing_ok=True)
        self._state(operation_id, 'complete')
        return {'path':str(destination),'sha256':checksum,'sample_rate':24000,'channels':1,
                'frames':len(result.body)//2,'duration_seconds':len(result.body)/48000,
                'profile_id':profile.id,'voice_id':profile.voice_id,'model':profile.model,
                'operation_id':operation_id,'request_sha256':request_hash,'billed_microunits':None}


# Canonical public provider API shares this module's sole HTTP/TLS transport.
from .canonical_gateway import (
    Authorization, Gateway, UsageLedger, money, _request,
    _reject_decoded_credential, validate_usage_ledger_document,
)
