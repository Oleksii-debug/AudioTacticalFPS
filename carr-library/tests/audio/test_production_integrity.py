"""Real production failure/recovery tests, using explicitly synthetic audio."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

from carr_library.audio import ffmpeg as audio, jobs, production


FFMPEG = Path(os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg') or '/missing-ffmpeg')
pytestmark = pytest.mark.skipif(not FFMPEG.is_file(), reason='Requires installed FFmpeg')


class SyntheticEngine:
    def cache_identity(self, voice):
        return {'fixture': 'constant-pcm-v1'}

    def synthesize(self, text, output, **kwargs):
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 8000, 800, 'NONE', 'not compressed'))
            stream.writeframes(b'\x10\x00' * 800)


def render(plan, root, **kwargs):
    return production.render_project(plan, root, ffmpeg=FFMPEG,
        engine=kwargs.pop('engine', SyntheticEngine()), **kwargs)


def test_builtin_discovery_can_be_cancelled_without_breaking_legacy_engine(tmp_path):
    callbacks = []

    class CancellableEngine(SyntheticEngine):
        def cache_identity(self, voice, *, cancelled):
            callbacks.append(cancelled())
            return super().cache_identity(voice)

    plan = production.parse_script('[output sample_rate=8000 stems=false]\nFixture')
    result = render(plan, tmp_path / 'new', engine=CancellableEngine())
    assert callbacks == [False]
    assert audio.wav_info(result['output']).frames == 800
    legacy = render(plan, tmp_path / 'legacy')
    assert audio.wav_info(legacy['output']).frames == 800


def test_engine_identity_internal_typeerror_is_not_retried(tmp_path):
    calls = []

    class BrokenIdentity(SyntheticEngine):
        def cache_identity(self, voice, *, cancelled=lambda: False):
            calls.append(voice)
            raise TypeError('A genuine adapter defect')

    with pytest.raises(TypeError, match='adapter defect'):
        render(production.parse_script('Fixture'), tmp_path, engine=BrokenIdentity())
    assert len(calls) == 1


def test_changed_source_after_preflight_does_not_seal_wrong_cache_key(tmp_path, monkeypatch):
    assets = tmp_path / 'assets'
    assets.mkdir()
    source = audio.silence(assets / 'door.wav', 800, sample_rate=8000)
    original = audio.process_voice

    def change_before_decode(source, output, **kwargs):
        data_offset = audio.wav_info(source).data_offset
        with Path(source).open('r+b') as stream:
            stream.seek(data_offset)
            stream.write(b'\x01\x00' * 1600)
        return original(source, output, **kwargs)

    monkeypatch.setattr(audio, 'process_voice', change_before_decode)
    plan = production.parse_script('[output sample_rate=8000 stems=false]\n[sound door.wav]')
    with pytest.raises(RuntimeError, match='changed'):
        render(plan, tmp_path)
    assert not list((tmp_path / 'cache').glob('*.wav'))
    assert not list((tmp_path / 'renders').iterdir())


def test_mutation_during_real_ffmpeg_processing_is_not_published(tmp_path, monkeypatch):
    source = audio.silence(tmp_path / 'source.wav', 800, sample_rate=8000)
    original = audio.run_ffmpeg

    def change_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        data_offset = audio.wav_info(source).data_offset
        with source.open('r+b') as stream:
            stream.seek(data_offset)
            stream.write(b'\x01\x00' * 1600)
        return result

    monkeypatch.setattr(audio, 'run_ffmpeg', change_after_read)
    with pytest.raises(RuntimeError, match='changed'):
        audio.process_voice(source, tmp_path / 'output.wav', ffmpeg=FFMPEG, sample_rate=8000)
    assert not (tmp_path / 'output.wav').exists()


def test_existing_project_storage_is_counted_before_render(tmp_path):
    old = tmp_path / 'music-jobs' / 'old'
    old.mkdir(parents=True)
    (old / 'retained.wav').write_bytes(b'x' * (1024 ** 2))
    plan = replace(production.parse_script('[output sample_rate=8000 stems=false]\n[pause .1]'),
                   disk_budget_bytes=1024 ** 2)
    with pytest.raises(RuntimeError, match='disk budget'):
        render(plan, tmp_path)
    assert not list((tmp_path / 'cache').glob('*.wav'))


def test_final_directory_race_preserves_other_empty_directory(tmp_path, monkeypatch):
    original = production.publish_noreplace
    raced = []

    def race(source, destination, **options):
        destination.mkdir()
        raced.append(destination.stat().st_ino)
        return original(source, destination, **options)

    monkeypatch.setattr(production, 'publish_noreplace', race)
    plan = production.parse_script('[output sample_rate=8000 stems=false]\n[pause .1]')
    with pytest.raises(FileExistsError):
        render(plan, tmp_path)
    output = next((tmp_path / 'renders').iterdir())
    assert output.stat().st_ino == raced[0]
    assert not list(output.iterdir())
    assert json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))['state'] == 'failed'


def test_process_death_after_complete_directory_move_resumes_verified_output(tmp_path):
    code = """
