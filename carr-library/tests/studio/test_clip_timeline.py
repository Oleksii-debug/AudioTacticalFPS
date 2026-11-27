"""Actual sample ranges, authored markers and the existing retained Studio owner."""
from array import array
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import struct
import sys
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime, AudioCancelled, Effect
from carr_library.durable_state import encode
from carr_library.speech import VoiceRegistry
from carr_library.studio.clips import ClipEditor, timeline_seconds
from carr_library.studio.project import Marker, Project
from carr_library.studio.render import ProductionService


@pytest.fixture
def prepared(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Real FFmpeg and ffprobe are required.'
    app = LibraryApp(tmp_path / 'library')
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]), runtime_factory=AudioRuntime)
    editor = ClipEditor(app.root, service=service)
    yield app, editor
    assert editor.close() is True
    assert service.close() is True


def constant(path, value=7000, *, frames=48000, rate=24000):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', value) * frames)
    return path


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        shape = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        data = stream.readframes(stream.getnframes())
    values = array('h')
    values.frombytes(data)
    if sys.byteorder != 'little':
        values.byteswap()
    return shape, data, values


def render(editor, project, root):
    root.mkdir()
    result = editor.service.run(editor.service.submit(project, root))
    assert result['status'] == 'complete'
    return result, Path(result['destination']) / 'master.wav'


@pytest.mark.parametrize('value,rate,expected', [
    ('00:00:00.125', 24000, .125), ('01:02:03.5', 48000, 3723.5),
    ('.25', 24000, .25), ('0.0000625', 24000, 2 / 24000),
    (str(1 / 24000), 24000, 1 / 24000), ('4.1666666666666665e-05', 24000, 1 / 24000),
    (0, 48000, 0), (31_536_000, 24000, 31_536_000)])
def test_exact_bounded_timecodes_are_rounded_once_to_output_samples(value, rate, expected):
    assert timeline_seconds(value, rate) == expected


@pytest.mark.parametrize('value', [True, -1, float('inf'), float('nan'), '1e999', '00:60:00',
    '00:00:60', 'abc', '1:2:3', '0x10', '', ' 1', '1 '])
def test_invalid_timecodes_are_not_silent_timeline_edits(value):
    with pytest.raises(ValueError):
        timeline_seconds(value, 24000)


@pytest.mark.parametrize('action', ['silence_range', 'cut_range', 'trim_range'])
def test_exact_range_changes_only_selected_track_and_keeps_original_bytes(prepared, tmp_path, action):
    app, editor = prepared
    source = constant(tmp_path / 'Exact original.wav')
    original = source.read_bytes()
    project = editor.create('Sample ranges')
    scene = project.scenes[0].id
    project = editor.add(project, scene, source, trim_start=.25, duration=1, start=.25, pan=-1, gain_db=0)
    cue = project.scenes[0].cues[0]
    _, before = render(editor, project, tmp_path / 'before')
    changed = getattr(editor, action)(project, scene, cue.id, '00:00:00.25', '.75')
    _, after = render(editor, changed, tmp_path / 'after')
    shape, raw, values = pcm(after)
    before_shape, before_raw, before_values = pcm(before)
    assert shape == before_shape == (24000, 2, 30000)
    assert not any(values[1::2])
    first, last = 12000, 24000  # Scene .25 plus clip-relative .25 .. .75.
    if action == 'trim_range':
        assert not any(values[:first * 2]) and not any(values[last * 2:])
        assert raw[first * 4:last * 4] == before_raw[first * 4:last * 4]
    else:
        assert not any(values[first * 2:last * 2])
        assert raw[:first * 4] == before_raw[:first * 4]
        assert raw[last * 4:] == before_raw[last * 4:]
    if action == 'cut_range':
        pasted = editor.paste(changed, scene, start=2)
        copied = pasted.scenes[0].cues[-1]
        assert copied.trim_start == .5 and copied.duration == .5 and copied.start == 2
        assert copied.path == cue.path
    assert source.read_bytes() == Path(cue.path).read_bytes() == original
    assert changed.original_script == project.original_script


