"""Immutable provider data; an access statement is a claim, not permission.

Raw response bytes are retained exactly for provenance. They are deliberately
excluded from repr and are never rendered as HTML or treated as source quotes.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import ipaddress
import json
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit

from ..research.academic_library import normalize_doi as _catalog_doi


PROVIDERS = ('crossref', 'unpaywall', 'europe_pmc')
PROVIDER_HOSTS = {
    'crossref': 'api.crossref.org',
    'unpaywall': 'api.unpaywall.org',
    'europe_pmc': 'www.ebi.ac.uk',
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TEXT = 4000
MAX_ABSTRACT = 100_000
MAX_AUTHORS = 100
MAX_LOCATIONS = 100
MAX_URL = 4000
_BIDI = frozenset('\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')


def normalize_doi(value):
    """Use the catalog's ASCII-only DOI folding and resolver URL semantics."""
    doi = _catalog_doi(value)
    if not doi:
        raise ValueError('Enter a DOI to look up.')
    return doi


def text_value(value, name, maximum=MAX_TEXT, *, multiline=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{name} must be text of at most {maximum} characters.')
    allowed = '\t\r\n' if multiline else ''
    if any(c in _BIDI or unicodedata.category(c) in ('Cs', 'Cc') and c not in allowed
           or not multiline and c in '\u2028\u2029' for c in value):
        raise ValueError(f'{name} contains unsupported control characters.')
    return value


def safe_source_url(value, *, optional=False):
    """Accept public-shaped HTTPS links without credentials or query secrets.

    This is syntactic validation only. The transport separately validates DNS,
    peer addresses, TLS, redirects, permission and byte/time budgets.
    """
    if optional and value == '':
        return ''
    message = 'The provider supplied an unsafe or unsupported source URL.'
    try:
        text_value(value, 'Source URL', MAX_URL)
        if (not value or any(c.isspace() for c in value) or '\\' in value
                or re.search(r'%(?![0-9a-fA-F]{2})', value)):
            raise ValueError()
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme != 'https' or not host or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or parts.fragment or host.endswith('.') or '%' in host):
            raise ValueError()
        host = host.encode('idna').decode('ascii').lower()
        if (len(host) > 253 or '.' not in host or host == 'localhost'
                or host.endswith(('.localhost', '.local', '.internal'))
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                       for label in host.split('.'))):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r'(?:[0-9]+\.)*[0-9]+', host):
                raise ValueError()
        else:
            raise ValueError()
        decoded = unquote(value, errors='strict')
        if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
            raise ValueError()
        secret_names = {'token', 'accesstoken', 'apikey', 'key', 'password',
                        'secret', 'signature', 'sig', 'auth', 'authorization',
                        'credential', 'credentials', 'session', 'sessionid'}
        for name, _ in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100):
            folded = re.sub(r'[^a-z0-9]', '', name.lower())
            if (folded in secret_names or folded.startswith(('xamz', 'xgoog'))
                    or folded.endswith(('token', 'password', 'secret', 'signature'))):
                raise ValueError()
        # The transport emits an ASCII request target, without rewriting URLs.
        ((parts.path or '/') + parts.query).encode('ascii')
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(message) from None
    return value


def _provider(value):
    if value not in PROVIDERS:
        raise ValueError('Unsupported metadata provider.')


def _tuple_text(value, name, maximum_count, maximum_length=MAX_TEXT):
    if not isinstance(value, tuple) or len(value) > maximum_count:
        raise ValueError(f'{name} must be an immutable tuple of at most {maximum_count} values.')
    for part in value:
        text_value(part, name, maximum_length)


@dataclass(frozen=True)
class ProviderConfig:
    unpaywall_email: str = field(default='', repr=False)
    crossref_mailto: str = field(default='', repr=False)

    def __post_init__(self):
        for value in (self.unpaywall_email, self.crossref_mailto):
            text_value(value, 'Contact email', 254)
            if (not isinstance(value, str) or len(value) > 254
                    or value and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value)
                    or any(ord(c) < 33 or ord(c) == 127 for c in value)):
                raise ValueError('Provide a valid contact email address for the selected provider.')


