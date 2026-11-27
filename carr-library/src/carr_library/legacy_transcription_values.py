"""Pure values for the archived canonical transcription protocol.

Reused from canonical e69ba240; these validators and serializers only authenticate
historical data. They never select a runtime, execute inference or grant current
completion authority. The active supplied-worker protocol remains separate.
"""
from hashlib import sha256
import json
import math
import re

from pathlib import PurePosixPath, PureWindowsPath


def encode(value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')) + '\n').encode('utf-8')
    if len(raw) > 32 * 1024**2:
        raise ValueError('Historical transcription JSON exceeds its byte budget.')
    return raw

FORMATS = ('txt', 'srt', 'vtt', 'json')
AUDIO_EXTENSIONS = frozenset(('.mp3', '.wav', '.m4a', '.mp4', '.ogg', '.opus', '.flac',
                             '.aac', '.wma', '.webm', '.mkv', '.3gp', '.amr', '.m4b'))
MAX_RESULT_BYTES = 32 * 1024**2 - 65536
MAX_SEGMENTS = 250000
DEFAULT_SETTINGS = {
    'language': 'auto', 'task': 'transcribe', 'device': 'cpu', 'compute_type': 'int8',
    'beam_size': 1, 'parallel_files': 1, 'vad_filter': True,
    'condition_on_previous_text': True, 'multilingual': False,
    'output_txt': True, 'output_json': True, 'output_srt': False, 'output_vtt': False,
    'include_headers': False, 'output_mode': 'separate',
}


def fingerprint(value):
    return sha256(encode(value)).hexdigest()


def settings(value=None):
    if value is not None and (type(value) is not dict or set(value) - set(DEFAULT_SETTINGS)):
        raise ValueError('Unsupported transcription settings.')
    result = {**DEFAULT_SETTINGS, **(value or {})}
    if result['language'] is None:
        result['language'] = 'auto'
    if not isinstance(result['language'], str) or not re.fullmatch(r'auto|[a-z]{2,3}', result['language']):
        raise ValueError('Choose auto or an explicit two/three-letter language code.')
    if result['task'] not in ('transcribe', 'translate'):
        raise ValueError('The worker supports transcription or translation into English only.')
    if result['device'] not in ('cpu', 'cuda'):
        raise ValueError('Choose CPU or CUDA explicitly; no automatic device substitution.')
    if result['compute_type'] not in ('int8', 'int8_float16', 'int8_float32', 'float16', 'float32'):
        raise ValueError('Choose an explicit supported compute type.')
    if result['device'] == 'cpu' and result['compute_type'] in ('float16', 'int8_float16'):
        raise ValueError('Float16 computation requires an explicitly selected CUDA device.')
    for name, maximum in (('beam_size', 20), ('parallel_files', 4)):
        if type(result[name]) is not int or not 1 <= result[name] <= maximum:
            raise ValueError(f'{name} must be an integer from 1 to {maximum}.')
    for name in ('vad_filter', 'condition_on_previous_text', 'multilingual', 'include_headers',
                 'output_txt', 'output_srt', 'output_vtt', 'output_json'):
        if type(result[name]) is not bool:
            raise ValueError(f'{name} must be boolean.')
    if result['output_mode'] != 'separate':
        raise ValueError('The workstation preserves a separate result for each retained source.')
    if not any(result['output_' + name] for name in FORMATS):
        raise ValueError('Select at least one transcript export format.')
    return result


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30 * 86400:
        raise ValueError('The transcription worker returned an invalid timestamp.')
    return float(value)


def _text(value, maximum=100000):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValueError('The transcription worker returned invalid or excessive text.')
    value.encode('utf-8', 'strict')
    return value.strip()


def validated_result(meta, transcript):
    if type(meta) is not dict or not isinstance(meta.get('segments'), list):
        raise ValueError('A structured segment result is required.')
    language = meta.get('detected_language', meta.get('language'))
    if not isinstance(language, str) or not re.fullmatch('[a-z]{2,3}', language):
        raise ValueError('The worker did not supply a valid detected language.')
    duration = _seconds(meta.get('duration'))
    if len(meta['segments']) > MAX_SEGMENTS:
        raise ValueError('The transcript exceeds its segment budget.')
    segments, previous = [], 0.0
    for raw in meta['segments']:
        if type(raw) is not dict:
            raise ValueError('Invalid transcript segment.')
        start, end = _seconds(raw.get('start')), _seconds(raw.get('end'))
        if start < previous or end < start or end > duration + 1:
            raise ValueError('The worker returned inconsistent segment timestamps.')
        previous = start
        words = raw.get('words', [])
        if not isinstance(words, list) or len(words) > 10000:
            raise ValueError('The transcript exceeds its word timestamp budget.')
        accepted_words, previous_word = [], start
        for word in words:
            if type(word) is not dict:
                raise ValueError('Invalid transcript word.')
            beginning, ending = _seconds(word.get('start')), _seconds(word.get('end'))
            if beginning < previous_word or ending < beginning or ending > end + .01:
                raise ValueError('The worker returned inconsistent word timestamps.')
            previous_word = beginning
            accepted_words.append({'start': beginning, 'end': ending, 'word': _text(word.get('word'))})
        segments.append({'start': start, 'end': end, 'text': _text(raw.get('text')), 'words': accepted_words})
    text = '\n'.join(segment['text'] for segment in segments if segment['text'])
    if not isinstance(transcript, str) or transcript.strip() != text or meta.get('text') != text:
        raise ValueError('Transcript bytes do not match the structured segments.')
    result = {'language': language, 'duration': duration, 'segments': segments, 'text': text}
    if len(encode(result)) > MAX_RESULT_BYTES:
        raise ValueError('The transcript exceeds its result byte budget.')
    return result


def timestamp(value, *, vtt=False):
    # Adapted from the reviewed public PR136 formatter: round once, then divmod.
    milliseconds = round(_seconds(value) * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}{"." if vtt else ","}{milliseconds:03d}'


def encoded_outputs(result, provenance):
    srt, vtt = [], ['WEBVTT', '']
    for index, segment in enumerate((s for s in result['segments'] if s['text']), 1):
        srt.extend((str(index), f"{timestamp(segment['start'])} --> {timestamp(segment['end'])}", segment['text'], ''))
        vtt.extend((f"{timestamp(segment['start'], vtt=True)} --> {timestamp(segment['end'], vtt=True)}", segment['text'], ''))
    return {
        'txt': (result['text'] + ('\n' if result['text'] else '')).encode('utf-8'),
        'srt': ('\n'.join(srt) + ('\n' if srt else '')).encode('utf-8'),
        'vtt': ('\n'.join(vtt) + '\n').encode('utf-8'),
        'json': (json.dumps({'schema': 'resonance-transcript/1', **result, 'provenance': provenance},
                            ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8'),
    }


def fields(value, required, optional=()):
    if (type(value) is not dict or not set(required) <= value.keys()
            or not value.keys() <= set(required) | set(optional)):
        raise ValueError('Unknown or missing checkpoint fields.')


def text(value, maximum=32768, *, empty=False):
    if (type(value) is not str or len(value) > maximum or '\x00' in value
            or (not empty and not value.strip())):
        raise ValueError('Invalid checkpoint text.')
    value.encode('utf-8', 'strict')


def number(value, minimum=0, maximum=1e18, *, integer=False):
    if (type(value) not in ((int,) if integer else (int, float))
            or not minimum <= value <= maximum or not math.isfinite(value)):
        raise ValueError('Invalid checkpoint number.')


def digest(value):
    if type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Invalid checkpoint digest.')


def path(value):
    """Check inert cross-platform references without consulting this machine."""
    text(value)
    normalized = value.replace('\\', '/')
    without_drive = value[2:] if re.match(r'^[A-Za-z]:', value) else value
    if (normalized.startswith('//') or '://' in value or ':' in without_drive
            or any(ord(c) < 32 for c in value)
            or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())):
        raise ValueError('Invalid local checkpoint reference.')


