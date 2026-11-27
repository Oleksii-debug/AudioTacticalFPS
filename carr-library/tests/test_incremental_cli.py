"""The command surface runs the actual complete snapshot store."""
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.incremental_cli import main


def test_cli_create_list_verify_and_restore_exact_library(tmp_path, capsys):
    source, repository, restored = tmp_path / 'data', tmp_path / 'snapshots', tmp_path / 'restored'
    app = LibraryApp(source)
    workspace = app.create_workspace('CLI synthetic library')
    app.add_text(workspace, 'CLI source', 'Exact CLI snapshot quotation.')
    assert main(['create', '--data-dir', str(source), '--repository', str(repository)]) == 0
    created = json.loads(capsys.readouterr().out)
    digest = created['snapshot']['snapshot_sha256']
    assert created['status'] == 'complete' and len(digest) == 64
    assert main(['list', '--repository', str(repository)]) == 0
    history = json.loads(capsys.readouterr().out)
    assert history['object_verification'] == 'NOT_RUN'
    assert history['snapshots'][0]['snapshot_sha256'] == digest
    assert main(['verify', '--repository', str(repository), '--snapshot-sha256', digest]) == 0
    assert json.loads(capsys.readouterr().out)['snapshot'] == created['snapshot']
    assert main(['restore', '--repository', str(repository), '--snapshot-sha256', digest,
                 '--destination', str(restored)]) == 0
    assert Path(json.loads(capsys.readouterr().out)['path']) == restored.resolve()
    hit, = LibraryApp(restored).search(workspace, 'Exact CLI')
    assert hit.quote == 'Exact CLI'


def test_missing_source_cli_never_creates_a_new_empty_library(tmp_path, capsys):
    source, repository = tmp_path / 'missing', tmp_path / 'snapshots'
    assert main(['create', '--data-dir', str(source), '--repository', str(repository)]) == 1
    assert json.loads(capsys.readouterr().err)['status'] == 'failed'
    assert not source.exists() and not repository.exists()


def test_cli_invalid_digest_never_reports_verified_or_creates_destination(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'data')
    app.create_workspace('Synthetic')
    from carr_library.incremental_backup import create_snapshot
    repository = tmp_path / 'snapshots'
    create_snapshot(app, repository)
    target = tmp_path / 'refused'
    assert main(['restore', '--repository', str(repository), '--snapshot-sha256', 'bad',
                 '--destination', str(target)]) == 1
    output = capsys.readouterr()
    assert not output.out and json.loads(output.err)['status'] == 'failed'
    assert not target.exists()


def test_cli_requires_a_separately_supplied_checksum(tmp_path):
    with pytest.raises(SystemExit) as caught:
        main(['restore', '--repository', str(tmp_path), '--destination', str(tmp_path / 'new')])
    assert caught.value.code == 2


def test_cli_has_no_implicit_delete_or_schedule_command():
    with pytest.raises(SystemExit) as caught:
        main(['delete'])
    assert caught.value.code == 2
