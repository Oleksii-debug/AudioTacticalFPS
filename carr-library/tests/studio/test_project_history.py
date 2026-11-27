"""Saved project history, CAS refusal and actual PCM editing end to end."""
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import struct
import wave

import pytest

from carr_library.audio import AudioCancelled, AudioRuntime
from carr_library.audio.envelopes import envelope_effect
from carr_library.durable_state import atomic_json
from carr_library.speech import VoiceRegistry
from carr_library.studio.clips import ClipEditor
from carr_library.studio.project import Project
from carr_library.studio.project_history import RevisionRef, revisions
from carr_library.studio.render import ProductionService


@pytest.fixture
def editor(tmp_path):
    service = ProductionService(tmp_path / 'library/state/studio',
        voices=VoiceRegistry([]), runtime_factory=AudioRuntime)
    value = ClipEditor(tmp_path / 'library', service=service)
    yield value
    service.close()


def reference(project):
    return RevisionRef(project.revision, project.fingerprint)


def history_path(editor, project):
    return editor.service.projects.directory(project.id) / 'revisions' / (
        f'{project.revision:08d}-{project.fingerprint}.json')


def test_history_and_whole_project_restore_need_no_audio_or_voice_runtime(editor, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Passive project history must not open audio or voice engines.')
    monkeypatch.setattr(editor.service, '_runtime', forbidden)
    first = editor.create('Original title')
    second = editor.scene_duration(first, first.scenes[0].id, 4)
    third = editor.scene_duration(second, second.scenes[0].id, 8)
    assert editor.history(third) == (reference(second), reference(first))
    restored = editor.restore(third, reference(first))
    assert restored == replace(first, revision=4)
    assert editor.service.projects.load(first.id) == restored
    assert editor.history(restored)[0] == reference(third)
    assert Project.from_dict(json.loads(history_path(editor, third).read_text())) == third


def test_history_cursor_covers_all_revisions_once_with_bounded_pages(editor):
    project = editor.create()
    saved = []
    for seconds in range(2, 9):
        saved.append(project)
        project = editor.scene_duration(project, project.scenes[0].id, seconds)
    result, cursor = [], None
    while page := revisions(editor.service.projects, project, before_revision=cursor, limit=2):
        assert len(page) <= 2
        result.extend(page)
        cursor = page[-1].revision
    assert result == list(reversed([reference(item) for item in saved]))


@pytest.mark.parametrize('change', ['title', 'revision', 'id'])
def test_changed_history_payload_is_never_published_under_old_identity(editor, change):
    first = editor.create()
    current = editor.scene_duration(first, first.scenes[0].id, 3)
    path = history_path(editor, first)
    data = json.loads(path.read_text())
    data[change] = {'title': 'Unreviewed title', 'revision': 100, 'id': 'different-project'}[change]
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='differs'):
        editor.restore(current, reference(first))
    assert editor.service.projects.load(current.id) == current


def test_same_revision_replacement_cannot_race_restore_compare_and_swap(editor, monkeypatch):
    first = editor.create()
    current = editor.scene_duration(first, first.scenes[0].id, 3)
    actual_save = editor.service.projects.save
    foreign = replace(current, title='Concurrent edit at the same revision')
    def racing_save(value, **kwargs):
        atomic_json(editor.service.projects.directory(current.id) / 'project.json',
                    foreign.to_dict(), replace=True)
        return actual_save(value, **kwargs)
    monkeypatch.setattr(editor.service.projects, 'save', racing_save)
    with pytest.raises(ValueError, match='content changed'):
        editor.restore(current, reference(first))
    assert editor.service.projects.load(current.id) == foreign


@pytest.mark.parametrize('operation', ['history', 'restore'])
def test_stale_project_cannot_browse_or_restore_without_reload(editor, operation):
    first = editor.create()
    second = editor.scene_duration(first, first.scenes[0].id, 3)
    current = editor.scene_duration(second, second.scenes[0].id, 4)
    with pytest.raises(ValueError, match='changed'):
        if operation == 'history':
            editor.history(second)
        else:
            editor.restore(second, reference(first))
    assert editor.service.projects.load(current.id) == current


