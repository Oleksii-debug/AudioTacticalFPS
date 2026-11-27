"""Import/export provider configuration without credential values."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import ipaddress
import json
import math
from pathlib import Path
import re
from time import time
from urllib.parse import urlsplit

from .credentials import validate_profile_id, validate_reference
from .models import text, local_path
from .state import atomic_json, read_json, regular


CAPABILITIES = ('llm.chat', 'speech.synthesize', 'music.compose', 'sfx.generate',
                'translation.text', 'translation.languages')
ADAPTERS = ('openai-compatible', 'elevenlabs', 'google-translation-v2', 'google-translation-v3')


def google_v3_parent(address):
    """A fixed official authority and explicit project number/region.

    Numeric project identities avoid guessing Google's ID-to-number response
    normalization when verifying the selected model and glossary provenance.
    """
    target = endpoint(address)
    if (target.hostname != 'translation.googleapis.com' or target.port not in (None, 443)
            or not re.fullmatch(r'/v3/projects/[1-9][0-9]{0,19}/locations/[a-z][a-z0-9-]{0,62}', target.path)):
        raise ValueError('Advanced v3 requires https://translation.googleapis.com/v3/projects/PROJECT_NUMBER/locations/LOCATION.')
    return target.path[len('/v3/'):]


def google_oauth_credential(token, expires, address):
    """Prepare a short-lived IAM/ADC token record for the OS vault only."""
    google_v3_parent(address)
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{8,2200}', token)
            or token.startswith('AIza')):
        raise ValueError('Enter an OAuth access token from your authorized IAM/ADC account, not an API key or JSON key file.')
    if isinstance(expires, str):
        try:
            stamp = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError()
            expires = stamp.timestamp()
        except (ValueError, OverflowError):
            raise ValueError('Enter the token expiry with its UTC offset, for example 2026-11-27T15:30:00Z.') from None
    if (type(expires) not in (float, int) or not time() < expires <= time() + 12 * 3600
            or not math.isfinite(expires)):
        raise ValueError('Enter the real future OAuth token expiry, at most 12 hours from now.')
    return json.dumps({'schema': 'carr-google-oauth/1', 'access_token': token,
                       'expires_at': expires, 'endpoint': address}, separators=(',', ':'))


def google_oauth_token(record, address):
    """Read only an explicitly supplied vault record; never discover/refresh ADC."""
    try:
        data = json.loads(record)
        if (type(data) is not dict or set(data) != {'schema', 'access_token', 'expires_at', 'endpoint'}
                or data['schema'] != 'carr-google-oauth/1' or data['endpoint'] != address):
            raise ValueError()
        google_oauth_credential(data['access_token'], data['expires_at'], address)
        return data['access_token'], data['expires_at']
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Advanced v3 OAuth token expired or is invalid for this endpoint. Re-enter a current token and its expiry in AI and API connections; no automatic refresh was attempted.') from None


def save_google_token(store, vault, profile, token, expires, *, current=None):
    """Explicit same-profile token renewal preserves paused-job references.

    A changed account creates a new reference; no old job silently acquires new
    endpoint/model authority. No secret is written to a profile/checkpoint file.
    """
    if profile.adapter != 'google-translation-v3':
        raise ValueError('Choose the Advanced v3 adapter before storing an OAuth token.')
    if current is not None and (current.id != profile.id or store.get(current.id) != current):
        raise ValueError('The selected account changed. Reopen it before explicitly replacing its OAuth credential.')
    record = google_oauth_credential(token, expires, profile.endpoint)
    if current == profile and current.credential_ref:
        # Atomic CredWrite under the same exact nonsecret profile preserves the
        # approved job's identity. There is no subsequent profile write to fail.
        vault.put(profile.id, record, reference=current.credential_ref)
        return profile
    reference = vault.put(profile.id, record)
    updated = replace(profile, credential_ref=reference)
    try:
        store.save(updated)
    except Exception:
        vault.delete(profile.id, reference)
        raise
    if current is not None and current.credential_ref:
        vault.delete(current.id, current.credential_ref)
    return updated


def endpoint(value, *, local=False):
    text(value, 'provider endpoint', maximum=2048)
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or '\\' in value or '%' in value or any(c.isspace() for c in value)):
        raise ValueError('Use an endpoint without embedded credentials, query, fragment or escaped authority.')
    try:
        port = parsed.port
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
        # Invalid ports must not be mistaken for ordinary hostnames.
        port = parsed.port
    if local:
        if parsed.scheme not in ('http', 'https') or address is None or not address.is_loopback:
            raise ValueError('Local services require an explicit loopback IP address (127.0.0.1 or ::1).')
    else:
        if parsed.scheme != 'https' or parsed.hostname.lower() in ('localhost',) or (address and not address.is_global):
            raise ValueError('Remote providers require HTTPS and a public host.')
    if any(part in ('.', '..') for part in parsed.path.split('/')):
        raise ValueError('Provider endpoint path is invalid.')
    return parsed


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    adapter: str
    endpoint: str
    model: str
    capabilities: tuple = ('llm.chat',)
    credential_ref: str | None = None
    local: bool = False
    enabled: bool = False
    voice: str | None = None
    rights_note: str = ''

    def __post_init__(self):
        validate_profile_id(self.id)
        text(self.name, 'provider name', maximum=160)
        text(self.model, 'provider model', maximum=256)
        text(self.rights_note, 'provider rights note', maximum=4096, empty=True)
        if self.adapter not in ADAPTERS or type(self.local) is not bool or type(self.enabled) is not bool:
            raise ValueError('Unsupported provider profile.')
        target = endpoint(self.endpoint, local=self.local)
        object.__setattr__(self, 'endpoint', self.endpoint.rstrip('/'))
        object.__setattr__(self, 'capabilities', tuple(self.capabilities))
        if not self.capabilities or len(set(self.capabilities)) != len(self.capabilities) or any(c not in CAPABILITIES for c in self.capabilities):
            raise ValueError('Unsupported provider capability.')
        if self.adapter == 'openai-compatible' and self.capabilities != ('llm.chat',):
            raise ValueError('The compatible-chat adapter supports only chat completion.')
        if self.adapter == 'elevenlabs':
            if self.local or target.hostname != 'api.elevenlabs.io' or target.port not in (None, 443) or target.path not in ('', '/v1'):
                raise ValueError('ElevenLabs credentials may only be sent to its official HTTPS API host.')
            if any(c not in ('speech.synthesize', 'music.compose', 'sfx.generate') for c in self.capabilities):
                raise ValueError('The ElevenLabs adapter does not provide chat completion or translation.')
        if self.adapter == 'google-translation-v2':
            if (self.local or target.hostname != 'translation.googleapis.com'
                    or target.port not in (None, 443) or target.path != '/language/translate/v2'):
                raise ValueError('Google Translation credentials require its exact official HTTPS Basic v2 endpoint.')
            if self.model != 'nmt' or any(c not in ('translation.text', 'translation.languages') for c in self.capabilities):
                raise ValueError('This Google adapter supports Basic v2 NMT text and language discovery only; Advanced v3 requires IAM/ADC.')
        if self.adapter == 'google-translation-v3':
            google_v3_parent(self.endpoint)
            if self.local or self.model != 'general/nmt' or any(c not in ('translation.text', 'translation.languages') for c in self.capabilities):
                raise ValueError('Advanced v3 uses an explicit remote NMT model (general/nmt) and an IAM/ADC OAuth token.')
        if self.credential_ref is not None:
            validate_reference(self.credential_ref, self.id)
        if self.voice is not None:
            validate_profile_id(self.voice)

    def to_dict(self):
        return {**asdict(self), 'capabilities': list(self.capabilities)}


class ProfileStore:
    def __init__(self, directory):
        self.directory = local_path(directory, must_exist=False)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_path(self.directory)

    def path(self, identity):
        return self.directory / (validate_profile_id(identity) + '.profile.json')

    def save(self, profile):
        if not isinstance(profile, ProviderProfile):
            raise TypeError('A validated provider profile is required.')
        atomic_json(self.path(profile.id), {'schema': 'carr-provider-profile/1', **profile.to_dict()})
        return profile

    def get(self, identity):
        value = read_json(self.path(identity), maximum=32768)
        if value.pop('schema', None) != 'carr-provider-profile/1' or value.get('id') != identity:
            raise ValueError('Invalid provider profile.')
        return ProviderProfile(**value)

    def list(self):
        return tuple(self.get(p.name[:-len('.profile.json')]) for p in sorted(self.directory.glob('*.profile.json')))

    def remove(self, identity, *, vault=None):
        profile = self.get(identity)
        if profile.credential_ref and vault is not None:
            vault.delete(profile.id, profile.credential_ref)
        self.path(identity).unlink()

    def export_json(self):
        # Export deliberately clears vault references; another machine must reconnect.
        return json.dumps({'schema': 'carr-provider-profiles/1', 'profiles': [
            {**p.to_dict(), 'credential_ref': None, 'enabled': False} for p in self.list()]},
            ensure_ascii=False, indent=2)

    def import_json(self, value):
        text(value, 'provider import', maximum=1024 * 1024)
        document = json.loads(value)
        if type(document) is not dict or set(document) != {'schema', 'profiles'} or document['schema'] != 'carr-provider-profiles/1':
            raise ValueError('Unsupported provider profile export.')
        if not isinstance(document['profiles'], list) or len(document['profiles']) > 1000:
            raise ValueError('Too many provider profiles.')
        profiles = []
        for data in document['profiles']:
            if type(data) is not dict:
                raise ValueError('Invalid provider import.')
            profile = ProviderProfile(**{**data, 'credential_ref': None, 'enabled': False})
            if self.path(profile.id).exists() or any(x.id == profile.id for x in profiles):
                raise FileExistsError('Profile import does not overwrite existing accounts.')
            profiles.append(profile)
        for profile in profiles:
            self.save(profile)
        return tuple(profiles)