def test_silencing_an_overlap_does_not_mute_another_track_or_move_its_time(prepared, tmp_path):
    app, editor = prepared
    project = editor.create()
    scene = project.scenes[0].id
    left_source = constant(tmp_path / 'left.wav', 5000)
    right_source = constant(tmp_path / 'right.wav', 3000)
    project = editor.add(project, scene, left_source, duration=1, pan=-1, gain_db=0)
    project = editor.add(project, scene, right_source, duration=1, pan=1, gain_db=0)
    left, right = project.scenes[0].cues
    muted = editor.silence_range(project, scene, left.id, '.25', '.75')
    assert next(item for item in muted.scenes[0].cues if item.id == right.id) == right
    _, output = render(editor, muted, tmp_path / 'muted')
    _, _, values = pcm(output)
    middle = values[6000 * 2:18000 * 2]
    assert not any(middle[::2]) and min(middle[1::2]) > 2900


def test_crossfade_is_an_actual_sample_aligned_overlap_and_one_revision(prepared, tmp_path):
    app, editor = prepared
    source_a, source_b = constant(tmp_path / 'a.wav', 8000), constant(tmp_path / 'b.wav', 4000)
    originals = source_a.read_bytes(), source_b.read_bytes()
    project = editor.create()
    scene = project.scenes[0].id
    project = editor.add(project, scene, source_a, duration=1, pan=-1, gain_db=0)
    project = editor.add(project, scene, source_b, duration=1, start=1, pan=-1, gain_db=0)
    left, right = project.scenes[0].cues
    changed = editor.crossfade(project, scene, left.id, right.id, '.25')
    first, second = changed.scenes[0].cues
    assert changed.revision == project.revision + 1
    assert (first.fade_out, second.fade_in, second.start) == (.25, .25, .75)
    _, output = render(editor, changed, tmp_path / 'crossfade')
    shape, _, values = pcm(output)
    assert shape == (24000, 2, 42000)
    assert not any(values[1::2])
    overlap = values[18000 * 2:24000 * 2:2]
    assert 3900 <= min(overlap) <= max(overlap) <= 8100
    assert overlap[0] > overlap[-1] and max(values[:18000 * 2:2]) > 7900
    assert (source_a.read_bytes(), source_b.read_bytes()) == originals
    assert editor.history(changed).scenes == project.scenes


def test_join_different_originals_creates_verified_stereo_derivative_not_source_rewrite(prepared, tmp_path):
    app, editor = prepared
    source_a, source_b = constant(tmp_path / 'a.wav', 7000), constant(tmp_path / 'b.wav', 4000)
    originals = source_a.read_bytes(), source_b.read_bytes()
    project = editor.create()
    scene = project.scenes[0].id
    project = editor.add(project, scene, source_a, duration=.5, pan=-1, gain_db=0)
    project = editor.add(project, scene, source_b, duration=.75, start=.5, pan=1, gain_db=0)
    left, right = project.scenes[0].cues
    selected_a = pcm(editor.preview(project, scene, left.id))[1]
    selected_b = pcm(editor.preview(project, scene, right.id))[1]
    joined = editor.join_rendered(project, scene, left.id, right.id)
    cue, = joined.scenes[0].cues
    assert joined.schema == 3 and joined.revision == project.revision + 1
    assert cue.id == left.id and cue.routing == 'stereo'
    assert cue.path not in (left.path, right.path) and cue.duration == 1.25
    assert (cue.gain_db, cue.pan, cue.trim_start, cue.fade_in, cue.fade_out) == (0, 0, 0, 0, 0)
    assert pcm(Path(cue.path))[1] == selected_a + selected_b
    _, master = render(editor, joined, tmp_path / 'joined')
    assert pcm(master)[1] == selected_a + selected_b
    assert editor.history(joined).scenes == project.scenes
    assert (source_a.read_bytes(), source_b.read_bytes()) == originals
    assert not list(editor.service.cache.glob('.clip-join-*'))