import os, sys
from carr_library.audio import production
class UnusedEngine:
    def synthesize(self, *args, **kwargs):
        raise AssertionError('Silence requires no speech engine')
original = production.publish_noreplace
def stop_after_move(source, destination, **options):
    original(source, destination, **options)
    os._exit(17)
production.publish_noreplace = stop_after_move
plan = production.parse_script('[output sample_rate=8000 stems=false]\\n[pause .1]')
production.render_project(plan, sys.argv[1], engine=UnusedEngine(), ffmpeg=sys.argv[2])
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(production.__file__).parents[2]))
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path), str(FFMPEG)],
                            env=environment, capture_output=True, timeout=30)
    assert result.returncode == 17, result.stderr.decode(errors='replace')
    before = json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))
    assert before['state'] == 'running'
    plan = production.parse_script('[output sample_rate=8000 stems=false]\n[pause .1]')
    completed = render(plan, tmp_path)
    assert audio.wav_info(completed['output']).frames == 800
    assert json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))['state'] == 'complete'
    assert len(list((tmp_path / 'renders').iterdir())) == 1
    assert Path(completed['output']).stat().st_nlink == 1


def test_editing_export_cannot_damage_sealed_scene(tmp_path):
    plan = production.parse_script('[output sample_rate=8000 stems=false]\nFixture')
    completed = render(plan, tmp_path)
    hashes = {path: jobs.sha256_file(path) for path in (tmp_path / 'cache').glob('*.wav')}
    chapter = Path(completed['output']).parent / 'chapter-000001.wav'
    chapter.write_bytes(b'user changed exported chapter')
    assert all(jobs.sha256_file(path) == checksum for path, checksum in hashes.items())
    repaired = render(plan, tmp_path)
    assert audio.wav_info(Path(repaired['output']).parent / 'chapter-000001.wav').frames == 800


def test_inherited_ffreport_does_not_create_unrequested_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FFREPORT', 'file=unrequested-ffmpeg-report.log:level=48')
    report = tmp_path / 'unrequested-ffmpeg-report.log'
    audio.ffmpeg_identity(FFMPEG)
    assert not report.exists()
    source = audio.silence(tmp_path / 'input.wav', 800, sample_rate=8000)
    output = audio.process_voice(source, tmp_path / 'output.wav', ffmpeg=FFMPEG, sample_rate=8000)
    assert audio.wav_info(output).frames == 800
    assert not report.exists()


