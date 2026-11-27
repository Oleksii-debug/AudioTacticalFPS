"""Explicit PCM input adoption with real producer, files and FFmpeg boundaries."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
from threading import Event
from types import SimpleNamespace
import warnings
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import library_sidecars, managed
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.managed import MissingInputAssets
from carr_library.audio.runtime import available_tool
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController


def pcm(path, *, amplitude=1200, frames=800):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(struct.pack('<h', amplitude if i % 20 < 10 else -amplitude)
                                   for i in range(frames)))
    return path.read_bytes()


def script(*names):
    return '[output sample_rate=8000 stems=false]\n' + '\n'.join('[sound "' + name + '"]' for name in names)


def external(tmp_path, *names):
    app = LibraryApp(tmp_path / 'library')
    inputs = tmp_path / 'selected-inputs'
    inputs.mkdir()
    output = tmp_path / 'external-output'
    output.mkdir()
    value = StudioController(private_root=app.root)
    value.new(script(*(names or ('effect.bin',))), input_kind='script', title='Selected local inputs')
    value.configure(output_dir=output)
    source = value.save(tmp_path / 'external-project.json')
    return app, value, inputs, source


def owned(value):
    root, document, audio = value._managed.paths(value.project_id)
    return root, document, audio / 'assets'


def assert_uncommitted(value, source, before):
    root, document, assets = owned(value)
    assert not document.exists()
    assert value.project_path == source and source.read_bytes() == before
    assert value.output_dir == source.parent / 'external-output'
    assert not list(root.glob('.studio-inputs-*'))
    assert not list(document.parent.glob('.studio-save-*'))
    return assets


def test_adoption_copies_only_typed_input_history_and_preserves_external_project(tmp_path):
    app, value, folder, source = external(tmp_path, 'ORIGINAL.WAV')
    original = source.read_bytes()
    value.edit_script(script('middle.bin'))
    value.edit_script(script('current.bin'))
    before = value.draft.to_dict()
    names = ('ORIGINAL.WAV', 'middle.bin', 'current.bin')
    supplied = {name: pcm(folder / name, amplitude=1000 + i * 100) for i, name in enumerate(names)}
    pcm(folder / 'unrelated.wav')
    (folder / 'credentials.json').write_bytes(b'PRIVATE NEIGHBOUR')
    saved = value.save_in_library(input_directory=folder)
    assert saved == owned(value)[1] and saved.is_relative_to(app.root)
    assert value.draft.to_dict() == before and not value.dirty
    assert source.read_bytes() == original
    assert {path.name for path in owned(value)[2].iterdir()} == set(names)
    for name, raw in supplied.items():
        target = owned(value)[2] / name
        assert target.read_bytes() == raw == (folder / name).read_bytes()
        assert target.stat().st_nlink == 1 and not target.samefile(folder / name)
    reopened = StudioController(private_root=app.root).open(saved)
    assert reopened.draft.to_dict() == before
    assert reopened.draft.undo() and reopened.script == script('middle.bin')
    assert reopened.draft.undo() and reopened.script == script('ORIGINAL.WAV')
    assert not list(owned(value)[0].glob('.studio-inputs-*'))


def test_saved_output_pointer_does_not_authorize_external_input_access(tmp_path):
    _, value, folder, source = external(tmp_path)
    pcm(value.output_dir / 'assets' / 'effect.bin')
    before = source.read_bytes()
    with pytest.raises(MissingInputAssets) as exc:
        value.save_in_library()
    assert exc.value.names == ('effect.bin',)
    assets = assert_uncommitted(value, source, before)
    assert not assets.exists() and not owned(value)[0].exists()


def test_adopted_inputs_survive_default_backup_restore_and_real_render_without_originals(tmp_path):
    app, value, folder, source = external(tmp_path, 'effect.bin')
    raw = pcm(folder / 'effect.bin')
    saved = value.save_in_library(input_directory=folder)
    snapshot = tmp_path / 'snapshot'
    token = create_backup(app, snapshot)
    manifest = json.loads((snapshot / 'manifest.json').read_bytes())
    assert manifest['generated_audio_included'] is False
    assert {Path(row['filename']).name for row in manifest['sidecars']} == {'project.json', 'effect.bin'}
    restored = restore_backup(snapshot, tmp_path / 'restored', token)
    shutil.rmtree(folder)
    source.unlink()
    (owned(value)[2] / 'effect.bin').unlink()
    ffmpeg = available_tool('ffmpeg')
    assert ffmpeg is not None, 'Real local FFmpeg is required for the adopted-input round trip.'
    reopened = StudioController(private_root=restored, ffmpeg=ffmpeg)
    reopened.open(restored / saved.relative_to(app.root))
    assert (reopened.output_dir / 'assets' / 'effect.bin').read_bytes() == raw
    result = reopened.render()
    assert result['status'] == 'complete' and Path(result['output']).is_relative_to(restored)
    assert wav_info(result['output']).frames == 800
    with wave.open(result['output'], 'rb') as rendered:
        assert any(rendered.readframes(rendered.getnframes()))


@pytest.mark.parametrize('kind', ['missing', 'not-pcm', 'truncated', 'directory'])
def test_invalid_selected_inputs_do_not_commit_or_change_external_files(tmp_path, kind):
    _, value, folder, source = external(tmp_path)
    selected = folder / 'effect.bin'
    if kind == 'not-pcm':
        selected.write_bytes(b'not audio')
    elif kind == 'truncated':
        selected.write_bytes(pcm(selected)[:-1])
    elif kind == 'directory':
        selected.mkdir()
    before = source.read_bytes()
    with pytest.raises(ValueError):
        value.save_in_library(input_directory=folder)
    assert not assert_uncommitted(value, source, before).exists()


@pytest.mark.parametrize('folder', ['https://example.invalid/audio', r'\\server\share', '//server/share'])
def test_uri_and_unc_input_folders_are_rejected_before_access(tmp_path, folder):
    _, value, _, source = external(tmp_path)
    before = source.read_bytes()
    with pytest.raises(ValueError, match='local'):
        value.save_in_library(input_directory=folder)
    assert not assert_uncommitted(value, source, before).exists()


@pytest.mark.parametrize('name', ['CON .wav', 'COM¹.wav', 'bad?.wav'])
def test_nonportable_input_names_are_refused_before_any_owned_write(tmp_path, name):
    _, value, folder, source = external(tmp_path, name)
    before = source.read_bytes()
    with pytest.raises(ValueError, match='filename'):
        value.save_in_library(input_directory=folder)
    assert not assert_uncommitted(value, source, before).exists()


def test_case_colliding_references_are_refused_on_every_filesystem(tmp_path):
    _, value, folder, source = external(tmp_path, 'effect.wav', 'EFFECT.wav')
    with pytest.raises(ValueError, match='case-insensitive'):
        value.save_in_library(input_directory=folder)
    assert not owned(value)[1].exists()


def test_actual_directory_link_or_windows_junction_is_refused(tmp_path):
    _, value, folder, source = external(tmp_path)
    raw = pcm(folder / 'effect.bin')
    link = tmp_path / 'selected-link'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(folder)],
                       check=True, capture_output=True, timeout=10)
    else:
        link.symlink_to(folder, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='links|reparse'):
            value.save_in_library(input_directory=link)
        assert (folder / 'effect.bin').read_bytes() == raw and not owned(value)[1].exists()
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()


def test_actual_hardlinked_input_is_refused(tmp_path):
    _, value, folder, source = external(tmp_path)
    raw = pcm(folder / 'effect.bin')
    os.link(folder / 'effect.bin', tmp_path / 'external-alias.bin')
    with pytest.raises(ValueError, match='hard links'):
        value.save_in_library(input_directory=folder)
    assert (folder / 'effect.bin').read_bytes() == raw and not owned(value)[1].exists()


@pytest.mark.parametrize('different', [False, True], ids=['identical-reuse', 'conflict'])
def test_explicit_selection_cannot_silently_replace_or_ignore_an_existing_filename(tmp_path, different):
    _, value, folder, source = external(tmp_path)
    raw = pcm(folder / 'effect.bin')
    saved = value.save_in_library(input_directory=folder)
    before = saved.read_bytes()
    selected = tmp_path / 'another-selection'
    selected.mkdir()
    replacement = pcm(selected / 'effect.bin', amplitude=800 if different else 1200)
    if different:
        with pytest.raises(ValueError, match='conflicts'):
            value.save_in_library(input_directory=selected)
        assert saved.read_bytes() == before
    else:
        assert value.save_in_library(input_directory=selected) == saved
    assert (owned(value)[2] / 'effect.bin').read_bytes() == raw
    assert (selected / 'effect.bin').read_bytes() == replacement


def test_cancel_during_staged_copy_preserves_external_project_and_releases_locks(tmp_path, monkeypatch):
    _, value, folder, source = external(tmp_path)
    raw = pcm(folder / 'effect.bin', frames=600000)
    before = source.read_bytes()
    cancelled = Event()
    copy = library_sidecars._copy
    def copied(source_path, target, **kwargs):
        result = copy(source_path, target, **kwargs)
        if Path(source_path).parent == folder and target is not None:
            cancelled.set()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(library_sidecars, '_copy', copied)
        with pytest.raises(TimeoutError, match='cancelled'):
            value.save_in_library(input_directory=folder, cancelled=cancelled.is_set)
    assets = assert_uncommitted(value, source, before)
    assert not assets.exists() and (folder / 'effect.bin').read_bytes() == raw
    assert value.save_in_library(input_directory=folder) == owned(value)[1]


def test_cancel_after_first_input_publication_preserves_existing_inputs_and_document(tmp_path):
    _, value, folder, source = external(tmp_path, 'a.bin')
    first = pcm(folder / 'a.bin')
    saved = value.save_in_library(input_directory=folder)
    before = saved.read_bytes()
    value.edit_script(script('a.bin', 'b.bin', 'c.bin'))
    pcm(folder / 'b.bin')
    pcm(folder / 'c.bin')
    with pytest.raises(TimeoutError):
        value.save_in_library(input_directory=folder,
            cancelled=lambda: (owned(value)[2] / 'b.bin').exists())
    assert saved.read_bytes() == before and value.dirty
    assert {p.name for p in owned(value)[2].iterdir()} == {'a.bin'}
    assert (owned(value)[2] / 'a.bin').read_bytes() == first
    assert not list(owned(value)[0].glob('.studio-inputs-*'))


@pytest.mark.parametrize('boundary', ['input-race', 'project-disk-failure', 'project-race'])
def test_actual_no_clobber_publication_and_metadata_failure_roll_back_only_own_inputs(tmp_path, monkeypatch, boundary):
    _, value, folder, source = external(tmp_path)
    raw = pcm(folder / 'effect.bin')
    before = source.read_bytes()
    _, document, assets = owned(value)
    publish = managed._publish_new
    competitor = b'COMPETING FILE; DO NOT REPLACE'
    def intercepted(incoming, destination, *args, **kwargs):
        destination = Path(destination)
        if boundary == 'input-race' and destination == assets / 'effect.bin':
            destination.write_bytes(competitor)
        if destination == document:
            assert (assets / 'effect.bin').read_bytes() == raw
            if boundary == 'project-disk-failure':
                raise OSError('synthetic publication disk failure')
            if boundary == 'project-race':
                destination.write_bytes(competitor)
        return publish(incoming, destination, *args, **kwargs)
    monkeypatch.setattr(managed, '_publish_new', intercepted)
    with pytest.raises(OSError):
        value.save_in_library(input_directory=folder)
    assert value.project_path == source and source.read_bytes() == before
    assert (folder / 'effect.bin').read_bytes() == raw
    if boundary == 'input-race':
        assert (assets / 'effect.bin').read_bytes() == competitor and not document.exists()
    else:
        assert not (assets / 'effect.bin').exists()
        assert document.read_bytes() == competitor if boundary == 'project-race' else not document.exists()
    assert not list(owned(value)[0].glob('.studio-inputs-*'))


def test_cleanup_does_not_delete_a_replacement_of_its_published_input(tmp_path):
    _, value, folder, source = external(tmp_path)
    pcm(folder / 'effect.bin')
    target = owned(value)[2] / 'effect.bin'
    competitor = b'REPLACED BY ANOTHER WRITER'
    def cancelled():
        if target.exists():
            target.unlink()
            target.write_bytes(competitor)
            return True
        return False
    with pytest.raises(TimeoutError):
        value.save_in_library(input_directory=folder, cancelled=cancelled)
    assert target.read_bytes() == competitor and not owned(value)[1].exists()


@pytest.mark.parametrize('change', ['source', 'folder', 'staged-copy', 'after-readback'])
def test_changed_source_or_selected_folder_or_staged_bytes_cannot_be_committed(tmp_path, monkeypatch, change):
    _, value, folder, source = external(tmp_path)
    raw = pcm(folder / 'effect.bin')
    before = source.read_bytes()
    copy = library_sidecars._copy
    def copied(source_path, target, **kwargs):
        result = copy(source_path, target, **kwargs)
        if Path(source_path).parent == folder and target is not None:
            if change == 'source':
                (folder / 'effect.bin').write_bytes(raw + b'changed')
            elif change == 'folder':
                folder.rename(tmp_path / 'original-selected-folder')
                folder.mkdir()
                (folder / 'effect.bin').write_bytes(raw)
            elif change == 'staged-copy':
                changed = bytearray(target.read_bytes())
                changed[-1] ^= 1
                target.write_bytes(changed)
        if (change == 'after-readback' and target is None
                and Path(source_path).parent.name.startswith('.studio-inputs-')):
            changed = bytearray(Path(source_path).read_bytes())
            changed[-1] ^= 1
            Path(source_path).write_bytes(changed)
        return result
    monkeypatch.setattr(library_sidecars, '_copy', copied)
    with pytest.raises(ValueError, match='changed|digest'):
        value.save_in_library(input_directory=folder)
    assert not assert_uncommitted(value, source, before).exists()


@pytest.mark.parametrize('limit', ['MAX_INPUT_FILE_BYTES', 'MAX_INPUT_BYTES', 'MAX_INPUT_FILES'])
def test_input_file_count_and_streaming_byte_budgets_precede_commit(tmp_path, monkeypatch, limit):
    _, value, folder, source = external(tmp_path, 'one.bin', 'two.bin')
    first = pcm(folder / 'one.bin')
    second = pcm(folder / 'two.bin')
    bound = {'MAX_INPUT_FILE_BYTES': len(first) - 1,
             'MAX_INPUT_BYTES': len(first) + len(second) - 1, 'MAX_INPUT_FILES': 1}[limit]
    monkeypatch.setattr(managed, limit, bound)
    with pytest.raises(ValueError):
        value.save_in_library(input_directory=folder)
    assert not owned(value)[1].exists() and not owned(value)[2].exists()


def test_postcommit_directory_sync_warning_does_not_reverse_adoption(tmp_path, monkeypatch):
    _, value, folder, source = external(tmp_path)
    raw = pcm(folder / 'effect.bin')
    sync = managed._sync_directory
    def failure(path):
        if Path(path) == owned(value)[1].parent:
            raise OSError('synthetic directory durability failure')
        return sync(path)
    monkeypatch.setattr(managed, '_sync_directory', failure)
    with warnings.catch_warnings():
        warnings.simplefilter('error', RuntimeWarning)
        saved = value.save_in_library(input_directory=folder)
    assert not value.dirty and saved == owned(value)[1]
    assert sha256(saved.read_bytes()).hexdigest() == value._file_hash
    assert (owned(value)[2] / 'effect.bin').read_bytes() == raw
    assert value._managed.last_warning and 'saved' in value._managed.last_warning


def test_new_document_publication_has_no_postcommit_temporary_hardlink_cleanup(tmp_path, monkeypatch):
    app, value, folder, source = external(tmp_path)
    pcm(folder / 'effect.bin')
    unlink = Path.unlink
    def unavailable(path, *args, **kwargs):
        if path.name.startswith('.studio-save-'):
            raise OSError('synthetic temporary cleanup failure')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unavailable)
    saved = value.save_in_library(input_directory=folder)
    assert saved.stat().st_nlink == 1 and not value.dirty
    assert value._managed._state(saved)[1] == value._file_hash
    # A successful save remains independently readable by the real backup path.
    assert len(create_backup(app, tmp_path / 'snapshot')) == 64


@pytest.mark.parametrize('accepted', [False, True], ids=['chooser-cancel', 'explicit-worker-adoption'])
def test_dialog_requests_a_folder_and_uses_cancellable_worker_for_real_adoption(tmp_path, accepted):
    _, value, folder, source = external(tmp_path)
    raw = pcm(folder / 'effect.bin')
    jobs, messages, destroyed = [], [], []
    chooser = SimpleNamespace(ShowModal=lambda: int(accepted), GetPath=lambda: str(folder),
                              Destroy=lambda: destroyed.append(True))
    dialog = object.__new__(AudioStudioDialog)
    dialog.busy, dialog.controller, dialog.dialog = False, value, object()
    dialog.cancelled = Event()
    dialog.wx = SimpleNamespace(DirDialog=lambda *args, **kwargs: chooser, ID_OK=1, DD_DIR_MUST_EXIST=2)
    dialog._capture = lambda: None
    dialog._refresh_text = lambda: None
    dialog._message = lambda message, **kwargs: messages.append(message)
    dialog._run = lambda label, action, done: jobs.append((label, action, done))
    dialog._error = lambda error: pytest.fail(str(error))
    assert dialog.save_in_library() is accepted
    assert destroyed == [True] and not owned(value)[1].exists()
    if accepted:
        assert len(jobs) == 1
        _, action, done = jobs[0]
        done(action())
        assert (owned(value)[2] / 'effect.bin').read_bytes() == raw and messages
    else:
        assert not jobs