@dataclass(frozen=True)
class ProviderRequest:
    provider: str
    endpoint: str
    query: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self):
        _provider(self.provider)
        safe_source_url(self.endpoint)
        parts = urlsplit(self.endpoint)
        if (parts.hostname != PROVIDER_HOSTS[self.provider] or parts.query
                or not isinstance(self.query, tuple) or len(self.query) > 4):
            raise ValueError('Invalid provider request.')
        path = parts.path
        prefix = {'crossref': '/works/', 'unpaywall': '/v2/'}.get(self.provider)
        if prefix:
            if not path.startswith(prefix):
                raise ValueError('Invalid provider endpoint.')
            normalize_doi(unquote(path[len(prefix):], errors='strict'))
        elif path != '/europepmc/webservices/rest/search':
            raise ValueError('Invalid provider endpoint.')
        allowed = {'crossref': {'mailto'}, 'unpaywall': {'email'},
                   'europe_pmc': {'query', 'format', 'resultType', 'pageSize'}}[self.provider]
        seen = set()
        for pair in self.query:
            if (not isinstance(pair, tuple) or len(pair) != 2
                    or pair[0] not in allowed or pair[0] in seen):
                raise ValueError('Invalid provider query.')
            seen.add(pair[0])
            text_value(pair[1], 'Provider query', MAX_URL)


@dataclass(frozen=True)
class AccessLocation:
    provider: str
    url: str
    landing_url: str = ''
    format: str = 'unknown'
    version: str = ''
    license: str = ''
    reported_open_access: bool | None = None
    host_type: str = ''

    def __post_init__(self):
        _provider(self.provider)
        safe_source_url(self.url)
        safe_source_url(self.landing_url, optional=True)
        if self.format not in ('pdf', 'xml', 'html', 'unknown'):
            raise ValueError('Unsupported source format.')
        for name in ('version', 'license', 'host_type'):
            text_value(getattr(self, name), name)
        if self.reported_open_access is not None and type(self.reported_open_access) is not bool:
            raise ValueError('An open-access claim must be true, false or unknown.')

    @property
    def id(self):
        values = (self.provider, self.url, self.landing_url, self.format,
                  self.version, self.license, self.reported_open_access, self.host_type)
        return 'location_' + sha256(json.dumps(values, ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')).hexdigest()

    @property
    def host(self):
        return urlsplit(self.url).hostname


@dataclass(frozen=True)
class MetadataPreview:
    provider: str
    doi: str
    title: str = ''
    authors: tuple[str, ...] = ()
    year: str = ''
    item_type: str = 'other'
    container_title: str = ''
    publisher: str = ''
    volume: str = ''
    issue: str = ''
    pages: str = ''
    isbn: tuple[str, ...] = ()
    abstract: str = ''
    language: str = ''
    source_url: str = ''
    locations: tuple[AccessLocation, ...] = ()
    warnings: tuple[str, ...] = ()
    raw_json: bytes = field(default=b'', repr=False)
    raw_sha256: str = ''

    def __post_init__(self):
        _provider(self.provider)
        if normalize_doi(self.doi) != self.doi:
            raise ValueError('The preview DOI must use the catalog canonical form.')
        for name in ('title', 'container_title', 'publisher', 'volume', 'issue', 'pages', 'language'):
            text_value(getattr(self, name), name)
        text_value(self.year, 'Year', 32)
        text_value(self.abstract, 'Abstract', MAX_ABSTRACT, multiline=True)
        if self.item_type not in ('book', 'article', 'thesis', 'report', 'webpage', 'other'):
            raise ValueError('Unsupported bibliographic item type.')
        _tuple_text(self.authors, 'Authors', MAX_AUTHORS)
        _tuple_text(self.isbn, 'ISBNs', 100, 100)
        _tuple_text(self.warnings, 'Warnings', 300)
        safe_source_url(self.source_url, optional=True)
        if (not isinstance(self.locations, tuple) or len(self.locations) > MAX_LOCATIONS
                or any(not isinstance(item, AccessLocation) or item.provider != self.provider
                       for item in self.locations)):
            raise ValueError('Invalid immutable source locations.')
        if not isinstance(self.raw_json, bytes) or len(self.raw_json) > MAX_RESPONSE_BYTES:
            raise ValueError('Provider responses must be exact bytes within the response limit.')
        if (not isinstance(self.raw_sha256, str) or self.raw_sha256
                and not re.fullmatch(r'[0-9a-f]{64}', self.raw_sha256)):
            raise ValueError('Invalid provider response digest.')
        if self.raw_json:
            digest = sha256(self.raw_json).hexdigest()
            if self.raw_sha256 and self.raw_sha256 != digest:
                raise ValueError('The provider response digest does not match the preserved bytes.')
            object.__setattr__(self, 'raw_sha256', digest)
        elif self.raw_sha256:
            raise ValueError('A response digest requires the preserved response bytes.')
