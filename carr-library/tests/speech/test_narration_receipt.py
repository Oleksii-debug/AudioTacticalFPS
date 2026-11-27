"""Portable WAV provenance: actual PCM, bounded parsing and inert source identity."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import json
import struct
import wave

import pytest

from carr_library.durable_state import encode
from carr_library.speech import narration_receipt as receipt
from carr_library.speech.runtime import SpeechSettings, Voice


def hash_bytes(value):
    return sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


class Snapshot:
    """Explicit synthetic reading interface, not library authorization."""
    source_text = 'Оригінал 🚂.\n\nŽltý e\u0301.'
    text = 'Edited 🚂.\n\nŽltý e\u0301.'
    revision = 1
    source_sha256 = hash_bytes(source_text)

    def __init__(self, bound=True):
        first, second = self.source_text.split('\n\n')
        self.source_anchor = ({'workspace': 'workspace-1', 'edition': 'edition-1',
            'revision': 'revision-1', 'asset_sha256': hash_bytes(b'original asset'),
            'quotation_authority': False, 'positions': [
                {'ordinal': 1, 'kind': 'physical_page', 'index': 2, 'start': 0,
                 'end': len(first), 'text_sha256': hash_bytes(first),
                 'text_verified_at_capture': False},
                {'ordinal': 2, 'kind': 'physical_page', 'index': 4,
                 'start': len(first) + 2, 'end': len(self.source_text),
                 'text_sha256': hash_bytes(second), 'text_verified_at_capture': True}]}
                 if bound else None)

    def source_span(self, start, end):
        return SimpleNamespace(start=0, end=len(self.source_text), exact=False)

    def to_dict(self):
        return {'source_text': self.source_text, 'text': self.text, 'revision': self.revision,
                'source_anchor': deepcopy(self.source_anchor)}


def plain_wave(path, *, channels=1, width=2, frames=5):
    data = bytes((i * 29 + 3) % 256 for i in range(channels * width * frames))
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(22050)
        audio.writeframes(data)
    return data


def add_receipt(path, snapshot, pcm, *, settings=None):
    selected = receipt.capture_narration(snapshot, 0, len(snapshot.text))
    return receipt.append_narration_receipt(path, selected,
        Voice('private-local-voice-id', 'Fixture', 'fixture', 'uk'), settings or SpeechSettings(),
        pcm_sha256=hash_bytes(pcm), runtime_sha256=hash_bytes('fixture runtime'))


@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('channels', [1, 2])
@pytest.mark.parametrize('width', [1, 2, 3, 4])
@pytest.mark.parametrize('frames', [5, 6])
def test_receipt_roundtrip_preserves_pcm_and_unicode_identity(tmp_path, bound, channels, width, frames):
    path = tmp_path / 'озвучення.wav'
    snapshot = Snapshot(bound)
    pcm = plain_wave(path, channels=channels, width=width, frames=frames)
    record = add_receipt(path, snapshot, pcm)
    assert receipt.read_narration_receipt(path, document=snapshot) == record
    with wave.open(str(path), 'rb') as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) == (channels, width, frames)
        assert audio.readframes(frames) == pcm
    raw = path.read_bytes()
    assert snapshot.source_text.encode() not in raw and snapshot.text.encode() not in raw
    assert b'private-local-voice-id' not in raw
    assert record['narration']['source_anchor'] == snapshot.source_anchor
    assert record['narration']['quotation_authority'] is False
    assert record['pcm']['sha256'] == hash_bytes(pcm)
    assert 'never authorizes a quotation' in receipt.describe_narration_receipt(record)
    if bound:
        assert 'Captured snapshot PDF pages (2): 2, 4.' in receipt.describe_narration_receipt(record)
        assert 'indices 2 to 4' not in receipt.describe_narration_receipt(record)
        assert 'printed page labels are not attested' in receipt.describe_narration_receipt(record)


@pytest.mark.parametrize('field,value', [
    ('quotation_authority', True), ('source_sha256', 'z' * 64),
    ('source_characters', True), ('reading_revision', 501), ('reading_characters', 0),
    ('reading_project_sha256', None), ('reading_text_sha256', '0' * 63),
    ('selection', {'start': 1, 'end': 1, 'text_sha256': '0' * 64}),
    ('selection', {'start': False, 'end': 2, 'text_sha256': '0' * 64}),
    ('source_span', {'start': None, 'end': 2, 'exact': False}),
    ('source_span', {'start': 0, 'end': 1, 'exact': True}),
    ('unexpected', 'not in schema')])
def test_invalid_snapshot_receipt_is_rejected(field, value):
    snapshot = Snapshot()
    record = receipt.capture_narration(snapshot, 0, len(snapshot.text))
    record[field] = value
    with pytest.raises(ValueError):
        receipt.validate_narration(record, snapshot.text)


@pytest.mark.parametrize('change', ['pcm-byte', 'riff-length', 'format-tag', 'rate', 'data-length',
                                   'receipt-tag', 'receipt-length', 'metadata-byte', 'extra-chunk'])
def test_modified_wav_rejected_after_reopen(tmp_path, change):
    path = tmp_path / 'audio.wav'
    pcm = plain_wave(path)
    add_receipt(path, Snapshot(), pcm)
    raw = bytearray(path.read_bytes())
    offset = 44 + len(pcm)
    if change == 'pcm-byte':
        raw[44] ^= 1
    elif change == 'riff-length':
        raw[4] ^= 1
    elif change == 'format-tag':
        raw[20] = 3
    elif change == 'rate':
        struct.pack_into('<I', raw, 24, 16000)
        struct.pack_into('<I', raw, 28, 32000)
    elif change == 'data-length':
        struct.pack_into('<I', raw, 40, len(pcm) + 2)
    elif change == 'receipt-tag':
        raw[offset:offset+4] = b'JUNK'
    elif change == 'receipt-length':
        struct.pack_into('<I', raw, offset + 4, receipt.MAX_RECEIPT_BYTES + 1)
    elif change == 'metadata-byte':
        raw[offset + 10] ^= 1
    else:
        raw.extend(b'JUNK\0\0\0\0')
        struct.pack_into('<I', raw, 4, len(raw) - 8)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        receipt.read_narration_receipt(path)


@pytest.mark.parametrize('change', ['authority', 'notice', 'geometry', 'voice', 'settings',
                                   'ordinal', 'printed-label', 'extra-field'])
def test_resealed_invalid_metadata_remains_inert_and_is_rejected(tmp_path, change):
    path = tmp_path / 'audio.wav'
    pcm = plain_wave(path)
    record = add_receipt(path, Snapshot(), pcm)
    if change == 'authority':
        record['narration']['quotation_authority'] = True
    elif change == 'notice':
        record['notice'] = 'Verified scientific quotation.'
    elif change == 'geometry':
        record['pcm']['frames'] += 1
    elif change == 'voice':
        record['voice']['runtime_sha256'] = 'not a digest'
    elif change == 'settings':
        record['settings']['rate'] = True
    elif change == 'ordinal':
        record['narration']['source_anchor']['positions'][1]['ordinal'] = 1
    elif change == 'printed-label':
        record['narration']['source_anchor']['positions'][0]['printed'] = 'xvii'
    else:
        record['extra'] = 'unexpected'
    metadata = encode({'record': record, 'sha256': hash_bytes(encode(record))})
    raw = bytearray(path.read_bytes()[:44 + len(pcm)])
    raw.extend(struct.pack('<4sI', b'rsrc', len(metadata)))
    raw.extend(metadata)
    if len(metadata) & 1:
        raw.extend(b'\0')
    struct.pack_into('<I', raw, 4, len(raw) - 8)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        receipt.read_narration_receipt(path)


@pytest.mark.parametrize('frames', [5, 6])
def test_plain_wave_is_not_reclassified_as_provenance(tmp_path, frames):
    path = tmp_path / 'legacy.wav'
    plain_wave(path, width=1, frames=frames)
    assert receipt.read_narration_receipt(path) is None
    assert 'no Resonance' in receipt.describe_narration_receipt(None)


@pytest.mark.parametrize('change', ['edition', 'edited-text'])
def test_same_pcm_cannot_retarget_saved_reading_snapshot(tmp_path, change):
    path = tmp_path / 'audio.wav'
    snapshot = Snapshot()
    add_receipt(path, snapshot, plain_wave(path))
    altered = Snapshot()
    if change == 'edition':
        altered.source_anchor['edition'] = 'different-edition'
    else:
        altered.text = altered.text.replace('Edited', 'EdiTed')
    with pytest.raises(ValueError, match='reading project differs'):
        receipt.read_narration_receipt(path, document=altered)


def test_second_receipt_never_overwrites_first(tmp_path):
    path = tmp_path / 'audio.wav'
    pcm = plain_wave(path)
    add_receipt(path, Snapshot(), pcm)
    saved = path.read_bytes()
    with pytest.raises(ValueError, match='only be attached once'):
        add_receipt(path, Snapshot(False), pcm)
    assert path.read_bytes() == saved


def test_receipt_budget_counts_audio_and_metadata(tmp_path):
    path = tmp_path / 'audio.wav'
    pcm = plain_wave(path, frames=1700)
    prior = path.read_bytes()
    with pytest.raises(ValueError, match='audio-output budget'):
        add_receipt(path, Snapshot(), pcm, settings=SpeechSettings(max_output_bytes=4096))
    assert path.read_bytes() == prior


def test_receipt_inspection_cancellation_preserves_bytes(tmp_path):
    path = tmp_path / 'audio.wav'
    pcm = plain_wave(path)
    add_receipt(path, Snapshot(), pcm)
    prior = path.read_bytes()
    with pytest.raises(InterruptedError):
        receipt.read_narration_receipt(path, cancelled=lambda: True)
    assert path.read_bytes() == prior


def test_wrong_pcm_hash_refuses_attachment(tmp_path):
    path = tmp_path / 'audio.wav'
    pcm = plain_wave(path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='PCM changed'):
        add_receipt(path, Snapshot(), pcm + b'wrong')
    assert path.read_bytes() == before


@pytest.mark.parametrize('span,expected', [
    (SimpleNamespace(start=0, end=2, exact=True), 'exact snapshot source offsets 0 to 2'),
    (SimpleNamespace(start=0, end=7, exact=False), 'approximate snapshot source offsets 0 to 7'),
    (SimpleNamespace(start=None, end=None, exact=False), 'no mapped source offsets'),
])
def test_description_distinguishes_snapshot_from_selection(tmp_path, span, expected):
    path = tmp_path / 'selection.wav'
    snapshot = Snapshot()
    snapshot.source_span = lambda start, end: span
    pcm = plain_wave(path)
    selection = receipt.capture_narration(snapshot, 0, 2)
    receipt.append_narration_receipt(path, selection,
        Voice('private-local-voice-id', 'Fixture', 'fixture', 'uk'),
        SpeechSettings(), pcm_sha256=hash_bytes(pcm))
    description = receipt.describe_narration_receipt(
        receipt.read_narration_receipt(path, document=snapshot))
    assert 'Captured snapshot PDF pages (2): 2, 4.' in description
    assert 'not necessarily the narrated selection' in description
    assert expected in description
    assert 'never authorizes a quotation' in description


def test_description_bounds_long_logical_snapshot_without_inventing_range(tmp_path):
    path = tmp_path / 'logical.wav'
    snapshot = Snapshot()
    snapshot.source_text = snapshot.text = '\n\n'.join('x' for _ in range(20))
    snapshot.source_sha256 = hash_bytes(snapshot.source_text)
    snapshot.source_anchor['positions'] = [
        {'ordinal': n + 1, 'kind': 'logical_segment', 'index': 2 * n + 1,
         'start': 3 * n, 'end': 3 * n + 1, 'text_sha256': hash_bytes('x'),
         'text_verified_at_capture': False} for n in range(20)]
    add_receipt(path, snapshot, plain_wave(path))
    description = receipt.describe_narration_receipt(
        receipt.read_narration_receipt(path, document=snapshot))
    assert 'Captured snapshot logical segments (20): 1, 3, 5, 7' in description
    assert 'and 4 more (listed in the receipt)' in description
    assert 'PDF pages' not in description
    assert 'indices 1 to 39' not in description
