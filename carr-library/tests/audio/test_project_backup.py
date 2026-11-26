"""Verified project portability, hostile archives and checkpoint resumption."""
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import wave
import zipfile

import pytest

from carr_library.audio.backup import backup_project, restore_project
from carr_library.audio.jobs import RenderCancelled, project_lock
from carr_library.ui.audio_studio import StudioController


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'audio'
    root.mkdir()
    controller = StudioController(private_root=tmp_path / 'private')
    controller.new('Оригінал.\r\nSecond line.', title='Portable audio')
    controller.configure(output_dir=root)
    file = tmp_path / 'studio.json'
    controller.save(file)
    return controller, file, root


def _rewrite(source, destination, *, transform=None, extras=()):
    with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(destination, 'w') as output:
        for info in incoming.infolist():
            data = incoming.read(info)
            name, data = transform(info.filename, data) if transform else (info.filename, data)
            output.writestr(name, data)
        for name, data in extras:
            output.writestr(name, data)


def test_snapshot_roundtrip_relocates_settings_and_recovery_with_exact_source(project, tmp_path, writable_file_fsync):
    controller, file, root = project
    controller.draft.replace_literal('Оригінал', 'Оригіна́л')
    controller.save_recovery()
    cache = root / 'cache'
    cache.mkdir()
    (cache / ('a' * 64 + '.wav')).write_bytes(b'Synthetic sealed audio bytes')
    (cache / ('a' * 64 + '.mid')).write_bytes(b'Synthetic MIDI bytes')
    (cache / ('a' * 64 + '.music.json')).write_text('{"source":"procedural"}', encoding='utf-8')
    (root / 'api-connections.json').write_text('PRIVATE PROVIDER SETTINGS', encoding='utf-8')
    (root / 'secret-key.txt').write_text('PRIVATE TEST KEY', encoding='utf-8')
    (root / 'helper.py').write_text('raise RuntimeError()', encoding='utf-8')
    original_project = file.read_bytes()
    snapshot = tmp_path / 'проект.zip'
    result = backup_project(file, snapshot, project_dir=root)
    with zipfile.ZipFile(snapshot) as archive:
        assert all('api-connections' not in name and 'secret' not in name and 'helper' not in name for name in archive.namelist())
        assert 'audio/cache/' + 'a' * 64 + '.mid' in archive.namelist()
    restored = restore_project(snapshot, tmp_path / 'відновлений')
    reopened = StudioController(private_root=tmp_path / 'other-private')
    reopened.open(restored['project_file'])
    assert reopened.draft.source_text == 'Оригінал.\r\nSecond line.'
    assert reopened.output_dir == Path(restored['project_dir'])
    assert reopened.script == reopened.draft.source_text
    reopened.recover()
    assert reopened.script == 'Оригіна́л.\r\nSecond line.'
    assert reopened.output_dir == Path(restored['project_dir'])
    assert file.read_bytes() == original_project
    assert restored['recovery_relocated'] is True and result['files'] == 5
    assert writable_file_fsync


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_flush_failure_leaves_no_published_snapshot_or_restore(project, tmp_path, monkeypatch, operation):
    _, file, root = project
    before = file.read_bytes()
    snapshot, destination = tmp_path / 'snapshot.zip', tmp_path / 'restored'
    if operation == 'restore':
        backup_project(file, snapshot, project_dir=root)

    def fail_flush(descriptor):
        raise OSError(errno.EIO, 'Synthetic disk flush failure')

    monkeypatch.setattr(os, 'fsync', fail_flush)
    with pytest.raises(OSError, match='flush failure'):
        if operation == 'backup':
            backup_project(file, snapshot, project_dir=root)
        else:
            restore_project(snapshot, destination)
    assert not (snapshot if operation == 'backup' else destination).exists()
    assert not list(tmp_path.glob('.audio-snapshot-*'))
    assert not list(tmp_path.glob('.audio-restore-*'))
    assert file.read_bytes() == before


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_cancellation_during_flush_prevents_publication(project, tmp_path, monkeypatch, operation):
    _, file, root = project
    snapshot, destination = tmp_path / 'snapshot.zip', tmp_path / 'restored'
    if operation == 'restore':
        backup_project(file, snapshot, project_dir=root)
    real_fsync = os.fsync
    cancelled = []

    def cancel_during_flush(descriptor):
        real_fsync(descriptor)
        cancelled.append(True)

    monkeypatch.setattr(os, 'fsync', cancel_during_flush)
    with pytest.raises(RenderCancelled):
        if operation == 'backup':
            backup_project(file, snapshot, project_dir=root, cancelled=lambda: bool(cancelled))
        else:
            restore_project(snapshot, destination, cancelled=lambda: bool(cancelled))
    assert cancelled
    assert not (snapshot if operation == 'backup' else destination).exists()
    assert not list(tmp_path.glob('.audio-snapshot-*'))
    assert not list(tmp_path.glob('.audio-restore-*'))