def test_saved_bypass_preserves_chain_and_exact_dry_wet_source(prepared, tmp_path):
    app, editor = prepared
    source = constant(tmp_path / 'source.wav')
    original = source.read_bytes()
    project = editor.create()
    scene = project.scenes[0].id
    project = editor.add(project, scene, source, duration=1, pan=-1, gain_db=0)
    cue = project.scenes[0].cues[0]
    effects = (Effect('gain', gain_db=-6), Effect('lowpass', frequency=5000, enabled=False))
    project = editor.set_effects(project, 'clip', effects, scene_id=scene, clip_id=cue.id)
    wet = pcm(editor.preview_range(project, scene, cue.id, '.25', '.75'))[2]
    dry = pcm(editor.preview_range(project, scene, cue.id, '.25', '.75', dry=True))[2]
    assert .497 < max(wet) / max(dry) < .505
    bypassed = editor.bypass_effects(project, scene, cue.id, True)
    saved = bypassed.scenes[0].cues[0]
    assert saved.effects == effects and saved.effects_bypassed
    assert pcm(editor.preview_range(bypassed, scene, cue.id, '.25', '.75'))[2] == dry
    restored = editor.history(bypassed)
    assert restored.scenes[0].cues[0].effects == effects
    assert not restored.scenes[0].cues[0].effects_bypassed
    assert source.read_bytes() == original


def test_marker_annotations_roundtrip_revision_and_real_common_backup(prepared, tmp_path, monkeypatch):
    from carr_library.library_backup import create_backup, restore_backup
    app, editor = prepared
    source = constant(tmp_path / 'original.wav')
    original = source.read_bytes()
    project = editor.create('Marker provenance')
    scene = project.scenes[0].id
    project = editor.add(project, scene, source, duration=1)
    project = editor.set_marker(project, scene, '00:00:00.125', 'Authored edit marker — Україна')
    first = project.scenes[0].markers[0]
    project = editor.set_marker(project, scene, '.25', 'Revised authored marker', marker_id=first.id)
    assert len(project.scenes[0].markers) == 1 and project.scenes[0].markers[0].id == first.id
    result, output = render(editor, project, tmp_path / 'marked')
    annotation, = result['result']['markers']
    assert annotation == dict(scene=scene, id=first.id, label='Revised authored marker', position=.25, start=.25)
    assert 'not transcript' in result['result']['marker_notice']
    assert result['result']['lines'] == []
    raw = encode(project.to_dict())
    assert Project.from_dict(json.loads(raw)) == project
    archive = tmp_path / 'backup'
    proof = create_backup(app, archive)
    restored = restore_backup(archive, tmp_path / 'restored', proof)
    reopened = ProductionService(restored / 'state/studio', voices=VoiceRegistry([]), runtime_factory=AudioRuntime)
    try:
        saved = reopened.projects.load(project.id)
        assert saved.scenes[0].markers == project.scenes[0].markers
        assert saved.original_script == project.original_script
        assert Path(saved.scenes[0].cues[0].path).read_bytes() == original
    finally:
        reopened.close()
    removed = editor.remove_marker(project, scene, first.id)
    assert not removed.scenes[0].markers
    assert editor.history(removed).scenes[0].markers == project.scenes[0].markers
    assert source.read_bytes() == original


@pytest.mark.parametrize('value', [
    dict(id='same', position=True, label='no'), dict(id='same', position=float('nan'), label='no'),
    dict(id='same', position=-1, label='no'), dict(id='same', position=0, label=''),
    dict(id='same', position=0, label='x' * 513)])
def test_marker_values_are_not_unbounded_or_untyped_state(value):
    with pytest.raises(ValueError):
        Marker(**value)


def test_duplicate_and_oversized_markers_are_rejected_before_audio_access():
    from carr_library.studio.project import Scene
    marker = Marker('one', 0, 'Label')
    with pytest.raises(ValueError, match='markers'):
        Scene('scene', 'Scene', (), minimum_duration=1, markers=(marker, marker))
    with pytest.raises(ValueError, match='markers'):
        Scene('scene', 'Scene', (), minimum_duration=1, markers=(marker,) * 10001)


@pytest.mark.parametrize('operation', ['copy_range', 'cut_range', 'trim_range', 'silence_range',
                                      'crossfade', 'set_marker', 'remove_marker', 'bypass', 'join'])
