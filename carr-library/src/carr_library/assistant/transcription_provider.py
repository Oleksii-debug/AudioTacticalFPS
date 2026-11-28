"""One documented optional STT contract over the existing provider gateway.

Official protocol: https://elevenlabs.io/docs/api-reference/speech-to-text/convert
Only an explicitly selected retained local file is uploaded. URL inputs,
webhooks, speaker identification, extra paid edits and automatic retries are
not exposed. This adapter performs no local ASR and claims no acoustic quality.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re

from ..durable_state import local_path, regular
from ..catalog.intake import _open_file_source, _stream_change_time
from ..integrations.transcription_values import validated_result
from .models import canonical

OPERATION = 'speech.transcribe'
PROTOCOL = 'elevenlabs-stt-v1'
MODEL = 'scribe_v2'
MAX_INPUT_BYTES = 24 * 1024**2
MAX_RESPONSE_BYTES = 8 * 1024**2
MAX_WORDS = 10000
MAX_TEXT_CHARACTERS = 100000
SUFFIXES = frozenset(('.wav', '.flac', '.mp3', '.m4a', '.ogg', '.opus',
                     '.aac', '.wma', '.webm', '.mp4', '.mkv'))
RETENTION = ('The selected recording is sent to ElevenLabs under your own account. '
             'Its normal service retention applies; enterprise zero-retention is not enabled. '
             'No model inference quality or language coverage is certified by this adapter.')
CAPABILITY = {
    'schema': 'resonance-provider-capability/1', 'operation': OPERATION,
    'adapter': 'elevenlabs', 'model': MODEL,
    'endpoint': 'https://api.elevenlabs.io/v1/speech-to-text',
    'method': 'POST', 'request': 'multipart/form-data; one file',
    'formats': sorted(SUFFIXES), 'max_input_bytes': MAX_INPUT_BYTES,
    'max_response_bytes': MAX_RESPONSE_BYTES, 'max_words': MAX_WORDS,
    'max_text_characters': MAX_TEXT_CHARACTERS,
    'language': 'auto or an explicitly selected ISO-639-1/3 code; actual vendor support is dynamic',
    'timestamps': 'provider word timestamps; no independent alignment or acoustic qualification',
    'streaming': False, 'webhooks': False, 'automatic_retry': False,
    'idempotency': 'local exact request ledger; uncertain sent calls are never automatically retried',
    'cost': 'explicit local ledger reservation; not a reported price or a provider-enforced billing cap',
    'retention': RETENTION,
}


def _stop(cancelled):
    if cancelled():
        raise InterruptedError('Selected audio verification was cancelled.')


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_audio(path, *, expected=None, cancelled=lambda: False):
    """Read bounded exact bytes through the current stable-source primitive."""
    _stop(cancelled)
    selected = local_path(path, exists=True)
    suffix = selected.suffix.lower()
    before = regular(selected)
    if suffix not in SUFFIXES or not 0 < before.st_size <= MAX_INPUT_BYTES:
        raise ValueError('Choose a supported local audio/video file up to 24 MiB.')
    if expected is not None and (type(expected) is not dict
            or set(expected) != {'sha256', 'size_bytes', 'suffix'}
            or type(expected['size_bytes']) is not int or type(expected['sha256']) is not str
            or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])
            or expected['suffix'] != suffix or expected['size_bytes'] != before.st_size):
        raise ValueError('Selected audio differs from its reviewed identity.')
    blocks, count, digest = [], 0, sha256()
    with _open_file_source(selected) as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Selected audio changed while opening.')
        changed = _stream_change_time(stream)
        while True:
            _stop(cancelled)
            block = stream.read(min(1024**2, before.st_size - count + 1))
            if not block:
                break
            count += len(block)
            if count > before.st_size or count > MAX_INPUT_BYTES:
                raise ValueError('Selected audio changed or exceeded its byte limit.')
            digest.update(block)
            blocks.append(block)
        _stop(cancelled)
        if (count != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before)
                or _stamp(regular(local_path(selected, exists=True))) != _stamp(before)
                or _stream_change_time(stream) != changed):
            raise ValueError('Selected audio changed during its stable read.')
    result = {'sha256': digest.hexdigest(), 'size_bytes': count, 'suffix': suffix}
    if expected is not None and result != expected:
        raise ValueError('Selected audio bytes differ from the approved recording.')
    return selected, result, b''.join(blocks)


@dataclass(frozen=True)
class AudioUpload:
    """A source route, never serialized in a provider payload or a credential."""
    path: Path

    def read(self, expected, *, cancelled):
        return read_audio(self.path, expected=expected, cancelled=cancelled)[2]


def request(profile, payload):
    if (profile.adapter != 'elevenlabs' or profile.model != MODEL
            or profile.capabilities != (OPERATION,) or profile.local or profile.voice is not None):
        raise ValueError('Choose the dedicated ElevenLabs scribe_v2 transcription profile.')
    if type(payload) is not dict or set(payload) != {'audio', 'language'}:
        raise ValueError('Transcription accepts one exact selected audio identity and language.')
    source = payload['audio']
    if (type(source) is not dict or set(source) != {'sha256', 'size_bytes', 'suffix'}
            or type(source['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', source['sha256'])
            or type(source['size_bytes']) is not int or not 0 < source['size_bytes'] <= MAX_INPUT_BYTES
            or source['suffix'] not in SUFFIXES):
        raise ValueError('Invalid bounded selected audio identity.')
    language = payload['language']
    if type(language) is not str or not re.fullmatch('auto|[a-z]{2,3}', language):
        raise ValueError('Choose auto or an ISO-639-1/3 language code.')
    fields = {'model_id': MODEL, 'timestamps_granularity': 'word',
              'diarize': False, 'tag_audio_events': False,
              'use_multi_channel': False, 'webhook': False,
              'no_verbatim': False, 'file_format': 'other'}
    if language != 'auto':
        fields['language_code'] = language
    # The canonical approval includes exact file bytes, every transmitted field
    # and the disclosed query setting. The filename is deliberately generic.
    body = {'schema': PROTOCOL, 'audio': dict(source), 'fields': fields,
            'enable_logging': True}
    return '/speech-to-text?enable_logging=true', body, 'application/json'


def multipart(body, upload, *, cancelled):
    if not isinstance(upload, AudioUpload):
        raise ValueError('An explicitly selected audio upload is required.')
    raw = upload.read(body['audio'], cancelled=cancelled)
    marker = ('----resonance-stt-' + sha256(canonical(body).encode('utf-8')).hexdigest()).encode('ascii')
    if marker in raw:
        raise ValueError('The selected recording conflicts with the reviewed multipart framing.')
    chunks = []
    for name, value in sorted(body['fields'].items()):
        encoded = ('true' if value is True else 'false' if value is False else value).encode('ascii')
        chunks.extend((b'--' + marker + b'\r\n',
            b'Content-Disposition: form-data; name="' + name.encode('ascii') + b'"\r\n\r\n',
            encoded, b'\r\n'))
    filename = ('selected-audio' + body['audio']['suffix']).encode('ascii')
    chunks.extend((b'--' + marker + b'\r\n',
        b'Content-Disposition: form-data; name="file"; filename="' + filename + b'"\r\n',
        b'Content-Type: application/octet-stream\r\n\r\n', raw, b'\r\n--' + marker + b'--\r\n'))
    result = b''.join(chunks)
    _stop(cancelled)
    if len(result) > MAX_INPUT_BYTES + 16384:
        raise ValueError('The approved upload exceeded its finite multipart allowance.')
    return result, 'multipart/form-data; boundary=' + marker.decode('ascii')


def response(value):
    """Map only documented synchronous text/timestamps to the current model."""
    if type(value) is not dict or not {'language_code', 'language_probability', 'text', 'words'} <= value.keys():
        raise ValueError('The provider did not return a synchronous timestamped transcript.')
    if any(value.get(name) not in (None, [], {}) for name in (
            'transcripts', 'additional_formats', 'entities', 'edited_transcript')):
        raise ValueError('The provider returned unrequested multichannel, exported or edited content.')
    if value.get('channel_index') is not None:
        raise ValueError('The selected request did not authorize a separate channel transcript.')
    language, probability, raw_text = value['language_code'], value['language_probability'], value['text']
    if (type(language) is not str or not re.fullmatch('[a-z]{2,3}', language)
            or type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1
            or type(raw_text) is not str or '\x00' in raw_text
            or len(raw_text) > MAX_TEXT_CHARACTERS or len(raw_text.encode('utf-8')) > 2 * 1024**2):
        raise ValueError('The provider returned an invalid language or transcript.')
    rows = value['words']
    if type(rows) is not list or len(rows) > MAX_WORDS * 2:
        raise ValueError('The transcript exceeds its bounded token allowance.')
    words, previous, finish = [], 0.0, 0.0
    for row in rows:
        if type(row) is not dict or row.get('type') not in ('word', 'spacing'):
            raise ValueError('The provider returned an unrequested audio-event or invalid word.')
        text = row.get('text')
        if type(text) is not str or '\x00' in text or len(text) > 4096:
            raise ValueError('The provider returned an invalid word.')
        if row.get('channel_index') is not None:
            raise ValueError('The request did not authorize multichannel words.')
        if row['type'] == 'spacing':
            if not text or not text.isspace():
                raise ValueError('A spacing token contains transcript content.')
            continue
        start, end = row.get('start'), row.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous or not 0 <= start <= end <= 30 * 86400 or not text.strip()):
            raise ValueError('The provider omitted or contradicted the requested word timestamps.')
        previous, finish = float(start), max(finish, float(end))
        words.append({'start': float(start), 'end': float(end), 'word': text.strip()})
    if len(words) > MAX_WORDS:
        raise ValueError('The transcript exceeds its word timestamp allowance.')
    text = raw_text.strip()
    if bool(text) != bool(words):
        raise ValueError('The requested transcript text and timed words are incomplete.')
    duration = value.get('audio_duration_secs')
    duration_basis = 'provider reported audio duration'
    if duration is None:
        duration, duration_basis = finish, 'end of provider reported word timeline; source duration not measured'
    if (type(duration) not in (int, float) or not math.isfinite(duration)
            or not finish <= duration <= 30 * 86400):
        raise ValueError('The provider returned an inconsistent audio duration.')
    segments = ([{'start': words[0]['start'], 'end': finish, 'text': text, 'words': words}]
                if words else [])
    normalized = validated_result({'language': language, 'duration': duration,
        'segments': segments, 'text': text}, text)
    reference = value.get('transcription_id')
    if reference is not None and (type(reference) is not str or not re.fullmatch('[A-Za-z0-9_.:-]{1,256}', reference)):
        raise ValueError('The provider returned an invalid transcript reference.')
    return dict(transcript=normalized, text=normalized['text'],
        provider_transcript_id=reference, detected_language_probability=float(probability),
        provider_text_sha256=sha256(raw_text.encode('utf-8')).hexdigest(),
        duration_basis=duration_basis, timestamp_source='provider reported; not independently aligned',
        text_normalization='existing transcription model trims outer segment whitespace')
