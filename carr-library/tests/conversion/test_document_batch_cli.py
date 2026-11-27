"""Actual CLI capture/conversion/preview/receipt/recovery with original local text."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.conversion import cli
from carr_library.conversion.batches import DocumentBatches
from carr_library.library_backup import restore_backup
from carr_library.speech.reading import ReadingDocument


@pytest.fixture
def workspace(tmp_path, capsys):
    library = LibraryApp(tmp_path / 'бібліотека')
    source = tmp_path / 'перший документ.txt'
    text = '😀 Original first paragraph.\r\n\r\n[Scene: literal prose]\r\nSpeaker: remains source text.\n'
    source.write_bytes(text.encode('utf-8'))
    destination = tmp_path / 'outputs'; destination.mkdir()
    def call(*arguments, root=None, expected=0):
        status = cli.main(['--data-dir', str(root or library.root), *(str(value) for value in arguments)])
        captured = capsys.readouterr()
        assert status == expected, captured.err
        return json.loads(captured.out) if captured.out.strip() else None, captured.err
    return library, source, text, destination, call


def prepared(workspace):
    library, source, _, _, call = workspace
    row, _ = call('capture', source)
    identity = row['id']
    assert row['status'] == 'preparing' and row['prepared_files'] == 0
    assert not any((library.root / 'state').glob('studio/*'))
    source.unlink()  # subsequent CLI operations require only retained inputs
    row, _ = call('prepare', identity)
    assert row['status'] == 'prepared' and row['prepared_files'] == 1
    return identity


def approved(workspace):
    _, source, _, destination, call = workspace
    identity = prepared(workspace)
    review_file = source.parent / 'review.json'
    review, _ = call('preview', identity, '--destination', destination, '--mode', 'blank-lines', '--output', review_file)
    assert len(review['preview']['outputs']) == 2 and not list(destination.iterdir())
    row, _ = call('approve', '--review', review_file, '--review-sha256', review['review_sha256'])
    assert row['status'] == 'reviewed' and not list(destination.iterdir())
    return identity, review_file, review


def test_complete_cli_review_run_and_restart_reuse_are_explicit(workspace):
    library, _, text, destination, call = workspace
    identity, _, review = approved(workspace)
    row, _ = call('run', identity)
    assert row['status'] == 'complete' and row['completed_outputs'] == 2
    outputs = [destination / name for name in review['preview']['outputs']]
    assert b''.join(path.read_bytes() for path in outputs) == text.encode('utf-8')
    before = [(path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) for path in outputs]
    assert call('run', identity)[0]['status'] == 'complete'
    assert before == [(path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) for path in outputs]
    saved, _ = call('show', identity)
    assert 'Stored checkpoint metadata' in saved['verification']
    assert saved['review_sha256'] == review['review_sha256']
    jobs, _ = call('jobs')
    assert jobs[0]['id'] == identity and jobs[0]['outputs'] == 2
    assert (library.root / 'state' / 'conversion' / 'batches' / (identity + '.json')).is_file()


@pytest.mark.parametrize('change', ['hash', 'name', 'unknown', 'destination'])
def test_changed_review_or_wrong_confirmation_never_approves_outputs(workspace, change):
    _, source, _, destination, call = workspace
    identity = prepared(workspace)
    review_file = source.parent / 'review.json'
    review, _ = call('preview', identity, '--destination', destination, '--output', review_file)
    expected = review['review_sha256']
    if change == 'hash': expected = '0' * 64
    elif change == 'name': review['preview']['outputs'][0] = 'different.txt'
    elif change == 'unknown': review['execute'] = True
    else:
        destination.rename(destination.with_name('original-output'))
        destination.mkdir()
    review_file.write_text(json.dumps(review), encoding='utf-8')
    result, error = call('approve', '--review', review_file, '--review-sha256', expected, expected=1)
    assert result is None and 'Document batch:' in error
    assert not list(destination.iterdir())
    assert call('show', identity)[0]['review'] is None


def test_preview_export_never_overwrites_file_or_writes_inside_private_library(workspace):
    library, source, _, destination, call = workspace
    identity = prepared(workspace)
    selected = source.parent / 'occupied.json'; selected.write_bytes(b'Keep original file')
    call('preview', identity, '--destination', destination, '--output', selected, expected=1)
    assert selected.read_bytes() == b'Keep original file'
    call('preview', identity, '--destination', destination, '--output', library.root / 'review.json', expected=1)
    assert not (library.root / 'review.json').exists() and not list(destination.iterdir())


def test_cli_refuses_output_collision_and_run_without_approval(workspace):
    _, source, _, destination, call = workspace
    identity = prepared(workspace)
    call('run', identity, expected=1)
    review_file = source.parent / 'review.json'
    review, _ = call('preview', identity, '--destination', destination)
    occupied = destination / review['preview']['outputs'][0]; occupied.write_bytes(b'Another file')
    review, _ = call('preview', identity, '--destination', destination, '--output', review_file)
    assert review['preview']['conflicts'] == [occupied.name]
    call('approve', '--review', review_file, '--review-sha256', review['review_sha256'], expected=1)
    assert occupied.read_bytes() == b'Another file'


def test_real_ctrl_c_after_first_publication_preserves_receipt_and_resumes(workspace, monkeypatch):
    library, _, text, destination, call = workspace
    identity, _, review = approved(workspace)
    original = cli._progress
    raised = []
    previous_handler = signal.getsignal(signal.SIGINT)
    def interrupt(message):
        original(message)
        if str(message).startswith('Verified output 1 ') and not raised:
            raised.append(True)
            signal.raise_signal(signal.SIGINT)
    monkeypatch.setattr(cli, '_progress', interrupt)
    row, _ = call('run', identity, expected=130)
    assert raised and row['status'] == 'paused' and row['completed_outputs'] == 1
    assert signal.getsignal(signal.SIGINT) is previous_handler
    first = destination / review['preview']['outputs'][0]
    stamp = first.stat().st_ino, first.stat().st_mtime_ns
    monkeypatch.setattr(cli, '_progress', original)
    assert call('run', identity)[0]['status'] == 'complete'
    assert (first.stat().st_ino, first.stat().st_mtime_ns) == stamp
    assert b''.join((destination / name).read_bytes() for name in review['preview']['outputs']) == text.encode()


def test_control_commands_report_requests_without_fabricating_worker_acknowledgment(workspace):
    _, _, _, _, call = workspace
    identity = prepared(workspace)
    for command in ('pause', 'cancel'):
        row, _ = call(command, identity)
        assert row['control_requested'] == command and row['status'] == 'prepared'
        assert 'Requested' in row['control_status']


def test_real_backup_restore_requires_new_review_then_exports_without_original(workspace):
    library, source, text, destination, call = workspace
    identity, _, _ = approved(workspace)
    assert call('run', identity)[0]['status'] == 'complete'
    backup = source.parent / 'backup'
    manifest = library.backup(backup)
    shutil.rmtree(library.root)
    restored = source.parent / 'restored'
    restore_backup(backup, restored, expected_manifest_sha256=manifest)
    assert call('show', identity, root=restored)[0]['status'] == 'waiting-input'
    call('run', identity, root=restored, expected=1)
    new_output = source.parent / 'restored-output'; new_output.mkdir()
    review_file = source.parent / 'restored-review.json'
    review, _ = call('preview', identity, '--destination', new_output, '--mode', 'blank-lines',
                    '--output', review_file, root=restored)
    call('approve', '--review', review_file, '--review-sha256', review['review_sha256'], root=restored)
    assert call('run', identity, root=restored)[0]['status'] == 'complete'
    assert b''.join((new_output / name).read_bytes() for name in review['preview']['outputs']) == text.encode()
    assert not source.exists()


def test_reading_copy_capture_keeps_derivative_text_without_interpreting_roles(workspace):
    _, source, _, destination, call = workspace
    document = ReadingDocument('Original retained text.')
    document.set_text('[Scene: this remains text]\nNarrator: also text.')
    project = source.parent / 'reading.json'; document.save(project)
    row, _ = call('capture', '--reading-copies', project)
    identity = row['id']; call('prepare', identity)
    review_file = source.parent / 'copy-review.json'
    review, _ = call('preview', identity, '--destination', destination, '--output', review_file)
    call('approve', '--review', review_file, '--review-sha256', review['review_sha256'])
    call('run', identity)
    assert (destination / review['preview']['outputs'][0]).read_text() == document.text
    assert ReadingDocument.load(project).source_text == 'Original retained text.'


def test_installed_entry_dispatch_and_utf8_protocol_under_legacy_console_encoding(workspace):
    library, source, _, _, _ = workspace
    environment = dict(os.environ, PYTHONIOENCODING='cp1252',
        PYTHONPATH=str(Path(cli.__file__).parents[2]))
    command = [sys.executable, '-m', 'carr_library', 'documents', '--data-dir', str(library.root)]
    capture = subprocess.run([*command, 'capture', str(source)], env=environment,
        capture_output=True, timeout=20, check=False)
    assert capture.returncode == 0, capture.stderr.decode('utf-8', errors='replace')
    identity = json.loads(capture.stdout)['id']
    result = subprocess.run([*command, 'show', identity], env=environment,
        capture_output=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    saved = json.loads(result.stdout)
    assert saved['sources'][0]['name'] == source.name
    assert source.name.encode('utf-8') in result.stdout


def test_cli_requires_existing_library_and_does_not_create_one(tmp_path, capsys):
    missing = tmp_path / 'missing'
    assert cli.main(['--data-dir', str(missing), 'jobs']) == 1
    assert not missing.exists()
    folder = tmp_path / 'ordinary-folder'; folder.mkdir()
    assert cli.main(['--data-dir', str(folder), 'jobs']) == 1
    assert not (folder / 'state').exists() and not (folder / 'library.sqlite').exists()
    capsys.readouterr()
