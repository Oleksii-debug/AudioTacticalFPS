"""Fail-closed typed batch plans and fixed local-container boundaries."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import struct

import pytest

from carr_library.audio.batch_models import (
    AudioPreset, BatchItem, BatchState, BatchControl, InputSnapshot,
    RuntimeIdentity, Selection, canonical, output_names,
)
from carr_library.audio.media_conversion import input_arguments, selected_file, reject_external_mov_references


def fixture_state():
    preset = AudioPreset(output_format='wav', sample_rate=8000, channels=1, max_seconds=3, timeout_seconds=10)
    source = InputSnapshot('1' * 32 + '.wav', 64, '2' * 64, 'original.wav')
    item = BatchItem('1' * 32, 1, 'audio', 'Голос 🙂', preset, 0, source,
                     output_names('1' * 32, 1, 'Голос 🙂', preset))
    return BatchState.create('3' * 32, 'Exact queue', RuntimeIdentity('4' * 64, 'ffmpeg version fixture', '5' * 64), (item,))


@pytest.mark.parametrize('change', [
    {'sample_rate': True}, {'sample_rate': 12345}, {'channels': True}, {'channels': 3},
    {'bitrate_kbps': 31}, {'bitrate_kbps': 321}, {'output_format': 'm3u'},
    {'split': 'arbitrary-shell'}, {'max_seconds': float('nan')}, {'max_seconds': 0},
    {'timeout_seconds': float('inf')}, {'timeout_seconds': 0}, {'label': 'line\nbreak'},
    {'output_format': 'opus', 'sample_rate': 8000}, {'output_format': 'wma', 'sample_rate': 44100},
])
def test_invalid_per_item_presets_are_refused_before_native_work(change):
    with pytest.raises(ValueError):
        AudioPreset(**change)


@pytest.mark.parametrize('change', [
    {'kind': 'url'}, {'source': ''}, {'title': '\x00'}, {'audio_stream': True}, {'audio_stream': -1},
    {'audio_stream': 64}, {'kind': 'book', 'source': 'not-a-project'},
    {'kind': 'book', 'source': 'a' * 32, 'audio_stream': 1},
    {'preset': AudioPreset(split='chapters')},
])
def test_selection_requires_explicit_typed_local_scope(change):
    values = {'kind': 'audio', 'source': '/selected/local.wav', 'title': 'Source', 'preset': AudioPreset(), 'audio_stream': 0}
    with pytest.raises(ValueError):
        Selection(**{**values, **change})


def test_full_plan_roundtrip_keeps_sources_names_runtime_and_exact_order():
    state = fixture_state()
    restored = BatchState.from_dict(json.loads(canonical(state.to_dict())))
    assert restored == state
    assert state.plan_sha256 == restored.plan_sha256
    assert state.items[0].outputs[0].startswith('0001-Голос-')
    assert state.items[0].id in state.items[0].outputs[0]
    progressed = replace(state, items=(replace(state.items[0], state='running', attempt=1),), status='running')
    assert progressed.plan_sha256 == state.plan_sha256
    assert progressed.items[0].fingerprint == state.items[0].fingerprint


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(extra=True),
    lambda value: value.update(plan_sha256='0' * 64),
    lambda value: value['items'][0].update(title='Changed after preview'),
    lambda value: value['items'][0]['source'].update(sha256='0' * 64),
    lambda value: value['items'][0]['preset'].update(channels=2),
    lambda value: value['items'][0].update(outputs=['somebody-else.wav']),
    lambda value: value.update(status='complete'),
    lambda value: value['items'][0].update(attempt=True),
])
def test_saved_plan_cannot_silently_change_identity_or_claim_completion(mutation):
    value = json.loads(canonical(fixture_state().to_dict()))
    mutation(value)
    with pytest.raises(ValueError):
        BatchState.from_dict(value)


@pytest.mark.parametrize('title', ['CON', 'same/title', 'Той самий заголовок', 'same:title', '🙂'])
def test_unicode_titles_have_portable_collision_safe_deterministic_names(title):
    preset = AudioPreset(output_format='flac', split='chapters')
    first = output_names('a' * 32, 1, title, preset, 2)
    second = output_names('b' * 32, 2, title, preset, 2)
    assert first == output_names('a' * 32, 1, title, preset, 2)
    assert len(set(name.casefold() for name in (*first, *second))) == 4
    assert all(Path(name).name == name and name.endswith('.flac') for name in first)
    assert first[0].endswith('-chapter-0001.flac') and first[1].endswith('-chapter-0002.flac')


@pytest.mark.parametrize('value', [
    'https://example.invalid/audio.mp3', 'http://example.invalid/a.mp4',
    'file:///private/audio.wav', 'concat:one.mp3|two.mp3', 'pipe:0',
    '//server/share/a.wav', '\\\\server\\share\\a.wav', 'data:audio/wav;base64,AA',
])
def test_urls_protocols_playlists_and_network_roots_are_not_file_selections(value):
    with pytest.raises(ValueError):
        selected_file(value, 'video' if value.endswith('.mp4') else 'audio')


@pytest.mark.parametrize('suffix', ['.m3u', '.m3u8', '.ffconcat', '.pls', '.url', '.txt'])
def test_unreviewed_local_extensions_cannot_select_a_demuxer(tmp_path, suffix):
    source = tmp_path / ('playlist' + suffix)
    source.write_text('https://example.invalid/remote.wav', encoding='utf-8')
    with pytest.raises(ValueError):
        selected_file(source, 'audio')


def box(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def mov_with_reference(flags, target=b''):
    entry = box(b'url ', struct.pack('>I', flags) + target)
    table = box(b'dref', struct.pack('>II', 0, 1) + entry)
    return box(b'ftyp', b'isom\x00\x00\x00\x00isom') + box(
        b'moov', box(b'trak', box(b'mdia', box(b'minf', box(b'dinf', table)))))


def test_mov_self_contained_reference_is_checked_without_opening_a_track(tmp_path):
    source = tmp_path / 'self-contained.mp4'
    source.write_bytes(mov_with_reference(1))
    before = source.read_bytes()
    reject_external_mov_references(source)
    assert source.read_bytes() == before


@pytest.mark.parametrize('payload', [
    mov_with_reference(0, b'https://example.invalid/private.wav\x00'),
    mov_with_reference(0, b'../other.wav\x00'),
    box(b'moov', box(b'rmra', b'')),
    box(b'moov', box(b'cmov', b'')),
    b'\x00\x00\x00\x04moov',
    b'\x00\x00\x00\x01moov',
])
def test_external_or_ambiguous_mov_references_are_refused_before_ffmpeg(tmp_path, payload):
    source = tmp_path / 'unreviewed.mp4'
    source.write_bytes(payload)
    with pytest.raises(ValueError):
        reject_external_mov_references(source)


def test_fixed_mov_arguments_disable_external_tracks_and_keep_one_input():
    args = input_arguments(Path('/explicit/local movie.mp4'), 'mov')
    assert args[args.index('-protocol_whitelist') + 1] == 'file'
    assert args[args.index('-format_whitelist') + 1] == 'mov'
    assert args[args.index('-enable_drefs') + 1] == '0'
    assert args[args.index('-use_absolute_path') + 1] == '0'
    assert args.count('-i') == 1 and args[-1] == str(Path('/explicit/local movie.mp4').absolute())


@pytest.mark.parametrize('change', [{'action': 'resume-and-delete'}, {'sequence': -1}, {'sequence': True}, {'batch_id': 'foreign'}])
def test_control_records_are_typed_monotonic_intents(change):
    with pytest.raises(ValueError):
        BatchControl(**{'batch_id': 'a' * 32, **change})
