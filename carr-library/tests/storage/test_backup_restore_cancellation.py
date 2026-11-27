"""Cancellation remains truthful before and during durable restore publication."""
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.reader.reading_state import ReadingState
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume


def populated(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Library')
    app.add_text(workspace, 'Source', 'Exact original source.')
    store = ReaderDraftStore(app.root, slot='multiblock-cancellation-fixture')
    # A real draft crosses several copy blocks while staying within the
    # document's character budget. Arbitrary protected-state bytes are invalid.
    store.save(ReaderResume(ReadingDocument('я' * (3 * 1024 * 1024 // 2))))
    assert store.path.stat().st_size > 3 * 1024 * 1024
    backup = tmp_path / 'backup'
    return backup, create_backup(app, backup)


def test_immediate_restore_cancel_does_not_touch_destination(tmp_path):
    source, digest = populated(tmp_path)
    target = tmp_path / 'restored'
    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(source, target, digest, cancelled=lambda: True)
    assert not target.exists()


@pytest.mark.parametrize('component', ['library.sqlite', 'reader-draft'])
def test_cancel_after_first_private_copy_chunk_never_publishes(tmp_path, component):
    source, digest = populated(tmp_path)
    if component == 'reader-draft':
        component = next((source / 'state/speech').glob('*.reader.json')).relative_to(source)
    target = tmp_path / 'restored'
    observed = False
    def cancelled():
        nonlocal observed
        for stage in tmp_path.glob('.carr-library-restore-*'):
            if (stage / component).exists():
                observed = True
                return True
        return False
    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(source, target, digest, cancelled=cancelled)
    assert observed and not target.exists()


def test_cancel_during_original_hashing_does_not_publish(tmp_path, monkeypatch):
    source, digest = populated(tmp_path)
    target = tmp_path / 'restored'
    opened = False
    original = Path.open
    def observe(path, mode='r', *args, **kwargs):
        nonlocal opened
        result = original(path, mode, *args, **kwargs)
        if path.parent == source / 'originals' and mode == 'rb':
            opened = True
        return result
    monkeypatch.setattr(Path, 'open', observe)
    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(source, target, digest, cancelled=lambda: opened)
    assert opened and not target.exists()


def test_cancel_after_destination_creation_keeps_blocking_incomplete_marker(tmp_path):
    source, digest = populated(tmp_path)
    target = tmp_path / 'restored'
    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(source, target, digest, cancelled=target.exists)
    assert (target / INCOMPLETE).exists()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(target)


def test_legacy_archive_copy_can_be_cancelled_without_activation(tmp_path):
    fixtures = Path(__file__).parents[1] / 'fixtures/legacy-backups-5580d4d'
    entry = json.loads((fixtures / 'fixtures.json').read_text())['archives']['v4']
    data = (fixtures / 'v4.zip').read_bytes()
    assert sha256(data).hexdigest() == entry['archive_sha256']
    source = tmp_path / 'legacy'
    source.mkdir()
    with ZipFile(fixtures / 'v4.zip') as incoming:
        incoming.extractall(source)
    target = tmp_path / 'restored'
    observed = False
    def cancelled():
        nonlocal observed
        for stage in tmp_path.glob('.legacy-library-restore-*'):
            if any((stage / 'legacy-backups').rglob('library.sqlite')):
                observed = True
                return True
        return False
    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(source, target, entry['manifest_sha256'], cancelled=cancelled)
    assert observed and not target.exists()


def test_cancel_callback_is_required_to_be_callable(tmp_path):
    with pytest.raises(ValueError, match='callback'):
        restore_backup(tmp_path / 'unused-source', tmp_path / 'unused-target', 'a' * 64, cancelled=True)
