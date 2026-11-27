"""Passive adapters for completed primary-Studio cache receipts and media.

These checks authenticate retained bytes and their producer receipt. They do
not launch a decoder, execute a provider, or qualify perceptual audio quality.
"""
import math
from pathlib import PurePosixPath
import re

from .ingest.restore import RestoreError

PREFIX = 'state/studio/cache/'
MAX_BYTES = 32 * 1024**3
RECEIPT_BYTES = 16 * 1024
_KEY = r'[0-9a-f]{64}'
_RECEIPT = re.compile(PREFIX + '(' + _KEY + r')(-encoded)?\.json\Z')
_MEDIA = re.compile(PREFIX + '(' + _KEY + r')(?:-([0-9a-f]{10}))?\.(wav|flac|mp3|ogg|opus|m4a|m4b|aiff|wma)\Z')


def limit(name):
    if _RECEIPT.fullmatch(name):
        return RECEIPT_BYTES
    if _MEDIA.fullmatch(name):
        return MAX_BYTES
    return None


def is_media(name):
    return _MEDIA.fullmatch(name) is not None


def validate_receipt(name, value):
    match = _RECEIPT.fullmatch(name)
    if not match or type(value) is not dict:
        raise RestoreError('Invalid generated-audio receipt.')
    key, encoded = match.groups()
    fields = {'sha256', 'file'} if encoded else {
        'schema', 'key', 'file', 'sha256', 'duration', 'sample_rate', 'channels'}
    if set(value) != fields or type(value.get('file')) is not str:
        raise RestoreError('Invalid generated-audio receipt fields.')
    target = PREFIX + value['file']
    media = _MEDIA.fullmatch(target)
    if (not media or media.group(1) != key or
            not re.fullmatch(_KEY, value.get('sha256', '') if type(value.get('sha256')) is str else '')):
        raise RestoreError('Generated-audio receipt does not bind its exact cache file.')
    if encoded:
        if media.group(3) == 'wav':
            raise RestoreError('An encoded-audio receipt needs an encoded format.')
    else:
        if (type(value['schema']) is not int or value['schema'] != 1 or value['key'] != key
                or media.group(2) is None or media.group(3) != 'wav'
                or type(value['duration']) not in (int, float)
                or not 0 < value['duration'] <= MAX_BYTES or not math.isfinite(value['duration'])
                or type(value['sample_rate']) is not int or not 8000 <= value['sample_rate'] <= 384000
                or type(value['channels']) is not int or value['channels'] not in (1, 2)):
            raise RestoreError('Invalid completed PCM receipt.')
    return target


def validate_container(name, header, size):
    """Bounded signature checks; decoding remains the audio runtime's job."""
    suffix = PurePosixPath(name).suffix
    valid = False
    if suffix == '.wav':
        valid = size >= 44 and header[:4] in (b'RIFF', b'RF64') and header[8:12] == b'WAVE'
    elif suffix == '.flac':
        valid = size >= 42 and header.startswith(b'fLaC')
    elif suffix == '.mp3':
        valid = size >= 10 and (header.startswith(b'ID3') or
                               len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0)
    elif suffix in ('.ogg', '.opus'):
        valid = size >= 27 and header.startswith(b'OggS\x00')
    elif suffix in ('.m4a', '.m4b'):
        valid = size >= 16 and header[4:8] == b'ftyp' and 8 <= int.from_bytes(header[:4], 'big') <= size
    elif suffix == '.aiff':
        valid = size >= 12 and header.startswith(b'FORM') and header[8:12] in (b'AIFF', b'AIFC')
    elif suffix == '.wma':
        valid = size >= 30 and header.startswith(bytes.fromhex('3026b2758e66cf11a6d900aa0062ce6c'))
    if not valid:
        raise RestoreError('Generated audio does not match its declared container.')


def validate_collection(entries, receipts):
    files = {entry['filename']: entry for entry in entries}
    audio = {name for name in files if is_media(name)}
    expected_receipts = {name for name in files if _RECEIPT.fullmatch(name)}
    if set(receipts) != expected_receipts:
        raise RestoreError('Generated-audio receipts are incomplete.')
    referenced = set()
    for name, value in receipts.items():
        target = validate_receipt(name, value)
        if (target not in audio or target in referenced
                or files[target]['sha256'] != value['sha256']):
            raise RestoreError('Generated-audio receipt and retained bytes disagree.')
        referenced.add(target)
    if audio != referenced:
        raise RestoreError('Generated audio requires its completed producer receipt.')