@pytest.mark.parametrize('boundary', ['final-reservation', 'stage-directory-sync'])
def test_stop_at_final_publication_boundary_retains_chunks_and_resumes(tmp_path, monkeypatch, boundary):
    calls = []

    class ObservedEngine(SyntheticEngine):
        def synthesize(self, text, output, **kwargs):
            calls.append(text)
            return super().synthesize(text, output, **kwargs)

    engine = ObservedEngine()
    plan = production.parse_script('[output sample_rate=8000 stems=false]\nFixture')
    stopped = False
    armed = False
    observed = []
    actual_check = production.check_cancelled
    actual_reserve = jobs.ChunkCache.reserve
    actual_sync = jobs._sync_directory

    def check_then_arm(cancelled):
        nonlocal armed
        actual_check(cancelled)
        if list(tmp_path.glob('.work-*/finished/complete.json')):
            armed = True

    def reserve_then_stop(cache, amount):
        nonlocal stopped
        result = actual_reserve(cache, amount)
        if armed and amount == 0 and not observed and boundary == 'final-reservation':
            observed.append(boundary)
            stopped = True
        return result

    def sync_then_stop(directory):
        nonlocal stopped
        actual_sync(directory)
        path = Path(directory)
        if (armed and path.name == 'finished' and (path / 'complete.json').is_file()
                and not observed and boundary == 'stage-directory-sync'):
            observed.append(boundary)
            stopped = True

    monkeypatch.setattr(production, 'check_cancelled', check_then_arm)
    monkeypatch.setattr(jobs.ChunkCache, 'reserve', reserve_then_stop)
    monkeypatch.setattr(jobs, '_sync_directory', sync_then_stop)
    with pytest.raises(jobs.RenderCancelled, match='retained for resume'):
        render(plan, tmp_path, engine=engine, cancelled=lambda: stopped)
    assert observed == [boundary] and calls == ['Fixture']
    assert json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))['state'] == 'cancelled'
    assert not list((tmp_path / 'renders').iterdir())
    assert not list(tmp_path.glob('.work-*'))
    retained = {path: jobs.sha256_file(path) for path in (tmp_path / 'cache').glob('*.wav')}
    assert retained

    stopped, armed = False, False
    completed = render(plan, tmp_path, engine=engine, cancelled=lambda: stopped)
    assert calls == ['Fixture']
    assert audio.wav_info(completed['output']).frames == 800
    assert all(jobs.sha256_file(path) == checksum for path, checksum in retained.items())
    assert json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))['state'] == 'complete'


def test_stop_after_complete_directory_move_keeps_completed_result(tmp_path, monkeypatch):
    actual_publish = production.publish_noreplace
    observed = []
    stopped = False

    def publish_then_stop(source, destination, **options):
        nonlocal stopped
        result = actual_publish(source, destination, **options)
        observed.append(Path(destination))
        stopped = True
        return result

    monkeypatch.setattr(production, 'publish_noreplace', publish_then_stop)
    plan = production.parse_script('[output sample_rate=8000 stems=false]\nFixture')
    completed = render(plan, tmp_path, cancelled=lambda: stopped)
    assert stopped and observed == [Path(completed['output']).parent]
    assert audio.wav_info(completed['output']).frames == 800
    assert json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))['state'] == 'complete'
    replay = render(plan, tmp_path)
    assert replay['output'] == completed['output']
    assert len(list((tmp_path / 'renders').iterdir())) == 1



def _silence_pcm(path, segments, *, channels=2, rate=8000):
    import struct
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        for amplitudes, frames in segments:
            assert len(amplitudes) == channels
            stream.writeframesraw(struct.pack('<' + 'h' * channels, *amplitudes) * frames)
    return path


def _silence_completed(root, *, contextual=False):
    script = '[output sample_rate=8000 stems=false]\n[pause .1]\n'
    if contextual:
        assets = root / 'assets'
        assets.mkdir(parents=True)
        audio.silence(assets / 'music.wav', 800, sample_rate=8000)
        script += 'Fixture\n[pause .1]\n[clip music.wav id=music start=.05 duration=.1 track=music]\n'
    plan = production.parse_script(script)
    return plan, render(plan, root)


def _silence_inventory(root):
    return {
        'directories': sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_dir()),
        'files': {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()},
    }


