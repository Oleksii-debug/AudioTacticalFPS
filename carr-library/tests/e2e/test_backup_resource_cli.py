"""Real canonical and incremental commands with explicit caller resource policy."""
import argparse
from dataclasses import replace
from hashlib import sha256
import importlib.metadata
import json
from pathlib import Path
import sys
import sysconfig

import pytest

from carr_library import __main__ as primary, backup_incremental_cli as cli
from carr_library.application import LibraryApp
from carr_library.backup_budget import BackupBudget, MIB, GIB
from carr_library.backup_incremental import BackupReference, create_incremental_backup
from carr_library.backup_options import (FIELDS, add_resource_arguments,
    parse_resource_values, resource_values, resources_from_arguments)
from carr_library.library_backup import create_backup, verify_backup


def choices():
    return ['--backup-reviewed-resources', '--backup-memory-mib', '768',
            '--backup-staging-mib', '256', '--backup-seconds', '300']


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Public backup resources')
    assert app.add_text(workspace, 'Exact original', 'Original Ω.\r\nSecond exact Ї.',
                        edition_id='original')['state'] == 'complete'
    yield app, workspace


def parser():
    value = argparse.ArgumentParser()
    add_resource_arguments(value)
    return value


def test_resource_flags_are_omitted_or_preserve_every_typed_field_exactly():
    assert resources_from_arguments(parser().parse_args([])) is None
    assert resources_from_arguments(parser().parse_args(['--backup-reviewed-resources'])) == BackupBudget()
    selected = replace(BackupBudget(), original_bytes=2*GIB, assets=50_000,
        asset_bytes=32*GIB, jobs=60_000, checkpoints=2_000_000,
        memory_bytes=2*GIB, staging_bytes=64*GIB, seconds=600.25)
    assert parse_resource_values(resource_values(selected)) == selected
    argv = [item for name, flag, _label, _unit in FIELDS
            for item in ('--backup-' + flag, resource_values(selected)[name])]
    assert resources_from_arguments(parser().parse_args(argv)) == selected
    with pytest.raises(ValueError, match='whole MiB'):
        resource_values(replace(selected, original_bytes=selected.original_bytes+1))


@pytest.mark.parametrize('field,value', [
    ('assets', '0'), ('assets', '-1'), ('assets', '2147483648'), ('assets', '１'),
    ('original_bytes', '0'), ('original_bytes', '1.5'),
    ('original_bytes', '99999999999999999999999999999999999'),
    ('seconds', 'nan'), ('seconds', 'inf'), ('seconds', '-inf'),
    ('seconds', '0.09'), ('seconds', '86400.01'),
    ('manifest_bytes', '257'), ('memory_bytes', '1'),
    ('staging_bytes', ''), ('pending_files', True),
])
def test_invalid_external_resources_do_not_reach_a_writer(field, value):
    selected = resource_values()
    selected[field] = value
    with pytest.raises((ValueError, TypeError)):
        parse_resource_values(selected)


def test_resource_shape_cannot_silently_supply_an_extra_or_missing_field():
    selected = resource_values()
    for invalid in ({**selected, 'execute': 'yes'},
                    {name: value for name, value in selected.items() if name != 'jobs'}):
        with pytest.raises(ValueError):
            parse_resource_values(invalid)


@pytest.mark.parametrize('arguments', [
    ['--backup-unknown', '1'],
    ['--backup-assets', '-1'],
    ['--backup-seconds', 'nan'],
    ['--backup-seconds', 'inf'],
    ['--backup-reviewed-resources'],
])
def test_primary_invalid_or_unscoped_resource_options_create_no_library(tmp_path, arguments):
    missing = tmp_path / 'mistyped-library'
    with pytest.raises(SystemExit) as failure:
        primary.main(['--data-dir', str(missing), *arguments])
    assert failure.value.code == 2
    assert not missing.exists()


@pytest.mark.parametrize('selected', [False, True])
def test_primary_full_backup_and_restore_use_the_actual_canonical_writer(source, tmp_path, capsys, selected):
    app, workspace = source
    folder = tmp_path / 'backup'
    flags = choices() if selected else []
    assert primary.main(['--data-dir', str(app.root), '--backup', str(folder), *flags]) == 0
    output = capsys.readouterr().out
    raw = (folder / 'manifest.json').read_bytes()
    checksum = sha256(raw).hexdigest()
    assert checksum in output
    manifest = verify_backup(folder, checksum)
    assert manifest['version'] == 4 and 'backup_budget' not in manifest
    target = tmp_path / 'restored'
    assert primary.main(['--data-dir', str(target), '--restore', str(folder),
        '--manifest-sha256', checksum, *flags]) == 0
    assert not (target / '.restore-incomplete').exists()
    reopened = LibraryApp(target)
    hit, = reopened.search(workspace, 'Second exact')
    assert reopened.copy(workspace, hit.ref, hit.revision, 'quote') == 'Second exact'


