"""Versioned master edits and actual PCM; the speech fixture is synthetic."""
from array import array
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest

from carr_library.audio.effect_editing import EffectPreset, RoleEffectEditor
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.jobs import RenderCancelled
from carr_library.audio.mastering import process_master, split_master
from carr_library.audio.models import Effect, ProductionPlan, digest
from carr_library.audio.production import parse_script, plan_narration, render_project
from carr_library.audio.runtime import resolve_ffmpeg


@pytest.fixture
def ffmpeg():
    path = resolve_ffmpeg()
    if path is None:
        pytest.skip('Actual master signal checks require local FFmpeg.')
    return path


def stereo(path, *, frames=16000, rate=8000, amplitude=5000):
    values = array('h', (round(amplitude * math.sin(2 * math.pi * frequency * index / rate))
                        for index in range(frames) for frequency in (311, 487)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((2, 2, rate, frames, 'NONE', 'PCM'))
        stream.writeframes(values.tobytes())
    return path


def pcm(path):
    info = wav_info(path)
    with Path(path).open('rb') as stream:
        stream.seek(info.data_offset)
        raw = stream.read(info.frames * info.channels * 2)
    return raw


def samples(path):
    result = array('h', pcm(path))
    if sys.byteorder != 'little':
        result.byteswap()
    return result


class SyntheticEngine:
    def __init__(self):
        self.calls = []

    def cache_identity(self, voice):
        return {'fixture': 'master-synthetic-stereo-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        self.calls.append(text)
        return stereo(output, frames=1600)


@pytest.mark.parametrize('source,expected', [
    ('Exact words.', '89d876663e66d633a0216e88d425ac288d99c7230e76ff9c15bc0659955e93db'),
    ('[role Alice pan=left]\n[effect Alice gain gain_db=-3]\nAlice: Exact words.',
     'e4023f780756999bdbee3c81c991bb2c74e85cf5be8804434a72b0c8f0b0868b'),
    ('[clip retained.wav id=bed start=.25 duration=1]\n[duration 2]\nExact words.',
     'fb242a01b1479701ab92fc6d7adf6896344ba5bc41b04e10dcf103e786e64a6f'),
])
def test_actual_pre_master_v1_v2_plan_fingerprints_are_unchanged(source, expected):
    # Captured from exact public 947193c before adding v3 fields.
    plan = parse_script(source)
    assert digest(plan.to_dict()) == expected
    assert not {'master_mix', 'master_effects'} & plan.to_dict().keys()
    assert ProductionPlan.from_dict(plan.to_dict()) == plan


def test_literal_narration_never_parses_the_new_master_directives():
    plan = plan_narration('[master mix=.5]\fExact words.')
    assert digest(plan.to_dict()) == '0ea97fe2c240dfa16107d2b6e30a899a55c7e3db27733e3bf2ba14f24cacf345'
    assert plan.schema_version == 1 and plan.master_effects == ()
    assert plan.scenes[0].events[0].text == '[master mix=.5]'


def test_v3_master_and_clips_roundtrip_preserves_source_and_explicit_fields():
    source = '[master mix=.5]\n[master-effect gain gain_db=-6]\n[clip bed.wav id=bed]\nWords.'
    plan = parse_script(source)
    assert plan.schema_version == 3 and plan.master_mix == .5
    assert plan.master_effects == (Effect('gain', (('gain_db', -6.),)),)
    assert plan.scenes[0].clips[0].id == 'bed'
    assert ProductionPlan.from_dict(json.loads(json.dumps(plan.to_dict()))) == plan
    assert plan.original_script == source
    for field in ('master_mix', 'master_effects'):
        raw = plan.to_dict()
        del raw[field]
        with pytest.raises(ValueError, match='explicit'):
            ProductionPlan.from_dict(raw)
    raw = parse_script('Words.').to_dict()
    with pytest.raises(ValueError, match='legacy'):
        ProductionPlan.from_dict({**raw, 'master_mix': 1, 'master_effects': []})


@pytest.mark.parametrize('directive', [
    '[master mix=nan]', '[master mix=1.1]', '[master mix=-.1]', '[master mix=true]',
    '[master mix=.5]\n[master mix=.5]', '[master unknown=1]',
    '[master-effect tempo factor=1]', '[master-effect pitch semitones=0]',
    '[master-effect gain gain_db=-3 enabled=False]',
    '[master-effect gain gain_db=nan enabled=false]', '[master-effect gain]\n' * 17,
])
def test_master_directives_fail_closed_before_any_native_work(directive):
    with pytest.raises(ValueError):
        parse_script(directive + '\nExact words.')


@pytest.mark.parametrize('prefix', ['Words.', '[pause .2]', '[clip bed.wav id=bed]', '[duration 2]',
                                   '# First\nWords.\n# Second'])
def test_global_master_directives_cannot_retroactively_change_started_timeline(prefix):
    with pytest.raises(ValueError, match='before'):
        parse_script(prefix + '\n[master-effect gain]\nWords.')


def test_combined_role_master_edit_retains_exact_non_effect_source_and_reversible_draft():
    from carr_library.reader.narration_text import NarrationDraft

    source = ('[role Master pan=left]\r\n [effect Master gain gain_db=-3] \r\n'
              '# Оригінальний розділ\r\nMaster:  Точні слова. \r\nNarrator: After.')
    editor = RoleEffectEditor(source)
    assert editor.targets == ('Narrator', 'Master', None)
    editor.bypass('Master', 0)
    editor.add(None, Effect('gain', (('gain_db', -6),)))
    editor.set_master_mix(.5)
    changed = editor.script()
    before, after = editor.plan, parse_script(changed)
    assert after.schema_version == 3 and after.master_mix == .5
    assert before.scenes == after.scenes
    assert changed.endswith('# Оригінальний розділ\r\nMaster:  Точні слова. \r\nNarrator: After.')
    assert after.roles[1].pan == -1 and after.roles[1].effects[0].enabled is False
    draft = NarrationDraft(source, source_id='reviewed-original')
    assert draft.edit_text(changed, reason='Explicit master and role edit')
    assert draft.source_text == source and draft.undo() and draft.text == source


def test_master_reorder_and_bypass_reuse_presets_but_refuse_active_timing_effects():
    source = (' [master mix=5e-1] \r\n [master-effect gain gain_db=-6e0] \r\n'
              '[master-effect compressor ratio=2]\r\nNarrator: Words.')
    editor = RoleEffectEditor(source)
    assert editor.script() == source
    editor.move(None, 1, 0)
    changed = editor.script()
    assert changed.startswith(' [master mix=5e-1] \r\n[master-effect compressor ratio=2]\r\n'
                              ' [master-effect gain gain_db=-6e0] \r\n')
    before = editor.effects(None)
    with pytest.raises(ValueError, match='roles'):
        editor.apply_preset(None, EffectPreset('Faster', (Effect('tempo'),)))
    assert editor.effects(None) == before
    editor.apply_preset(None, EffectPreset('Inactive pitch', (Effect('pitch', enabled=False),)))
    with pytest.raises(ValueError, match='roles'):
        editor.bypass(None, 0)
    assert not editor.effects(None)[0].enabled
    assert parse_script(editor.script()).master_effects == editor.effects(None)


@pytest.mark.parametrize('mix', [0, .5, 1])
def test_real_pcm_linear_dry_wet_has_exact_ratio_stereo_and_frame_count(tmp_path, ffmpeg, mix):
    source = stereo(tmp_path / 'original.wav')
    original = source.read_bytes()
    effect = Effect('gain', (('gain_db', -12),))
    output = process_master(source, tmp_path / 'output.wav', ffmpeg=ffmpeg, effects=(effect,), mix=mix)
    gain = 1 - mix + mix * 10 ** (-12 / 20)
    before, after = samples(source), samples(output)
    assert len(after) == len(before) and wav_info(output).rate == 8000
    assert max(abs(actual - expected * gain) for actual, expected in zip(after, before)) <= 1.1
    assert after[::2] != after[1::2]  # The original stereo placement was not flattened.
    assert source.read_bytes() == original


@pytest.mark.parametrize('frames', [1, 17, 79, 8000])
def test_real_pcm_master_never_drops_short_programmes(tmp_path, ffmpeg, frames):
    source = stereo(tmp_path / 'original.wav', frames=frames)
    output = process_master(source, tmp_path / 'short.wav', ffmpeg=ffmpeg)
    assert wav_info(output).frames == frames and pcm(output) == pcm(source)


def test_real_master_order_and_bypass_change_only_the_selected_processing(tmp_path, ffmpeg):
    source = stereo(tmp_path / 'original.wav', amplitude=7000)
    gain = Effect('gain', (('gain_db', 12),))
    limiter = Effect('limiter', (('ceiling_db', -18),))
    one = process_master(source, tmp_path / 'gain-first.wav', ffmpeg=ffmpeg, effects=(gain, limiter))
    two = process_master(source, tmp_path / 'limiter-first.wav', ffmpeg=ffmpeg, effects=(limiter, gain))
    bypassed = process_master(source, tmp_path / 'bypassed.wav', ffmpeg=ffmpeg,
                             effects=(replace(gain, enabled=False),))
    assert max(map(abs, samples(two))) > max(map(abs, samples(one))) * 3
    assert pcm(bypassed) == pcm(source)
    assert wav_info(one).frames == wav_info(two).frames == wav_info(source).frames


def test_actual_master_child_read_is_protected_against_a_restored_same_size_write(tmp_path, ffmpeg, monkeypatch):
    from carr_library.audio import mastering

    source = stereo(tmp_path / 'original.wav', frames=8000)
    before = source.read_bytes()
    output = tmp_path / 'result.wav'
    real = mastering.audio.run_ffmpeg
    observed = []
    command = [sys.executable, '-I', '-c',
        'from pathlib import Path\nimport os, sys\n'
        'p=Path(sys.argv[1]); stamp=p.stat(); raw=p.read_bytes()\n'
        'try:\n'
        ' with p.open("r+b") as f:\n'
        '  f.seek(50); f.write(b"\\x01\\x00"); f.flush(); os.fsync(f.fileno())\n'
        '  f.seek(0); f.write(raw); f.flush(); os.fsync(f.fileno())\n'
        ' os.utime(p,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))\n'
        ' print("restored")\n'
        'except PermissionError:\n print("blocked")\n', str(source)]
    def actual_read(*args, **kwargs):
        # Deterministic invocation barrier: the competing native process runs
        # precisely inside the input interval, before real FFmpeg reads it.
        child = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        observed.append(child.stdout.strip())
        return real(*args, **kwargs)
    monkeypatch.setattr(mastering.audio, 'run_ffmpeg', actual_read)
    if os.name == 'nt':
        process_master(source, output, ffmpeg=ffmpeg)
        assert observed == ['blocked'] and output.is_file()
    else:
        with pytest.raises(RuntimeError, match='changed'):
            process_master(source, output, ffmpeg=ffmpeg)
        assert observed == ['restored'] and not output.exists()
    assert source.read_bytes() == before
    assert not list(tmp_path.glob('.master-*'))
    with source.open('r+b') as stream:
        stream.seek(50)
        stream.write(b'\x01\x00')  # The final/exceptional exit released the lease.


def test_final_master_input_validation_finishes_before_publication(tmp_path, ffmpeg, monkeypatch):
    from contextlib import contextmanager
    from carr_library.audio import mastering

    source = stereo(tmp_path / 'original.wav', frames=8000)
    output = tmp_path / 'result.wav'
    reader = mastering.verified_reader
    observed = []
    @contextmanager
    def refuse_after_read(*args, **kwargs):
        with reader(*args, **kwargs) as stream:
            yield stream
        observed.append(True)
        assert not output.exists()
        raise RuntimeError('final input validation refused')
    monkeypatch.setattr(mastering, 'verified_reader', refuse_after_read)
    with pytest.raises(RuntimeError, match='final input validation refused'):
        process_master(source, output, ffmpeg=ffmpeg)
    assert observed and not output.exists() and not list(tmp_path.glob('.master-*'))


def test_real_master_delay_crosses_chapters_once_and_partitions_are_byte_exact(tmp_path, ffmpeg):
    source = tmp_path / 'impulse.wav'
    values = array('h', [0] * 16000)
    values[7800:7802] = array('h', [10000, -8000])
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((2, 2, 8000, 8000, 'NONE', 'PCM'))
        stream.writeframes(values.tobytes())
    output = process_master(source, tmp_path / 'master.wav', ffmpeg=ffmpeg,
                            effects=(Effect('delay', (('delay_ms', 100), ('decay', .5))),))
    assert wav_info(output).frames == 8000
    assert max(map(abs, samples(output)[8000:])) > 1000
    parts = split_master(output, ((0, 4000), (4000, 8000)), tmp_path)
    assert [wav_info(part).frames for part in parts] == [4000, 4000]
    assert b''.join(pcm(part) for part in parts) == pcm(output)


@pytest.mark.parametrize('bounds', [((1, 8000),), ((0, 4000), (4001, 8000)), ((0, 7999),),
                                     ((0, 4001), (4000, 8000)), ((False, 8000),)])
def test_invalid_chapter_partition_creates_no_partial_chapter(tmp_path, bounds):
    source = stereo(tmp_path / 'source.wav', frames=8000)
    with pytest.raises(ValueError):
        split_master(source, bounds, tmp_path)
    assert not list(tmp_path.glob('chapter-*.wav'))


def test_real_render_master_only_edit_reuses_speech_and_keeps_premaster_stems(tmp_path, ffmpeg):
    engine = SyntheticEngine()
    plain = '[output sample_rate=8000]\n# One\nNarrator: First.\n# Two\nNarrator: Second.'
    plan = parse_script('[master mix=1]\n[master-effect gain gain_db=-6]\n' + plain)
    first = render_project(plan, tmp_path / 'project', engine=engine, ffmpeg=ffmpeg)
    assert engine.calls == ['First.', 'Second.']
    assert first['master_processing']['mix'] == 1
    assert first['master_processing']['output_sha256'] == hashlib.sha256(Path(first['output']).read_bytes()).hexdigest()
    assert all('pre-master' in label for label in first['stem_labels'].values())
    chapters = sorted(Path(first['output']).parent.glob('chapter-*.wav'))
    assert b''.join(pcm(path) for path in chapters) == pcm(first['output'])
    assert [wav_info(path).frames for path in chapters] == [1600, 1600]
    changed = replace(plan, master_mix=.5)
    second = render_project(changed, tmp_path / 'project', engine=engine, ffmpeg=ffmpeg)
    assert engine.calls == ['First.', 'Second.'] and second['run_key'] != first['run_key']
    assert first['chapters'] == second['chapters']
    assert Path(first['subtitles']).read_bytes() == Path(second['subtitles']).read_bytes()
    assert [pcm(path) for path in first['stems']] == [pcm(path) for path in second['stems']]
    resumed = render_project(changed, tmp_path / 'project', engine=engine, ffmpeg=ffmpeg)
    assert resumed['output'] == second['output'] and len(engine.calls) == 2


def test_master_cancel_never_publishes_partial_render_and_resume_reuses_speech(tmp_path, ffmpeg, monkeypatch):
    from carr_library.audio import mastering

    cancelled = [False]
    real = mastering.process_master
    def cancel_after_processing(*args, **kwargs):
        result = real(*args, **kwargs)
        cancelled[0] = True
        return result

    engine = SyntheticEngine()
    plan = parse_script('[master mix=.5]\n[master-effect gain gain_db=-6]\n'
                        '[output sample_rate=8000 stems=false]\nWords.')
    root = tmp_path / 'project'
    monkeypatch.setattr(mastering, 'process_master', cancel_after_processing)
    with pytest.raises(RenderCancelled):
        render_project(plan, root, engine=engine, ffmpeg=ffmpeg, cancelled=lambda: cancelled[0])
    assert len(engine.calls) == 1 and not list((root / 'renders').glob('*/complete.json'))
    assert not list(root.glob('.work-*')) and not list(root.rglob('.master-*'))
    monkeypatch.setattr(mastering, 'process_master', real)
    output = render_project(plan, root, engine=engine, ffmpeg=ffmpeg)
    assert len(engine.calls) == 1 and output['frames'] == 1600


def test_completed_master_survives_full_library_backup_and_exported_publication(tmp_path, ffmpeg):
    from carr_library.application import LibraryApp
    from carr_library.audio.publishing import PublicationService, inspect_epub
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    from carr_library.ui.audio_studio import StudioController

    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Mastered source')
    source = '[output sample_rate=8000]\n# First\nТочні слова.\n# Second\nMore exact words.'
    controller = StudioController(private_root=app.root)
    controller.new(source, input_kind='script')
    editor = RoleEffectEditor(source)
    editor.add(None, Effect('gain', (('gain_db', -6),)))
    editor.set_master_mix(.5)
    controller.edit_script(editor.script())
    controller.save_in_library()
    relative = controller.project_path.relative_to(app.root)
    plan = parse_script(controller.script)
    engine = SyntheticEngine()
    before = render_project(plan, controller.output_dir, engine=engine, ffmpeg=ffmpeg)
    manifest = create_backup(app, tmp_path / 'backup')
    verify_backup(tmp_path / 'backup', manifest)
    restored = tmp_path / 'restored'
    restore_backup(tmp_path / 'backup', restored, manifest)
    reopened = StudioController(private_root=restored).open(restored / relative)
    assert reopened.draft.source_text == source and reopened.script == controller.script
    after = render_project(plan, reopened.output_dir, engine=engine, ffmpeg=ffmpeg)
    assert len(engine.calls) == 2 and pcm(before['output']) == pcm(after['output'])
    assert before['master_processing'] == after['master_processing']
    export = tmp_path / 'mastered.epub'
    service = PublicationService(reopened.output_dir, private_root=restored, ffmpeg=ffmpeg,
                                 reserve_bytes=0, expected_plan_sha256=digest(plan.to_dict()))
    service.export_epub(after['run_key'], export, language='uk')
    inspected = inspect_epub(export)
    assert inspected and export.is_file()
    from carr_library.audio.daisy import inspect_daisy
    daisy = tmp_path / 'mastered-daisy.zip'
    service.export_daisy(after['run_key'], daisy, publisher='Synthetic test', language='uk')
    assert inspect_daisy(daisy) and daisy.is_file()


def test_clip_preview_explicitly_excludes_programme_master_processing(tmp_path, ffmpeg, monkeypatch):
    from carr_library.audio.clips import ClipEditor
    from carr_library.operations import ResourceBroker
    from carr_library.operations import admission

    root = tmp_path / 'project'
    (root / 'assets').mkdir(parents=True)
    stereo(root / 'assets' / 'tone.wav', frames=8000)
    source = '[master mix=.5]\n[master-effect gain gain_db=-6]\n[clip tone.wav id=bed]\nWords.'
    editor = ClipEditor(source, project_dir=root)
    captured = []
    real_request = admission.production_request
    def request(plan, *args, **kwargs):
        captured.append(plan)
        return real_request(plan, *args, **kwargs)
    monkeypatch.setattr(admission, 'production_request', request)
    broker = ResourceBroker(tmp_path / 'coordinator')
    output = editor.preview('bed', ffmpeg=ffmpeg, broker=broker)
    assert len(captured) == 1 and captured[0].schema_version == 2
    assert captured[0].master_effects == () and captured[0].master_mix == 1
    assert wav_info(output).frames == 48000 and editor.plan.original_script == source
