"""Real-library recovery records; these do not establish power-loss/NVDA proof."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import bootstrap
import carr_library
from carr_library.application import LibraryApp
from carr_library.evidence_drafts import (
    DraftConflict, EvidenceDrafts, FILENAME, MAX_NOTE_BYTES, validate_evidence_drafts,
)
from carr_library.ingest.writer_guard import WriterBusy, exclusive_writer


PACKAGE_PATH = os.pathsep.join(str(Path(package.__file__).resolve().parent.parent)
                               for package in (carr_library, bootstrap))


@pytest.fixture
def evidence(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recovery library')
    topic = app.create_topic(workspace, 'Recovery topic')
    app.add_text(workspace, 'Original source', 'Exact authenticated original text.')
    hit, = app.search(workspace, 'Exact')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved baseline')
    row, = app.evidence(workspace, topic)
    return app, row, EvidenceDrafts(app.root)


def test_unicode_and_empty_note_drafts_survive_restart_without_committing(evidence):
    app, row, store = evidence
    draft = store.checkpoint(row, 'Українська думка. Slovenská poznámka.\r\n🚂')
    restarted = EvidenceDrafts(app.root)
    assert restarted.get(*draft.key) == draft
    assert draft.matches(app.evidence(draft.workspace, draft.topic)[0])
    empty = restarted.checkpoint(row, '', expected=draft.revision)
    assert empty.text == '' and empty.revision != draft.revision
    assert app.evidence(row.anchor.identity.workspace, row.topic_id)[0].note == 'Saved baseline'


def test_stale_editor_cannot_replace_or_remove_newer_draft(evidence):
    _, row, store = evidence
    first = store.checkpoint(row, 'First checkpoint')
    second = store.checkpoint(row, 'Newer checkpoint', expected=first.revision)
    with pytest.raises(DraftConflict):
        store.checkpoint(row, 'Stale overwrite', expected=first.revision)
    with pytest.raises(DraftConflict):
        store.checkpoint(row, 'Another window without ownership')
    assert not store.discard(first)
    assert store.get(*second.key) == second
    assert store.discard(second)
    assert store.get(*second.key) is None


def test_saved_note_and_source_changes_are_distinguished_from_recovery(evidence):
    _, row, store = evidence
    draft = store.checkpoint(row, 'Uncommitted note')
    changed_note = replace(row, note='Another editor saved this')
    assert draft.source_matches(changed_note) and not draft.matches(changed_note)
    changed_source = replace(row, anchor=replace(row.anchor, metadata=replace(row.anchor.metadata, title='Changed metadata')))
    assert not draft.source_matches(changed_source) and not draft.matches(changed_source)
    changed_scope = replace(row, topic_id='another-topic')
    assert not draft.matches(changed_scope)


def test_distinct_scopes_merge_without_eviction(evidence):
    app, row, store = evidence
    first = store.checkpoint(row, 'One topic')
    other = replace(row, topic_id='another-topic', evidence_id='other-evidence')
    second = EvidenceDrafts(app.root).checkpoint(other, 'Another topic')
    assert store.get(*first.key) == first and store.get(*second.key) == second
    assert EvidenceDrafts(app.root.parent).get(*first.key) is None


def test_invalid_saved_note_can_still_have_a_bounded_recovery_copy(evidence):
    _, row, store = evidence
    # Existing note validation rejects >100k characters and NUL. A recovery
    # copy preserves such invalid editor input without committing it as a note.
    text = 'x' * 100001 + '\x00'
    draft = store.checkpoint(row, text)
    assert store.get(*draft.key).text == text
    with pytest.raises(ValueError, match='too large'):
        store.checkpoint(row, 'я' * MAX_NOTE_BYTES, expected=draft.revision)
    assert store.get(*draft.key) == draft


def test_disk_failure_preserves_previous_checkpoint_and_cleans_temp(evidence, monkeypatch):
    _, row, store = evidence
    draft = store.checkpoint(row, 'Durable earlier note')
    before = store.path.read_bytes()

    def reject(*args):
        raise OSError('Synthetic disk full')

    monkeypatch.setattr('carr_library.evidence_drafts.os.replace', reject)
    with pytest.raises(OSError, match='disk full'):
        store.checkpoint(row, 'New note', expected=draft.revision)
    assert store.path.read_bytes() == before
    assert not list(store.root.glob('.evidence-drafts-*.tmp'))


def test_directory_fsync_failure_returns_committed_revision_and_warning(evidence, monkeypatch):
    _, row, store = evidence
    actual = os.fsync
    count = 0
    def second_fsync_fails(descriptor):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('Synthetic directory durability failure after replace')
        return actual(descriptor)
    monkeypatch.setattr('carr_library.evidence_drafts.os.fsync', second_fsync_fails)
    committed = store.checkpoint(row, 'Already published')
    assert store.get(*committed.key) == committed
    if os.name == 'posix':
        assert 'published' in store.last_warning and 'durability' in store.last_warning
    else:
        assert count == 1 and store.last_warning == ''
        # Windows does not open/fsync a POSIX directory; the next call is its
        # temporary-file flush and must not receive our directory-only fault.
        monkeypatch.setattr('carr_library.evidence_drafts.os.fsync', actual)
    subsequent = store.checkpoint(row, 'Next edit', expected=committed.revision)
    assert store.get(*subsequent.key) == subsequent
    assert store.last_warning == ''


@pytest.mark.parametrize('raw', [b'', b'not JSON', b'{"version":1,"version":1}',
    b'{"kind":"carr-evidence-drafts","version":true,"drafts":[]}',
    b'{"kind":"carr-evidence-drafts","version":2,"drafts":[]}',
    b'{"kind":"carr-evidence-drafts","version":1,"drafts":[],"secret":1}',
    b'{"kind":"carr-evidence-drafts","version":1,"drafts":[NaN]}'])
def test_invalid_existing_storage_is_not_silently_reset(evidence, raw):
    _, row, store = evidence
    store.path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.checkpoint(row, 'Do not replace malformed recovery data')
    assert store.path.read_bytes() == raw


def test_duplicate_scoped_draft_is_rejected(evidence):
    _, row, store = evidence
    store.checkpoint(row, 'Draft')
    value = json.loads(store.path.read_bytes())
    value['drafts'].append(value['drafts'][0])
    with pytest.raises(ValueError, match='invalid'):
        validate_evidence_drafts(json.dumps(value).encode())


def test_existing_directory_and_hardlink_are_not_followed(evidence, tmp_path):
    _, row, store = evidence
    store.path.mkdir()
    with pytest.raises(ValueError):
        store.checkpoint(row, 'No write into directory')
    store.path.rmdir()
    original = tmp_path / 'unrelated.txt'
    original.write_text('Keep original', encoding='utf-8')
    os.link(original, store.path)
    with pytest.raises(ValueError):
        store.checkpoint(row, 'No linked write')
    assert original.read_text(encoding='utf-8') == 'Keep original'


def test_busy_library_writer_does_not_destroy_existing_draft(evidence):
    app, row, store = evidence
    draft = store.checkpoint(row, 'Safe checkpoint')
    with exclusive_writer(app.root):
        with pytest.raises(WriterBusy):
            store.checkpoint(row, 'Busy write', expected=draft.revision)
    assert store.get(*draft.key) == draft


def test_limits_reject_new_entries_without_evicting_old_notes(evidence, monkeypatch):
    _, row, store = evidence
    draft = store.checkpoint(row, 'Keep this')
    monkeypatch.setattr('carr_library.evidence_drafts.MAX_DRAFTS', 1)
    with pytest.raises(ValueError, match='limit'):
        store.checkpoint(replace(row, evidence_id='another-evidence'), 'No silent eviction')
    assert store.get(*draft.key) == draft


def test_checkpoint_survives_process_exit_without_destructors(evidence):
    app, row, store = evidence
    program = '''import os, sys
from carr_library.application import LibraryApp
from carr_library.evidence_drafts import EvidenceDrafts
app=LibraryApp(sys.argv[1])
row,=app.evidence(sys.argv[2],sys.argv[3])
EvidenceDrafts(app.root).checkpoint(row,'Process stopped after durable checkpoint')
os._exit(19)
'''
    environment = dict(os.environ)
    environment['PYTHONPATH'] = PACKAGE_PATH
    result = subprocess.run([sys.executable, '-B', '-P', '-s', '-c', program, str(app.root),
                             row.anchor.identity.workspace, row.topic_id], env=environment,
                            capture_output=True, timeout=20)
    assert result.returncode == 19, result.stderr.decode()
    assert EvidenceDrafts(app.root).get(row.anchor.identity.workspace, row.topic_id, row.evidence_id).text == (
        'Process stopped after durable checkpoint')
    assert app.evidence(row.anchor.identity.workspace, row.topic_id)[0].note == 'Saved baseline'
