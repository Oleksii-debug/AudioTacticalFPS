"""Current-service book CLI contracts, including an isolated real process."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.audio import book_cli as cli
from carr_library.audio.book_audio import BookAudioService
from carr_library.audio.book_batch import BookBatchService
from carr_library.audio.book_plan import SplitOptions
from carr_library.audio.models import Role
from tests.audio.test_book_plans import library


def request(library, tmp_path, order=(2, 3)):
    value = {'schema': 'carr-book-capture-request/1', 'title': 'Explicit ordered review ü',
        'books': [{'edition': library[index]['edition'], 'options': asdict(SplitOptions()),
                   'role': asdict(Role('Narrator', voice='fixture:uk', language='uk')),
                   'sample_rate': 8000, 'format': 'wav'} for index in order]}
    path = tmp_path / 'selected-books.json'
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def invoke(library, capsys, *args):
    code = cli.main(['--data-dir', str(library[0].root), '--workspace', library[1], *map(str, args)])
    output = capsys.readouterr()
    return code, json.loads(output.out) if output.out else None, output.err


def test_capture_review_prepare_and_status_have_no_runtime_or_output_authority(library, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(BookAudioService, '_engine',
                        lambda *args: pytest.fail('Capture/review/prepare never discovers a voice runtime'))
    path = request(library, tmp_path, order=(3, 2))
    code, captured, errors = invoke(library, capsys, 'capture', '--selection', path)
    assert code == 0 and captured['status'] == 'captured' and captured['executed'] is False
    assert [row['edition'] for row in captured['books']] == [library[3]['edition'], library[2]['edition']]
    assert all(row['id'] in errors for row in captured['books'])
    assert not (library[0].root / 'audio-projects').exists()
    output = tmp_path / 'explicit-output'
    output.mkdir()
    flags = ('--output-parent', output, '--output-parent', output)
    code, review, _ = invoke(library, capsys, 'review', captured['batch'], *flags)
    assert code == 0 and not review['prepared']
    assert all(Path(name).parent == output for row in review['books'] for name in row['destinations'])
    code, ready, _ = invoke(library, capsys, 'prepare', captured['batch'], *flags,
                           '--reviewed-sha256', review['approval_sha256'])
    assert code == 0 and ready['prepared'] and ready['executed'] is False
    assert ready['approval_sha256'] != review['approval_sha256']
    assert list(output.iterdir()) == []
    assert len(list((library[0].root / 'audio-projects').glob('*/studio/project.json'))) == 2
    code, status, _ = invoke(library, capsys, 'status', captured['batch'])
    assert code == 0 and status['verified_outputs'] is False
    assert [row['plan'] for row in status['books']] == [row['id'] for row in captured['books']]
    code, saved, _ = invoke(library, capsys, 'list', '--batches')
    assert code == 0 and saved['batches'][0]['id'] == captured['batch']


def test_exact_review_pin_blocks_queued_destination_or_order_changes(library, tmp_path, capsys):
    _, captured, _ = invoke(library, capsys, 'capture', '--selection', request(library, tmp_path))
    output, changed = tmp_path / 'reviewed-output', tmp_path / 'different-output'
    output.mkdir()
    changed.mkdir()
    _, review, _ = invoke(library, capsys, 'review', captured['batch'],
                         '--output-parent', output, '--output-parent', output)
    code, result, error = invoke(library, capsys, 'prepare', captured['batch'],
        '--output-parent', output, '--output-parent', changed,
        '--reviewed-sha256', review['approval_sha256'])
    assert code == 1 and result is None and 'Review' in error
    assert not (library[0].root / 'audio-projects').exists()


def test_capture_acknowledges_saved_reviews_before_a_later_batch_failure(library, tmp_path, capsys, monkeypatch):
    from carr_library.audio.book_store import BookStore
    original = BookStore.save
    def save(self, value, **kwargs):
        if value.__class__.__name__ == 'BookBatch':
            raise OSError('injected failure before immutable batch publication')
        return original(self, value, **kwargs)
    monkeypatch.setattr(BookStore, 'save', save)
    code, result, error = invoke(library, capsys, 'capture', '--selection', request(library, tmp_path))
    assert code == 1 and result is None
    books = BookAudioService(library[0], library[1]).saved()
    assert len(books) == 2
    assert all(plan.id in error for plan in books)
    assert 'saved_plans' in error and '"saved_batch": null' in error
    assert not (library[0].root / 'audio-projects').exists()


@pytest.mark.parametrize('arguments', [
    ['prepare', 'a' * 32, '--output-parent', '.', '--reviewed-sha256', 'bad'],
    ['duration-review', 'a' * 32, '--part', '1', '--maximum-ms', '0', '--output', 'a.zip'],
    ['duration-publish', 'a' * 32, '--part', '0', '--maximum-ms', '60000', '--output', 'a.zip',
     '--reviewed-sha256', '0' * 64],
    ['batch', '--plan', 'not-an-identity'],
], ids=['review-pin', 'duration-bound', 'part-bound', 'plan-id'])
def test_invalid_flags_never_create_a_new_library(tmp_path, arguments):
    root = tmp_path / 'does-not-exist'
    with pytest.raises(SystemExit) as stopped:
        cli.main(['--data-dir', str(root), '--workspace', 'explicit', *arguments])
    assert stopped.value.code == 2 and not root.exists()


def test_pure_request_bounds_before_expanding_repeated_objects():
    row = {'edition': 'source', 'options': {}, 'role': {}}
    with pytest.raises(ValueError, match='1 to 32'):
        cli._selection({'schema': 'carr-book-capture-request/1', 'title': 'Oversized', 'books': [row] * 33})
    with pytest.raises(ValueError, match='requires edition|requires|Each'):
        cli._selection({'schema': 'carr-book-capture-request/1', 'title': 'No executable authority',
                        'books': [dict(row, runtime='/unapproved/program')]})


def child_arguments(library, *args):
    import carr_library
    import bootstrap
    package = str(Path(carr_library.__file__).resolve().parent.parent)
    boot = str(Path(bootstrap.__file__).resolve().parent.parent)
    script = (
        'import pathlib,sys;sys.path[:0]=sys.argv[1:3];'
        'import carr_library,bootstrap;'
        'assert pathlib.Path(carr_library.__file__).resolve().parent.parent==pathlib.Path(sys.argv[1]);'
        'assert pathlib.Path(bootstrap.__file__).resolve().parent.parent==pathlib.Path(sys.argv[2]);'
        'from carr_library.audio.book_cli import main;'
        'raise SystemExit(main(sys.argv[3:]))'
    )
    return [sys.executable, '-I', '-c', script, package, boot, '--data-dir', str(library[0].root),
            '--workspace', library[1], *map(str, args)]


def test_isolated_process_capture_and_reopen_exact_order(library, tmp_path):
    selected = request(library, tmp_path, order=(3, 2))
    captured = subprocess.run(child_arguments(library, 'capture', '--selection', selected),
        cwd=tmp_path, capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert captured.returncode == 0, captured.stderr[-4000:]
    result = json.loads(captured.stdout)
    assert result['executed'] is False
    assert [row['edition'] for row in result['books']] == [library[3]['edition'], library[2]['edition']]
    reopened = subprocess.run(child_arguments(library, 'status', result['batch']),
        cwd=tmp_path, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert reopened.returncode == 0, reopened.stderr[-4000:]
    assert [row['plan'] for row in json.loads(reopened.stdout)['books']] == [
        row['id'] for row in result['books']]
    assert not (library[0].root / 'audio-projects').exists()
