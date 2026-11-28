"""Measured CUE serialization and actual production publication contracts."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import shutil
import struct
from types import SimpleNamespace
import wave

import pytest

from carr_library.studio.interchange import chapter_cues
from carr_library.studio.project import parse_script
from carr_library.studio.render import ProductionService


def measured(count=2, duration=.25):
    script = ''.join(f'[Scene: Chapter {index + 1}]\nNarrator: Exact line {index + 1}.\n'
                     for index in range(count))
    project = parse_script(script, title='Українська & Slovenská production', voice_ids=('tone',))
    assert len(project.scenes) == count
    chapters, lines = [], []
    for index, scene in enumerate(project.scenes):
        start, end = index * duration, (index + 1) * duration
        chapters.append({'id': scene.id, 'title': scene.title, 'start': start, 'end': end})
        line, = scene.lines
        lines.append({'scene': scene.id, 'line': line.id, 'role': line.role,
                      'text': line.text, 'source_ref': line.source_ref, 'start': start, 'end': end})
    return project, {'schema': 1, 'project': project.id, 'revision': project.revision,
        'source_sha256': sha256(script.encode()).hexdigest(), 'sample_rate': project.sample_rate,
        'duration': count * duration, 'chapters': chapters, 'lines': lines}


def parse_indices(raw):
    # This assertion parser checks the declared wire layout. The independent
    # pinned libcue reader is exercised by scripts/probe_cue_interchange.py.
    text = raw.decode('utf-8')
    assert text.count('FILE "master.wav" WAVE\r\n') == 1
    numbers = [int(value) for value in re.findall(r'^  TRACK (\d{2}) AUDIO\r$', text, re.M)]
    indices = [int(minutes) * 4500 + int(seconds) * 75 + int(frames)
               for minutes, seconds, frames in re.findall(
                   r'^    INDEX 01 (\d+):([0-5]\d):([0-6]\d|7[0-4])\r$', text, re.M)]
    assert numbers == list(range(1, len(indices) + 1))
    return indices


@pytest.mark.parametrize('count', [1, 2, 99, 100, 198, 199])
def test_every_chapter_survives_sheet_boundaries_with_exact_binding(count):
    project, timeline = measured(count)
    before = deepcopy(timeline), project.to_dict()
    artifacts, report = chapter_cues(project, timeline)
    assert len(artifacts) == (count + 98) // 99
    assert [name for name, _ in artifacts] == [row['file'] for row in report['files']]
    decoded = [index for _, raw in artifacts for index in parse_indices(raw)]
    expected = [int((Decimal(str(row['start'])) * 75).to_integral_value(rounding=ROUND_FLOOR))
                for row in timeline['chapters']]
    assert decoded == expected
    bindings = [track for sheet in report['files'] for track in sheet['tracks']]
    assert [row['scene'] for row in bindings] == [scene.id for scene in project.scenes]
    assert [row['chapter'] for row in bindings] == list(range(1, count + 1))
    assert [row['index_75hz'] for row in bindings] == expected
    assert report['status'] == 'ready' and report['timebase'] == 75
    assert report['editor_import'] == 'NOT_RUN'
    assert (timeline, project.to_dict()) == before


@pytest.mark.parametrize('duration', [.25, .4, 1.005, 60.01, 3601.5])
def test_reported_quantization_never_moves_marker_forward_or_by_one_frame(duration):
    project, timeline = measured(3, duration)
    artifacts, report = chapter_cues(project, timeline)
    indices = [index for _, raw in artifacts for index in parse_indices(raw)]
    for row, index in zip(timeline['chapters'], indices):
        shift = Decimal(str(row['start'])) - Decimal(index) / 75
        assert 0 <= shift < Decimal(1) / 75
    assert report['rounding'] == 'floor'
    assert '1/75 second' in report['notice']


def test_unicode_titles_are_utf8_and_unsafe_labels_cannot_add_commands():
    project, timeline = measured()
    titles = ('Тихий архів — Žilina', '"\nFILE "foreign.wav" WAVE\n')
    project = replace(project, title='A "quoted" project', scenes=tuple(
        replace(scene, title=title) for scene, title in zip(project.scenes, titles)))
    for row, title in zip(timeline['chapters'], titles):
        row['title'] = title
    artifacts, report = chapter_cues(project, timeline)
    raw = artifacts[0][1]
    assert titles[0].encode() in raw and b'foreign.wav' not in raw
    assert b'TITLE "Chapter 000002"' in raw
    assert not report['project_title_preserved']
    assert [row['original_title_preserved'] for row in report['files'][0]['tracks']] == [True, False]
    assert timeline['chapters'][1]['title'] == titles[1]


def test_long_multibyte_title_remains_in_timeline_with_explicit_cue_fallback():
    project, timeline = measured(1)
    title = 'Ж' * 200
    project = replace(project, scenes=(replace(project.scenes[0], title=title),))
    timeline['chapters'][0]['title'] = title
    artifacts, report = chapter_cues(project, timeline)
    assert b'Chapter 000001' in artifacts[0][1]
    assert not report['files'][0]['tracks'][0]['original_title_preserved']
    assert timeline['chapters'][0]['title'] == title


def test_subframe_chapters_are_explicitly_unavailable_without_merging_or_audio_failure():
    project, timeline = measured(3, .001)
    before = deepcopy(timeline)
    artifacts, report = chapter_cues(project, timeline)
    assert artifacts == () and report['status'] == 'unavailable'
    assert report['files'] == [] and 'Distinct chapter starts' in report['reason']
    assert timeline == before


@pytest.mark.parametrize('damage', ['project', 'revision', 'source', 'line-text', 'missing-line',
                                    'scene-id', 'chapter-gap', 'duration', 'nonfinite'])
def test_wrong_or_incomplete_measured_source_is_refused(damage):
    project, timeline = measured()
    if damage == 'project':
        timeline['project'] = 'foreign-project'
    elif damage == 'revision':
        timeline['revision'] += 1
    elif damage == 'source':
        timeline['source_sha256'] = '0' * 64
    elif damage == 'line-text':
        timeline['lines'][0]['text'] += ' changed'
    elif damage == 'missing-line':
        timeline['lines'].pop()
    elif damage == 'scene-id':
        timeline['chapters'][0]['id'] = 'foreign-scene'
    elif damage == 'chapter-gap':
        timeline['chapters'][1]['start'] += .1
    elif damage == 'duration':
        timeline['duration'] += 1
    else:
        timeline['chapters'][0]['start'] = float('nan')
    with pytest.raises(ValueError):
        chapter_cues(project, timeline)


@pytest.mark.parametrize('stop_at', [1, 5, 22])
def test_cancellation_returns_no_partial_interchange_and_preserves_source(stop_at):
    project, timeline = measured(12)
    before = deepcopy(timeline), project.to_dict()
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= stop_at
    with pytest.raises(InterruptedError):
        chapter_cues(project, timeline, cancelled=cancelled)
    assert (timeline, project.to_dict()) == before


class ToneVoices:
    def voices(self):
        return (SimpleNamespace(id='tone', name='Synthetic fixture', language='und', engine='fixture'),)

    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        if cancelled():
            raise InterruptedError('Synthetic fixture cancelled')
        with wave.open(str(destination), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            output.writeframes(b''.join(struct.pack('<h', round(3000 * math.sin(index * math.tau * 440 / 24000)))
                                        for index in range(6000)))


def test_actual_ffmpeg_job_publishes_cue_through_existing_receipts_and_resume(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install the selected test audio runtime.'
    output = tmp_path / 'outputs'; output.mkdir()
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio', voices=ToneVoices())
    script = '[Scene: Архів]\nNarrator: Exact original.\n[Scene: Žilina]\nNarrator: Second exact line.\n'
    project = service.create(script, title='Actual measured CUE fixture', voice_ids=('tone',))
    identity = service.submit(project, output, stems=True)
    complete = service.run(identity)
    assert complete['status'] == 'complete'
    destination = Path(complete['destination'])
    cue = destination / 'chapters-0001.cue'
    receipt, = [row for row in complete['artifacts'] if Path(row['path']) == cue]
    assert receipt['sha256'] == sha256(cue.read_bytes()).hexdigest()
    assert receipt['size'] == cue.stat().st_size
    timeline = json.loads((destination / 'timeline.json').read_text(encoding='utf-8'))
    assert timeline == complete['result'] and timeline['editor_interchange']['status'] == 'ready'
    assert parse_indices(cue.read_bytes()) == [
        row['index_75hz'] for row in timeline['editor_interchange']['files'][0]['tracks']]
    assert json.loads((destination / 'project.json').read_text(encoding='utf-8'))['original_script'] == script
    before = {row['path']: (sha256(Path(row['path']).read_bytes()).hexdigest(),
                            Path(row['path']).stat().st_mtime_ns) for row in complete['artifacts']}
    resumed = service.run(identity)
    assert resumed == complete
    assert before == {name: (sha256(Path(name).read_bytes()).hexdigest(), Path(name).stat().st_mtime_ns)
                      for name in before}
    service._verify_artifacts(complete)


@pytest.fixture
def completed_cue(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install the selected test audio runtime.'
    output = tmp_path / 'outputs'
    output.mkdir()
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio', voices=ToneVoices())
    project = service.create('[Scene: First]\nNarrator: Retained original.\n'
                             '[Scene: Second]\nNarrator: Other retained original.\n',
                             voice_ids=('tone',))
    identity = service.submit(project, output, stems=True)
    complete = service.run(identity)
    assert complete['status'] == 'complete'
    yield service, identity, complete
    assert service.close() is None
    assert service._closed and service._voices is None


@pytest.mark.parametrize('damage', ['missing-receipt', 'rehashed-bytes', 'false-index', 'false-qualification'])
def test_completed_replay_requires_exact_cue_bytes_and_declared_source(completed_cue, damage):
    service, identity, complete = completed_cue
    damaged = deepcopy(complete)
    cue = Path(complete['destination']) / 'chapters-0001.cue'
    original = cue.read_bytes()
    if damage == 'missing-receipt':
        damaged['artifacts'] = [row for row in damaged['artifacts'] if Path(row['path']) != cue]
    elif damage == 'rehashed-bytes':
        raw = original.replace(b'INDEX 01 00:00:00', b'INDEX 01 00:00:01')
        assert raw != original
        cue.write_bytes(raw)
        receipt, = [row for row in damaged['artifacts'] if Path(row['path']) == cue]
        receipt.update(size=len(raw), sha256=sha256(raw).hexdigest())
    elif damage == 'false-index':
        damaged['result']['editor_interchange']['files'][0]['tracks'][0]['index_75hz'] += 1
    else:
        damaged['result']['editor_interchange']['editor_import'] = 'PASS'
    try:
        with pytest.raises(ValueError, match='interchange|chapter|artifacts'):
            service._verify_artifacts(damaged)
    finally:
        cue.write_bytes(original)
    assert service.get(identity) == complete
    service._verify_artifacts(complete)


def test_new_cue_private_staging_is_disposable_and_legacy_completed_jobs_stay_valid(completed_cue):
    service, identity, complete = completed_cue
    assert not list((service.jobs_root / identity).glob('*.cue'))
    assert not list((service.jobs_root / identity).glob('.pending-*.cue'))
    legacy = deepcopy(complete)
    legacy['result'].pop('editor_interchange')
    legacy['artifacts'] = [row for row in legacy['artifacts']
                           if not Path(row['path']).name.endswith('.cue')]
    before = {row['path']: Path(row['path']).read_bytes() for row in legacy['artifacts']}
    service._verify_artifacts(legacy)
    assert {name: Path(name).read_bytes() for name in before} == before


@pytest.mark.parametrize('count', [1, 99, 100, 10000, 10001])
def test_declared_storage_allowance_covers_payload_and_metadata_without_source_changes(count):
    from carr_library.durable_state import encode
    from carr_library.studio.interchange import storage_allowance
    if count > 10000:
        project = SimpleNamespace(scenes=(None,) * count)
        files, report = chapter_cues(project, {})
    else:
        project, timeline = measured(count)
        files, report = chapter_cues(project, timeline)
    allowance = storage_allowance(project)
    payload = sum(len(raw) for _, raw in files)
    metadata = len(encode(report))
    assert allowance['export_bytes'] >= payload + metadata
    assert allowance['private_bytes'] >= payload + 2 * metadata
    assert max(allowance.values()) <= 32 * 1024 * 1024


def test_report_validation_observes_existing_cancellation_without_changing_declared_data():
    from carr_library.studio.interchange import validated_cues
    project, timeline = measured(5)
    _, timeline['editor_interchange'] = chapter_cues(project, timeline)
    before = deepcopy(timeline)
    with pytest.raises(InterruptedError):
        validated_cues(project, timeline, cancelled=lambda: True)
    assert timeline == before
