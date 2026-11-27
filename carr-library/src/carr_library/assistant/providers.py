"""Explicit, bounded provider calls. LOCAL_ONLY is the immutable default mode.

The gateway never downloads a model, retries a billable call, follows redirects,
logs secret values, or turns provider text into executable actions.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import http.client
import html
import json
import math
import os
from pathlib import Path
import re
import ssl
from tempfile import NamedTemporaryFile
from time import monotonic, time
from uuid import uuid4
from urllib.parse import urlencode

from filelock import FileLock
from .credentials import WindowsCredentialVault, validate_profile_id
from .models import canonical, local_path, text
from .profiles import (CAPABILITIES, ProviderProfile, endpoint,
                       google_v3_parent, google_oauth_token)
from .state import atomic_json, read_json, regular


MODES = ('LOCAL_ONLY', 'APPROVED_REMOTE', 'HYBRID')


class ProviderError(RuntimeError):
    """User-safe provider failure, without remote response bodies or credentials."""


def translation_receipt_sha256(value, detected_source):
    """Bind decoded translation and detected language independently of wire JSON."""
    return sha256(canonical({'text': value, 'detected_source': detected_source}).encode('utf-8')).hexdigest()


def money(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Enter an explicit nonnegative cost bound.') from None
    if not result.is_finite() or result < 0 or result > Decimal('1000000'):
        raise ValueError('Invalid provider cost bound.')
    return result


@dataclass(frozen=True)
class Authorization:
    """A user-approved envelope; provider endpoints and profile IDs are exact."""
    id: str
    task_id: str
    profiles: tuple  # (profile ID, exact endpoint) pairs
    operations: tuple
    data_classes: tuple
    budget: str
    max_calls: int
    expires_at: float
    request_hashes: tuple = ()
    currency: str = 'USD'

    def __post_init__(self):
        validate_profile_id(self.id)
        text(self.task_id, 'authorized task', maximum=128)
        if type(self.max_calls) is not int or not 1 <= self.max_calls <= 100000:
            raise ValueError('Invalid provider call budget.')
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at):
            raise ValueError('Invalid provider authorization expiry.')
        if not re.fullmatch('[A-Z]{3}', self.currency):
            raise ValueError('Use a three-letter budget currency.')
        money(self.budget)
        if not self.profiles or any(len(p) != 2 for p in self.profiles):
            raise ValueError('Approve exact provider profiles and endpoints.')
        for identity, address in self.profiles:
            validate_profile_id(identity)
            endpoint(address, local=False)
        if not self.operations or any(o not in CAPABILITIES for o in self.operations):
            raise ValueError('Invalid authorized operation.')
        if not self.data_classes or any(x not in ('user-instruction', 'selected-script', 'selected-excerpt', 'synthetic-test') for x in self.data_classes):
            raise ValueError('Approve a bounded selected data class; whole-library uploads are unavailable.')
        if any(not re.fullmatch('[0-9a-f]{64}', h) for h in self.request_hashes):
            raise ValueError('Invalid authorized request checksum.')


class UsageLedger:
    """Crash-safe reservations; uncertain charge state is never blindly retried."""
    def __init__(self, directory):
        self.directory = local_path(directory, must_exist=False)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, authorization_id):
        return self.directory / (validate_profile_id(authorization_id) + '.usage.json')

    def reserve(self, authorization, operation_id, request_hash, bound, profile, operation):
        path = self._path(authorization.id)
        lock = path.with_suffix('.lock')
        regular(lock, missing=True)
        with FileLock(lock, mode=0o600):
            fingerprint = sha256(canonical(asdict(authorization)).encode('utf-8')).hexdigest()
            value = read_json(path) if path.exists() else {'schema': 'carr-provider-usage/1',
                'authorization': fingerprint, 'currency': authorization.currency, 'operations': {}}
            if value.get('authorization') != fingerprint or value.get('schema') != 'carr-provider-usage/1':
                raise PermissionError('Provider authorization changed; approve a new envelope.')
            operations = value['operations']
            profile_hash = sha256(canonical(profile.to_dict()).encode('utf-8')).hexdigest()
            retry_not_sent = False
            if operation_id in operations:
                prior = operations[operation_id]
                if prior['request_sha256'] != request_hash:
                    raise PermissionError('Operation identity was reused for different data.')
                if prior.get('status') != 'not_sent':
                    raise ProviderError('This operation has already been attempted. Reconcile its recorded output or charge state before another call.')
                if (prior.get('profile') != profile.id or prior.get('model') != profile.model
                        or prior.get('operation') != operation or prior.get('profile_sha256') != profile_hash
                        or money(prior['reserved_cost']) != bound):
                    raise PermissionError('A not-sent operation cannot change its approved profile, operation or cost bound.')
                # The prior attempt never reached connection.request. The same
                # immutable envelope may reserve this identity again, under the
                # lock; reserved/uncertain/complete attempts remain blocked.
                retry_not_sent = True
            spent = sum((money(v['reserved_cost']) for v in operations.values() if v['status'] != 'not_sent'), Decimal(0))
            calls = len(operations) - int(retry_not_sent)
            if calls >= authorization.max_calls or spent + bound > money(authorization.budget):
                raise PermissionError('The approved provider call or cost budget would be exceeded.')
            operations[operation_id] = {'profile': profile.id, 'model': profile.model, 'operation': operation,
                'profile_sha256': profile_hash,
                'request_sha256': request_hash, 'reserved_cost': str(bound), 'cost_kind': 'user-approved upper bound',
                'actual_cost': None, 'status': 'reserved', 'output_sha256': None, 'usage': None}
            atomic_json(path, value)

    def finish(self, authorization_id, operation_id, *, status, output_hash=None, usage=None):
        if status not in ('complete', 'uncertain', 'not_sent'):
            raise ValueError('Invalid usage reconciliation status.')
        path = self._path(authorization_id)
        lock = path.with_suffix('.lock')
        regular(lock, missing=True)
        with FileLock(lock, mode=0o600):
            value = read_json(path)
            row = value['operations'][operation_id]
            row.update(status=status, output_sha256=output_hash, usage=usage)
            atomic_json(path, value)

    def get(self, authorization_id):
        return read_json(self._path(authorization_id))


def _request(profile, operation, payload):
    if type(payload) is not dict:
        raise ValueError('Provider request must be a typed JSON object.')
    if operation not in profile.capabilities:
        raise PermissionError('The provider profile does not support this operation.')
    if profile.adapter in ('google-translation-v2', 'google-translation-v3'):
        def language(value):
            if not isinstance(value, str) or not re.fullmatch('[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', value):
                raise ValueError('Choose an explicit supported translation language code.')
            return value
        advanced = profile.adapter == 'google-translation-v3'
        parent = google_v3_parent(profile.endpoint) if advanced else None
        model = parent + '/models/' + profile.model if advanced else profile.model
        if operation == 'translation.languages':
            if set(payload) != {'target'}:
                raise ValueError('Language discovery accepts only the display language.')
            body = {('displayLanguageCode' if advanced else 'target'): language(payload['target']), 'model': model}
            return ('/supportedLanguages?' if advanced else '/languages?') + urlencode(body), body, 'application/json'
        allowed = {'q', 'source', 'target', 'glossary'} if advanced else {'q', 'source', 'target'}
        if operation != 'translation.text' or set(payload) - allowed or not {'q', 'target'} <= set(payload):
            raise ValueError('Translation requires selected text and a target language.')
        if advanced:
            body = {'contents': [text(payload['q'], 'selected translation text', maximum=5000)],
                    'targetLanguageCode': language(payload['target']), 'mimeType': 'text/plain', 'model': model}
            if 'source' in payload:
                body['sourceLanguageCode'] = language(payload['source'])
            if 'glossary' in payload:
                glossary = payload['glossary']
                if ('source' not in payload or parent.endswith('/global') or not isinstance(glossary, str)
                        or not re.fullmatch(re.escape(parent) + r'/glossaries/[A-Za-z0-9_-]{1,128}', glossary)):
                    raise ValueError('Choose an existing glossary in the exact project and non-global region, and an explicit source language.')
                body['glossaryConfig'] = {'glossary': glossary, 'ignoreCase': False}
            return ':translateText', body, 'application/json'
        body = {'q': text(payload['q'], 'selected translation text', maximum=5000),
                'target': language(payload['target']), 'format': 'text', 'model': profile.model}
        if 'source' in payload:
            body['source'] = language(payload['source'])
        return '', body, 'application/json'
    if profile.adapter == 'openai-compatible':
        if set(payload) - {'messages', 'max_tokens', 'temperature', 'response_format'} or 'messages' not in payload:
            raise ValueError('Unsupported compatible-chat request fields.')
        messages = payload['messages']
        if not isinstance(messages, list) or not 1 <= len(messages) <= 128:
            raise ValueError('Chat messages exceed the selected request limit.')
        for message in messages:
            if type(message) is not dict or set(message) != {'role', 'content'} or message['role'] not in ('system', 'user', 'assistant'):
                raise ValueError('Only text chat messages are accepted; provider tool execution is unavailable.')
            text(message['content'], 'chat text', maximum=512 * 1024)
        tokens = payload.get('max_tokens', 2048)
        if type(tokens) is not int or not 1 <= tokens <= 32768:
            raise ValueError('Invalid output token budget.')
        temperature = payload.get('temperature', 0)
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError('Invalid sampling temperature.')
        if 'response_format' in payload and payload['response_format'] != {'type': 'json_object'}:
            raise ValueError('Only plain JSON-object response mode is supported.')
        body = {**payload, 'model': profile.model, 'max_tokens': tokens, 'temperature': temperature, 'stream': False}
        return '/chat/completions', body, 'application/json'
    if profile.adapter != 'elevenlabs':
        raise ValueError('Unsupported provider adapter.')
    if operation == 'speech.synthesize':
        if set(payload) != {'text'} or not profile.voice:
            raise ValueError('Speech synthesis requires text and a configured voice ID.')
        text(payload['text'], 'selected speech text', maximum=100000)
        return '/text-to-speech/' + profile.voice + '?output_format=mp3_44100_128', {
            'text': payload['text'], 'model_id': profile.model}, 'audio/mpeg'
    if operation == 'music.compose':
        if set(payload) != {'prompt', 'duration_ms'}:
            raise ValueError('Music generation requires a prompt and explicit duration.')
        text(payload['prompt'], 'music prompt', maximum=4100)
        if type(payload['duration_ms']) is not int or not 3000 <= payload['duration_ms'] <= 600000:
            raise ValueError('This provider call supports 3 to 600 seconds; longer projects need multiple approved cues.')
        return '/music?output_format=mp3_44100_128', {'prompt': payload['prompt'],
            'music_length_ms': payload['duration_ms'], 'model_id': profile.model,
            'force_instrumental': True}, 'audio/mpeg'
    if operation == 'sfx.generate':
        if set(payload) - {'text', 'duration_seconds', 'loop'} or not {'text', 'duration_seconds'} <= set(payload):
            raise ValueError('Sound-effect generation requires text and explicit duration.')
        text(payload['text'], 'sound-effect prompt', maximum=4100)
        duration = payload['duration_seconds']
        if type(duration) not in (float, int) or not math.isfinite(duration) or not 0.5 <= duration <= 30:
            raise ValueError('This provider supports a sound-effect cue of 0.5 to 30 seconds.')
        if type(payload.get('loop', False)) is not bool:
            raise ValueError('Invalid sound-effect loop option.')
        return '/sound-generation?output_format=mp3_44100_128', {
            **payload, 'model_id': profile.model}, 'audio/mpeg'
    raise ValueError('Unsupported provider operation.')


def _reject_decoded_credential(value, secret_bytes):
    """Check structured results after JSON decoding and text normalization.

    Raw scanning cannot recognize JSON Unicode escapes or HTML character
    references. Inspect the actual returned values and their display-text form
    before any receipt, language cache or completed-usage record is published.
    """
    if not secret_bytes:
        return
    secret = secret_bytes.decode('utf-8')
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if secret in current or secret in html.unescape(current):
                raise ProviderError('The provider response contained credential material and was discarded.')
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)


class Gateway:
    def __init__(self, directory, *, vault=None, mode='LOCAL_ONLY'):
        if mode not in MODES:
            raise ValueError('Unknown provider connection mode.')
        self.mode = mode
        self.vault = vault or WindowsCredentialVault()
        self.ledger = UsageLedger(Path(directory) / 'usage')

    def request(self, profile, operation, payload, *, task_id, data_class,
                authorization=None, max_cost=None, operation_id=None, output=None,
                cancelled=lambda: False, timeout=120.0, max_response_bytes=64 * 1024 * 1024):
        if not isinstance(profile, ProviderProfile) or not profile.enabled:
            raise PermissionError('Enable and explicitly select this provider profile first.')
        if self.mode not in MODES:
            raise PermissionError('Provider mode is invalid.')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError('Invalid provider time budget.')
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 1024 * 1024 * 1024:
            raise ValueError('Invalid response byte budget.')
        route, body, expected_type = _request(profile, operation, payload)
        raw = canonical(body).encode('utf-8')
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Provider request exceeds the selected-data limit.')
        request_hash = sha256(raw).hexdigest()
        operation_id = validate_profile_id(operation_id or uuid4().hex)
        target = endpoint(profile.endpoint, local=profile.local)
        reserved = False
        sent = False
        if not profile.local:
            if self.mode == 'LOCAL_ONLY':
                raise PermissionError('LOCAL ONLY blocks external providers before credentials or connections are opened.')
            if not isinstance(authorization, Authorization) or authorization.expires_at <= time():
                raise PermissionError('An active, explicit remote production authorization is required.')
            if (authorization.task_id != task_id or (profile.id, profile.endpoint) not in tuple(tuple(x) for x in authorization.profiles)
                    or operation not in authorization.operations or data_class not in authorization.data_classes
                    or (authorization.request_hashes and request_hash not in authorization.request_hashes)):
                raise PermissionError('This task, destination, operation or selected data is outside the approved envelope.')
            if max_cost is None:
                raise PermissionError('The cost is unknown; approve an explicit maximum before calling this provider.')
            bound = money(max_cost)
            self.ledger.reserve(authorization, operation_id, request_hash, bound, profile, operation)
            reserved = True
        elif max_cost not in (None, 0, '0'):
            raise ValueError('Local inference is not recorded as an external billed request.')
        destination = None
        temporary = None
        connection = None
        headers = {'Content-Type': 'application/json', 'Accept': expected_type,
                   'User-Agent': 'UniversalResearchLibrary/0.2'}
        secret_bytes = None
        started = monotonic()
        try:
            if output is not None:
                if expected_type == 'application/json':
                    raise ProviderError('Chat responses are returned as text; audio output paths are not used for chat.')
                destination = local_path(output, must_exist=False)
                local_path(destination.parent)
                if destination.exists():
                    raise FileExistsError('Provider output already exists; choose another name.')
            if expected_type != 'application/json' and destination is None:
                raise ValueError('Choose a new output file for generated audio.')
            if cancelled():
                raise InterruptedError('Provider operation cancelled before transmission.')
            if profile.credential_ref:
                secret = self.vault.get(profile.id, profile.credential_ref)
                if not isinstance(secret, str) or not secret or any(c in secret for c in '\r\n\x00'):
                    raise ProviderError('The stored provider credential is invalid.')
                if profile.adapter == 'google-translation-v3':
                    try:
                        secret, expires_at = google_oauth_token(secret, profile.endpoint)
                    except ValueError as error:
                        raise ProviderError(str(error)) from None
                    headers['X-goog-user-project'] = google_v3_parent(profile.endpoint).split('/')[1]
                key_header = {'openai-compatible': 'Authorization', 'elevenlabs': 'xi-api-key',
                              'google-translation-v2': 'X-goog-api-key', 'google-translation-v3': 'Authorization'}[profile.adapter]
                headers[key_header] = (
                    'Bearer ' + secret if profile.adapter in ('openai-compatible', 'google-translation-v3') else secret)
                secret_bytes = secret.encode('utf-8')
                del secret
            elif not profile.local:
                raise ProviderError('Reconnect this remote provider account before generation.')
            if cancelled():
                raise InterruptedError('Provider operation cancelled before transmission.')
            cls = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
            options = {'timeout': min(timeout, 30.0)}
            if target.scheme == 'https':
                options['context'] = ssl.create_default_context()
            connection = cls(target.hostname, target.port, **options)
            # No proxy environment or redirects are consulted. Host/TLS authority stays fixed.
            prefix = target.path.rstrip('/')
            if profile.adapter == 'elevenlabs' and not prefix:
                prefix = '/v1'
            sent = True
            method = 'GET' if operation == 'translation.languages' else 'POST'
            connection.request(method, prefix + route, body=None if method == 'GET' else raw, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                if 300 <= response.status < 400:
                    raise ProviderError('The provider attempted a redirect; credentials were not forwarded.')
                raise ProviderError('Provider request failed with HTTP %d. Charge state requires reconciliation; no automatic retry.' % response.status)
            content_type = response.getheader('Content-Type', '').split(';', 1)[0].lower()
            if content_type != expected_type and not (expected_type == 'audio/mpeg' and content_type == 'audio/mp3'):
                raise ProviderError('Provider returned an unexpected content type.')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > max_response_bytes):
                raise ProviderError('Provider response exceeds the approved byte budget.')
            count = 0
            digest = sha256()
            chunks = []
            tail = b''
            stream = None
            if destination:
                stream = NamedTemporaryFile(dir=destination.parent, prefix='.carr-provider-', suffix='.tmp', delete=False)
                temporary = Path(stream.name)
            try:
                while True:
                    if cancelled() or monotonic() - started > timeout:
                        raise InterruptedError('Provider operation cancelled or timed out; charge state may be uncertain.')
                    block = response.read(65536)
                    if not block:
                        break
                    count += len(block)
                    if count > max_response_bytes:
                        raise ProviderError('Provider response exceeded the approved byte budget.')
                    if secret_bytes:
                        inspected = tail + block
                        if secret_bytes in inspected:
                            raise ProviderError('The provider response contained credential material and was discarded.')
                        tail = inspected[-len(secret_bytes):]
                    digest.update(block)
                    if stream:
                        stream.write(block)
                    else:
                        chunks.append(block)
                if count == 0 or (length is not None and int(length) != count):
                    raise ProviderError('Provider returned empty or truncated output.')
                if stream:
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if stream:
                    stream.close()
            usage = None
            result = {'operation_id': operation_id, 'provider': profile.id, 'model': profile.model,
                      'request_sha256': request_hash, 'output_sha256': digest.hexdigest(), 'size': count,
                      'local': profile.local, 'actual_cost': None, 'artifacts': []}
            if expected_type == 'application/json':
                parsed = json.loads(b''.join(chunks))
                if profile.adapter == 'google-translation-v3':
                    if type(parsed) is not dict:
                        raise ProviderError('The Advanced v3 response schema is invalid.')
                    if operation == 'translation.languages':
                        languages = parsed.get('languages')
                        if (not isinstance(languages, list) or not 1 <= len(languages) <= 1000
                                or any(type(row) is not dict or type(row.get('languageCode')) is not str
                                       or not re.fullmatch('[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', row['languageCode'])
                                       or type(row.get('displayName', '')) is not str or len(row.get('displayName', '')) > 512
                                       or type(row.get('supportSource', False)) is not bool
                                       or type(row.get('supportTarget', False)) is not bool for row in languages)
                                or len({row['languageCode'] for row in languages}) != len(languages)):
                            raise ProviderError('The Advanced v3 supported-language response is invalid.')
                        # Omitted proto-JSON booleans mean false, never guessed support.
                        result['languages'] = [{'language': row['languageCode'], 'name': row.get('displayName', ''),
                            'source': row.get('supportSource', False), 'target': row.get('supportTarget', False)} for row in languages]
                    else:
                        glossary = body.get('glossaryConfig')
                        translations = parsed.get('glossaryTranslations' if glossary else 'translations')
                        if (not isinstance(translations, list) or len(translations) != 1
                                or type(translations[0]) is not dict or type(translations[0].get('translatedText')) is not str):
                            raise ProviderError('The requested Advanced v3 translation or glossary result is missing.')
                        row = translations[0]
                        if row.get('model') != body['model']:
                            raise ProviderError('The Advanced v3 response did not verify the requested model and location.')
                        if glossary:
                            observed = row.get('glossaryConfig')
                            if (type(observed) is not dict or observed.get('glossary') != glossary['glossary']
                                    or type(observed.get('ignoreCase', False)) is not bool
                                    or observed.get('ignoreCase', False) != glossary['ignoreCase']):
                                raise ProviderError('The Advanced v3 response did not verify the selected glossary; no fallback was accepted.')
                        elif row.get('glossaryConfig'):
                            raise ProviderError('An unrequested glossary was returned.')
                        # text/plain output is already text. HTML-unescaping would
                        # corrupt literal entities in Advanced v3 source derivatives.
                        result['text'] = text(row['translatedText'], 'translation output', maximum=max_response_bytes)
                        detected = row.get('detectedLanguageCode')
                        if detected == '':
                            detected = None
                        if detected is not None and (not isinstance(detected, str) or not re.fullmatch('[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', detected)):
                            raise ProviderError('The detected translation language is invalid.')
                        result.update(detected_source=detected, reported_model=row['model'], glossary_config=glossary)
                        usage = {'submitted_characters': len(body['contents'][0]),
                                 'translation_sha256': sha256(result['text'].encode('utf-8')).hexdigest(),
                                 'decoded_translation_sha256': translation_receipt_sha256(result['text'], detected)}
                elif profile.adapter == 'google-translation-v2':
                    data = parsed.get('data') if type(parsed) is dict else None
                    if type(data) is not dict:
                        raise ProviderError('The translation response schema is invalid.')
                    if operation == 'translation.languages':
                        languages = data.get('languages')
                        if (not isinstance(languages, list) or not 1 <= len(languages) <= 1000
                                or any(type(row) is not dict or type(row.get('language')) is not str
                                       or not re.fullmatch('[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', row['language'])
                                       or type(row.get('name', '')) is not str or len(row.get('name', '')) > 512
                                       for row in languages)
                                or len({row['language'] for row in languages}) != len(languages)):
                            raise ProviderError('The supported-language response is invalid.')
                        result['languages'] = [{'language': row['language'], 'name': row.get('name', '')} for row in languages]
                    else:
                        translations = data.get('translations')
                        if (not isinstance(translations, list) or len(translations) != 1
                                or type(translations[0]) is not dict or type(translations[0].get('translatedText')) is not str):
                            raise ProviderError('The text translation response is invalid.')
                        decoded = html.unescape(text(translations[0]['translatedText'], 'translation output', maximum=max_response_bytes))
                        result['text'] = text(decoded, 'translation output', maximum=max_response_bytes)
                        detected = translations[0].get('detectedSourceLanguage')
                        if detected is not None and (not isinstance(detected, str) or not re.fullmatch('[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}', detected)):
                            raise ProviderError('The detected translation language is invalid.')
                        result['detected_source'] = detected
                        usage = {'submitted_characters': len(body['q']),
                                 'translation_sha256': sha256(result['text'].encode('utf-8')).hexdigest(),
                                 'decoded_translation_sha256': translation_receipt_sha256(result['text'], detected)}
                else:
                    choices = parsed.get('choices') if type(parsed) is dict else None
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        raise ProviderError('The chat response schema is invalid.')
                    message = choices[0].get('message')
                    if type(message) is not dict or type(message.get('content')) is not str or message.get('tool_calls'):
                        raise ProviderError('The adapter accepts text responses only; unsolicited tool calls were rejected.')
                    result['text'] = text(message['content'], 'provider response text', maximum=max_response_bytes)
                    observed_usage = parsed.get('usage')
                    if isinstance(observed_usage, dict):
                        usage = {k: v for k, v in observed_usage.items()
                                 if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int and v >= 0}
                result['usage'] = usage
                _reject_decoded_credential(result, secret_bytes)
            if destination:
                local_path(destination.parent)
                os.link(temporary, destination)
                result['artifacts'] = [{'path': str(destination), 'sha256': digest.hexdigest(), 'size': count}]
            if reserved:
                self.ledger.finish(authorization.id, operation_id, status='complete', output_hash=digest.hexdigest(), usage=usage)
            return result
        except BaseException as exc:
            if reserved:
                self.ledger.finish(authorization.id, operation_id, status='uncertain' if sent else 'not_sent')
            if isinstance(exc, (PermissionError, FileExistsError, ProviderError, InterruptedError)):
                raise
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ProviderError('Provider communication failed (%s). No credential or response details were logged.' % type(exc).__name__) from None
        finally:
            headers.clear()
            secret_bytes = None
            if connection:
                connection.close()
            if temporary:
                temporary.unlink(missing_ok=True)
