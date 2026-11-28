"""CLI and real pre-update backup through the current domain services."""
from dataclasses import replace
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.delivery_cli import create_update_backup, main, status_text
from carr_library.delivery_service import LicenseState
from carr_library.library_backup import restore_backup
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_fixture_spec = spec_from_file_location(
    '_carr_delivery_fixture', Path(__file__).with_name('test_delivery_service.py'))
_fixture_module = module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
setup = _fixture_module.setup


def test_pre_update_backup_preserves_populated_library_and_does_not_activate(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic update preservation')
    topic = app.create_topic(workspace, 'Research')
    imported = app.add_text(workspace, 'Original source', 'A retained railway book.')
    hit, = app.search(workspace, 'railway')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, 'Keep this note.')
    saved = create_update_backup(app, tmp_path / 'backup')
    restored = tmp_path / 'restored'
    restore_backup(saved.directory, restored, expected_manifest_sha256=saved.manifest_sha256)
    reopened = LibraryApp(restored)
    assert reopened.evidence(workspace, topic)[0].evidence_id == evidence
    assert reopened.evidence(workspace, topic)[0].note == 'Keep this note.'
    recovered_hit, = reopened.search(workspace, 'railway')
    assert reopened.copy(workspace, recovered_hit.ref, recovered_hit.revision, 'quote') == 'railway'
    assert app.search(workspace, 'railway')[0].revision == imported['id']


def test_cli_offline_import_export_status_and_no_clobber(tmp_path, capsys):
    service, provider, _ = setup()
    root = tmp_path / 'library'
    root.mkdir()
    exported = tmp_path / 'signed.json'
    assert main(['--data-dir', str(root), 'export-license', str(exported)], service=service) == 0
    assert exported.read_bytes() == b'explicit synthetic signed license'
    assert main(['--data-dir', str(root), 'export-license', str(exported)], service=service) == 1
    assert main(['import-license', str(exported)], service=service) == 0
    assert provider.value.generation == 2
    assert main(['status'], service=service) == 0
    assert 'Verified license' in capsys.readouterr().out


def test_cli_requires_exact_recovered_update_and_backup(tmp_path, capsys):
    service, provider, _ = setup()
    package = tmp_path / 'package'
    package.write_bytes(b'fixture')
    assert main(['import-update', str(package)], service=service) == 0
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Preserved')
    app.add_text(workspace, 'Source', 'Preserved source.')
    args = ['--data-dir', str(app.root), 'install', '--stage-id', 'b' * 32,
        '--target-build', 'build-2', '--sha256', 'a' * 64,
        '--backup-dir', str(tmp_path / 'backup'), '--approve']
    wrong = args.copy()
    wrong[wrong.index('--sha256') + 1] = 'd' * 64
    assert main(wrong, service=service) == 1
    assert not (tmp_path / 'backup').exists() and 'install' not in provider.calls
    assert main(args, service=service) == 0
    assert (tmp_path / 'backup' / 'manifest.json').is_file()
    assert app.search(workspace, 'Preserved')
    assert provider.calls.count('install') == 1


@pytest.mark.parametrize('action', ['check', 'renew'])
def test_cli_never_implies_network_consent(action, capsys):
    service, provider, _ = setup()
    assert main([action], service=service) == 1
    assert provider.calls == []
    assert 'Approve' in capsys.readouterr().out


def test_entrypoint_uses_injected_service_and_absent_default(capsys):
    from carr_library.__main__ import main as launch
    service, provider, _ = setup()
    assert launch(['delivery', 'status'], delivery=service) == 0
    assert 'Verified license' in capsys.readouterr().out
    assert launch(['delivery', 'status']) == 0
    assert 'not configured' in capsys.readouterr().out


def test_status_distinguishes_perpetual_and_expired_with_preserved_data():
    service, provider, _ = setup()
    expired = replace(provider.value, state=LicenseState.EXPIRED)
    perpetual = replace(provider.value, state=LicenseState.PERPETUAL, valid_until=None)
    assert 'expired' in status_text(expired)
    assert 'perpetual' in status_text(perpetual)
    assert 'remain accessible' in status_text(expired)
