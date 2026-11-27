"""Real CLI invocations and exact request/plan bindings; no remote provider."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json
from carr_library.language_qualification.catalog import canonical_language_tag
from carr_library.library_backup import restore_backup
from carr_library.runtime_settings import load_voice_preferences, save_voice_preferences
from carr_library.studio.book_audio import BookAudioService
from carr_library.studio.book_cli import capture_request, chosen_outputs, read_review, reviewed_document

_spec = importlib.util.spec_from_file_location('book_cli_batch_fixtures', Path(__file__).with_name('test_book_batch.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
batch = _fixtures.batch


def request(e):
    return {'schema': 'carr-book-batch-request/1', 'title': 'Explicit CLI books', 'books': [
        {'edition': source['edition'], 'voice': {'language': language, 'voice_id': voice},
         'output_parent': str(output), 'options': {'mode': 'document'}, 'format': format}
        for source, language, voice, output, format in zip(e.imported, ('en', 'zh'),
            ('synthetic-one', 'synthetic-two'), e.outputs, ('wav', 'flac'))]}


def test_cli_lists_existing_bookmarks_and_request_freezes_explicit_ids(batch):
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    e = batch
    saved = ReadingState(e.app.root)
    bookmark = saved.add_bookmark(ReadingDesk(e.app, e.workspace, e.imported[0]['edition']).page(1).location,
                                  'CLI selected source boundary')
    listed = cli(e.app.root, e.workspace, 'bookmarks', e.imported[0]['edition'])
    assert listed.returncode == 0, listed.stderr
    rows = json.loads(listed.stdout)
    assert len(rows) == 1 and rows[0]['bookmark']['id'] == bookmark.id and rows[0]['current_revision']
    declaration = request(e)
    declaration['books'][0]['options']['mode'] = 'bookmarks'
    declaration['books'][0]['bookmark_ids'] = [bookmark.id]
    selected, plans, outputs = capture_request(e.books, declaration)
    assert plans[0].landmarks['records'][0]['id'] == bookmark.id
    assert plans[1].landmarks_json == ''
    review = e.groups.review(selected, outputs, plans=plans)
    saved.rename_bookmark(e.workspace, e.imported[0]['edition'], bookmark.id, 'Changed exact CLI boundary')
    with pytest.raises(ValueError, match='inventory changed'):
        e.groups.prepare(selected, outputs, plans=plans, approval=review.fingerprint)
    assert not e.production.jobs() and not e.voices.calls


def test_request_freezes_exact_profile_and_explicit_voice_choices(batch, tmp_path):
    e = batch
    preferences = load_voice_preferences(e.app.root / 'state')
    configured = {'favorites': ['synthetic-one'], 'defaults': {'zh': 'synthetic-two'},
        'profiles': {'Quiet reading': {'voice_id': 'synthetic-one', 'language': 'en', 'rate': 2, 'volume': 44}}}
    save_voice_preferences(e.app.root / 'state', configured, expected=preferences)
    declaration = request(e)
    declaration['books'][0]['voice'] = {'language': 'en', 'profile': 'Quiet reading'}
    declaration['books'][1]['voice'] = {'language': 'zh'}
    selected, plans, outputs = capture_request(e.books, declaration)
    assert [(plan.role.voice_id, plan.role.rate, plan.role.volume) for plan in plans] == [
        ('synthetic-one', 2, 44), ('synthetic-two', 0, 100)]
    review = e.groups.review(selected, outputs, plans=plans)
    document = tmp_path / 'review.json'
    atomic_json(document, reviewed_document(selected, plans, outputs, review))
    before = tuple(plan.fingerprint for plan in plans)
    changed = json.loads(json.dumps(configured))
    changed['profiles']['Quiet reading']['volume'] = 12
    save_voice_preferences(e.app.root / 'state', changed, expected=configured)
    restored, captured, parents, approval = read_review(document, e.workspace, None)
    assert restored == selected and parents == outputs and approval == review.fingerprint
    assert tuple(plan.fingerprint for plan in captured) == before
    assert captured[0].role.volume == 44
    assert not e.production.jobs() and not e.voices.calls


@pytest.mark.parametrize('change', ['missing-voice', 'language', 'unknown-control', 'extra-book-field', 'wrong-scope'])
def test_invalid_or_unavailable_request_never_prepares_or_speaks(batch, change):
    e = batch
    declaration = request(e)
    if change == 'missing-voice':
        declaration['books'][0]['voice']['voice_id'] = 'missing-installed-voice'
    elif change == 'language':
        declaration['books'][0]['voice']['language'] = 'en-US'
    elif change == 'unknown-control':
        declaration['books'][0]['voice']['pitch'] = 1
    elif change == 'extra-book-field':
        declaration['books'][0]['execute'] = True
    else:
        other = e.app.create_workspace('Different library')
        declaration['books'][0]['edition'] = e.app.add_text(other, 'Other', 'Not selected.')['edition']
    with pytest.raises(ValueError):
        capture_request(e.books, declaration)
    assert not e.production.jobs() and not e.books.saved() and not e.voices.calls


def test_output_mapping_is_complete_unique_and_follows_saved_order(batch):
    e = batch
    assert chosen_outputs(e.selected, [f'{plan.id}={parent}' for plan, parent in
        reversed(tuple(zip(e.plans, e.outputs)))]) == tuple(map(str, e.outputs))
    with pytest.raises(ValueError):
        chosen_outputs(e.selected, [f'{e.plans[0].id}={e.outputs[0]}'])
    with pytest.raises(ValueError):
        chosen_outputs(e.selected, [f'{e.plans[0].id}={parent}' for parent in e.outputs])


def cli(root, workspace, *arguments):
    import bootstrap
    import carr_library
    selected = dict.fromkeys(str(Path(module.__file__).resolve().parent.parent)
                             for module in (carr_library, bootstrap))
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=os.pathsep.join(selected))
    return subprocess.run([sys.executable, '-B', '-m', 'carr_library', 'studio', 'book',
        '--data-dir', str(root), '--workspace', workspace, *map(str, arguments)],
        env=environment, cwd=root.parent, text=True, encoding='utf-8', capture_output=True, timeout=90)


@pytest.fixture
def actual_cli(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual local CLI')
    sources = tuple(app.add_text(workspace, title, text) for title, text in (
        ('First original', 'A short first book.'), ('Second original', 'A short second book.')))
    books = BookAudioService(app, workspace)
    voices = tuple(books.production.discover_voices().voices())
    assert voices, 'The qualified local runtime must expose an actual installed voice.'
    selected = next((voice for voice in voices if voice.id == 'flite:slt'), voices[0])
    language = canonical_language_tag(selected.language, adapter_underscores=True)
    outputs = (tmp_path / 'first-output', tmp_path / 'second-output')
    for parent in outputs:
        parent.mkdir()
    declaration = {'schema': 'carr-book-batch-request/1', 'title': 'Actual CLI selection', 'books': [
        {'edition': source['edition'], 'voice': {'language': language, 'voice_id': selected.id},
         'output_parent': str(parent), 'format': format}
        for source, parent, format in zip(sources, outputs, ('wav', 'flac'))]}
    request_path, review_path = tmp_path / 'request.json', tmp_path / 'review.json'
    atomic_json(request_path, declaration)
    yield app, workspace, books, sources, outputs, request_path, review_path
    books.close()


def test_actual_cli_preview_prepare_run_status_and_verified_resume(actual_cli):
    app, workspace, books, sources, outputs, request_path, review_path = actual_cli
    preview = cli(app.root, workspace, 'preview', '--request', request_path, '--review', review_path)
    assert preview.returncode == 0, preview.stderr
    reviewed = json.loads(preview.stdout)
    assert reviewed['prepared'] is False and len(reviewed['books']) == 2
    assert not books.production.jobs() and all(not list(parent.iterdir()) for parent in outputs)
    repeated = cli(app.root, workspace, 'preview', '--request', request_path, '--review', review_path)
    assert repeated.returncode == 1 and 'not be overwritten' in repeated.stderr
    wrong = cli(app.root, workspace, 'prepare', '--review', review_path, '--approval', '0' * 64)
    assert wrong.returncode == 1 and not books.production.jobs()
    prepared = cli(app.root, workspace, 'prepare', '--review', review_path, '--approval', reviewed['approval'])
    assert prepared.returncode == 0, prepared.stderr
    ready = json.loads(prepared.stdout)
    assert ready['prepared'] is True and ready['approval'] != reviewed['approval']
    assert len(books.production.jobs()) == 2 and all(not list(parent.iterdir()) for parent in outputs)
    mappings = [argument for row, parent in zip(ready['books'], outputs)
                for argument in ('--output', row['plan'] + '=' + str(parent))]
    refused = cli(app.root, workspace, 'run', ready['batch'], *mappings, '--approval', reviewed['approval'])
    assert refused.returncode == 1 and all(not list(parent.iterdir()) for parent in outputs)
    run = cli(app.root, workspace, 'run', ready['batch'], *mappings, '--approval', ready['approval'])
    assert run.returncode == 0, run.stderr
    complete = json.loads(run.stdout)
    assert complete['status'] == 'complete'
    assert [book['format'] for book in complete['stored']['books']] == ['wav', 'flac']
    artifacts = {artifact['path']: (sha256(Path(artifact['path']).read_bytes()).hexdigest(),
                 Path(artifact['path']).stat().st_mtime_ns)
                 for state in books.production.jobs() for artifact in state['artifacts']}
    again = cli(app.root, workspace, 'run', ready['batch'], *mappings, '--approval', ready['approval'])
    assert again.returncode == 0 and json.loads(again.stdout)['status'] == 'complete', again.stderr
    assert artifacts == {path: (sha256(Path(path).read_bytes()).hexdigest(), Path(path).stat().st_mtime_ns)
                         for path in artifacts}
    status = cli(app.root, workspace, 'status', ready['batch'])
    assert status.returncode == 0 and 'Stored Studio checkpoints' in json.loads(status.stdout)['notice']
    listed = cli(app.root, workspace, 'list')
    assert listed.returncode == 0 and json.loads(listed.stdout)[0]['batch'] == ready['batch']
    stopped = cli(app.root, workspace, 'pause', ready['batch'])
    assert stopped.returncode == 1 and 'not currently running' in stopped.stderr
    assert all(app.assets.resolve(source['asset']).is_file() for source in sources)


def test_actual_cli_rechecks_source_before_preparation(actual_cli):
    app, workspace, books, sources, outputs, request_path, review_path = actual_cli
    preview = cli(app.root, workspace, 'preview', '--request', request_path, '--review', review_path)
    assert preview.returncode == 0, preview.stderr
    reviewed = json.loads(preview.stdout)
    app.update_metadata(workspace, sources[1]['edition'], title='Changed after exact review')
    result = cli(app.root, workspace, 'prepare', '--review', review_path, '--approval', reviewed['approval'])
    assert result.returncode == 1 and not books.production.jobs(), result.stderr
    assert 'revision' in result.stderr or 'source' in result.stderr.lower()
    assert all(not list(parent.iterdir()) for parent in outputs)


def test_actual_book_help_creates_no_library(tmp_path):
    root = tmp_path / 'unused'
    result = cli(root, 'unused', '--help')
    assert result.returncode == 0 and 'preview' in result.stdout and 'pause' in result.stdout
    assert not root.exists()


def test_actual_cli_restore_prepares_new_outputs_without_old_external_review(actual_cli, tmp_path):
    app, workspace, books, sources, outputs, request_path, review_path = actual_cli
    preview = cli(app.root, workspace, 'preview', '--request', request_path, '--review', review_path)
    assert preview.returncode == 0, preview.stderr
    initial = json.loads(preview.stdout)
    prepared = cli(app.root, workspace, 'prepare', '--review', review_path, '--approval', initial['approval'])
    assert prepared.returncode == 0, prepared.stderr
    original = json.loads(prepared.stdout)
    backup = tmp_path / 'backup'
    receipt = app.backup(backup)
    books.close()
    shutil.rmtree(app.root)
    review_path.unlink()
    restored_root = restore_backup(backup, tmp_path / 'restored', receipt)
    status = cli(restored_root, workspace, 'status', original['batch'])
    assert status.returncode == 0, status.stderr
    assert all(book['output_parent'] is None for book in json.loads(status.stdout)['books'])
    parents = (tmp_path / 'restored-one', tmp_path / 'restored-two')
    for parent in parents:
        parent.mkdir()
    mappings = [argument for row, parent in zip(original['books'], parents)
                for argument in ('--output', row['plan'] + '=' + str(parent))]
    reviewed = cli(restored_root, workspace, 'review', original['batch'], *mappings)
    assert reviewed.returncode == 0, reviewed.stderr
    fresh = json.loads(reviewed.stdout)
    assert not fresh['prepared'] and fresh['approval'] != original['approval']
    refused = cli(restored_root, workspace, 'run', original['batch'], *mappings, '--approval', fresh['approval'])
    assert refused.returncode == 1 and all(not list(parent.iterdir()) for parent in parents)
    ready = cli(restored_root, workspace, 'prepare', '--batch', original['batch'],
                *mappings, '--approval', fresh['approval'])
    assert ready.returncode == 0, ready.stderr
    rebound = json.loads(ready.stdout)
    assert rebound['prepared'] and rebound['approval'] != fresh['approval']
    assert all('-restored-' in Path(path).name for row in rebound['books'] for path in row['destinations'])
    assert all(not list(parent.iterdir()) for parent in parents)
    result = cli(restored_root, workspace, 'run', original['batch'], *mappings, '--approval', rebound['approval'])
    assert result.returncode == 0 and json.loads(result.stdout)['status'] == 'complete', result.stderr



def test_actual_cli_duration_review_publish_and_existing_receipt_verification(batch):
    from carr_library.studio.measured_audio import inspect
    e = batch
    _, prepared = _fixtures.prepare(e)
    assert e.groups.run(e.selected.id, e.outputs, approval=prepared.fingerprint)['status'] == 'complete'
    output = e.outputs[0].parent / 'cli-measured-parts.zip'
    plan = e.plans[0]
    args = (plan.id, '--part', '1', '--output', str(output), '--milliseconds', '70')
    result = cli(e.app.root, e.workspace, 'duration-review', *args)
    assert result.returncode == 0, result.stderr
    reviewed = json.loads(result.stdout)
    assert reviewed['content']['pcm']['frames'] == 4800 and not output.exists()
    refused = cli(e.app.root, e.workspace, 'duration-publish', *args, '--approval', '0' * 64)
    assert refused.returncode == 1 and not output.exists()
    result = cli(e.app.root, e.workspace, 'duration-publish', *args,
                 '--approval', reviewed['approval_sha256'])
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['format'] == 'pcm-parts' and receipt['status'] == 'complete'
    assert inspect(output)['content'] == reviewed['content']
    before = (sha256(output.read_bytes()).hexdigest(), output.stat().st_mtime_ns)
    repeated = cli(e.app.root, e.workspace, 'duration-publish', *args,
                   '--approval', reviewed['approval_sha256'])
    assert repeated.returncode == 1
    assert (sha256(output.read_bytes()).hexdigest(), output.stat().st_mtime_ns) == before