@pytest.mark.parametrize('operation', ['history', 'restore'])
def test_pre_cancel_never_reads_history_or_creates_revision(editor, monkeypatch, operation):
    first = editor.create()
    current = editor.scene_duration(first, first.scenes[0].id, 3)
    def forbidden(*args, **kwargs):
        raise AssertionError('Cancelled history must not load a project.')
    monkeypatch.setattr(editor.service.projects, 'load', forbidden)
    with pytest.raises(AudioCancelled):
        if operation == 'history':
            editor.history(current, cancelled=lambda: True)
        else:
            editor.restore(current, reference(first), cancelled=lambda: True)


def test_history_hardlink_is_refused_without_reading_or_replacing_its_target(editor, tmp_path):
    first = editor.create()
    current = editor.scene_duration(first, first.scenes[0].id, 3)
    path = history_path(editor, first)
    outside = tmp_path / 'external-history-copy.json'
    os.link(path, outside)
    original = outside.read_bytes()
    with pytest.raises(ValueError, match='without links'):
        editor.restore(current, reference(first))
    with pytest.raises(ValueError, match='without links'):
        editor.history(current)
    assert outside.read_bytes() == original
    assert editor.service.projects.load(current.id) == current


def test_history_ambiguous_revisions_are_visible_failure(editor):
    first = editor.create()
    current = editor.scene_duration(first, first.scenes[0].id, 3)
    altered = replace(first, title='Another revision-one payload')
    path = history_path(editor, altered)
    atomic_json(path, altered.to_dict())
    with pytest.raises(ValueError, match='ambiguous'):
        editor.history(current)


@pytest.mark.parametrize('revision,fingerprint', [
    (True, 'a' * 64), (0, 'a' * 64), (-1, 'a' * 64), (10**16, 'a' * 64),
    (1, 'A' * 64), (1, '../foreign'), (1, None),
])
def test_revision_references_cannot_be_paths_or_unbounded_numbers(revision, fingerprint):
    with pytest.raises(ValueError):
        RevisionRef(revision, fingerprint)


def test_current_or_missing_revision_is_never_synthesized_as_history(editor):
    first = editor.create()
    with pytest.raises(ValueError, match='earlier'):
        editor.restore(first, reference(first))
    current = editor.scene_duration(first, first.scenes[0].id, 3)
    with pytest.raises((ValueError, FileNotFoundError)):
        editor.restore(current, RevisionRef(1, 'f' * 64))
    assert editor.service.projects.load(current.id) == current


def tone(path, rate=24000):
    values = [round(4000 * math.sin(2 * math.pi * 300 * index / rate)) for index in range(2 * rate)]
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack('<' + 'h' * len(values), *values))
    return path


def pcm(path):
    with wave.open(str(path), 'rb') as reader:
        count, channels, rate = reader.getnframes(), reader.getnchannels(), reader.getframerate()
        values = struct.unpack('<' + 'h' * count * channels, reader.readframes(count))
    return values, channels, rate