def _silence_reseal_timeline(completed, change):
    """Explicit fixture resealing; never a product repair or authenticity claim."""
    directory = Path(completed['manifest']).parent
    manifest = json.loads(Path(completed['manifest']).read_text('utf-8'))
    timeline = json.loads((directory / 'timeline.json').read_text('utf-8'))
    change(timeline)
    production.atomic_json(directory / 'timeline.json', timeline)
    row = next(item for item in manifest['files'] if item['name'] == 'timeline.json')
    row.update(size=(directory / 'timeline.json').stat().st_size,
               sha256=jobs.sha256_file(directory / 'timeline.json'))
    production.atomic_json(directory / 'complete.json', manifest)


@pytest.mark.parametrize('channels', [1, 2])
def test_silence_scanner_crosses_real_block_boundaries(tmp_path, channels):
    block_frames = audio._SILENCE_BLOCK_BYTES // (2 * channels)
    source = _silence_pcm(tmp_path / 'block.wav', [
        ((100,) * channels, block_frames - 2),
        ((0,) * channels, 8),
        ((100,) * channels, 3),
    ], channels=channels)
    measured = audio.measure_silence(source, threshold_dbfs=-120, minimum_seconds=8 / 8000)
    assert measured['intervals'] == [{
        'start_frame': block_frames - 2, 'end_frame': block_frames + 6, 'frames': 8,
    }]
    assert measured['interval_count'] == measured['retained_interval_count'] == 1
    assert measured['omitted_interval_count'] == 0 and measured['qualifying_frames'] == 8
    assert measured['criteria']['threshold_pcm16'] == 0
    assert measured['criteria']['minimum_frames'] == 8
    assert measured['sha256'] == jobs.sha256_file(source)


@pytest.mark.parametrize('amplitudes,length,threshold,matched', [
    ((3276, 3276), 3, -20, True),
    ((3277, 0), 3, -20, False),
    ((0, -3277), 3, -20, False),
    ((-3276, 3276), 3, -20, True),
    ((0, 0), 2, -120, False),
    ((0, 0), 3, -120, True),
    ((-32768, 32767), 3, 0, True),
    ((0, 1), 3, -120, False),
])
def test_silence_scanner_applies_all_channel_threshold_and_exact_length(
        tmp_path, amplitudes, length, threshold, matched):
    source = _silence_pcm(tmp_path / 'criterion.wav', [(amplitudes, length)])
    result = audio.measure_silence(source, threshold_dbfs=threshold, minimum_seconds=3 / 8000)
    assert result['interval_count'] == int(matched)
    assert result['qualifying_frames'] == (length if matched else 0)
    assert result['criteria']['minimum_frames'] == 3
    assert result['criteria']['channel_rule'] == 'every-channel-at-or-below'
    assert result['intervals'] == (
        [{'start_frame': 0, 'end_frame': length, 'frames': length}] if matched else [])


@pytest.mark.parametrize('maximum', [1, 2])
def test_silence_scanner_counts_all_omitted_intervals(tmp_path, maximum):
    source = _silence_pcm(tmp_path / 'capped.wav', [
        ((0, 0), 2), ((100, 100), 1), ((0, 0), 3),
        ((100, 100), 1), ((0, 0), 4),
    ])
    result = audio.measure_silence(source, threshold_dbfs=-120,
                                   minimum_seconds=2 / 8000, max_intervals=maximum)
    assert result['interval_count'] == 3
    assert result['retained_interval_count'] == len(result['intervals']) == maximum
    assert result['omitted_interval_count'] == 3 - maximum
    assert result['qualifying_frames'] == 9
    assert result['intervals'] == [
        {'start_frame': 0, 'end_frame': 2, 'frames': 2},
        {'start_frame': 3, 'end_frame': 6, 'frames': 3},
    ][:maximum]
    assert result['frames'] == 11 and result['sha256'] == jobs.sha256_file(source)


