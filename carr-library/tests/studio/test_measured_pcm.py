"""Actual bounded RIFF/RF64 bytes and cancellation, independent of a codec process."""
from hashlib import sha256
from io import BytesIO
import struct
import wave

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.studio import measured_audio as measured


def riff(channels=1, width=2, frames=9):
    target = BytesIO()
    with wave.open(target, 'wb') as stream:
        stream.setparams((channels, width, 24000, frames, 'NONE', 'PCM'))
        stream.writeframes(bytes((index * 13 + 7) % 256 for index in range(frames * channels * width)))
    raw = target.getvalue()
    if (frames * channels * width) % 2:
        raw = raw[:4] + struct.pack('<I', len(raw) - 8 + 1) + raw[8:] + b'\0'
    return raw


def rf64(raw):
    # The standard tiny RF64 fixture exercises 64-bit metadata without a large file.
    body = raw[12:]
    size = struct.unpack('<I', raw[40:44])[0]
    align = struct.unpack('<H', raw[32:34])[0]
    body = body[:28] + struct.pack('<I', 0xffffffff) + body[32:]
    return (b'RF64' + struct.pack('<I', 0xffffffff) + b'WAVE'
        + struct.pack('<4sIQQQI', b'ds64', 28, len(raw) + 28, size, size // align, 0) + body)


@pytest.mark.parametrize('container,channels,width', [
    ('riff', 1, 1), ('riff', 2, 2), ('rf64', 1, 2), ('rf64', 2, 3),
])
def test_actual_small_pcm_containers_preserve_every_frame(container, channels, width, tmp_path):
    raw = riff(channels, width)
    source_pcm = raw[44:44 + 9 * channels * width]
    if container == 'rf64':
        raw = rf64(raw)
    path = tmp_path / 'captured.wav'
    path.write_bytes(raw)
    info = measured.pcm_info(path, {'sample_rate': 24000, 'channels': channels, 'duration': 9 / 24000},
                             lambda: None)
    assert info == {'sample_rate': 24000, 'channels': channels, 'sample_width': width,
                    'frames': 9, 'pcm_sha256': sha256(source_pcm).hexdigest()}
    with measured.pcm_reader(BytesIO(raw), lambda: None) as reader:
        assert b''.join(measured._frames(reader, 9, lambda: None)) == source_pcm
        assert reader.readframes(1) == b''


@pytest.mark.parametrize('change', [
    'container-size', 'ds64-frames', 'ds64-data', 'ds64-table', 'duplicate-data',
    'duplicate-format', 'float-format', 'block-align', 'partial-frame', 'truncated', 'trailing',
])
def test_malformed_rf64_metadata_is_refused_before_pcm_acceptance(change):
    raw = bytearray(rf64(riff()))
    if change == 'container-size':
        struct.pack_into('<Q', raw, 20, len(raw) - 7)
    elif change == 'ds64-frames':
        struct.pack_into('<Q', raw, 36, 10)
    elif change == 'ds64-data':
        struct.pack_into('<Q', raw, 28, 20)
    elif change == 'ds64-table':
        struct.pack_into('<I', raw, 44, 1)
    elif change in ('duplicate-data', 'duplicate-format'):
        extra = b'data' + struct.pack('<I', 2) + b'12' if change == 'duplicate-data' else bytes(raw[48:72])
        raw.extend(extra)
        struct.pack_into('<Q', raw, 20, len(raw) - 8)
    elif change == 'float-format':
        struct.pack_into('<H', raw, 56, 3)
    elif change == 'block-align':
        struct.pack_into('<H', raw, 68, 1)
    elif change == 'partial-frame':
        struct.pack_into('<Q', raw, 28, 17)
    elif change == 'truncated':
        raw.pop()
    else:
        raw.extend(b'junk')
    with pytest.raises(ValueError):
        with measured.pcm_reader(BytesIO(raw), lambda: None) as reader:
            tuple(measured._frames(reader, reader.getnframes(), lambda: None))


def test_rf64_output_header_uses_exact_long_frame_count_without_allocating_payload():
    frames = 0x100000001
    raw = measured._header({'channels': 2, 'sample_width': 2, 'sample_rate': 48000}, frames)
    assert len(raw) == 80 and raw[:12] == b'RF64\xff\xff\xff\xffWAVE'
    assert struct.unpack('<QQQI', raw[20:48]) == (72 + 4 * frames, 4 * frames, frames, 0)
    assert raw[72:] == b'data\xff\xff\xff\xff'


def test_real_raw_frame_iteration_checks_cancellation_before_following_block(tmp_path):
    path = tmp_path / 'larger.wav'
    path.write_bytes(riff(frames=600000))
    stop = []
    def check():
        if stop:
            raise AudioCancelled('Stopped after first actual PCM block.')
    with path.open('rb') as source, measured.pcm_reader(source, check) as reader:
        blocks = measured._frames(reader, reader.getnframes(), check)
        first = next(blocks)
        assert len(first) == 1024 * 1024
        stop.append(True)
        with pytest.raises(AudioCancelled):
            next(blocks)


def test_same_path_pcm_replacement_after_read_is_not_a_verified_snapshot(tmp_path):
    path = tmp_path / 'master.wav'
    original = riff()
    path.write_bytes(original)
    replacement = tmp_path / 'replacement.wav'
    replacement.write_bytes(original)
    swapped = []
    def check():
        if path.exists() and not swapped:
            # Trigger only after the descriptor is held and its data has been read.
            return
    with pytest.raises((ValueError, OSError)) as error:
        with measured._regular_stream(path, check) as stream:
            assert stream.read() == original
            import os
            os.replace(replacement, path)
            swapped.append(True)
    assert path.read_bytes() == original
    if not swapped:
        # Windows may prevent replacement while the actual read descriptor is held.
        # Either refusal or the later identity check must keep the snapshot unaccepted.
        assert getattr(error.value, 'winerror', None) in (5, 32, 33)
        assert replacement.read_bytes() == original
    else:
        assert isinstance(error.value, ValueError) and 'changed' in str(error.value)


def test_archive_directory_is_bounded_before_zipfile_allocates_members():
    end = struct.pack('<4s4H2IH', b'PK\x05\x06', 0, 0, 60000, 60000, 1, 0, 0)
    with pytest.raises(ValueError, match='bounded graph'):
        measured._bounded_directory(BytesIO(b'x' + end), 23)
