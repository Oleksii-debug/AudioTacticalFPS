"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit, urlunsplit, quote


class ProviderError(ValueError):
    pass


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
