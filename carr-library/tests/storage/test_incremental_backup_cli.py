"""Installed-route subprocess checks; no shell, provider or inference is used."""
import json
import os
from pathlib import Path
import subprocess
import sys

from carr_library.application import LibraryApp
from carr_library.library_backup import verify_backup


def run_cli(*arguments):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                       PYTHONIOENCODING='ascii', PYTHONUTF8='0')
    return subprocess.run([sys.executable, '-B', '-m', 'carr_library',
                           'backup-repository', *map(str, arguments)],
                          capture_output=True, timeout=60, env=environment)


def success(*arguments):
    result = run_cli(*arguments)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout.decode('utf-8'))


def test_actual_cli_creates_lists_verifies_exports_and_restores_successive_revisions(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека')
    workspace = app.create_workspace('Джерела')
    app.add_text(workspace, 'Перша книга', 'Railway first original.')
    repository = tmp_path / 'Знімки'
    created = success('init', '--repository', repository)
    assert created['path'] == str(repository)
    first = success('create', '--repository', repository, '--data-dir', app.root)
    app.add_text(workspace, 'Друга книга', 'Railway later original.')
    second = success('create', '--repository', repository, '--data-dir', app.root,
                     '--parent-sha256', first['snapshot_sha256'])
    assert second['parent'] == first['snapshot_sha256']
    assert second['published'] and second['reused_chunks'] > 0
    listing = success('list', '--repository', repository)
    assert len(listing['snapshots']) == 2 and listing['payloads_verified'] is False
    verified = success('verify', '--repository', repository,
                       '--snapshot-sha256', second['snapshot_sha256'])
    assert verified['payloads_verified'] is True
    output = tmp_path / 'Повна копія'
    exported = success('materialize', '--repository', repository,
                       '--snapshot-sha256', second['snapshot_sha256'], '--output', output)
    assert exported['published']
    assert verify_backup(output, second['backup_manifest_sha256'])['version'] in (1, 2, 3, 4, 5)
    for number, saved in ((1, first), (2, second)):
        destination = tmp_path / ('Відновлення-' + str(number))
        restored = success('restore', '--repository', repository,
                           '--snapshot-sha256', saved['snapshot_sha256'], '--output', destination)
        assert restored['restored'] and not restored['staging_cleanup_pending']
        assert len(LibraryApp(destination).search(workspace, 'Railway')) == number


def test_actual_cli_requires_existing_library_and_never_initializes_implicitly(tmp_path):
    repository = tmp_path / 'repository'
    absent_library = tmp_path / 'absent-library'
    failed = run_cli('create', '--repository', repository, '--data-dir', absent_library)
    assert failed.returncode == 1
    assert not repository.exists() and not absent_library.exists()
    success('init', '--repository', repository)
    failed = run_cli('create', '--repository', repository, '--data-dir', absent_library)
    assert failed.returncode == 1 and not absent_library.exists()
    assert success('list', '--repository', repository)['snapshots'] == []


def test_actual_cli_no_clobber_and_wrong_independent_digest_refuse_publication(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('One library')
    repository = tmp_path / 'repository'
    success('init', '--repository', repository)
    saved = success('create', '--repository', repository, '--data-dir', app.root)
    existing = tmp_path / 'keep'
    existing.mkdir()
    (existing / 'owner.txt').write_bytes(b'keep unchanged')
    refused = run_cli('restore', '--repository', repository,
                      '--snapshot-sha256', saved['snapshot_sha256'], '--output', existing)
    assert refused.returncode == 1
    assert list(existing.iterdir()) == [existing / 'owner.txt']
    assert (existing / 'owner.txt').read_bytes() == b'keep unchanged'
    destination = tmp_path / 'wrong-hash'
    refused = run_cli('restore', '--repository', repository,
                      '--snapshot-sha256', '0' * 64, '--output', destination)
    assert refused.returncode == 1 and not destination.exists()
    missing = run_cli('restore', '--repository', repository, '--output', destination)
    assert missing.returncode == 2 and not destination.exists()


def test_existing_cli_dispatch_remains_available(tmp_path):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for route in ('sync', 'operations', 'assistant', 'import-text'):
        result = subprocess.run([sys.executable, '-B', '-m', 'carr_library', route, '--help'],
            capture_output=True, timeout=20, env=environment)
        assert result.returncode == 0, (route, result.stderr)
    result = subprocess.run([sys.executable, '-B', '-m', 'carr_library', '--help'],
        capture_output=True, timeout=20, env=environment)
    assert result.returncode == 0
    assert b'--backup' in result.stdout and b'--restore' in result.stdout
