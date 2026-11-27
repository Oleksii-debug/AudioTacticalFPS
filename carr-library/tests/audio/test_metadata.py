"""Actual FFmpeg tags, still covers and unchanged encoded audio/chapters."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import shutil
import struct
from threading import Event
from types import SimpleNamespace
import wave
import zlib

import pytest

from carr_library.audio import AudioBudget, AudioError, AudioRuntime
from carr_library.audio import metadata
from carr_library.audio.metadata import MetadataChanges, inspect_metadata, review_metadata, publish_metadata
from carr_library.audio.process import run_process


TAGS = {'title': 'Reviewed α 😀 =;# title', 'artist': 'Author and narrator', 'album': 'Book album',
        'album_artist': 'Book author', 'composer': 'Composer', 'genre': 'Audiobook', 'date': '2026-11-27',
        'track': '2/3', 'disc': '1/2', 'comment': 'Exact comment 😀\nsecond line', 'copyright': 'Original synthetic fixture'}


def png(width=32, height=24):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + bytes((30, 70, 140)) * width) * height)) + chunk(b'IEND', b''))


@pytest.fixture
def actual(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual local FFmpeg and FFprobe are required.'
    source = tmp_path / 'оригінал.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(struct.pack('<h', (n % 100 - 50) * 100) for n in range(24000)))
    root = tmp_path / 'exports'; root.mkdir()
    runtime = AudioRuntime(root, input_roots=(tmp_path,), budget=AudioBudget(reserve_disk_bytes=0))
    cover = tmp_path / 'обкладинка.png'; cover.write_bytes(png())
    return runtime, source, root, cover


def encoded(actual, suffix='.flac'):
    runtime, wave_source, root, _ = actual
    source = root / ('source' + suffix)
    if suffix == '.m4b':
        runtime.audiobook(wave_source, source, title='Original measured book', chapters=[
            {'title': 'First 😀', 'start': 0, 'end': .375}, {'title': 'Second α', 'start': .375, 'end': 1}])
    else:
        runtime.convert(wave_source, source)
    return source


@pytest.mark.parametrize('suffix', ['.mp3', '.flac', '.m4a', '.m4b'])
def test_actual_packet_hash_uses_stable_receipt_and_matches_independent_copied_payload(actual, monkeypatch, suffix):
    runtime, pcm, _, _ = actual
    source = encoded(actual, suffix)
    packets = run_process([*runtime._base_args(), *runtime._input_args(source),
        '-map', '0:a:0', '-c:a', 'copy', '-f', 'data', 'pipe:1'], budget=runtime.budget).stdout
    assert packets
    expected = sha256(packets).hexdigest()
    with wave.open(str(pcm), 'rb') as decoded:
        assert expected != sha256(decoded.readframes(decoded.getnframes())).hexdigest()
    observed = []
    def capture(args, **kwargs):
        result = run_process(args, **kwargs)
        observed.append(result.stdout)
        return result
    monkeypatch.setattr(metadata, 'run_process', capture)
    assert metadata._stream_hash(runtime, source, lambda: False) == expected
    # The published 9.1 streamhash default is headered version 2; the hash
    # muxer's single-record protocol remains the same on older local FFmpeg.
    assert len(observed) == 1
    assert observed[0].decode('ascii').splitlines() == ['SHA256=' + expected]


@pytest.mark.parametrize('output', [
    b'', b'SHA1=' + b'0' * 64, b'SHA256=' + b'0' * 63,
    b'SHA256=' + b'g' * 64, b'SHA256=' + b'0' * 64 + b'\nSHA256=' + b'1' * 64,
    b'0,a,SHA256=' + b'0' * 64,
    b'#format: stream checksums\n#version: 2\n#hash: SHA256\n0, 123, ' + b'0' * 64,
])
def test_packet_hash_rejects_missing_ambiguous_or_changed_receipt_protocol(actual, monkeypatch, output):
    runtime, source, _, _ = actual
    monkeypatch.setattr(metadata, 'run_process', lambda *args, **kwargs: SimpleNamespace(stdout=output))
    with pytest.raises(AudioError, match='did not attest'):
        metadata._stream_hash(runtime, source, lambda: False)


@pytest.mark.parametrize('suffix', ['.mp3', '.flac', '.m4a', '.m4b'])
@pytest.mark.parametrize('kind', ['png', 'jpeg'])
def test_actual_portable_tags_cover_and_audio_roundtrip(actual, suffix, kind):
    runtime, original_pcm, root, cover = actual
    if kind == 'jpeg':
        jpeg = cover.with_suffix('.jpg')
        run_process([*runtime._base_args(), '-protocol_whitelist', 'file', '-f', 'png_pipe', '-i', str(cover),
            '-frames:v', '1', '-c:v', 'mjpeg', '-threads:v', '1', '-f', 'image2', '-update', '1', str(jpeg)], budget=runtime.budget)
        cover = jpeg
    source = encoded(actual, suffix)
    source_bytes, cover_bytes, pcm_bytes = source.read_bytes(), cover.read_bytes(), original_pcm.read_bytes()
    before = inspect_metadata(runtime, source)
    target = root / ('reviewed' + suffix)
    plan = review_metadata(runtime, source, target, MetadataChanges(TAGS, 'replace', str(cover)))
    assert not target.exists()
    result = publish_metadata(runtime, plan)
    after = inspect_metadata(runtime, target)
    assert dict(after.tags) == TAGS
    assert after.cover.sha256 == sha256(cover_bytes).hexdigest() and after.cover.size == len(cover_bytes)
    assert (after.cover.width, after.cover.height) == (32, 24)
    assert after.chapters == before.chapters
    assert (after.audio.codec, after.audio.sample_rate, after.audio.channels) == (before.audio.codec, before.audio.sample_rate, before.audio.channels)
    assert metadata._stream_hash(runtime, source, lambda: False) == metadata._stream_hash(runtime, target, lambda: False)
    assert source.read_bytes() == source_bytes and cover.read_bytes() == cover_bytes and original_pcm.read_bytes() == pcm_bytes
    assert result.audio.sha256 == sha256(target.read_bytes()).hexdigest()
    receipt = json.loads(result.receipt_path.read_bytes())
    assert receipt == result.receipt and result.warning == ''
    assert receipt['audio_reencoded'] is False and receipt['cover'] == asdict(after.cover)
    assert receipt['source']['sha256'] == sha256(source_bytes).hexdigest()
    assert receipt['output']['sha256'] == sha256(target.read_bytes()).hexdigest()
    assert not list(root.glob('.audio-metadata-*'))


@pytest.mark.parametrize('mode', ['keep', 'remove'])
@pytest.mark.parametrize('suffix', ['.mp3', '.flac', '.m4b'])
def test_existing_cover_is_kept_or_removed_only_as_reviewed(actual, mode, suffix):
    runtime, _, root, cover = actual
    original = encoded(actual, suffix)
    first = root / ('first' + suffix)
    publish_metadata(runtime, review_metadata(runtime, original, first, MetadataChanges(TAGS, 'replace', str(cover))))
    original_bytes = first.read_bytes()
    target = root / ('second' + suffix)
    result = publish_metadata(runtime, review_metadata(runtime, first, target,
        MetadataChanges({'title': 'Second explicit title', 'comment': ''}, mode)))
    after = inspect_metadata(runtime, target)
    expected = {**TAGS, 'title': 'Second explicit title'}; expected.pop('comment')
    assert dict(after.tags) == expected
    assert (after.cover is not None) == (mode == 'keep')
    assert result.receipt['audio_packet_sha256'] == metadata._stream_hash(runtime, first, lambda: False)
    assert first.read_bytes() == original_bytes


@pytest.mark.parametrize('changes', [
    {'tags': {'unknown': 'value'}}, {'tags': [('title', 'a'), ('title', 'b')]},
    {'tags': {'title': 'bad\0value'}}, {'tags': {'album': 'x' * 4097}},
    {'tags': {'track': '0'}}, {'tags': {'track': '4/3'}}, {'tags': {'disc': '-1'}},
    {'cover_mode': 'automatic'}, {'cover_mode': 'replace'}, {'cover_path': 'cover.png'},
])
def test_invalid_tag_and_cover_choices_are_refused_before_any_worker(changes):
    with pytest.raises(ValueError): MetadataChanges(**changes)


@pytest.mark.parametrize('which', ['audio', 'cover'])
def test_changed_reviewed_input_never_publishes(actual, which):
    runtime, _, root, cover = actual
    source = encoded(actual)
    target = root / 'changed.flac'
    plan = review_metadata(runtime, source, target, MetadataChanges({'title': 'Reviewed'}, 'replace', str(cover)))
    selected = source if which == 'audio' else cover
    before = selected.read_bytes()
    selected.write_bytes(before[:-1] + bytes((before[-1] ^ 1,)))
    with pytest.raises(AudioError, match='changed after review'):
        publish_metadata(runtime, plan)
    assert not target.exists() and not list(root.glob('.audio-metadata-*'))
    assert selected.read_bytes() != before


@pytest.mark.parametrize('which', ['source', 'cancel', 'encoded-output'])
def test_actual_completed_remux_is_not_published_after_late_change(actual, monkeypatch, which):
    runtime, _, root, _ = actual
    source = encoded(actual, '.mp3')
    target = root / 'late.mp3'
    plan = review_metadata(runtime, source, target, MetadataChanges({'title': 'Reviewed'}))
    stopped = Event(); original = metadata.run_process; remuxed = []
    def run(args, **kwargs):
        result = original(args, **kwargs)
        if str(args[-1]).endswith('tagged.mp3'):
            remuxed.append(True)
            if which == 'cancel': stopped.set()
            else:
                changed = source if which == 'source' else Path(args[-1])
                with changed.open('r+b') as stream:
                    stream.seek(-10, 2); value = stream.read(1); stream.seek(-1, 1)
                    stream.write(bytes((value[0] ^ 1,)))
        return result
    monkeypatch.setattr(metadata, 'run_process', run)
    with pytest.raises(AudioError):
        publish_metadata(runtime, plan, cancelled=stopped.is_set)
    assert remuxed == [True] and not target.exists() and not list(root.glob('.audio-metadata-*'))


def test_existing_output_or_receipt_is_not_overwritten(actual):
    runtime, _, root, _ = actual
    source = encoded(actual)
    target = root / 'occupied.flac'; target.write_bytes(b'keep existing output')
    with pytest.raises(FileExistsError): review_metadata(runtime, source, target, MetadataChanges({'title': 'Reviewed'}))
    assert target.read_bytes() == b'keep existing output'
    target.unlink(); receipt = target.with_name(target.name + '.metadata.json'); receipt.write_bytes(b'keep receipt')
    with pytest.raises(FileExistsError): review_metadata(runtime, source, target, MetadataChanges({'title': 'Reviewed'}))
    assert receipt.read_bytes() == b'keep receipt' and not target.exists()


def test_replaced_approved_directory_does_not_regain_publication_authority(actual):
    runtime, _, root, _ = actual
    source = encoded(actual)
    moved_source = root.parent / source.name; source.rename(moved_source)
    target = root / 'reviewed.flac'
    plan = review_metadata(runtime, moved_source, target, MetadataChanges({'title': 'Reviewed'}))
    root.rename(root.with_name('original-exports')); root.mkdir()
    with pytest.raises(AudioError, match='directory changed'):
        publish_metadata(runtime, plan)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('after_commit', ['cancel', 'receipt-failure'])
def test_committed_audio_remains_successful_after_late_cancel_or_receipt_failure(actual, monkeypatch, after_commit):
    runtime, _, root, _ = actual
    source = encoded(actual)
    target = root / 'committed.flac'
    plan = review_metadata(runtime, source, target, MetadataChanges({'title': 'Committed'}))
    stopped = Event(); publish = metadata.publish_file
    def complete(staged, destination, **kwargs):
        result = publish(staged, destination, **kwargs)
        if destination == target:
            if after_commit == 'cancel': stopped.set()
            else: target.with_name(target.name + '.metadata.json').write_bytes(b'competing receipt')
        return result
    monkeypatch.setattr(metadata, 'publish_file', complete)
    result = publish_metadata(runtime, plan, cancelled=stopped.is_set)
    assert target.exists() and result.audio.sha256 == sha256(target.read_bytes()).hexdigest()
    if after_commit == 'cancel':
        assert result.receipt_path.exists() and result.warning == ''
    else:
        assert result.receipt_path is None and 'Audio was saved' in result.warning
        assert target.with_name(target.name + '.metadata.json').read_bytes() == b'competing receipt'


@pytest.mark.parametrize('bad_cover', ['not-image', 'truncated-png', 'too-wide'])
def test_cover_must_be_a_complete_bounded_still_image(actual, bad_cover):
    runtime, _, root, cover = actual
    source = encoded(actual)
    if bad_cover == 'not-image': cover.write_bytes(b'not a local PNG or JPEG')
    elif bad_cover == 'truncated-png': cover.write_bytes(png()[:45])
    else: cover.write_bytes(png(8193, 1))
    with pytest.raises((ValueError, AudioError)):
        review_metadata(runtime, source, root / 'refused.flac', MetadataChanges({}, 'replace', str(cover)))
    assert not (root / 'refused.flac').exists()


def test_unsupported_container_and_unapproved_transcode_are_explicit(actual):
    runtime, wav, root, _ = actual
    with pytest.raises(ValueError, match='MP3, FLAC'):
        inspect_metadata(runtime, wav)
    source = encoded(actual)
    with pytest.raises(ValueError, match='container family'):
        review_metadata(runtime, source, root / 'not-transcoded.mp3', MetadataChanges({'title': 'Reviewed'}))


def test_actual_animated_png_is_not_misrepresented_as_a_still_cover(actual):
    runtime, _, root, _ = actual
    source = encoded(actual)
    cover = root / 'animated.png'
    run_process([*runtime._base_args(), '-f', 'lavfi', '-i', 'testsrc=size=8x8:rate=2:duration=1',
        '-frames:v', '2', '-c:v', 'apng', '-plays', '0', '-threads:v', '1', '-f', 'apng', str(cover)],
        budget=runtime.budget)
    original = cover.read_bytes()
    with pytest.raises(ValueError, match='Animated PNG'):
        review_metadata(runtime, source, root / 'not-published.flac', MetadataChanges({}, 'replace', str(cover)))
    assert cover.read_bytes() == original and not (root / 'not-published.flac').exists()


def test_actual_studio_output_keeps_project_receipts_and_completed_replay(actual):
    from carr_library.speech import VoiceRegistry
    from carr_library.studio.project import Cue, Project, Scene
    from carr_library.studio.render import ProductionService

    runtime, pcm, root, cover = actual
    voices = VoiceRegistry([])
    service = ProductionService(root.parent / 'studio-state', voices=voices, input_roots=(pcm.parent,))
    try:
        project = Project('metadata-book', 'Original Studio title', (), (
            Scene('first', 'First measured chapter', (), cues=(Cue('one', str(pcm)),)),
            Scene('last', 'Last measured chapter', (), cues=(Cue('two', str(pcm)),))),
            original_script='Original authored audio cues', mode='authored')
        original_project = project.to_dict()
        original_fingerprint = project.fingerprint
        job = service.submit(project, root, format='m4b', stems=False)
        complete = service.run(job)
        source = Path(complete['destination']) / 'audiobook.m4b'
        target = root / 'reviewed-studio-book.m4b'
        before = inspect_metadata(runtime, source)
        result = publish_metadata(runtime, review_metadata(runtime, source, target,
            MetadataChanges({'title': 'Reviewed release title', 'artist': 'Explicit author'}, 'replace', str(cover))))
        assert result.receipt['audio_reencoded'] is False
        assert inspect_metadata(runtime, target).chapters == before.chapters
        assert project.to_dict() == original_project and project.fingerprint == original_fingerprint
        assert all(sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256']
                   for item in complete['artifacts'])
        service.runtime_factory = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Completed Studio job relaunched'))
        replay = service.run(job)
        assert replay['artifacts'] == complete['artifacts'] and replay['fingerprint'] == original_fingerprint
    finally:
        try:
            service.close()
        finally:
            voices.close()