def test_snapshot_never_overwrites_and_busy_project_cannot_be_snapshotted(project, tmp_path):
    controller, file, root = project
    snapshot = tmp_path / 'backup.zip'
    with project_lock(root):
        with pytest.raises(RuntimeError, match='render'):
            backup_project(file, snapshot, project_dir=root)
    assert not snapshot.exists()
    backup_project(file, snapshot, project_dir=root)
    before = snapshot.read_bytes()
    with pytest.raises(FileExistsError):
        backup_project(file, snapshot, project_dir=root)
    existing = tmp_path / 'existing'
    existing.mkdir()
    with pytest.raises(FileExistsError):
        restore_project(snapshot, existing)
    assert snapshot.read_bytes() == before and list(existing.iterdir()) == []


def test_checksums_and_unknown_archive_extras_reject_before_publication(project, tmp_path):
    _, file, root = project
    snapshot = tmp_path / 'backup.zip'
    backup_project(file, snapshot, project_dir=root)
    corrupt = tmp_path / 'corrupt.zip'
    def damage(name, data):
        return name, data.replace(b'Portable audio', b'Corrupt! audio') if name == 'studio/project.json' else data
    _rewrite(snapshot, corrupt, transform=damage)
    with pytest.raises(ValueError, match='integrity'):
        restore_project(corrupt, tmp_path / 'bad-restore')
    extra = tmp_path / 'extra.zip'
    _rewrite(snapshot, extra, extras=(('untrusted.py', b'print(1)'),))
    with pytest.raises(ValueError, match='contents'):
        restore_project(extra, tmp_path / 'extra-restore')
    assert not (tmp_path / 'bad-restore').exists()
    assert not (tmp_path / 'extra-restore').exists()
    assert not list(tmp_path.glob('.audio-restore-*'))


@pytest.mark.parametrize('name', ['../escape.txt', '/tmp/escape', 'audio/assets/CON.wav',
                                 'audio/assets/file.wav:stream', 'audio/assets/sub\\evil.wav'])
