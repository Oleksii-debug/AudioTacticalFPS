"""Actual adjacent-source consolidation, current draft undo and portable receipts."""
from array import array
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
import wave
from zipfile import ZipFile

import pytest

from carr_library.audio import clip_consolidation as consolidation
from carr_library.audio.clip_audio import process_clip
from carr_library.audio.clips import ClipEditor, clip_directive
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.jobs import RenderCancelled
from carr_library.audio.models import Clip
from carr_library.audio.production import parse_script, render_project
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.operations import ResourceBroker
from carr_library.ui.audio_studio import StudioController


@pytest.fixture
def ffmpeg():
    path = resolve_ffmpeg()
    if path is None:
        pytest.fail('This required consolidation suite needs the prepared local FFmpeg runtime.')
    return path


def pcm(path, *, rate, channels, frequency):
    count = rate // 2
    values = array('h', (round(6000 * math.sin(2 * math.pi * (frequency + channel * 71) * i / rate))
                        for i in range(count) for channel in range(channels)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, count, 'NONE', 'PCM'))
        stream.writeframes(values.tobytes())
    return path


def samples(path):
    info = wav_info(path)
    with Path(path).open('rb') as stream:
        stream.seek(info.data_offset)
        return stream.read(info.data_size)


def fixture(tmp_path, *, root=None):
    root = root or tmp_path / 'audio'
    assets = root / 'assets'
    assets.mkdir(parents=True, exist_ok=True)
    one = pcm(assets / 'first.wav', rate=8000, channels=1, frequency=311)
    two = pcm(assets / 'second.wav', rate=16000, channels=2, frequency=487)
    first = Clip('kept', one.name, start=.1, trim_start=.05, duration=.2, gain_db=-3,
                 pan=-.5, track='microphone', fade_in=.02, fade_out=.03, title='Перший 🙂')
    second = Clip('removed', two.name, start=.3, trim_start=.1, duration=.15, gain_db=-7,
                  pan=.4, track='microphone', fade_in=.01, fade_out=.02, title='Другий')
    script = ('[output sample_rate=8000 stems=false]\r\n# Chapter\r\n'
              + clip_directive(first) + '\r\n' + clip_directive(second) + '\r\n')
    return root, first, second, script, {one: one.read_bytes(), two: two.read_bytes()}


def bake(root, first, second, ffmpeg, **options):
    return consolidation.consolidate_clips(root, first, second, ffmpeg=ffmpeg,
                                          sample_rate=8000, **options)


def test_actual_different_rates_and_processing_bake_exact_ordered_pcm(tmp_path, ffmpeg):
    root, first, second, script, originals = fixture(tmp_path)
    expected = []
    for index, clip in enumerate((first, second)):
        target = tmp_path / f'expected-{index}.wav'
        process_clip(root / 'assets' / clip.asset, target, clip, ffmpeg=ffmpeg, sample_rate=8000)
        expected.append(samples(target))
    broker = ResourceBroker(tmp_path / 'coordinator')
    result = bake(root, second, first, ffmpeg, broker=broker)
    combined = result['clip']
    path = root / 'assets' / combined.asset
    assert combined.id == first.id and combined.start == first.start and combined.track == first.track
    assert (combined.gain_db, combined.pan, combined.trim_start, combined.duration,
            combined.fade_in, combined.fade_out) == (0, 0, 0, None, 0, 0)
    assert wav_info(path).frames == 2800 and wav_info(path).channels == 2
    assert samples(path) == b''.join(expected)
    assert path.stat().st_nlink == 1 and path.name == sha256(path.read_bytes()).hexdigest() + '.wav'
    receipt = consolidation.verify_receipt(root, root / result['receipt'])
    assert [row['clip'] for row in receipt['inputs']] == [asdict(first), asdict(second)]
    assert {path: path.read_bytes() for path in originals} == originals
    assert not list(root.glob('.clip-consolidation-*'))


