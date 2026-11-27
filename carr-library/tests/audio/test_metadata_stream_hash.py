"""Actual FFmpeg 9.1 diagnostic output plus strict packet-hash grammar refusals."""
import pytest
from carr_library.audio import metadata
from carr_library.audio.models import AudioError

# Captured from actual pinned Windows FFmpeg in run 45996687047, job140497717908.
# These are synthetic FLAC/MP3/AAC; owned and direct stdout were byte-identical.
CASES = [
    (".flac", "#format: stream checksums\n#version: 2\n#hash: SHA256\n#extradata 0,                              34, ea1d1a6c12eefac033a8c0a1fde96f75ebcecdf7488b73d1d83b4912d3892021\n#software: Lavf63.16.100\n#tb 0: 1/24000\n#media_type 0: audio\n#codec_id 0: flac\n#sample_rate 0: 24000\n#channel_layout_name 0: stereo\n#stream#, size, hash\n0,       28685, 32c80b39a523d2e0670594379a59b748f87899a84e36553d435c36dfa4f1475c\n".encode('ascii'), "32c80b39a523d2e0670594379a59b748f87899a84e36553d435c36dfa4f1475c"),
    (".mp3", "#format: stream checksums\n#version: 2\n#hash: SHA256\n#software: Lavf63.16.100\n#tb 0: 1/14112000\n#media_type 0: audio\n#codec_id 0: mp3\n#sample_rate 0: 24000\n#channel_layout_name 0: stereo\n#stream#, size, hash\n0,       21120, 93ff7b23822b5810d600eacf41d810d874944b4b278ac318ff0c244551d9d811\n".encode('ascii'), "93ff7b23822b5810d600eacf41d810d874944b4b278ac318ff0c244551d9d811"),
    (".m4a", "#format: stream checksums\n#version: 2\n#hash: SHA256\n#extradata 0,                               5, 85f4062733753e7cfef60938048157eca77627247609ae3f0e39b09fb891859c\n#software: Lavf63.16.100\n#tb 0: 1/24000\n#media_type 0: audio\n#codec_id 0: aac\n#sample_rate 0: 24000\n#channel_layout_name 0: stereo\n#stream#, size, hash\n0,       15103, 2efc8a382fbd224f83aae54fbc6c98788a1d567fb964e1b72938ccdf17bd8a1e\n".encode('ascii'), "2efc8a382fbd224f83aae54fbc6c98788a1d567fb964e1b72938ccdf17bd8a1e"),
]


@pytest.mark.parametrize('suffix,raw,expected', CASES)
def test_exact_observed_v2_audio_packet_digest(suffix, raw, expected):
    assert metadata._parse_stream_hash(raw) == expected
    assert metadata._parse_stream_hash(raw.replace(b'\n', b'\r\n')) == expected


def test_legacy_single_audio_digest_remains_exactly_compatible():
    assert metadata._parse_stream_hash(b'0,a,SHA256=' + b'a' * 64 + b'\n') == 'a' * 64


@pytest.mark.parametrize('mutate', [
    lambda raw: raw.replace(b'#version: 2', b'#version: 3'),
    lambda raw: raw.replace(b'#hash: SHA256', b'#hash: MD5'),
    lambda raw: raw.replace(b'#media_type 0: audio', b'#media_type 0: video'),
    lambda raw: raw.replace(b'#media_type 0: audio\n', b''),
    lambda raw: raw.replace(b'#media_type 0: audio\n', b'#media_type 0: audio\n#media_type 0: audio\n'),
    lambda raw: raw.replace(b'#tb 0:', b'#tb 1:'),
    lambda raw: raw.replace(b'#tb 0: 1/24000', b'#tb 0: 1/0'),
    lambda raw: raw.replace(b'#stream#, size, hash', b'#stream#, dts, pts, duration, size, hash'),
    lambda raw: raw.replace(b'0,       28685,', b'1,       28685,'),
    lambda raw: raw.replace(b'0,       28685,', b'0,       0,'),
    lambda raw: raw + b'1, 1, ' + b'a' * 64 + b'\n',
    lambda raw: raw.replace(b'#software: Lavf63.16.100\n', b'#unreviewed_header: value\n'),
    lambda raw: raw.replace(b'ea1d1a6c', b'not-hex!'),
    lambda raw: raw + b'\xff',
])
def test_v2_refuses_ambiguous_wrong_stream_algorithm_and_malformed_attestation(mutate):
    with pytest.raises(AudioError, match='did not attest'):
        metadata._parse_stream_hash(mutate(CASES[0][1]))


@pytest.mark.parametrize('raw', [
    b'', b'x' * 65537, b'0,v,SHA256=' + b'a' * 64,
    b'0,a,SHA256=' + b'A' * 64,
    b'0,a,SHA256=' + b'a' * 64 + b'\n0,a,SHA256=' + b'a' * 64,
])
def test_legacy_unknown_empty_or_multiple_digest_is_not_accepted(raw):
    with pytest.raises(AudioError):
        metadata._parse_stream_hash(raw)