def test_traversal_windows_devices_streams_and_noncanonical_members_rejected(project, tmp_path, name):
    _, file, root = project
    snapshot = tmp_path / 'backup.zip'
    backup_project(file, snapshot, project_dir=root)
    forged = tmp_path / 'forged.zip'
    def changed(original, data):
        if original == 'manifest.json':
            manifest = json.loads(data)
            manifest['files'][0]['path'] = name
            return original, json.dumps(manifest).encode()
        return name, data
    _rewrite(snapshot, forged, transform=changed)
    with pytest.raises(ValueError):
        restore_project(forged, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_linked_archive_member_and_linked_source_rejected(project, tmp_path):
    _, file, root = project
    cache = root / 'cache'
    cache.mkdir()
    secret = tmp_path / 'secret.txt'
    secret.write_text('Private data', encoding='utf-8')
    link = cache / ('b' * 64 + '.wav')
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip('Symbolic links unavailable on this platform')
    with pytest.raises(ValueError, match='links'):
        backup_project(file, tmp_path / 'bad.zip', project_dir=root)
    link.unlink()
    snapshot = tmp_path / 'good.zip'
    backup_project(file, snapshot, project_dir=root)
    bad = tmp_path / 'link.zip'
    with zipfile.ZipFile(snapshot) as source, zipfile.ZipFile(bad, 'w') as output:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == 'studio/project.json':
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(info, data)
    with pytest.raises(ValueError, match='Linked'):
        restore_project(bad, tmp_path / 'restored')


def test_budget_cancellation_and_concurrent_source_edit_leave_no_published_snapshot(project, tmp_path):
    controller, file, root = project
    with pytest.raises(ValueError, match='byte budget'):
        backup_project(file, tmp_path / 'small.zip', project_dir=root, byte_budget=1)
    with pytest.raises(RenderCancelled):
        backup_project(file, tmp_path / 'cancel.zip', project_dir=root, cancelled=lambda: True)
    def mutate(_):
        controller.edit_script('Changed while backing up')
        controller.save()
    with pytest.raises(ValueError, match='changed during'):
        backup_project(file, tmp_path / 'changed.zip', project_dir=root, on_status=mutate)
    assert not (tmp_path / 'changed.zip').exists()
    assert not list(tmp_path.glob('.audio-snapshot-*'))


def test_restore_cancellation_and_budgets_are_checked_before_final_directory(project, tmp_path):
    _, file, root = project
    snapshot = tmp_path / 'backup.zip'
    backup_project(file, snapshot, project_dir=root)
    with pytest.raises(ValueError, match='byte budget'):
        restore_project(snapshot, tmp_path / 'small', byte_budget=1)
    stopped = []
    with pytest.raises(RenderCancelled):
        restore_project(snapshot, tmp_path / 'cancelled', on_status=lambda message: stopped.append(True),
                        cancelled=lambda: bool(stopped))
    assert not (tmp_path / 'cancelled').exists()
    assert not list(tmp_path.glob('.audio-restore-*'))


def test_snapshot_after_real_render_restores_checked_outputs_without_new_synthesis(tmp_path):
    from carr_library.audio.production import render_project, parse_script
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg is required for the streaming render portability test.')
    class FixtureEngine:
        def __init__(self):
            self.calls = 0
        def cache_identity(self, voice):
            return {'fixture': 'backup-synthetic-wave-v1', 'voice': voice}
        def synthesize(self, text, output, **kwargs):
            self.calls += 1
            with wave.open(str(output), 'wb') as audio:
                audio.setparams((1, 2, 8000, 800, 'NONE', 'not compressed'))
                audio.writeframes(b''.join(struct.pack('<h', 3000 if index % 20 < 10 else -3000) for index in range(800)))
            return output
    engine = FixtureEngine()
    root = tmp_path / 'audio'
    root.mkdir()
    script = '[output sample_rate=8000 stems=false]\n# Part one\nNarrator: Exact text.\n[pause 0.1]'
    plan = parse_script(script, voice='fixture:local')
    before = render_project(plan, root, engine=engine, ffmpeg=Path(ffmpeg))
    digest = hashlib.sha256(Path(before['output']).read_bytes()).hexdigest()
    assert engine.calls == 1
    controller = StudioController(private_root=tmp_path / 'private')
    controller.new(script)
    controller.configure(voice='fixture:local', output_dir=root)
    file = tmp_path / 'studio.json'
    controller.save(file)
    snapshot = tmp_path / 'snapshot.zip'
    backup_project(file, snapshot, project_dir=root)
    restored = restore_project(snapshot, tmp_path / 'restored')
    after = render_project(plan, Path(restored['project_dir']), engine=engine, ffmpeg=Path(ffmpeg))
    assert engine.calls == 1
    assert hashlib.sha256(Path(after['output']).read_bytes()).hexdigest() == digest
    assert after['frames'] == before['frames']
    assert Path(after['output']).is_relative_to(Path(restored['project_dir']))
