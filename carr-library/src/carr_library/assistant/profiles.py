"""Import/export provider configuration without credential values."""
from dataclasses import asdict, dataclass
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from .credentials import validate_profile_id, validate_reference
from .models import text, local_path
from .state import atomic_json, read_json, regular


CAPABILITIES = ('llm.chat', 'speech.synthesize', 'music.compose', 'sfx.generate')
ADAPTERS = ('openai-compatible', 'elevenlabs')


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
            if 'llm.chat' in self.capabilities:
                raise ValueError('The ElevenLabs adapter does not provide chat completion.')
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
