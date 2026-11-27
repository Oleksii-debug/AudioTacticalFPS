"""Actual managed Studio producer, common V4 and local FFmpeg round trips."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.jobs import project_lock
from carr_library.audio import library_sidecars
from carr_library.audio.managed import ManagedProjects
from carr_library.audio.narration_bridge import snapshot_reading_project
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.audio_studio import StudioController


class NoSpeech:
    def synthesize(self, *args, **kwargs):
        raise AssertionError('These tests render actual silence/procedural music, not fake speech.')


def studio(tmp_path, *, script='[output sample_rate=8000 stems=false]\n[pause 0.12]'):
    app = LibraryApp(tmp_path / 'library')
    executable = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    assert executable and Path(executable).is_file(), 'A real local FFmpeg executable is required.'
    controller = StudioController(private_root=app.root, registry=NoSpeech(), ffmpeg=executable)
    controller.new(script, title='Owned audio', input_kind='script')
    controller.save_in_library()
    return app, controller


def manifest(path):
    return json.loads((path / 'manifest.json').read_text('utf-8'))


def resign(path, name, raw):
    (path / name).write_bytes(raw)
    value = manifest(path)
    entry = next(item for item in value['sidecars'] if item['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    serialized = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (path / 'manifest.json').write_bytes(serialized)
    return sha256(serialized).hexdigest()


def test_explicit_adoption_preserves_external_files_and_metadata_restore(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    source = tmp_path / 'external.json'
    outside = tmp_path / 'external-audio'
    outside.mkdir()
    secret = outside / 'profile.json'
    secret.write_bytes(b'SYNTHETIC EXTERNAL SECRET; NOT LIBRARY STATE')
    value = StudioController(private_root=app.root)
    value.new('Exact original\r\nText')
    value.configure(output_dir=outside)
    value.save(source)
    original = source.read_bytes()
    value.edit_script('Explicitly edited spoken version.')
    saved = value.save_in_library()
    assert saved.is_relative_to(app.root) and source.read_bytes() == original
    assert {path.name for path in value.output_dir.iterdir()} == {'.render.lock'}
    value.edit_script('An unsaved recoverable derivative.')
    value.save_recovery()
    snapshot = tmp_path / 'snapshot'
    checksum = create_backup(app, snapshot)
    records = manifest(snapshot)['sidecars']
    assert len(records) == 2 and all('/studio/' in row['filename'] for row in records)
    assert manifest(snapshot)['generated_audio_included'] is False
    restored = restore_backup(snapshot, tmp_path / 'restored', checksum)
    reopened = StudioController(private_root=restored)
    reopened.open(restored / saved.relative_to(app.root))
    reopened.recover()
    assert reopened.script == 'An unsaved recoverable derivative.'
    assert reopened.draft.source_text == 'Exact original\r\nText'
    assert reopened.output_dir.is_relative_to(restored) and reopened.output_dir.is_dir()
    assert source.read_bytes() == original and secret.read_bytes().startswith(b'SYNTHETIC EXTERNAL')


def test_actual_render_streamed_v4_restore_reopens_exact_completed_audio(tmp_path):
    app, value = studio(tmp_path)
    result = value.render()
    assert wav_info(result['output']).frames == 960
    before = Path(result['output']).read_bytes()
    project_bytes = value.project_path.read_bytes()
    # Neither installed models nor arbitrary settings/work state is inventoried.
    excluded = ['models/private-weight.bin', 'profiles/key.json', '.work-old/private.wav',
                'cache/quarantine/rejected.wav', 'unrelated.json']
    for name in excluded:
        path = value.output_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'SYNTHETIC EXCLUDED')
    snapshot = tmp_path / 'snapshot'
    checksum = create_backup(app, snapshot, include_generated=True)
    records = manifest(snapshot)['sidecars']
    names = {row['filename'] for row in records}
    assert manifest(snapshot)['generated_audio_included'] is True
    assert any('/cache/' in name and name.endswith('.wav') for name in names)
    assert any(name.endswith('/master.wav') for name in names)
    assert not any(name.endswith('render-journal.json') for name in names)
    assert not any(any(part in name for part in ('/models/', '/profiles/', '/quarantine/', '/.work-')) for name in names)
    restored = restore_backup(snapshot, tmp_path / 'restored', checksum)
    reopened = StudioController(private_root=restored, registry=NoSpeech(), ffmpeg=value.ffmpeg)
    reopened.open(restored / value.project_path.relative_to(app.root))
    assert reopened.project_path.read_bytes() == project_bytes
    completed = reopened.render()
    assert Path(completed['output']).is_relative_to(restored)
    assert Path(completed['output']).read_bytes() == before
    assert all((restored / name).stat().st_nlink == 1 for name in names)
    assert all((value.output_dir / name).is_file() for name in excluded)


def test_metadata_default_excludes_real_generated_files_without_losing_draft(tmp_path):
    app, value = studio(tmp_path)
    value.render()
    snapshot = tmp_path / 'metadata'
    checksum = create_backup(app, snapshot)
    assert [row['filename'] for row in manifest(snapshot)['sidecars']] == [value.project_path.relative_to(app.root).as_posix()]
    restored = restore_backup(snapshot, tmp_path / 'restored', checksum)
    reopened = StudioController(private_root=restored, registry=NoSpeech(), ffmpeg=value.ffmpeg)
    reopened.open(restored / value.project_path.relative_to(app.root))
    assert reopened.output_dir.is_dir() and not list(reopened.output_dir.iterdir())
    assert wav_info(reopened.render()['output']).frames == 960


def test_managed_snapshot_rechecks_source_bytes_despite_equal_metadata(tmp_path, monkeypatch):
    app, value = studio(tmp_path)
    source = value.project_path
    original = source.read_bytes()
    replacement = original.replace(b'Owned audio', b'Owned other')
    assert replacement != original and len(replacement) == len(original)
    identity, copy = library_sidecars._identity, library_sidecars._copy
    monkeypatch.setattr(library_sidecars, '_identity', lambda info: (*identity(info)[:-2], 0, 0))

    def copied_then_changed(path, target, **kwargs):
        receipt = copy(path, target, **kwargs)
        if path == source and target is not None:
            stamp = source.stat()
            source.write_bytes(replacement)
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        return receipt

    monkeypatch.setattr(library_sidecars, '_copy', copied_then_changed)
    destination = tmp_path / 'rejected-stale-audio'
    with pytest.raises(ValueError, match='digest or size mismatch'):
        create_backup(app, destination)
    assert not destination.exists()
    assert source.read_bytes() == replacement


def test_bridge_snapshot_and_historical_original_survive_common_backup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reading library')
    book = app.add_text(workspace, 'Synthetic source', 'Original \r\nПривіт e\u0301.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    reading = revise(new_project(page.raw_text, source_location=page.location), 'A reading derivative.')
    NarrationStore(app.root).save(reading)
    value = StudioController(private_root=app.root)
    value.use_reading_snapshot(snapshot_reading_project(reading))
    value.save_in_library()
    value.edit_script('A later Audio Studio revision.')
    value.save()
    backup = tmp_path / 'snapshot'
    checksum = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    reopened = StudioController(private_root=restored)
    reopened.open(restored / value.project_path.relative_to(app.root))
    assert reopened.script == 'A later Audio Studio revision.'
    assert reopened.draft.source_text == page.raw_text
    assert reopened.source_reference['quotation_authority'] is False


def test_live_render_lock_refuses_both_save_and_common_backup(tmp_path):
    app, value = studio(tmp_path)
    previous = value.project_path.read_bytes()
    value.edit_script('Changed text')
    with project_lock(value.output_dir):
        with pytest.raises(RuntimeError, match='render'):
            value.save()
        with pytest.raises(ValueError, match='active'):
            create_backup(app, tmp_path / 'rejected', include_generated=True)
    assert value.project_path.read_bytes() == previous
    assert not (tmp_path / 'rejected').exists()


def test_real_chunk_tampering_refuses_snapshot_without_repairing_original(tmp_path):
    app, value = studio(tmp_path)
    value.render()
    chunk = next(value.output_dir.glob('cache/*.wav'))
    with chunk.open('r+b') as stream:
        stream.seek(wav_info(chunk).data_offset)
        stream.write(b'\x01\x02')
    damaged = chunk.read_bytes()
    with pytest.raises(ValueError, match='checksum'):
        create_backup(app, tmp_path / 'rejected', include_generated=True)
    assert chunk.read_bytes() == damaged and not (tmp_path / 'rejected').exists()


def test_rehashed_chunk_receipt_cannot_turn_corruption_into_accepted_recovery(tmp_path):
    app, value = studio(tmp_path)
    value.render()
    backup = tmp_path / 'snapshot'
    create_backup(app, backup, include_generated=True)
    name = next(row['filename'] for row in manifest(backup)['sidecars']
                if '/cache/' in row['filename'] and row['filename'].endswith('.json'))
    payload = json.loads((backup / name).read_text())
    payload['sha256'] = '0' * 64
    checksum = resign(backup, name, json.dumps(payload).encode())
    with pytest.raises(ValueError, match='checksum'):
        restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


def test_external_hardlink_to_accepted_audio_is_rejected(tmp_path):
    app, value = studio(tmp_path)
    value.render()
    cached = next(value.output_dir.glob('cache/*.wav'))
    alias = tmp_path / 'external.wav'
    os.link(cached, alias)
    with pytest.raises(ValueError, match='outside the verified'):
        create_backup(app, tmp_path / 'rejected', include_generated=True)
    assert alias.is_file() and not (tmp_path / 'rejected').exists()


def test_cancelled_adoption_does_not_change_external_identity(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    value = StudioController(private_root=app.root)
    value.new('Original')
    external = tmp_path / 'external.json'
    value.save(external)
    before = external.read_bytes()
    with pytest.raises(TimeoutError):
        value.save_in_library(cancelled=lambda: True)
    assert value.project_path == external and value.output_dir is None
    assert external.read_bytes() == before and ManagedProjects(app.root).projects() == ()


def test_controller_refuses_replaced_or_retargeted_library(tmp_path):
    app, value = studio(tmp_path)
    other = tmp_path / 'other'
    other.mkdir()
    value.private_root = other
    with pytest.raises(ValueError, match='library changed'):
        value.save_in_library()
    value.private_root = app.root
    moved = tmp_path / 'moved'
    app.root.rename(moved)
    app.root.mkdir()
    with pytest.raises(ValueError, match='folder changed'):
        value.save_in_library()
    assert not list(app.root.iterdir())


@pytest.mark.parametrize('finish_first', [False, True], ids=['paused-music', 'completed-music'])
def test_actual_music_checkpoints_and_accepted_twins_survive_library_restore(tmp_path, finish_first):
    script = '[output sample_rate=8000 stems=false]\n[music mood=calm tempo=240 seed=7]\n[pause 12]'
    app, value = studio(tmp_path, script=script)
    stopped = []
    def pause(message):
        if message.startswith('Music chunk 1 of 3 verified;'):
            stopped.append(True)
    with pytest.raises(InterruptedError):
        value.render(cancelled=lambda: bool(stopped), on_status=pause)
    state, = value.output_dir.glob('music-jobs/*/cue.music-work/state.json')
    chunk = state.parent / 'part-00000000.wav'
    accepted = chunk.read_bytes()
    if finish_first:
        value.render()
    before = {path.relative_to(value.output_dir).as_posix(): path.read_bytes()
              for path in value.output_dir.rglob('*') if path.is_file() and path.name != '.render.lock'}
    backup = tmp_path / 'snapshot'
    checksum = create_backup(app, backup, include_generated=True)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    reopened = StudioController(private_root=restored, registry=NoSpeech(), ffmpeg=value.ffmpeg)
    reopened.open(restored / value.project_path.relative_to(app.root))
    retained = reopened.output_dir / chunk.relative_to(value.output_dir)
    stamp = retained.stat().st_mtime_ns
    result = reopened.render()
    assert wav_info(result['output']).frames == 96000
    assert retained.read_bytes() == accepted and retained.stat().st_mtime_ns == stamp
    if not finish_first:
        provenance, = reopened.output_dir.glob('music-jobs/*/cue.music.json')
        assert json.loads(provenance.read_text())['reused_chunks'] == 1
    assert {name: (value.output_dir / name).read_bytes() for name in before} == before


def test_readonly_verification_streams_without_a_second_media_copy(tmp_path, monkeypatch):
    from carr_library.audio import library_sidecars
    app, value = studio(tmp_path)
    value.render()
    backup = tmp_path / 'snapshot'
    checksum = create_backup(app, backup, include_generated=True)
    copy = library_sidecars._copy
    seen = []
    def verify_only(source, target, **kwargs):
        assert target is None, 'Read-only verification must not duplicate audio payloads.'
        seen.append(source)
        return copy(source, target, **kwargs)
    monkeypatch.setattr(library_sidecars, '_copy', verify_only)
    before = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in backup.rglob('*') if path.is_file()}
    assert verify_backup(backup, checksum) == manifest(backup)
    assert any(path.suffix == '.wav' for path in seen)
    assert {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in before} == before


def test_exact_historical_path_sidecars_v4_restores_same_managed_producer(tmp_path):
    app, value = studio(tmp_path)
    value.render()
    backup = tmp_path / 'snapshot'
    create_backup(app, backup, include_generated=True)
    previous = manifest(backup)
    payload = backup / 'sidecars'
    payload.mkdir()
    (backup / 'audio-projects').rename(payload / 'audio-projects')
    previous.pop('generated_audio_included')
    previous['sidecars'] = [dict(path=entry['filename'], size_bytes=entry['size_bytes'], sha256=entry['sha256'])
                            for entry in previous['sidecars']]
    raw = (json.dumps(previous, sort_keys=True) + '\n').encode()
    (backup / 'manifest.json').write_bytes(raw)
    checksum = sha256(raw).hexdigest()
    assert verify_backup(backup, checksum) == previous
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    opened = StudioController(private_root=restored, registry=NoSpeech(), ffmpeg=value.ffmpeg)
    opened.open(restored / value.project_path.relative_to(app.root))
    assert wav_info(opened.render()['output']).frames == 960


def test_private_json_neighbor_is_excluded_and_rehashed_injection_refused(tmp_path):
    app, value = studio(tmp_path)
    output = Path(value.render()['output']).parent
    private = output / 'credentials.json'
    private.write_bytes(b'{"key":"SYNTHETIC PRIVATE"}')
    backup = tmp_path / 'snapshot'
    create_backup(app, backup, include_generated=True)
    name = private.relative_to(app.root).as_posix()
    data = manifest(backup)
    assert name not in {row['filename'] for row in data['sidecars']}
    (backup / name).write_bytes(private.read_bytes())
    data['sidecars'].append(dict(filename=name, size_bytes=private.stat().st_size,
                                sha256=sha256(private.read_bytes()).hexdigest()))
    raw = (json.dumps(data, sort_keys=True) + '\n').encode()
    (backup / 'manifest.json').write_bytes(raw)
    with pytest.raises(ValueError, match='unsupported completed audio'):
        restore_backup(backup, tmp_path / 'rejected', sha256(raw).hexdigest())
    assert not (tmp_path / 'rejected').exists() and private.is_file()


def test_midstream_cancel_preserves_source_and_releases_locks(tmp_path, monkeypatch):
    from carr_library.audio import library_sidecars
    app, value = studio(tmp_path)
    output = Path(value.render()['output'])
    before = output.read_bytes()
    stopped = []
    copy = library_sidecars._copy
    def copy_one(source, target, **kwargs):
        result = copy(source, target, **kwargs)
        if source.suffix == '.wav':
            stopped.append(True)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(library_sidecars, '_copy', copy_one)
        with pytest.raises(InterruptedError):
            create_backup(app, tmp_path / 'cancelled', include_generated=True, cancelled=lambda: bool(stopped))
    assert not (tmp_path / 'cancelled').exists() and output.read_bytes() == before
    checksum = create_backup(app, tmp_path / 'retry', include_generated=True)
    assert verify_backup(tmp_path / 'retry', checksum)['generated_audio_included'] is True