@pytest.mark.parametrize('field,value', [
    ('threshold_dbfs', float('nan')), ('threshold_dbfs', float('inf')),
    ('threshold_dbfs', -float('inf')), ('threshold_dbfs', True),
    ('threshold_dbfs', '-20'), ('threshold_dbfs', -120.1),
    ('threshold_dbfs', .1), ('threshold_dbfs', 10 ** 400),
    ('minimum_seconds', 0), ('minimum_seconds', -.1),
    ('minimum_seconds', float('nan')), ('minimum_seconds', float('inf')),
    ('minimum_seconds', True), ('minimum_seconds', 86400.1),
    ('max_intervals', 0), ('max_intervals', 1001),
    ('max_intervals', True), ('max_intervals', 1.0),
])
def test_silence_inspection_rejects_invalid_explicit_criteria_before_io(tmp_path, monkeypatch, field, value):
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid inspection settings must fail before filesystem admission')
    monkeypatch.setattr(production, 'checked_directory', forbidden)
    monkeypatch.setattr(audio, 'regular_file', forbidden)
    options = {'threshold_dbfs': -40, 'minimum_seconds': .1, 'max_intervals': 1000}
    options[field] = value
    with pytest.raises(ValueError):
        production.inspect_completed_silence(tmp_path / 'complete.json', **options)
    with pytest.raises(ValueError):
        audio.measure_silence(tmp_path / 'absent.wav', **options)


