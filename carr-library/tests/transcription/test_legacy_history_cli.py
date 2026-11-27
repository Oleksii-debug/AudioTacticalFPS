"""Public CLI follows the same inert history and explicit current preparation."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library import transcription_cli as cli
from carr_library.transcription_receipt import PROTOCOL
from carr_library.transcription_state import read_definition
from tests.storage.legacy_transcription_fixture import canonical_data, current_runtime


def args(root, *values):
    return ['--data-dir', str(root), '--profile', PROTOCOL, *map(str, values)]


def result(capsys):
    return json.loads(capsys.readouterr().out)


def retained(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('CLI recovery')
    saved = canonical_data(app.root, complete=True)
    assert cli.main(args(app.root, 'retain-history')) == 0
    captured = result(capsys)
    assert captured['historical'] and captured['execution_started'] is False
    assert cli.main(args(app.root, 'history')) == 0
    row, = result(capsys)
    return app, saved, row


def reference(row):
    value = row['reference']
    return [value['job'], '--archive-kind', value['kind'], '--archive', value['archive']]


def test_history_listing_does_not_automatically_capture_or_create_current_jobs(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Earlier data')
    canonical_data(app.root)
    assert cli.main(args(app.root, 'history')) == 0
    assert result(capsys) == []
    assert not (app.root / 'state/transcription-imports').exists()
    assert not (app.root / 'transcription-jobs').exists()


def test_cli_prepares_only_explicit_old_sources_using_seventeen_current_settings(tmp_path, capsys):
    app, saved, row = retained(tmp_path, capsys)
    manifest = current_runtime(tmp_path / 'runtime')
    checksum = sha256(manifest.read_bytes()).hexdigest()
    assert cli.main(args(app.root, 'history-prepare', *reference(row), '--sources', saved.source['id'],
        '--manifest', manifest, '--expected-manifest-sha256', checksum,
        '--title', 'Current explicit recovery', '--language', 'uk', '--parallel-files', '2')) == 0
    prepared = result(capsys)
    definition = read_definition(app.root / 'transcription-jobs' / prepared['id'])
    assert definition['settings']['language'] == 'uk' and definition['settings']['parallel_files'] == 2
    assert definition['runtime_manifest_sha256'] == checksum and len(definition['settings']) == 17
    assert prepared['historical_source'] and prepared['id'] != saved.job['id']
    assert not (app.root / 'transcription-jobs' / prepared['id'] / 'execution.json').exists()


def test_cli_historical_output_is_exact_and_never_silently_overwrites(tmp_path, capsys):
    app, saved, row = retained(tmp_path, capsys)
    destination = tmp_path / 'accepted.txt'
    command = args(app.root, 'history-export', *reference(row), '--source', saved.source['id'],
                   '--format', 'txt', destination)
    assert cli.main(command) == 0
    assert result(capsys)['historical'] and destination.read_text(encoding='utf-8') == saved.text + '\n'
    before = destination.read_bytes()
    assert cli.main(command) == 1
    assert destination.read_bytes() == before
    capsys.readouterr()


def test_cli_saved_edit_becomes_separate_reading_project(tmp_path, capsys):
    from carr_library.reader.narration import NarrationStore
    app, saved, row = retained(tmp_path, capsys)
    assert cli.main(args(app.root, 'history-reading-copy', *reference(row),
                        '--source', saved.source['id'], '--edited')) == 0
    value = result(capsys)
    project, saved_digest = NarrationStore(app.root).load(value['id'])
    assert saved_digest == value['saved_sha256']
    assert project.original_text == 'Збережена окрема правка.' and value['citation_authority'] is False
    assert saved.source_path.read_bytes() == saved.raw


@pytest.mark.parametrize('operation', ['history', 'retain-history'])
def test_cli_requires_explicit_library_profile_for_earlier_history(tmp_path, operation):
    with pytest.raises(SystemExit) as error:
        cli.main(['--data-dir', str(tmp_path / 'not-created'), operation])
    assert error.value.code == 2 and not (tmp_path / 'not-created').exists()