def test_primary_incremental_command_and_declared_console_entrypoint_use_same_receipt(source, tmp_path, capsys):
    app, workspace = source
    base_path = tmp_path / 'base'
    checksum = create_backup(app, base_path)
    layer = tmp_path / 'layer'
    assert primary.main(['backup-incremental', 'create', '--data-dir', str(app.root),
        '--output', str(layer), '--base', str(base_path), checksum, *choices()]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['path'] == str(layer) and receipt['base_required'] is True
    selected = [entry for entry in importlib.metadata.entry_points(group='console_scripts')
                if entry.name == 'carr-backup-incremental']
    assert len(selected) == 1
    assert selected[0].value == 'carr_library.backup_incremental_cli:main'
    callback = selected[0].load()
    common = ['--source', str(layer), '--sha256', receipt['manifest_sha256'],
              '--base', str(base_path), checksum, *choices()]
    assert callback(['verify', *common]) == 0
    proof = json.loads(capsys.readouterr().out)
    assert proof['chain'] == [receipt['manifest_sha256'], checksum]
    target = tmp_path / 'recovered'
    assert primary.main(['backup-incremental', 'restore', *common, '--output', str(target)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['external_execution_review_required'] is True
    assert LibraryApp(target).edition_metadata(workspace, 'original').title == 'Exact original'


@pytest.mark.parametrize('mode', ['absent', 'empty'])
def test_incremental_mistyped_library_does_not_create_a_directory_or_database(tmp_path, capsys, mode):
    selected = tmp_path / 'missing-library'
    if mode == 'empty':
        selected.mkdir()
    destination = tmp_path / 'never-published'
    assert cli.main(['create', '--data-dir', str(selected), '--output', str(destination),
        '--base', str(tmp_path / 'unselected-base'), '0'*64, *choices()]) == 1
    assert 'Incremental backup:' in capsys.readouterr().err
    assert not (selected / 'library.sqlite').exists()
    assert selected.exists() == (mode == 'empty')
    assert not destination.exists()


@pytest.mark.parametrize('field,value', [
    ('assets', '-1'), ('seconds', 'nan'), ('seconds', 'inf'),
    ('memory-mib', '0'), ('original-mib', '1.5'),
])
def test_incremental_invalid_flags_fail_before_source_or_target_changes(tmp_path, capsys, field, value):
    selected, destination = tmp_path / 'missing', tmp_path / 'refused'
    assert cli.main(['create', '--data-dir', str(selected), '--output', str(destination),
        '--base', str(tmp_path / 'base'), '0'*64, '--backup-' + field, value]) == 1
    assert 'Incremental backup:' in capsys.readouterr().err
    assert not selected.exists() and not destination.exists()


def test_missing_required_incremental_base_cannot_publish_a_partial_restore(source, tmp_path, capsys):
    app, _ = source
    base_path = tmp_path / 'base'
    checksum = create_backup(app, base_path)
    policy = BackupBudget(memory_bytes=768*MIB, staging_bytes=256*MIB, seconds=300)
    layer = create_incremental_backup(app, tmp_path / 'layer',
        base=BackupReference(base_path, checksum), backup_budget=policy)
    target = tmp_path / 'refused'
    assert primary.main(['backup-incremental', 'restore', '--source', str(layer.path),
        '--sha256', layer.manifest_sha256, '--output', str(target), *choices()]) == 1
    assert 'required base' in capsys.readouterr().err
    assert not target.exists()


def test_ordinary_child_entrypoint_has_exact_selected_or_identical_installed_authority(
        source, tmp_path):
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    import carr_library
    app, _ = source
    base_path = tmp_path / 'base'
    checksum = create_backup(app, base_path)
    parent = Path(carr_library.__file__).resolve().parent
    checkout = Path(__file__).resolve().parents[2] / 'src' / 'carr_library'
    allowed = [str(parent)]
    if parent == checkout:
        installed = Path(sysconfig.get_path('purelib')).resolve() / 'carr_library'
        allowed.append(str(installed))
    expected = {str(path.relative_to(parent)).replace('\\', '/'): sha256(path.read_bytes()).hexdigest()
                for path in parent.rglob('*.py')}
    proof_path = tmp_path / 'selected-package.json'
    raw = json.dumps({'allowed': allowed, 'files': expected}, sort_keys=True).encode()
    proof_path.write_bytes(raw)
    child = r'''
import json, pathlib, sys, hashlib
import carr_library
raw = pathlib.Path(sys.argv[1]).read_bytes()
assert hashlib.sha256(raw).hexdigest() == sys.argv[2]
proof = json.loads(raw)
root = pathlib.Path(carr_library.__file__).resolve().parent
allowed, expected = proof["allowed"], proof["files"]
assert str(root) in allowed
actual = {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in root.rglob("*.py")}
assert actual == expected
import carr_library.__main__ as entry
assert pathlib.Path(entry.__file__).resolve() == root / "__main__.py"
assert entry.main(["backup-incremental", "verify", "--source", sys.argv[3],
                  "--sha256", sys.argv[4], "--backup-memory-mib", "768",
                  "--backup-staging-mib", "256", "--backup-seconds", "60"]) == 0
'''
    result = run_process([sys.executable, '-B', '-c', child,
        str(proof_path), sha256(raw).hexdigest(), str(base_path), checksum],
        budget=AudioBudget(timeout=90, max_output_bytes=2*MIB), cwd=tmp_path, owned_tree=True)
    proof = json.loads(result.stdout)
    assert proof['manifest_sha256'] == checksum and proof['chain'] == [checksum]