def test_completed_silence_inspection_is_source_bound_contextual_and_read_only(tmp_path, monkeypatch):
    plan, completed = _silence_completed(tmp_path, contextual=True)
    before = _silence_inventory(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError('Inspection must not render, decode, or create a cache')
    monkeypatch.setattr(production, 'render_project', forbidden)
    monkeypatch.setattr(audio, 'run_ffmpeg', forbidden)
    monkeypatch.setattr(production, 'ChunkCache', forbidden)
    report = production.inspect_completed_silence(completed['manifest'],
        threshold_dbfs=0, minimum_seconds=.05, expected_run_key=completed['run_key'])
    assert report['schema'] == 'carr-completed-silence-inspection/1'
    assert report['status'] == 'complete' and report['review_status'] == 'warnings'
    assert (report['frames'], report['sample_rate'], report['channels']) == (2400, 8000, 2)
    assert report['interval_count'] == report['retained_interval_count'] == 1
    assert report['omitted_interval_count'] == 0 and report['qualifying_frames'] == 2400
    interval = report['intervals'][0]
    assert (interval['start_frame'], interval['end_frame'], interval['frames']) == (0, 2400, 2400)
    assert interval['annotations'] == {
        'authored_pause_overlap_frames': 1600, 'spoken_line_overlap_frames': 800,
        'declared_music_clip_overlap_frames': 800,
    }
    assert interval['start_seconds'] == 0 and interval['end_seconds'] == interval['duration_seconds'] == .3
    assert report['source_sha256'] == plan.source_sha256 == completed['source_sha256']
    assert report['plan_sha256'] == production.digest(plan.to_dict()) == completed['plan_sha256']
    assert report['completion_sha256'] == jobs.sha256_file(completed['manifest'])
    records = {item['name']: item for item in completed['files']}
    assert report['master_sha256'] == records['master.wav']['sha256']
    assert report['timeline_sha256'] == records['timeline.json']['sha256']
    assert report['context'] == {
        'annotation_mode': 'aggregate-overlap-frames',
        'background_music': 'not-recorded-in-timeline',
        'merged_range_counts': {
            'authored_pause_overlap_frames': 2, 'spoken_line_overlap_frames': 1,
            'declared_music_clip_overlap_frames': 1,
        },
    }
    encoded = json.dumps(report, allow_nan=False)
    assert len(encoded) < 12000 and 'Fixture' not in encoded and str(tmp_path) not in encoded
    formatted = production.completed_silence_report_text(report)
    assert 'Review warnings only' in formatted and 'Background scene music is unknown' in formatted
    assert 'Frames [0, 2400)' in formatted and 'pause 1600' in formatted and 'music clip 800' in formatted
    assert _silence_inventory(tmp_path) == before

    # A single finding may span many recorded events. Aggregate annotations
    # must not copy their IDs/text or grow with that event population.
    def many_events(timeline):
        original = timeline['scenes'][0]['events'][0]
        timeline['scenes'][0]['events'] = [
            {**original, 'event_id': f'synthetic-context-{index}',
             'kind': 'pause' if index % 2 == 0 else 'line',
             'start_frame': index, 'frames': 1}
            for index in range(2400)
        ]
    _silence_reseal_timeline(completed, many_events)
    before = _silence_inventory(tmp_path)
    bounded = production.inspect_completed_silence(completed['manifest'],
        threshold_dbfs=0, minimum_seconds=.05, expected_run_key=completed['run_key'])
    assert bounded['intervals'][0]['annotations'] == {
        'authored_pause_overlap_frames': 1200, 'spoken_line_overlap_frames': 1200,
        'declared_music_clip_overlap_frames': 800,
    }
    encoded = json.dumps(bounded, allow_nan=False)
    assert len(encoded) < 12000 and 'synthetic-context-' not in encoded
    assert _silence_inventory(tmp_path) == before


@pytest.mark.parametrize('target', ['master.wav', 'timeline.json', 'complete.json'])
def test_completed_silence_rejects_changed_retained_inputs(tmp_path, target):
    _, completed = _silence_completed(tmp_path)
    path = Path(completed['manifest']).parent / target
    if target == 'master.wav':
        offset = audio.wav_info(path).data_offset
        with path.open('r+b') as stream:
            stream.seek(offset)
            stream.write(b'\x01\x00')
    elif target == 'timeline.json':
        with path.open('ab') as stream:
            stream.write(b'\n')
    else:
        path.write_bytes(b'{')
    before = _silence_inventory(tmp_path)
    with pytest.raises((ValueError, RuntimeError)):
        production.inspect_completed_silence(completed['manifest'], threshold_dbfs=-120, minimum_seconds=.01)
    assert _silence_inventory(tmp_path) == before
    assert not (tmp_path / 'quarantine').exists()


@pytest.mark.parametrize('target', ['master.wav', 'timeline.json', 'complete.json'])
def test_completed_silence_retains_readers_through_mutation_attempt(tmp_path, monkeypatch, target):
    _, completed = _silence_completed(tmp_path)
    source = Path(completed['manifest']).parent / target
    actual = audio.measure_silence
    attempts = []

    def replace_after_scan(*args, **kwargs):
        result = actual(*args, **kwargs)
        attempts.append(target)
        # Windows must refuse the write through its held verified owner.
        # POSIX permits it, then the same held identity/hash guards must refuse.
        with source.open('r+b') as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(bytes([first[0] ^ 1]))
        return result

    monkeypatch.setattr(audio, 'measure_silence', replace_after_scan)
    with pytest.raises((RuntimeError, PermissionError)):
        production.inspect_completed_silence(completed['manifest'], threshold_dbfs=-120, minimum_seconds=.01)
    assert attempts == [target]
    assert not (tmp_path / 'quarantine').exists()


@pytest.mark.parametrize('phase', ['before', 'during'])
def test_completed_silence_cancellation_keeps_completion_and_receipts(tmp_path, phase):
    _, completed = _silence_completed(tmp_path)
    before = _silence_inventory(tmp_path)
    stopped, observed = phase == 'before', []

    def status(message):
        nonlocal stopped
        observed.append(message)
        if message.startswith('Inspected '):
            stopped = True

    with pytest.raises(jobs.RenderCancelled):
        production.inspect_completed_silence(completed['manifest'], threshold_dbfs=-120,
            minimum_seconds=.01, cancelled=lambda: stopped, on_status=status)
    assert bool(observed) is (phase == 'during')
    assert _silence_inventory(tmp_path) == before


@pytest.mark.parametrize('boundary', ['run-key', 'manifest-name'])
def test_completed_silence_requires_expected_run_and_manifest_selection(tmp_path, boundary):
    _, completed = _silence_completed(tmp_path)
    before = _silence_inventory(tmp_path)
    selected = Path(completed['manifest'])
    options = {'expected_run_key': completed['run_key']}
    if boundary == 'run-key':
        options['expected_run_key'] = 'f' * 64 if completed['run_key'] != 'f' * 64 else 'e' * 64
    else:
        selected = selected.with_name('timeline.json')
    with pytest.raises(ValueError):
        production.inspect_completed_silence(selected, threshold_dbfs=-120, minimum_seconds=.01, **options)
    assert _silence_inventory(tmp_path) == before


@pytest.mark.parametrize('boundary', ['event-end', 'schema-type', 'source-binding', 'music-end'])
def test_silence_context_rejects_resealed_out_of_bounds_timeline(tmp_path, boundary):
    _, completed = _silence_completed(tmp_path)

    def change(timeline):
        if boundary == 'event-end':
            timeline['scenes'][0]['events'][0]['frames'] = completed['frames'] + 1
        elif boundary == 'schema-type':
            timeline['schema_version'] = True
        elif boundary == 'source-binding':
            timeline['source_sha256'] = 'e' * 64
        else:
            timeline['scenes'][0]['clips'] = [
                {'track': 'music', 'start_frame': completed['frames'] - 1, 'frames': 2},
            ]

    _silence_reseal_timeline(completed, change)
    before = _silence_inventory(tmp_path)
    with pytest.raises(ValueError, match='timeline'):
        production.inspect_completed_silence(completed['manifest'], threshold_dbfs=-120, minimum_seconds=.01)
    assert _silence_inventory(tmp_path) == before


def test_silence_inspection_preserves_legacy_completed_replay(tmp_path, monkeypatch):
    plan, completed = _silence_completed(tmp_path)
    # Existing replay deliberately refreshes its journal; normalize that
    # pre-existing behavior before comparing inspection/replay byte stability.
    completed = render(plan, tmp_path)
    before = _silence_inventory(tmp_path)
    report = production.inspect_completed_silence(completed['manifest'],
                                                  threshold_dbfs=-120, minimum_seconds=.1)
    assert report['interval_count'] == 1 and report['criteria']['minimum_frames'] == 800
    assert 'silence' not in json.loads(Path(completed['manifest']).read_text('utf-8'))['measurements']

    def forbidden(*args, **kwargs):
        raise AssertionError('A completed inspection must not invalidate or regenerate a historical render')
    monkeypatch.setattr(audio, 'silence', forbidden)
    monkeypatch.setattr(audio, 'measure_peak', forbidden)
    monkeypatch.setattr(production, 'publish_noreplace', forbidden)
    replay = render(plan, tmp_path)
    assert replay == completed
    assert _silence_inventory(tmp_path) == before


@pytest.mark.parametrize('boundary', ['counts', 'annotations', 'interval-cap', 'criterion-type'])
def test_silence_report_formatter_rejects_unbounded_or_forged_summary(tmp_path, boundary):
    _, completed = _silence_completed(tmp_path)
    report = production.inspect_completed_silence(completed['manifest'], threshold_dbfs=-120, minimum_seconds=.01)
    if boundary == 'counts':
        report['omitted_interval_count'] = 1
    elif boundary == 'annotations':
        report['intervals'][0]['annotations']['authored_pause_overlap_frames'] = completed['frames'] + 1
    elif boundary == 'interval-cap':
        report['intervals'] *= 1001
    else:
        report['criteria']['threshold_pcm16'] = False
    with pytest.raises(ValueError):
        production.completed_silence_report_text(report)