def test_actual_split_rejoin_cut_and_history_restore_preserve_pcm_and_original(editor, tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'This gate requires actual FFmpeg.'
    source = tone(tmp_path / 'original.wav')
    original = source.read_bytes()
    project = editor.create()
    project = editor.add(project, project.scenes[0].id, source, gain_db=0, duration=2,
                         fade_in=.1, fade_out=.1, title='Tone with exact source')
    scene, = project.scenes
    cue, = scene.cues
    curves = (
        envelope_effect('gain_envelope', ((0, 0), (.5, -6), (1.75, -12))),
        envelope_effect('pan_envelope', ((0, -1), (.5, 0), (1.75, 1))),
    )
    project = editor.set_effects(project, 'clip', curves, scene_id=scene.id, clip_id=cue.id)
    initial = project
    outputs = tmp_path / 'renders'
    outputs.mkdir()
    def render(value):
        result = editor.service.run(editor.service.submit(value, outputs))
        assert result['status'] == 'complete'
        return pcm(Path(result['destination']) / 'master.wav')
    baseline, channels, rate = render(project)
    assert channels == 2 and rate == 24000 and len(baseline) == 96000
    split = editor.split(project, scene.id, cue.id, 1)
    assert len(split.scenes[0].cues) == 2 and split.revision == project.revision + 1
    rendered, _, _ = render(split)
    assert len(rendered) == len(baseline)
    assert max(abs(a - b) for a, b in zip(rendered, baseline)) <= 2
    first, second = split.scenes[0].cues
    joined = editor.join(split, scene.id, first.id, second.id)
    assert joined.scenes == initial.scenes
    cut = editor.cut(joined, scene.id, cue.id, .75, 1.25)
    cut_pcm, _, _ = render(cut)
    assert len(cut_pcm) == len(baseline)
    first_cut, last_cut = round(.75 * rate) * channels, round(1.25 * rate) * channels
    assert max(abs(value) for value in cut_pcm[first_cut:last_cut]) == 0
    assert max(abs(a - b) for a, b in zip(cut_pcm[:first_cut], baseline[:first_cut])) <= 2
    assert max(abs(a - b) for a, b in zip(cut_pcm[last_cut:], baseline[last_cut:])) <= 2
    restored = editor.restore(cut, reference(initial))
    assert restored.scenes == initial.scenes and restored.original_script == initial.original_script
    assert render(restored) == (baseline, channels, rate)
    assert source.read_bytes() == original
    assert (editor.service.root / cue.path).read_bytes() == original
    assert sha256(original).hexdigest() in cue.path
    assert len([row for row in editor.service.jobs() if row['status'] == 'complete']) == 4


@pytest.mark.parametrize('operation', ['split', 'cut', 'join'])
def test_pre_cancel_clip_edit_never_opens_runtime(editor, monkeypatch, operation):
    project = editor.create()
    monkeypatch.setattr(editor.service, '_runtime', lambda: pytest.fail('Cancelled edit opened audio.'))
    with pytest.raises(AudioCancelled):
        if operation == 'split':
            editor.split(project, project.scenes[0].id, 'absent', 1, cancelled=lambda: True)
        elif operation == 'cut':
            editor.cut(project, project.scenes[0].id, 'absent', 0, 1, cancelled=lambda: True)
        else:
            editor.join(project, project.scenes[0].id, 'absent', 'also-absent', cancelled=lambda: True)
    assert editor.service.projects.load(project.id) == project

def test_actual_half_sample_scene_start_splits_without_a_one_sample_hole(editor, tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe')
    source = tone(tmp_path / 'fractional-scene.wav')
    original = source.read_bytes()
    project = editor.create()
    project = editor.add(project, project.scenes[0].id, source,
                         start=.5 / 24000, duration=2, gain_db=0, pan=-1)
    destination = tmp_path / 'half-sample-outputs'
    destination.mkdir()
    def render(value):
        job = editor.service.run(editor.service.submit(value, destination))
        return pcm(Path(job['destination']) / 'master.wav')
    baseline = render(project)
    split = editor.split(project, project.scenes[0].id, project.scenes[0].cues[0].id, 1 / 24000)
    actual = render(split)
    assert actual[1:] == baseline[1:] and len(actual[0]) == len(baseline[0])
    assert max(abs(a - b) for a, b in zip(actual[0], baseline[0])) <= 1
    assert round(split.scenes[0].cues[1].start * 24000) == 1
    assert source.read_bytes() == original


@pytest.mark.parametrize('operation', ['split', 'cut'])
def test_actual_different_source_rate_requires_reviewed_conversion(editor, tmp_path, operation):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe')
    source = tone(tmp_path / '48000-original.wav', rate=48000)
    project = editor.create()
    project = editor.add(project, project.scenes[0].id, source, duration=2)
    scene, cue = project.scenes[0], project.scenes[0].cues[0]
    with pytest.raises(ValueError, match='project sample rate'):
        if operation == 'split':
            editor.split(project, scene.id, cue.id, 1)
        else:
            editor.cut(project, scene.id, cue.id, .25, .5)
    assert editor.service.projects.load(project.id) == project


@pytest.mark.parametrize('settings', [{'trim_start': .5 / 24000, 'duration': 1},
                                     {'duration': 1 + .5 / 24000}])
def test_actual_unaligned_source_selection_is_never_silently_shifted(editor, tmp_path, settings):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe')
    source = tone(tmp_path / 'unaligned-original.wav')
    project = editor.create()
    project = editor.add(project, project.scenes[0].id, source, **settings)
    with pytest.raises(ValueError, match='original sample grid'):
        editor.split(project, project.scenes[0].id, project.scenes[0].cues[0].id, .5)
    assert editor.service.projects.load(project.id) == project
