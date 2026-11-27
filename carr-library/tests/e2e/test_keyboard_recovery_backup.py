"""Real complete-backup recovery of inert keymaps and authorized note drafts."""
from hashlib import sha256
import json

from filelock import FileLock
import pytest

from carr_library.application import LibraryApp
from carr_library.evidence_drafts import EvidenceDrafts, MAX_BYTES as DRAFT_LIMIT
from carr_library.ingest.writer_guard import WriterBusy, exclusive_writer
from carr_library.keyboard_preferences import KeymapStore, MAX_BYTES as KEYMAP_LIMIT
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry


@pytest.fixture
def evidence(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recovery library')
    topic = app.create_topic(workspace, 'Recovery topic')
    app.add_text(workspace, 'Original source', 'Exact authenticated original text.')
    hit, = app.search(workspace, 'Exact')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved baseline')
    row, = app.evidence(workspace, topic)
    return app, row


@pytest.mark.parametrize('included', ('keyboard', 'drafts', 'both'))
def test_complete_backup_preserves_exact_sidecars_and_original_evidence(evidence, tmp_path, included):
    app, row = evidence
    expected = {}
    draft = None
    if included in ('keyboard', 'both'):
        path = app.root / 'keyboard-shortcuts.json'
        KeymapStore(path).save({'reader.prepare_narration': 'Ctrl+Alt+J'},
                               CommandRegistry(builtin_commands()))
        expected[path.name] = path.read_bytes()
    if included in ('drafts', 'both'):
        store = EvidenceDrafts(app.root)
        draft = store.checkpoint(row, 'Unsaved українська note.\r\nSecond line.')
        expected[store.path.name] = store.path.read_bytes()
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['version'] == 3
    assert {record['path'] for record in manifest['sidecars']['files']} == set(expected)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    for name, raw in expected.items():
        assert (restored.root / name).read_bytes() == raw
        assert (app.root / name).read_bytes() == raw
    recovered_row, = restored.evidence(row.anchor.identity.workspace, row.topic_id)
    assert recovered_row.note == row.note == 'Saved baseline'
    assert recovered_row.anchors == row.anchors
    if draft is not None:
        copied = EvidenceDrafts(restored.root).get(*draft.key)
        assert copied == draft and copied.matches(recovered_row)
    if included in ('keyboard', 'both'):
        assert KeymapStore(restored.root / 'keyboard-shortcuts.json').load() == {
            'reader.prepare_narration': 'Ctrl+Alt+J'}


def test_backup_preserves_unknown_future_command_as_inert_data(evidence, tmp_path):
    app, _ = evidence
    path = app.root / 'keyboard-shortcuts.json'
    raw = b'{"version":1,"bindings":{"future.command":"Ctrl+Alt+J"}}\n'
    path.write_bytes(raw)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (restored / path.name).read_bytes() == raw
    assert KeymapStore(restored / path.name).load() == {'future.command': 'Ctrl+Alt+J'}
    with pytest.raises(ValueError, match='Unknown command'):
        CommandRegistry(builtin_commands()).validate(KeymapStore(restored / path.name).load())


@pytest.mark.parametrize('filename,valid,maximum', (
    ('keyboard-shortcuts.json', b'{"version":1,"bindings":{}}', KEYMAP_LIMIT),
    ('evidence-drafts.json', b'{"kind":"carr-evidence-drafts","version":1,"drafts":[]}', DRAFT_LIMIT),
))
@pytest.mark.parametrize('damage', ('duplicate', 'schema', 'oversized'))
@pytest.mark.parametrize('stage', ('backup', 'restore'))
def test_raw_sidecar_validation_prevents_publication(evidence, tmp_path, filename, valid, maximum, damage, stage):
    app, _ = evidence
    path = app.root / filename
    path.write_bytes(valid)
    if damage == 'duplicate':
        invalid = valid.replace(b'"version":1', b'"version":1,"version":1')
    elif damage == 'schema':
        invalid = valid.replace(b'"version":1', b'"version":true')
    else:
        # Parsing and reserializing first would erase this over-limit padding.
        invalid = valid + b' ' * (maximum + 1 - len(valid))
    backup = tmp_path / 'backup'
    if stage == 'backup':
        path.write_bytes(invalid)
        with pytest.raises(ValueError):
            create_backup(app, backup)
        assert not backup.exists()
        assert path.read_bytes() == invalid
    else:
        create_backup(app, backup)
        (backup / 'sidecars' / filename).write_bytes(invalid)
        manifest_path = backup / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        record, = manifest['sidecars']['files']
        record.update(size_bytes=len(invalid), sha256=sha256(invalid).hexdigest())
        manifest_path.write_text(json.dumps(manifest))
        checksum = sha256(manifest_path.read_bytes()).hexdigest()
        restored = tmp_path / 'restored'
        with pytest.raises(ValueError):
            restore_backup(backup, restored, checksum)
        assert not restored.exists()
        assert path.read_bytes() == valid


@pytest.mark.parametrize('existing', (False, True))
def test_keymap_publication_lock_blocks_complete_backup(evidence, tmp_path, existing):
    app, _ = evidence
    path = app.root / 'keyboard-shortcuts.json'
    if existing:
        KeymapStore(path).save({}, CommandRegistry(builtin_commands()))
    target = tmp_path / 'backup'
    with FileLock(path.with_name(path.name + '.lock'), timeout=0):
        with pytest.raises(ValueError, match='keyboard settings are busy'):
            create_backup(app, target)
    assert not target.exists()


def test_note_checkpoint_writer_lock_blocks_complete_backup(evidence, tmp_path):
    app, row = evidence
    draft = EvidenceDrafts(app.root).checkpoint(row, 'Uncommitted note')
    target = tmp_path / 'backup'
    with exclusive_writer(app.root):
        with pytest.raises(WriterBusy):
            create_backup(app, target)
    assert not target.exists()
    assert EvidenceDrafts(app.root).get(*draft.key) == draft