def test_cancelled_timeline_operations_never_publish_revision_or_rewrite_source(prepared, tmp_path, operation):
    app, editor = prepared
    source = constant(tmp_path / 'source.wav')
    original = source.read_bytes()
    project = editor.create()
    scene = project.scenes[0].id
    project = editor.add(project, scene, source, duration=.75)
    project = editor.add(project, scene, source, duration=.75, start=.75)
    project = editor.set_marker(project, scene, 0, 'Keep')
    left, right = project.scenes[0].cues
    marker = project.scenes[0].markers[0]
    options = dict(cancelled=lambda: True)
    calls = {
        'crossfade': lambda: editor.crossfade(project, scene, left.id, right.id, .25, **options),
        'set_marker': lambda: editor.set_marker(project, scene, 1, 'No write', **options),
        'remove_marker': lambda: editor.remove_marker(project, scene, marker.id, **options),
        'bypass': lambda: editor.bypass_effects(project, scene, left.id, True, **options),
        'join': lambda: editor.join_rendered(project, scene, left.id, right.id, **options),
    }
    invoke = calls.get(operation) or (lambda: getattr(editor, operation)(
        project, scene, left.id, .25, .5, **options))
    with pytest.raises((AudioCancelled, InterruptedError)):
        invoke()
    assert editor.service.projects.load(project.id) == project
    assert source.read_bytes() == original
    assert not list(editor.service.cache.glob('.clip-join-*'))


def test_failed_owned_close_is_retained_but_borrowed_service_never_closed(tmp_path, monkeypatch):
    borrowed = ProductionService(tmp_path / 'borrowed', voices=VoiceRegistry([]))
    editor = ClipEditor(tmp_path, service=borrowed)
    close = []
    monkeypatch.setattr(borrowed, 'close', lambda: close.append('borrowed'))
    assert editor.close() is True and close == []
    owned = ClipEditor(tmp_path / 'owned')
    attempts = []
    monkeypatch.setattr(owned.service, 'close', lambda: attempts.append('owned') or len(attempts) > 1)
    assert owned.close() is False
    assert owned.close() is True and attempts == ['owned', 'owned']
    with pytest.raises(ValueError, match='closed'):
        owned.create()


def test_passive_timeline_rejects_effect_marker_graph_tampering_without_engine_or_path_access(prepared, tmp_path, monkeypatch):
    from copy import deepcopy
    from carr_library import backup_producers, runtime_settings
    app, editor = prepared
    project = editor.create()
    scene = project.scenes[0].id
    project = editor.add(project, scene, constant(tmp_path / 'selected.wav'), duration=.5)
    cue = project.scenes[0].cues[0]
    project = editor.set_effects(project, 'clip', (Effect('gain', gain_db=-3),), scene_id=scene, clip_id=cue.id)
    project = editor.set_marker(project, scene, '.125', 'Authored marker')
    result, unused = render(editor, project, tmp_path / 'producer')
    value = result['result']
    before = encode(value)
    def forbidden(*args, **kwargs):
        pytest.fail('Passive metadata validation attempted engine or external path activation')
    monkeypatch.setattr(runtime_settings, 'audio_runtime', forbidden)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    backup_producers._timeline(value)
    mutations = []
    item = deepcopy(value); item['effects']['clip_bypass'][scene][cue.id] = 1; mutations.append(item)
    item = deepcopy(value); item['effects']['clips'][scene]['foreign'] = []; mutations.append(item)
    item = deepcopy(value); item['effects']['scenes']['foreign'] = []; mutations.append(item)
    item = deepcopy(value); item['effects']['master'] = [dict(kind='shell', parameters={}, enabled=True)]; mutations.append(item)
    item = deepcopy(value); item['effects']['master'] = [Effect('tempo', factor=2).to_dict()]; mutations.append(item)
    item = deepcopy(value); item['markers'][0]['position'] = True; mutations.append(item)
    item = deepcopy(value); item['markers'][0]['start'] += .125; mutations.append(item)
    item = deepcopy(value); item['markers'][0]['scene'] = 'foreign'; mutations.append(item)
    item = deepcopy(value); item['markers'].append(dict(item['markers'][0])); mutations.append(item)
    item = deepcopy(value); item['marker_notice'] = 'Verified source quotation'; mutations.append(item)
    item = deepcopy(value); item['effects']['unknown'] = {}; mutations.append(item)
    item = deepcopy(value); item.pop('markers'); mutations.append(item)
    for changed in mutations:
        with pytest.raises(ValueError):
            backup_producers._timeline(changed)
    assert len(mutations) == 12 and encode(value) == before


