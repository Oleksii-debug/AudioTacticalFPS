"""Verified project portability, hostile archives and checkpoint resumption."""
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


def test_snapshot_roundtrip_relocates_settings_and_recovery_with_exact_source(project, tmp_path):
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


@pytest.mark.parametrize('edited_file', ['project', 'asset'])
def test_same_size_edit_with_preserved_stamp_rejects_snapshot(project, tmp_path, monkeypatch, edited_file):
    from carr_library.audio import backup

    _, file, root = project
    source = file
    if edited_file == 'asset':
        source = root / 'assets' / 'sample.wav'
        source.parent.mkdir()
        source.write_bytes(b'Synthetic original audio')
    original = source.read_bytes()
    replacement = (original.replace(b'Portable audio', b'Modified audio') if edited_file == 'project'
                   else original.replace(b'original', b'modified'))
    assert replacement != original and len(replacement) == len(original)
    previous = tmp_path / 'previous.zip'
    backup_project(file, previous, project_dir=root)
    previous_bytes = previous.read_bytes()
    before = source.stat()
    identity = backup._identity
    # Windows ctime is creation time. Keep real identity, size and restored
    # mtime while omitting only POSIX's independently changing ctime.
    monkeypatch.setattr(backup, '_identity', lambda info: identity(info)[:-1])
    changed = False

    def mutate(_):
        nonlocal changed
        if changed:
            return
        # The asset sorts first; a project-only snapshot has one member.
        # In either case this callback runs after the selected file was copied.
        with source.open('r+b') as stream:
            stream.write(replacement)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert backup._identity(source.stat()) == backup._identity(before)
        changed = True

    destination = tmp_path / 'changed.zip'
    with pytest.raises(ValueError, match='changed during'):
        backup_project(file, destination, project_dir=root, on_status=mutate)
    assert changed and source.read_bytes() == replacement
    assert previous.read_bytes() == previous_bytes
    assert not destination.exists()
    assert not list(tmp_path.glob('.audio-snapshot-*'))


def test_snapshot_content_revalidation_cancellation_leaves_state_unchanged(project, tmp_path, monkeypatch):
    _, file, root = project
    source_bytes = file.read_bytes()
    previous = tmp_path / 'previous.zip'
    backup_project(file, previous, project_dir=root)
    previous_bytes = previous.read_bytes()
    reads = 0
    open_path = Path.open

    def observe_open(path, mode='r', *args, **kwargs):
        nonlocal reads
        if path == file and mode == 'rb':
            reads += 1
        return open_path(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observe_open)
    destination = tmp_path / 'cancelled.zip'
    # First read validates the project, second streams it into the archive,
    # and the third reads source bytes again before publication.
    with pytest.raises(RenderCancelled):
        backup_project(file, destination, project_dir=root, cancelled=lambda: reads >= 3)
    assert reads >= 3
    assert file.read_bytes() == source_bytes
    assert previous.read_bytes() == previous_bytes
    assert not destination.exists()
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
