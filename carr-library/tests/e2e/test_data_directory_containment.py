"""Real data survives portable program removal; nested paths fail before writes."""
import json
import os
import shutil
import subprocess

import pytest

from carr_library import data_paths
from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.preferences import remember_directory, selected_directory


@pytest.fixture
def program(tmp_path, monkeypatch):
    root = tmp_path / 'Portable program Україна'
    root.mkdir()
    (root / 'bundle-manifest.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('CARR_PROGRAM_DIR', str(root))
    return root


def make_library(path):
    app = LibraryApp(path)
    workspace = app.create_workspace('Persistent research')
    app.add_text(workspace, 'Original', 'Research survives program removal.')
    return app, workspace


def alias_directory(link, target):
    if os.name == 'nt':
        # Directory junctions do not require Developer Mode/admin symlink rights.
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize('suffix', ['', 'research', 'new/Research data', 'parts/../research'])
def test_new_library_inside_program_is_rejected_without_creating_files(program, suffix):
    destination = program / suffix
    before = set(program.iterdir())
    with pytest.raises(ValueError, match='inside the program folder'):
        LibraryApp(destination)
    assert set(program.iterdir()) == before
    assert not (program / 'library.sqlite').exists()


def test_existing_nested_library_and_remembered_location_are_preserved(program, tmp_path, monkeypatch):
    monkeypatch.delenv('CARR_PROGRAM_DIR')
    app, workspace = make_library(program / 'older build data')
    settings = tmp_path / 'settings.json'
    remember_directory(app.root, settings)
    before_settings = settings.read_bytes()
    before_database = app.database.read_bytes()
    monkeypatch.setenv('CARR_PROGRAM_DIR', str(program))
    for action in (lambda: LibraryApp(app.root),
                   lambda: selected_directory(settings),
                   lambda: remember_directory(app.root, settings)):
        with pytest.raises(ValueError, match='inside the program folder'):
            action()
    assert settings.read_bytes() == before_settings
    assert app.database.read_bytes() == before_database
    assert len(app.search(workspace, 'survives')) == 1


def test_failed_remember_does_not_replace_safe_existing_preference(program, tmp_path, monkeypatch):
    safe, _ = make_library(tmp_path / 'safe research')
    settings = tmp_path / 'settings.json'
    remember_directory(safe.root, settings)
    before = settings.read_bytes()
    monkeypatch.delenv('CARR_PROGRAM_DIR')
    unsafe, _ = make_library(program / 'old data')
    monkeypatch.setenv('CARR_PROGRAM_DIR', str(program))
    with pytest.raises(ValueError, match='inside the program folder'):
        remember_directory(unsafe.root, settings)
    assert settings.read_bytes() == before
    assert selected_directory(settings) == safe.root


def test_real_alias_cannot_bypass_library_or_backup_containment(program, tmp_path):
    alias = tmp_path / 'Program alias'
    alias_directory(alias, program)
    app, _ = make_library(tmp_path / 'external data')
    for action in (lambda: LibraryApp(alias / 'research'),
                   lambda: app.backup(alias / 'backup')):
        with pytest.raises(ValueError, match='inside the program folder'):
            action()
    assert not (program / 'research').exists()
    assert not (program / 'backup').exists()


def test_real_alias_in_saved_settings_is_checked_again(program, tmp_path, monkeypatch):
    monkeypatch.delenv('CARR_PROGRAM_DIR')
    app, _ = make_library(program / 'older data')
    alias = tmp_path / 'Alias'
    alias_directory(alias, program)
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'version': 1, 'active_data_dir': str(alias / 'older data')}))
    before = settings.read_bytes()
    monkeypatch.setenv('CARR_PROGRAM_DIR', str(program))
    with pytest.raises(ValueError, match='inside the program folder'):
        selected_directory(settings)
    assert settings.read_bytes() == before
    assert app.database.exists()


def test_paths_through_program_are_rejected_even_when_alias_points_outside(program, tmp_path):
    outside = tmp_path / 'Outside data'
    outside.mkdir()
    alias = program / 'outward alias'
    alias_directory(alias, outside)
    with pytest.raises(ValueError, match='inside the program folder'):
        LibraryApp(alias / 'new research')
    assert not (outside / 'new research').exists()
    # The direct, independent location remains usable.
    assert LibraryApp(outside / 'new research').database.is_file()


def test_backup_and_restore_reject_program_destinations_before_publication(program, tmp_path):
    app, workspace = make_library(tmp_path / 'external data')
    backup = tmp_path / 'safe backup'
    digest = app.backup(backup)
    before = (backup / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='inside the program folder'):
        app.backup(program / 'unsafe backup')
    with pytest.raises(ValueError, match='inside the program folder'):
        restore_backup(backup, program / 'unsafe restore', digest)
    assert not (program / 'unsafe backup').exists()
    assert not (program / 'unsafe restore').exists()
    assert (backup / 'manifest.json').read_bytes() == before
    assert app.search(workspace, 'survives')[0].verified


def test_safe_library_and_backup_survive_removal_of_synthetic_program(program, tmp_path, monkeypatch):
    app, workspace = make_library(tmp_path / 'Research Україна')
    settings = tmp_path / 'settings.json'
    remember_directory(app.root, settings)
    backup = tmp_path / 'Independent backup'
    digest = app.backup(backup)
    # Only the synthetic empty program fixture is removed; all research is outside.
    shutil.rmtree(program)
    monkeypatch.delenv('CARR_PROGRAM_DIR')
    reopened = LibraryApp(selected_directory(settings))
    assert reopened.search(workspace, 'survives')[0].verified
    restored = tmp_path / 'Restored research'
    restore_backup(backup, restored, digest)
    assert LibraryApp(restored).search(workspace, 'survives')[0].verified


def test_direct_bundled_python_discovers_program_without_launcher_env(program, monkeypatch):
    monkeypatch.delenv('CARR_PROGRAM_DIR')
    monkeypatch.setattr(data_paths.sys, 'prefix', str(program / '.venv'))
    with pytest.raises(ValueError, match='inside the program folder'):
        LibraryApp(program / 'research')
    assert not (program / 'research').exists()


@pytest.mark.parametrize('configured', ['', 'relative/program'])
def test_invalid_explicit_program_binding_cannot_disable_the_guard(configured, tmp_path, monkeypatch):
    monkeypatch.setenv('CARR_PROGRAM_DIR', configured)
    with pytest.raises(ValueError, match='absolute'):
        LibraryApp(tmp_path / 'new data')
    assert not (tmp_path / 'new data').exists()


def test_cli_custom_data_dir_is_rejected_before_loading_desktop(program, capsys):
    from carr_library.__main__ import main
    assert main(['--data-dir', str(program / 'cli data')]) == 1
    assert 'inside the program folder' in capsys.readouterr().err
    assert not (program / 'cli data').exists()