def test_pre_grid_trim_stem_receipt_cannot_alias_new_sample_cut_output(prepared, tmp_path):
    import math
    from carr_library.durable_state import atomic_json, digest_file
    from carr_library.studio.render import ENGINE_SCHEMA, production_runtime_identity, track_identity
    from carr_library.audio import Track
    app, editor = prepared
    source = tmp_path / 'Source at 44100.wav'
    samples = [round(6000 * math.sin(2 * math.pi * 3911 * frame / 44100))
               for frame in range(88200)]
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    original = source.read_bytes()
    project = editor.create('Old trim cache isolation')
    scene = project.scenes[0].id
    project = editor.add(project, scene, source, trim_start=.125 + .2 / 24000,
                         duration=1, start=0, pan=-1, gain_db=0)
    cue = project.scenes[0].cues[0]
    retained = Path(cue.path)
    runtime = editor.service._runtime()
    legacy = editor.service.cache / 'legacy-trim-before-resample.wav'
    # Independently retained old processing order, executed by the actual owned
    # runtime. The current mixer is not patched or replaced for this control.
    graph = (f'[0:a:0]atrim=start={cue.trim_start:.12g}:duration=1,asetpts=PTS-STARTPTS,'
             'aresample=24000,aformat=sample_fmts=fltp:channel_layouts=mono,aresample=24000,'
             'volume=0dB,pan=stereo|c0=1*c0|c1=0*c0,adelay=0S:all=1,'
             'apad=whole_len=24000,atrim=end_sample=24000[t0];'
             '[t0]amix=inputs=1:duration=longest:normalize=0[other];'
             '[other]amix=inputs=1:duration=longest:normalize=0[mixed];'
             '[mixed]alimiter=limit=0.95:level=0:latency=1,aresample=24000,'
             'apad=whole_len=24000,atrim=end_sample=24000[out]')
    with editor.service._audio_admission(runtime, operation_id='legacy-clip-grid-regression',
            disk_bytes=1024 * 1024, disk_path=editor.service.cache) as stopped:
        runtime._render([*runtime._base_args(), *runtime._input_args(retained),
            '-filter_complex', graph, '-map', '[out]'], legacy, expected_duration=1,
            rate=24000, channels=2, sources=(retained,), cancelled=stopped)
    old_track = dict(sha256=digest_file(retained), start=cue.start, gain_db=cue.gain_db,
        pan=cue.pan, role=cue.role, routing='pan', trim_start=cue.trim_start,
        duration=cue.duration, fade_in=cue.fade_in, fade_out=cue.fade_out, effects=[])
    runtime_key = production_runtime_identity(runtime, runtime.capabilities())
    old_identity = dict(tracks=[old_track], duration=1, sample_rate=24000)
    old_key = sha256(encode(dict(engine=ENGINE_SCHEMA, runtime=runtime_key,
                                kind='scene clip stem', input=old_identity))).hexdigest()
    atomic_json(editor.service.cache / (old_key + '.json'),
        dict(schema=1, key=old_key, file=legacy.name, sha256=digest_file(legacy),
             duration=1, sample_rate=24000, channels=2))
    actual_identity = track_identity(Track(retained, gain_db=0, pan=-1, role='sfx',
                                           trim_start=cue.trim_start, duration=1))
    assert actual_identity['selection_grid'] == 'resample-before-cut-v1'
    assert {key: value for key, value in actual_identity.items() if key != 'selection_grid'} == old_track
    result, master = render(editor, project, tmp_path / 'new grid')
    stem = Path(result['destination']) / 'stems/clips/sfx.wav'
    assert old_key not in result['nodes']
    assert pcm(stem)[0] == pcm(legacy)[0] == (24000, 2, 24000)
    assert pcm(stem)[1] != pcm(legacy)[1]
    assert pcm(stem)[1] == pcm(master)[1]
    assert source.read_bytes() == original
