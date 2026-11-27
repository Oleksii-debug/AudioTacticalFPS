"""Explicit Google desktop refresh credentials; no ADC or browser discovery.

Only an already-authorized Gateway calls this bounded token exchange. The
refresh credential remains in Windows Credential Manager; access tokens are
cached in the existing Gateway instance and never written to application state.
"""
from hashlib import sha256
import http.client
import json
import math
import re
import ssl
from threading import Lock
from time import monotonic
from urllib.parse import urlencode

from .profiles import google_oauth_token, google_v3_parent, _save_google_record

SCHEMA = 'carr-google-oauth-refresh/1'
TOKEN_HOST = 'oauth2.googleapis.com'
TOKEN_ROUTE = '/token'
MAX_RESPONSE = 32768
CLOCK_MARGIN = 60
TRANSLATION_SCOPES = frozenset((
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/cloud-translation',
))


class OAuthRefreshError(ValueError):
    """A redacted refresh failure, before the billed translation request."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OAuthRefreshError('The Google OAuth credential or token response has duplicate fields.')
        result[key] = value
    return result


def _part(value, label, maximum, *, empty=False):
    if (type(value) is not str or (not value and not empty) or len(value) > maximum
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise OAuthRefreshError('Enter a valid Google OAuth ' + label + '; do not paste a key file or URL.')
    return value


def refresh_credential(client_id, client_secret, refresh_token, address):
    """Validate an explicitly supplied desktop grant without any network call."""
    google_v3_parent(address)
    _part(client_id, 'desktop client ID', 320)
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{7,260}\.apps\.googleusercontent\.com', client_id) is None:
        raise OAuthRefreshError('Enter the Google Desktop app client ID ending in .apps.googleusercontent.com.')
    _part(client_secret, 'client secret', 256, empty=True)
    _part(refresh_token, 'refresh token', 1800)
    if len(refresh_token) < 8:
        raise OAuthRefreshError('Enter the refresh token from your already authorized Google desktop grant.')
    data = {'schema': SCHEMA, 'endpoint': address, 'client_id': client_id,
            'client_secret': client_secret, 'refresh_token': refresh_token}
    value = json.dumps(data, separators=(',', ':'), ensure_ascii=True)
    if len(value.encode('utf-8')) > 2560:
        raise OAuthRefreshError('The refresh credential exceeds the existing Windows vault limit.')
    return value


def parse_refresh_credential(record, address):
    if type(record) is not str or not 1 <= len(record.encode('utf-8')) <= 2560:
        raise OAuthRefreshError('The stored Google refresh credential is invalid.')
    try:
        data = json.loads(record, object_pairs_hook=_unique)
        if (type(data) is not dict
                or set(data) != {'schema', 'endpoint', 'client_id', 'client_secret', 'refresh_token'}
                or data['schema'] != SCHEMA or data['endpoint'] != address):
            raise OAuthRefreshError('The stored Google refresh credential is invalid for this endpoint.')
        refresh_credential(data['client_id'], data['client_secret'], data['refresh_token'], address)
        return data
    except (TypeError, UnicodeError, OverflowError, RecursionError, json.JSONDecodeError):
        raise OAuthRefreshError('The stored Google refresh credential is invalid.') from None


def save_google_refresh(store, vault, profile, client_id, client_secret, refresh_token, *, current, guard=lambda: None):
    """Explicit replacement of the selected account; no background vault write."""
    if profile.adapter != 'google-translation-v3':
        raise ValueError('Choose the Advanced v3 adapter before configuring automatic renewal.')
    record = refresh_credential(client_id, client_secret, refresh_token, profile.endpoint)
    return _save_google_record(store, vault, profile, record, current=current, guard=guard)


def credential_summary(record, address):
    """Local configuration check only; never exchanges or returns a token."""
    try:
        data = json.loads(record, object_pairs_hook=_unique)
    except (TypeError, ValueError, RecursionError):
        google_oauth_token(record, address)
        raise AssertionError('Unreachable invalid legacy credential.')
    if type(data) is dict and data.get('schema') == SCHEMA:
        parse_refresh_credential(record, address)
        return ('Automatic Google renewal is configured for this endpoint. '
                'No token exchange or API access was tested.')
    from datetime import datetime, timezone
    _, expires = google_oauth_token(record, address)
    return ('OAuth expiry: ' + datetime.fromtimestamp(expires, timezone.utc).isoformat()
            + '. This manually supplied access token is not refreshed automatically.')


class GoogleTokenSession:
    """Small, memory-only cache; every use still follows a fresh vault read."""

    def __init__(self):
        self._tokens = {}
        self._lock = Lock()

    def token(self, profile, record, *, timeout, guard):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise OAuthRefreshError('The Google token exchange has no valid remaining time budget.')
        guard()
        try:
            data = json.loads(record, object_pairs_hook=_unique)
        except (TypeError, ValueError, RecursionError):
            # Preserve the existing manual-token contract and its redacted error.
            return google_oauth_token(record, profile.endpoint)[0]
        if type(data) is not dict or data.get('schema') != SCHEMA:
            return google_oauth_token(record, profile.endpoint)[0]
        data = parse_refresh_credential(record, profile.endpoint)
        key = (profile.id, profile.credential_ref, profile.endpoint)
        digest = sha256(record.encode('utf-8')).hexdigest()
        deadline = monotonic() + timeout
        held = False
        try:
            while not held:
                guard()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise InterruptedError('Google token renewal exceeded the approved request time budget.')
                held = self._lock.acquire(timeout=min(0.05, remaining))
            guard()
            cached = self._tokens.get(key)
            if cached and cached[0] == digest and cached[2] > monotonic() + CLOCK_MARGIN:
                return cached[1]
            self._tokens.pop(key, None)
            token, expires = self._refresh(data, deadline=deadline, guard=guard)
            guard()
            self._tokens = {entry: value for entry, value in self._tokens.items()
                            if value[2] > monotonic() + CLOCK_MARGIN}
            if len(self._tokens) >= 16:
                self._tokens.pop(next(iter(self._tokens)))
            self._tokens[key] = (digest, token, expires)
            return token
        finally:
            data.clear()
            if held:
                self._lock.release()

    @staticmethod
    def _refresh(data, *, deadline, guard):
        connection = None
        headers = {'Content-Type': 'application/x-www-form-urlencoded',
                   'Accept': 'application/json',
                   'User-Agent': 'UniversalResearchLibrary/0.2'}
        fields = {'grant_type': 'refresh_token', 'client_id': data['client_id'],
                  'refresh_token': data['refresh_token']}
        if data['client_secret']:
            fields['client_secret'] = data['client_secret']
        body = urlencode(fields).encode('ascii')
        started = monotonic()

        def check():
            guard()
            if monotonic() >= deadline:
                raise InterruptedError('Google token renewal exceeded the approved request time budget.')

        try:
            check()
            connection = http.client.HTTPSConnection(TOKEN_HOST, 443,
                timeout=min(10.0, deadline - monotonic()), context=ssl.create_default_context())
            check()
            # Fixed official authority, no environment proxy, redirect, discovery,
            # source text, cost request, code grant or external process.
            connection.request('POST', TOKEN_ROUTE, body=body, headers=headers)
            check()
            response = connection.getresponse()
            check()
            if response.status != 200:
                if 300 <= response.status < 400:
                    raise OAuthRefreshError('Google token renewal attempted a redirect; it was not followed.')
                raise OAuthRefreshError('Google token renewal failed (HTTP %d). Reconnect the selected account; no translation was sent.' % response.status)
            content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json':
                raise OAuthRefreshError('Google token renewal returned an unexpected content type.')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE):
                raise OAuthRefreshError('Google token renewal exceeded its response budget.')
            chunks, count = [], 0
            while True:
                check()
                block = response.read(min(4096, MAX_RESPONSE + 1 - count))
                check()
                if not block:
                    break
                count += len(block)
                if count > MAX_RESPONSE:
                    raise OAuthRefreshError('Google token renewal exceeded its response budget.')
                chunks.append(block)
            if not count or (length is not None and int(length) != count):
                raise OAuthRefreshError('Google token renewal returned empty or truncated data.')
            result = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=_unique)
            if (type(result) is not dict
                    or not {'access_token', 'expires_in', 'token_type'} <= set(result)
                    or not set(result) <= {'access_token', 'expires_in', 'token_type', 'scope',
                                           'id_token', 'refresh_token', 'refresh_token_expires_in'}
                    or type(result['token_type']) is not str
                    or result['token_type'].casefold() != 'bearer'
                    or type(result['expires_in']) is not int
                    or not CLOCK_MARGIN < result['expires_in'] <= 43200
                    or type(result['access_token']) is not str
                    or re.fullmatch(r'[A-Za-z0-9._~+/=-]{8,2200}', result['access_token']) is None
                    or result['access_token'].startswith('AIza')):
                raise OAuthRefreshError('Google token renewal returned invalid token metadata.')
            if 'scope' in result:
                scope = result['scope']
                if (type(scope) is not str or len(scope) > 4096
                        or not TRANSLATION_SCOPES.intersection(scope.split())):
                    raise OAuthRefreshError('The renewed Google grant does not declare a translation scope.')
            if 'refresh_token' in result and result['refresh_token'] != data['refresh_token']:
                raise OAuthRefreshError('Google rotated the refresh credential. Reconnect explicitly before translating.')
            expires = started + result['expires_in']
            check()
            if expires <= monotonic() + CLOCK_MARGIN:
                raise OAuthRefreshError('The renewed Google access token is already too close to expiry.')
            token = result['access_token']
            # A malformed response must not turn another credential into a bearer.
            if token == data['refresh_token'] or token == data['client_secret']:
                raise OAuthRefreshError('Google token renewal returned invalid credential material.')
            return token, expires
        except (OAuthRefreshError, InterruptedError, PermissionError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise OAuthRefreshError('Google token renewal failed. No credential or response details were logged; no translation was sent.') from None
        finally:
            fields.clear()
            headers.clear()
            body = b''
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
