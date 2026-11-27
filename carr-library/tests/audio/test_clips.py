"""Clip model/editor compatibility and source-preserving storage boundaries."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import struct
import wave

import pytest

from carr_library.audio.clips import ClipEditor, clip_directive, retain_pcm_asset
from carr_library.audio.clip_audio import clip_window
from carr_library.audio.ffmpeg import WaveInfo
from carr_library.audio.models import Clip, ProductionPlan, Scene, digest
from carr_library.audio.production import parse_script, plan_narration
from carr_library.audio.jobs import RenderCancelled
from carr_library.ui.audio_studio import StudioController


def pcm(path, frames=8000):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, frames, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', 3000) * frames)


def test_version_one_serialization_remains_the_exact_existing_contract():
    plan = parse_script('Hello')
    expected = {'schema_version': 1, 'title': 'Audio project', 'original_script': 'Hello',
        'input_kind': 'script', 'output_format': 'wav', 'sample_rate': 48000,
        'export_stems': True, 'disk_budget_bytes': 32 * 1024 ** 3,
        'roles': [{'name': 'Narrator', 'voice': 'sapi5:default', 'language': 'en', 'pan': 0.0,
                   'rate': 0, 'pitch': 0, 'volume': 1.0, 'effects': []}],
        'scenes': [{'id': 'scene-000001', 'title': 'Audio project', 'music': None,
                    'events': [{'id': 'event-00000001', 'kind': 'line', 'role': 'Narrator',
                                'text': 'Hello', 'literal': False, 'source_start': None, 'source_end': None}]}]}
    assert json.loads(json.dumps(plan.to_dict())) == expected
    assert digest(plan.to_dict()) == digest(expected)
    assert ProductionPlan.from_dict(expected) == plan
    changed = json.loads(json.dumps(expected))
    changed['scenes'][0]['clips'] = []
    with pytest.raises(ValueError, match='scene'):
        ProductionPlan.from_dict(changed)


def test_version_two_roundtrip_keeps_clip_identity_and_literal_mode_never_parses_it():
    clip = Clip('c1', 'слова.wav', .3, -6, -.5, 'microphone', .1, .6, .1, .2, "Ім'я & звук")
    script = '[output sample_rate=8000]\n' + clip_directive(clip) + '\n[duration 2]'
    plan = parse_script(script)
    assert plan.schema_version == 2 and plan.scenes[0].clips == (clip,)
    assert ProductionPlan.from_dict(json.loads(json.dumps(plan.to_dict()))) == plan
    narration = plan_narration(script)
    assert narration.input_kind == 'narration' and narration.schema_version == 1
    assert all(not scene.clips for scene in narration.scenes)
    assert ''.join(e.text for s in narration.scenes for e in s.events) == script
    with pytest.raises(ValueError, match='version two'):
        replace(plan, schema_version=1)


@pytest.mark.parametrize('values', [
    {'start': True}, {'start': float('nan')}, {'duration': 0}, {'pan': 2},
    {'gain_db': 13}, {'fade_in': .8, 'fade_out': .3, 'duration': 1},
    {'track': 'shell'}, {'asset': '../escape.wav'}], ids=[
    'boolean', 'nan', 'zero-duration', 'pan', 'gain', 'overlapping-fades', 'track', 'path'])
def test_invalid_clip_controls_fail_before_processing(values):
    with pytest.raises(ValueError):
        Clip(**{'id': 'c1', 'asset': 'audio.wav', **values})


def test_clip_count_and_sample_trim_bounds_are_not_silent_clamps():
    with pytest.raises(ValueError, match='thirty-two'):
        Scene('scene', 'Title', (), clips=tuple(Clip('c'+str(n), 'a.wav') for n in range(33)))
    info = WaveInfo(8000, 1, 8000, 44, 16000)
    clip = Clip('clip', 'a.wav', .2, trim_start=.1, duration=.4, fade_in=.1, fade_out=.1)
    window = clip_window(clip, info, 16000)
    assert (window.source_start, window.source_end, window.frames, window.start_frame) == (800, 4000, 6400, 3200)
    for invalid in (replace(clip, trim_start=1), replace(clip, duration=2),
                    replace(clip, trim_start=.999999, duration=.000001, fade_in=0, fade_out=0)):
        with pytest.raises(ValueError, match='samples'):
            clip_window(invalid, info, 16000)


def test_editor_retains_independent_pcm_and_preserves_unrelated_crlf_draft(tmp_path):
    root, source = tmp_path / 'audio', tmp_path / 'original.wav'
    root.mkdir()
    pcm(source)
    original = source.read_bytes()
    script = '[role Alice voice="fixture:voice"]\r\n# Перша\r\nAlice: Exact words.\r\n# Друга\r\n[pause 1]'
    editor = ClipEditor(script, root)
    result = editor.apply({'action': 'add', 'scene_id': editor.plan.scenes[0].id,
                           'source': source, 'values': {'duration': .5, 'title': 'Голос'}})
    assert source.read_bytes() == original
    expected = root / 'assets' / (sha256(original).hexdigest() + '.wav')
    assert expected.read_bytes() == original and expected.stat().st_nlink == 1
    assert result['script'].startswith(script.split('# Друга')[0])
    assert result['script'].endswith('# Друга\r\n[pause 1]')
    changed = ClipEditor(result['script'], root)
    clip = changed.plan.scenes[0].clips[0]
    edited = changed.apply({'action': 'edit', 'clip_id': clip.id, 'values': {'gain_db': -6, 'pan': 1}})
    moved = ClipEditor(edited['script'], root).apply(
        {'action': 'move', 'clip_id': clip.id, 'scene_id': changed.plan.scenes[1].id})
    final = ClipEditor(moved['script'], root)
    assert not final.plan.scenes[0].clips
    assert final.plan.scenes[1].clips[0].id == clip.id
    assert final.plan.scenes[1].clips[0].gain_db == -6
    assert source.read_bytes() == original and expected.read_bytes() == original


def test_cancel_and_alias_refusal_leave_source_and_script_unchanged(tmp_path):
    root, source = tmp_path / 'audio', tmp_path / 'original.wav'
    root.mkdir()
    pcm(source)
    editor = ClipEditor('[duration 1]', root)
    with pytest.raises(RenderCancelled):
        editor.apply({'action': 'add', 'scene_id': editor.plan.scenes[0].id,
                      'source': source}, cancelled=lambda: True)
    assert editor.original == '[duration 1]' and not (root / 'assets').exists()
    name, _ = retain_pcm_asset(source, root)
    original = source.read_bytes()
    (root / 'assets' / name).write_bytes(b'changed')
    with pytest.raises(ValueError, match='different or aliased'):
        retain_pcm_asset(source, root)
    assert source.read_bytes() == original


def test_removing_last_clip_keeps_its_chapter_and_source_asset(tmp_path):
    root = tmp_path / 'audio'
    root.mkdir()
    editor = ClipEditor('[clip input.wav id=c1]\n# Second\n[pause 1]', root)
    result = editor.apply({'action': 'remove', 'clip_id': 'c1'})
    plan = parse_script(result['script'])
    assert len(plan.scenes) == 2 and plan.scenes[0].minimum_duration == 1


def test_controller_refuses_stale_edit_and_output_root_then_uses_existing_undo(tmp_path):
    root, other = tmp_path / 'audio', tmp_path / 'other'
    root.mkdir()
    other.mkdir()
    controller = StudioController(private_root=tmp_path / 'library')
    controller.new('[duration 1]', input_kind='script')
    controller.configure(output_dir=root)
    editor = controller.clip_editor(controller.clip_snapshot())
    value = controller.prepare_clip_edit(editor, {'action': 'duration', 'scene_id': 'scene-000001',
                                                  'minimum_duration': 2})
    controller.configure(output_dir=other)
    with pytest.raises(ValueError, match='draft changed'):
        controller.accept_clip_edit(value)
    controller.configure(output_dir=root)
    before = controller.script
    controller.accept_clip_edit(value)
    assert controller.script != before and parse_script(controller.script).scenes[0].minimum_duration == 2
    controller.draft.undo()
    assert controller.script == before
    controller.configure(input_kind='narration')
    with pytest.raises(ValueError, match='Structured'):
        controller.clip_snapshot()


def test_retained_input_retries_post_move_directory_durability_before_acknowledging(tmp_path, monkeypatch):
    from carr_library.audio import clips, jobs
    root, source = tmp_path / 'audio', tmp_path / 'original.wav'
    root.mkdir()
    pcm(source)
    original = source.read_bytes()
    target = root / 'assets' / (sha256(original).hexdigest() + '.wav')
    actual_sync, failing, observed = jobs._sync_directory, [True], []
    def sync(directory):
        if Path(directory) == target.parent and target.exists():
            observed.append(target.read_bytes())
            if failing[0]:
                raise OSError('Injected retained-input directory flush failure')
        return actual_sync(directory)
    monkeypatch.setattr(jobs, '_sync_directory', sync)
    monkeypatch.setattr(clips, '_sync_directory', sync)
    with pytest.raises(OSError, match='flush failure'):
        retain_pcm_asset(source, root)
    assert target.read_bytes() == original
    with pytest.raises(OSError, match='flush failure'):
        retain_pcm_asset(source, root)
    assert len(observed) == 2
    failing[0] = False
    name, info = retain_pcm_asset(source, root)
    assert name == target.name and info.frames == 8000
    assert observed[-1] == original and source.read_bytes() == original


def editing_fixture(tmp_path, *, sample_rate=8000):
    root, source = tmp_path / 'audio', tmp_path / 'source.wav'
    root.mkdir()
    pcm(source, frames=32000)
    name, info = retain_pcm_asset(source, root)
    clip = Clip('kept-clip', name, start=.5, trim_start=1, duration=2,
                fade_in=.2, fade_out=.3, gain_db=-3, pan=.25, title='Мова 🙂')
    prefix = f'[output sample_rate={sample_rate}]\r\n# Chapter\r\nNarrator: Exact words.\r\n'
    script = prefix + clip_directive(clip) + '\r\n[duration 5]\r\n'
    return root, source, clip, info, prefix, ClipEditor(script, root)


def test_copy_paste_preserves_retained_bytes_and_saved_controls(tmp_path):
    root, source, clip, _, prefix, editor = editing_fixture(tmp_path)
    before = source.read_bytes()
    copied = editor.apply({'action': 'copy', 'clip_id': clip.id})
    assert 'script' not in copied and copied['clip_id'] == clip.id
    result = editor.apply({'action': 'paste', 'scene_id': 'scene-000001',
                           'position': 3.25, 'clipboard': copied['clipboard']})
    plan = parse_script(result['script'])
    first, pasted = plan.scenes[0].clips
    assert first == clip and pasted.id != clip.id and pasted.start == 3.25
    assert replace(pasted, id=clip.id, start=clip.start) == clip
    assert result['script'].startswith(prefix)
    assert source.read_bytes() == before
    assert (root / 'assets' / clip.asset).read_bytes() == before
    assert list((root / 'assets').iterdir()) == [root / 'assets' / clip.asset]


def test_cut_paste_keeps_empty_chapter_and_original_asset(tmp_path):
    root, source, clip, _, _, _ = editing_fixture(tmp_path)
    script = '# First\r\n' + clip_directive(clip) + '\r\n# Second\r\n[duration 5]'
    editor = ClipEditor(script, root)
    cut = editor.apply({'action': 'cut', 'clip_id': clip.id})
    after = ClipEditor(cut['script'], root)
    assert len(after.plan.scenes) == 2 and after.plan.scenes[0].minimum_duration == 1
    assert not any(scene.clips for scene in after.plan.scenes)
    pasted = after.apply({'action': 'paste', 'scene_id': after.plan.scenes[1].id,
                         'clipboard': cut['clipboard'], 'position': 0})
    result = parse_script(pasted['script'])
    assert len(result.scenes[1].clips) == 1
    assert (root / 'assets' / clip.asset).read_bytes() == source.read_bytes()
    assert pasted['script'].startswith('# First\r\n[duration 1]\r\n# Second\r\n')


@pytest.mark.parametrize('sample_rate', [8000, 16000, 44100])
def test_split_then_reverse_selected_join_keeps_exact_source_and_timeline_samples(tmp_path, sample_rate):
    root, source, clip, info, prefix, editor = editing_fixture(tmp_path, sample_rate=sample_rate)
    original = source.read_bytes()
    before = clip_window(clip, info, sample_rate)
    result = editor.apply({'action': 'split', 'clip_id': clip.id, 'split_at': 1.25})
    changed = ClipEditor(result['script'], root)
    first, second = changed.plan.scenes[0].clips
    a, b = clip_window(first, info, sample_rate), clip_window(second, info, sample_rate)
    assert first.id == clip.id and second.id == result['clip_id'] and first.id != second.id
    assert a.source_start == before.source_start and a.source_end == b.source_start
    assert b.source_end == before.source_end and a.frames + b.frames == before.frames
    assert a.start_frame + a.frames == b.start_frame
    assert a.fade_in == before.fade_in and a.fade_out == b.fade_in == 0
    assert b.fade_out == before.fade_out
    joined = changed.apply({'action': 'join', 'clip_id': second.id, 'other_id': first.id})
    merged = parse_script(joined['script']).scenes[0].clips
    assert len(merged) == 1 and merged[0].id == clip.id
    assert clip_window(merged[0], info, sample_rate) == before
    assert joined['script'].startswith(prefix)
    assert source.read_bytes() == original and (root / 'assets' / clip.asset).read_bytes() == original


@pytest.mark.parametrize('at', [-1, 0, 2, 3, float('nan'), True, .000001, .1, 1.9])
def test_split_refuses_empty_samples_outside_bounds_and_cuts_inside_fades(tmp_path, at):
    root, source, clip, _, _, editor = editing_fixture(tmp_path)
    before = source.read_bytes()
    with pytest.raises(ValueError):
        editor.apply({'action': 'split', 'clip_id': clip.id, 'split_at': at})
    assert source.read_bytes() == before and (root / 'assets' / clip.asset).read_bytes() == before
    assert editor.plan.scenes[0].clips == (clip,)


@pytest.mark.parametrize('change', [
    {'start': 1.01}, {'trim_start': 1.01}, {'gain_db': -1}, {'pan': .5},
    {'track': 'music'}, {'fade_in': .1}])
def test_join_refuses_a_gap_mismatched_processing_or_an_interior_fade(tmp_path, change):
    root, _, clip, _, _, _ = editing_fixture(tmp_path)
    first = replace(clip, id='first', start=0, trim_start=0, duration=1,
                    fade_in=0, fade_out=0, gain_db=0, pan=0)
    second = replace(first, **{'id': 'second', 'start': 1, 'trim_start': 1, **change})
    editor = ClipEditor(clip_directive(first) + '\n' + clip_directive(second), root)
    with pytest.raises(ValueError, match='adjacent'):
        editor.apply({'action': 'join', 'clip_id': first.id, 'other_id': second.id})
    assert len(editor.plan.scenes[0].clips) == 2


@pytest.mark.parametrize('case', ['changed-audio', 'different-project', 'missing-copy'])
def test_paste_requires_the_unchanged_copied_asset_in_its_original_project(tmp_path, case):
    root, _, clip, _, _, editor = editing_fixture(tmp_path)
    copied = editor.apply({'action': 'copy', 'clip_id': clip.id})['clipboard']
    if case == 'changed-audio':
        retained = root / 'assets' / clip.asset
        raw = bytearray(retained.read_bytes())
        raw[-1] ^= 1
        retained.write_bytes(raw)
    elif case == 'different-project':
        other = tmp_path / 'other'
        other.mkdir()
        editor = ClipEditor('[duration 5]', other)
    else:
        copied = None
    with pytest.raises(ValueError):
        editor.apply({'action': 'paste', 'scene_id': 'scene-000001',
                      'clipboard': copied, 'position': 0})


def test_clipboard_and_split_cannot_bypass_the_existing_scene_limit(tmp_path):
    root, _, clip, _, _, _ = editing_fixture(tmp_path)
    script = '\n'.join(clip_directive(replace(clip, id=f'clip-{index}')) for index in range(32))
    editor = ClipEditor(script, root)
    copied = editor.apply({'action': 'copy', 'clip_id': 'clip-0'})['clipboard']
    for request in ({'action': 'paste', 'scene_id': 'scene-000001', 'clipboard': copied, 'position': 0},
                    {'action': 'split', 'clip_id': 'clip-0', 'split_at': 1}):
        with pytest.raises(ValueError, match='thirty-two'):
            editor.apply(request)
    assert len(editor.plan.scenes[0].clips) == 32


def test_cancelled_cut_never_changes_the_draft_or_retained_source(tmp_path):
    root, source, clip, _, _, editor = editing_fixture(tmp_path)
    before = editor.original
    with pytest.raises(RenderCancelled):
        editor.apply({'action': 'cut', 'clip_id': clip.id}, cancelled=lambda: True)
    assert editor.original == before
    assert (root / 'assets' / clip.asset).read_bytes() == source.read_bytes()


def test_controller_accepts_clipboard_only_after_stale_draft_guard_and_reuses_undo(tmp_path):
    root, source, clip, _, _, editor = editing_fixture(tmp_path)
    controller = StudioController(private_root=tmp_path / 'library')
    controller.new(editor.script, input_kind='script')
    controller.configure(output_dir=root)
    captured = controller.clip_editor(controller.clip_snapshot())
    cut = controller.prepare_clip_edit(captured, {'action': 'cut', 'clip_id': clip.id})
    controller.edit_script(editor.script + '\nNarrator: Later.')
    with pytest.raises(ValueError, match='changed'):
        controller.accept_clip_edit(cut)
    assert controller._clip_clipboard is None
    controller.draft.undo()
    controller.accept_clip_edit(cut)
    assert controller._clip_clipboard is cut['clipboard']
    current = controller.clip_editor(controller.clip_snapshot())
    paste = controller.prepare_clip_edit(current, {'action': 'paste', 'scene_id': 'scene-000001',
                                                   'position': 1})
    controller.accept_clip_edit(paste)
    assert controller.plan().scenes[0].clips[0].start == 1
    controller.draft.undo()
    assert not controller.plan().scenes[0].clips
    controller.new('[duration 1]', input_kind='script')
    assert controller._clip_clipboard is None
    assert (root / 'assets' / clip.asset).read_bytes() == source.read_bytes()


def test_opening_a_saved_project_clears_the_session_clipboard(tmp_path):
    root, _, clip, _, _, editor = editing_fixture(tmp_path)
    controller = StudioController(private_root=tmp_path / 'library')
    controller.new(editor.script, input_kind='script')
    controller.configure(output_dir=root)
    project = tmp_path / 'project.json'
    controller.save(project)
    captured = controller.clip_editor(controller.clip_snapshot())
    controller.accept_clip_edit(controller.prepare_clip_edit(
        captured, {'action': 'copy', 'clip_id': clip.id}))
    assert controller._clip_clipboard is not None
    controller.open(project)
    assert controller._clip_clipboard is None