def test_existing_receipt_reuses_verified_pcm_without_repeating_native_processing(tmp_path, ffmpeg, monkeypatch):
    root, first, second, _, originals = fixture(tmp_path)
    before = bake(root, first, second, ffmpeg)
    monkeypatch.setattr(consolidation, 'process_clip', lambda *a, **k: pytest.fail('Exact recipe must reuse PCM.'))
    after = bake(root, second, first, ffmpeg)
    assert after['clip'] == before['clip'] and after['receipt'] == before['receipt'] and after['reused']
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize('fault', ['gap', 'overlap', 'track', 'same-id', 'rate'], ids=[
    'timeline-gap', 'timeline-overlap', 'different-track', 'same-identity', 'unsupported-rate'])
def test_unreviewed_pair_is_refused_before_native_processing(tmp_path, monkeypatch, fault):
    root, first, second, _, originals = fixture(tmp_path)
    second = {'gap': replace(second, start=.31), 'overlap': replace(second, start=.29),
              'track': replace(second, track='sfx'), 'same-id': replace(second, id=first.id)}.get(fault, second)
    monkeypatch.setattr(consolidation.audio, 'ffmpeg_identity', lambda *a, **k: pytest.fail('Invalid pair spawned work.'))
    with pytest.raises(ValueError):
        consolidation.consolidate_clips(root, first, second, ffmpeg='unused',
                                         sample_rate=1234 if fault == 'rate' else 8000)
    assert {path: path.read_bytes() for path in originals} == originals
    assert not list(root.glob(consolidation.PREFIX + '*'))


