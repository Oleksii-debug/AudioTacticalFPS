"""Explicit local setup contracts; tiny archive tests are synthetic, not Windows qualification."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

from carr_library.audio import runtime

PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup_tool():
    path = PROJECT / 'packaging' / 'setup_local_audio.py'
    spec = importlib.util.spec_from_file_location('carr_local_setup_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def selected(tmp_path, monkeypatch, setup_tool):
    """Actual tiny ZIP and fixed member hashes; no executable is ever run."""
    members = {
        'ffmpeg.exe': ('fixture/bin/ffmpeg.exe', b'synthetic ffmpeg bytes'),
        'ffprobe.exe': ('fixture/bin/ffprobe.exe', b'synthetic ffprobe bytes'),
        'ffplay.exe': ('fixture/bin/ffplay.exe', b'synthetic ffplay bytes'),
        'LICENSE': ('fixture/LICENSE', b'synthetic license notice'),
    }
    archive = tmp_path / 'selected.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        for member, data in members.values():
            stream.writestr(member, data)
    inventory = {name: (member, len(data), sha256(data).hexdigest())
                 for name, (member, data) in members.items()}
    monkeypatch.setattr(runtime, 'ARCHIVE_MEMBERS', inventory)
    monkeypatch.setattr(runtime, 'FFMPEG_ARCHIVE_BYTES', archive.stat().st_size)
    monkeypatch.setattr(runtime, 'FFMPEG_ARCHIVE_SHA256', sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setattr(runtime, 'FFMPEG_VERSION', 'synthetic-fixed')
    monkeypatch.setattr(runtime, 'FFMPEG_SOURCE_COMMIT', 'a' * 40)
    library = tmp_path / 'library'
    library.mkdir()
    (library / 'keep.txt').write_bytes(b'Original library data')
    return archive, tmp_path / 'new-audio', library


def test_default_plan_is_read_only_and_uses_current_pinned_inventory(tmp_path, monkeypatch, setup_tool):
    output = tmp_path / 'not-created'
    monkeypatch.setattr(setup_tool, '_load_preparer', lambda: pytest.fail('Plan must not load preparation.'))
    report = setup_tool.plan(destination=output)
    assert report['status'] == 'plan' and report['archive'] is None
    assert report['archive_sha256'] == runtime.FFMPEG_ARCHIVE_SHA256
    assert report['archive_bytes'] == runtime.FFMPEG_ARCHIVE_BYTES
    assert report['runtime_files'] == sorted(runtime.ARCHIVE_MEMBERS)
    assert report['runtime_bytes'] == sum(row[1] for row in runtime.ARCHIVE_MEMBERS.values())
    assert report['required_free_bytes'] == 2 * report['runtime_bytes'] + 64 * 1024 ** 2
    assert report['downloads'] is False and report['engine_settings_changed'] is False
    assert 'NOT_RUN' in report['native_verification']
    assert not output.exists()


@pytest.mark.parametrize('kind', ['relative', 'parent', 'inside-program', 'same-program',
                                  'inside-library', 'library-parent'])
def test_destination_refuses_application_library_and_ambiguous_paths(tmp_path, monkeypatch, setup_tool, kind):
    program, library = tmp_path / 'program', tmp_path / 'library'
    program.mkdir()
    library.mkdir()
    monkeypatch.setenv('CARR_PROGRAM_DIR', str(program))
    monkeypatch.setattr(setup_tool, '_installation_root', lambda: program)
    values = {
        'relative': Path('relative'),
        'parent': tmp_path / 'safe' / '..' / 'new',
        'inside-program': program / 'new',
        'same-program': program,
        'inside-library': library / 'new',
        'library-parent': tmp_path,
    }
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*'))
    with pytest.raises(ValueError):
        setup_tool.plan(destination=values[kind], library_root=library)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*')) == before


def test_existing_destination_is_never_adopted_or_changed(tmp_path, setup_tool):
    output = tmp_path / 'existing'
    output.mkdir()
    marker = output / 'keep'
    marker.write_bytes(b'existing private file')
    with pytest.raises(FileExistsError):
        setup_tool.plan(destination=output)
    assert marker.read_bytes() == b'existing private file'


@pytest.mark.parametrize('kind', ['relative', 'hardlink', 'wrong-size'])
def test_archive_rejects_unsafe_or_unpinned_selection(tmp_path, setup_tool, selected, kind):
    archive, output, library = selected
    if kind == 'relative':
        value = Path(archive.name)
    elif kind == 'hardlink':
        value = tmp_path / 'alias.zip'
        os.link(archive, value)
    else:
        value = tmp_path / 'wrong.zip'
        value.write_bytes(b'wrong size')
    with pytest.raises(ValueError):
        setup_tool.plan(value, output, library_root=library)
    assert not output.exists()


def test_changed_selected_archive_requires_a_new_review(tmp_path, monkeypatch, setup_tool, selected):
    archive, output, library = selected
    reviewed = setup_tool.plan(archive, output, library_root=library)
    replacement = tmp_path / 'replacement.zip'
    replacement.write_bytes(archive.read_bytes())
    os.replace(replacement, archive)
    monkeypatch.setattr(setup_tool, '_require_windows', lambda: None)
    monkeypatch.setattr(setup_tool, '_load_preparer', lambda: pytest.fail('Changed identity must fail before preparation.'))
    with pytest.raises(ValueError, match='changed'):
        setup_tool.prepare_local(reviewed)
    assert not output.exists()
    assert (library / 'keep.txt').read_bytes() == b'Original library data'


@pytest.mark.parametrize('answer', ['NO', 'EOF'])
def test_interactive_refusal_never_starts_preparation(monkeypatch, setup_tool, selected, answer):
    archive, output, library = selected
    def respond(_prompt):
        if answer == 'EOF':
            raise EOFError
        return answer
    monkeypatch.setattr('builtins.input', respond)
    monkeypatch.setattr(setup_tool, 'prepare_local', lambda _plan: pytest.fail('Preparation was not authorized.'))
    assert setup_tool.main(['--interactive', '--archive', str(archive), '--destination', str(output),
                              '--library-root', str(library)]) == 0
    assert not output.exists()
    assert (library / 'keep.txt').read_bytes() == b'Original library data'


def test_synthetic_pinned_archive_uses_existing_preparer_and_final_verifier(monkeypatch, setup_tool, selected):
    archive, output, library = selected
    reviewed = setup_tool.plan(archive, output, library_root=library)
    preparer = setup_tool._load_preparer()
    native = []
    def synthetic_native_check(directory):
        native.append(Path(directory))
        for name in runtime.ARCHIVE_MEMBERS:
            runtime.regular_file(Path(directory) / name)
        return {'status': 'PASS', 'platform': 'Windows', 'fixture': 'synthetic-only'}
    monkeypatch.setattr(preparer, '_native_check', synthetic_native_check)
    monkeypatch.setattr(setup_tool, '_load_preparer', lambda: preparer)
    monkeypatch.setattr(setup_tool, '_require_windows', lambda: None)
    original = archive.read_bytes()
    report = setup_tool.prepare_local(reviewed)
    assert len(native) == 1
    persisted = runtime.verify_runtime(output / 'runtime')
    assert persisted['native_validation']['fixture'] == 'synthetic-only'
    assert report['status'] == 'prepared' and report['engine_settings_changed'] is False
    assert report['downloads'] is False
    assert Path(report['ffmpeg']).read_bytes() == b'synthetic ffmpeg bytes'
    assert Path(report['ffprobe']).read_bytes() == b'synthetic ffprobe bytes'
    assert archive.read_bytes() == original
    assert (library / 'keep.txt').read_bytes() == b'Original library data'


def test_disk_budget_refusal_precedes_any_created_work_folder(monkeypatch, setup_tool, selected):
    archive, output, library = selected
    reviewed = setup_tool.plan(archive, output, library_root=library)
    monkeypatch.setattr(setup_tool, '_require_windows', lambda: None)
    monkeypatch.setattr(setup_tool.shutil, 'disk_usage',
                        lambda _path: SimpleNamespace(free=reviewed['required_free_bytes'] - 1))
    monkeypatch.setattr(setup_tool, '_load_preparer', lambda: pytest.fail('Disk refusal must precede preparation.'))
    with pytest.raises(OSError, match='disk space'):
        setup_tool.prepare_local(reviewed)
    assert not output.exists()


def test_non_windows_preparation_does_not_write(monkeypatch, setup_tool, selected):
    archive, output, library = selected
    reviewed = setup_tool.plan(archive, output, library_root=library)
    with monkeypatch.context() as patch:
        patch.setattr(setup_tool.os, 'name', 'posix')
        with pytest.raises(RuntimeError, match='Windows x64'):
            setup_tool.prepare_local(reviewed)
    assert not output.exists()


def test_same_size_wrong_archive_fails_existing_hash_guard_without_runtime(monkeypatch, setup_tool, selected):
    archive, output, library = selected
    wrong = bytearray(archive.read_bytes())
    wrong[-1] ^= 1
    archive.write_bytes(wrong)
    reviewed = setup_tool.plan(archive, output, library_root=library)
    monkeypatch.setattr(setup_tool, '_require_windows', lambda: None)
    with pytest.raises(ValueError, match='SHA256'):
        setup_tool.prepare_local(reviewed)
    assert output.is_dir() and not (output / 'runtime').exists()
    assert (library / 'keep.txt').read_bytes() == b'Original library data'


def test_fixed_sibling_loading_restores_import_state(setup_tool):
    path_before, bytecode_before = list(sys.path), sys.dont_write_bytecode
    helper = setup_tool._load_preparer()
    assert callable(helper.prepare)
    assert sys.path == path_before
    assert sys.dont_write_bytecode == bytecode_before