def test_cancellation_during_second_native_step_keeps_both_originals_and_no_receipt(tmp_path, ffmpeg, monkeypatch):
    root, first, second, _, originals = fixture(tmp_path)
    native, calls = consolidation.process_clip, []
    stopped = [False]
    def process(*args, **kwargs):
        result = native(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            stopped[0] = True
        return result
    monkeypatch.setattr(consolidation, 'process_clip', process)
    with pytest.raises(RenderCancelled):
        bake(root, first, second, ffmpeg, cancelled=lambda: stopped[0])
    assert len(calls) == 2
    assert set((root / 'assets').iterdir()) == set(originals)
    assert {path: path.read_bytes() for path in originals} == originals
    assert not list(root.glob(consolidation.PREFIX + '*')) and not list(root.glob('.clip-consolidation-*'))


def test_final_original_reader_refusal_prevents_asset_and_receipt_publication(tmp_path, ffmpeg, monkeypatch):
    root, first, second, _, originals = fixture(tmp_path)
    original_reader = consolidation.verified_reader
    entered = {}
    @contextmanager
    def reader(path, **kwargs):
        path = Path(path)
        entered[path] = entered.get(path, 0) + 1
        # _source first verifies a snapshot; the second owner spans the actual
        # native operation. Refuse precisely its final identity acknowledgement.
        fail = path == root / 'assets' / first.asset and entered[path] == 2
        with original_reader(path, **kwargs) as stream:
            yield stream
        if fail:
            raise RuntimeError('Injected final original refusal')
    monkeypatch.setattr(consolidation, 'verified_reader', reader)
    with pytest.raises(RuntimeError, match='final original refusal'):
        bake(root, first, second, ffmpeg)
    assert set((root / 'assets').iterdir()) == set(originals)
    assert not list(root.glob(consolidation.PREFIX + '*')) and not list(root.glob('.clip-consolidation-*'))


@pytest.mark.parametrize('location', ['asset', 'receipt'])
def test_post_move_flush_failure_is_retried_before_completed_recipe_is_acknowledged(
        tmp_path, ffmpeg, monkeypatch, location):
    from carr_library.audio import clips, jobs
    root, first, second, _, originals = fixture(tmp_path)
    real = jobs._sync_directory
    failing, attempts = [True], []
    def sync(path):
        path = Path(path)
        selected = (location == 'asset' and path == root / 'assets' and len(list(path.iterdir())) > 2
                    or location == 'receipt' and path == root and list(root.glob(consolidation.PREFIX + '*')))
        if selected:
            attempts.append(path)
            if failing[0]:
                raise OSError('Injected consolidation directory flush failure')
        return real(path)
    for module in (jobs, clips, consolidation):
        monkeypatch.setattr(module, '_sync_directory', sync)
    for _ in range(2):
        with pytest.raises(OSError, match='directory flush failure'):
            bake(root, first, second, ffmpeg)
    assert len(attempts) >= 2
    failing[0] = False
    result = bake(root, first, second, ffmpeg)
    assert consolidation.verify_receipt(root, root / result['receipt'])
    assert {path: path.read_bytes() for path in originals} == originals


def test_changed_source_or_completed_output_is_never_reused_as_verified(tmp_path, ffmpeg):
    root, first, second, _, _ = fixture(tmp_path)
    result = bake(root, first, second, ffmpeg)
    output = root / 'assets' / result['clip'].asset
    offset = wav_info(output).data_offset + 20
    with output.open('r+b') as stream:
        stream.seek(offset)
        stream.write(b'\x01\x00')
    with pytest.raises(ValueError, match='retained audio'):
        bake(root, first, second, ffmpeg)


def test_current_controller_uses_existing_undo_and_rejects_late_acceptance(tmp_path, ffmpeg):
    root, first, second, script, originals = fixture(tmp_path)
    private = tmp_path / 'library'
    private.mkdir()
    controller = StudioController(private_root=private, ffmpeg=ffmpeg)
    controller.new(script, input_kind='script')
    controller.configure(output_dir=root)
    editor = controller.clip_editor(controller.clip_snapshot())
    result = controller.prepare_clip_edit(editor, {'action': 'consolidate',
        'clip_id': second.id, 'other_id': first.id})
    assert controller.script == script
    controller.accept_clip_edit(result)
    changed = controller.script
    assert changed != script and len(controller.plan().scenes[0].clips) == 1
    assert controller.draft.source_text == script
    assert controller.draft.undo() and controller.script == script
    controller.edit_script(script + '[pause .1]\r\n')
    before = controller.script
    with pytest.raises(ValueError, match='draft changed'):
        controller.accept_clip_edit(result)
    assert controller.script == before
    assert {path: path.read_bytes() for path in originals} == originals


def test_actual_full_render_preserves_adjacent_timeline_without_speech(tmp_path, ffmpeg):
    root, first, second, script, _ = fixture(tmp_path)
    class NoSpeech:
        def synthesize(self, *args, **kwargs):
            pytest.fail('Clip-only consolidation cannot invent a speech request.')
        def cache_identity(self, voice):
            return {'synthetic': 'unused'}
    before = render_project(parse_script(script), root, ffmpeg=ffmpeg, engine=NoSpeech())
    editor = ClipEditor(script, root)
    result = editor.consolidate(first.id, second.id, ffmpeg=ffmpeg)
    after = render_project(parse_script(result['script']), root, ffmpeg=ffmpeg, engine=NoSpeech())
    a, b = array('h', samples(before['output'])), array('h', samples(after['output']))
    assert len(a) == len(b) and before['frames'] == after['frames'] == 3600
    assert max(abs(x - y) for x, y in zip(a, b)) <= 2


@pytest.mark.parametrize('snapshot', ['project', 'library'])
def test_provenance_and_both_sources_survive_snapshots_with_offline_revalidation(tmp_path, ffmpeg, snapshot):
    from carr_library.application import LibraryApp
    from carr_library.audio.backup import backup_project, restore_project, input_assets
    from carr_library.library_backup import create_backup, restore_backup, verify_backup

    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Consolidation')
    controller = StudioController(private_root=app.root, ffmpeg=ffmpeg)
    controller.new('[duration 1]', input_kind='script')
    controller.save_in_library()
    root, first, second, script, originals = fixture(tmp_path, root=controller.output_dir)
    controller.edit_script(script)
    editor = controller.clip_editor(controller.clip_snapshot())
    result = controller.prepare_clip_edit(editor, {'action': 'consolidate',
        'clip_id': first.id, 'other_id': second.id})
    controller.accept_clip_edit(result)
    controller.save()
    receipt = root / result['consolidation_receipt']
    raw = receipt.read_bytes()
    inventory = input_assets(controller.project_path, root)
    assert inventory['audio/' + receipt.name] == receipt
    if snapshot == 'project':
        archive = tmp_path / 'project.zip'
        backup_project(controller.project_path, archive, project_dir=root,
                       include_cache=False, include_audio=False)
        with ZipFile(archive) as captured:
            assert captured.read('audio/' + receipt.name) == raw
            assert not any('/cache/' in name or '/renders/' in name for name in captured.namelist())
        restored = tmp_path / 'restored-project'
        restore_project(archive, restored)
        restored_root = restored / 'project' / 'audio'
    else:
        archive = tmp_path / 'full-backup'
        identity = create_backup(app, archive)
        verify_backup(archive, identity)
        restored = tmp_path / 'restored-library'
        restore_backup(archive, restored, identity)
        restored_root = restored / root.relative_to(app.root)
    assert (restored_root / receipt.name).read_bytes() == raw
    checked = consolidation.verify_receipt(restored_root, restored_root / receipt.name)
    assert [row['clip'] for row in checked['inputs']] == [asdict(first), asdict(second)]
    for source, original in originals.items():
        assert (restored_root / 'assets' / source.name).read_bytes() == original


@pytest.mark.parametrize('changed', ['source-path', 'frame-count', 'processor-field', 'boolean-rate',
                                     'receipt-name', 'unknown-field'])
def test_forged_provenance_fails_typed_validation(tmp_path, ffmpeg, changed):
    root, first, second, _, _ = fixture(tmp_path)
    result = bake(root, first, second, ffmpeg)
    value = json.loads((root / result['receipt']).read_bytes())
    name = result['receipt']
    if changed == 'source-path':
        value['inputs'][0]['audio']['asset'] = '../outside.wav'
    elif changed == 'frame-count':
        value['output']['frames'] += 1
    elif changed == 'processor-field':
        value['processor']['executable'] = '/unreviewed/program'
    elif changed == 'boolean-rate':
        value['sample_rate'] = True
    elif changed == 'receipt-name':
        name = consolidation.PREFIX + '0' * 64 + '.json'
    else:
        value['authority'] = 'automatic'
    with pytest.raises(ValueError):
        consolidation.decode_receipt(value, name=name)


def test_resource_wait_revalidates_original_before_any_native_clip_processing(tmp_path, ffmpeg, monkeypatch):
    root, first, second, _, _ = fixture(tmp_path)
    broker = ResourceBroker(tmp_path / 'coordinator')
    reserve = broker.reserve
    source = root / 'assets' / first.asset
    offset = wav_info(source).data_offset + 20

    @contextmanager
    def admitted(*args, **kwargs):
        with reserve(*args, **kwargs) as lease:
            with source.open('r+b') as stream:
                stream.seek(offset)
                stream.write(b'\x01\x00')
                stream.flush()
                os.fsync(stream.fileno())
            yield lease

    monkeypatch.setattr(broker, 'reserve', admitted)
    monkeypatch.setattr(consolidation, 'process_clip', lambda *a, **k: pytest.fail('Changed input reached DSP.'))
    with pytest.raises((ValueError, RuntimeError), match='changed'):
        bake(root, first, second, ffmpeg, broker=broker)
    assert not list(root.glob(consolidation.PREFIX + '*'))
    assert {path.name for path in (root / 'assets').iterdir()} == {first.asset, second.asset}
    from carr_library.audio.jobs import project_lock
    with project_lock(root):
        pass
