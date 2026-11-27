"""Actual common backup/restore of the selected keyboard and note producers."""
import ast
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library import backup_sidecars
from carr_library.application import LibraryApp
from carr_library.evidence_drafts import EvidenceDrafts
from carr_library.keyboard_preferences import KeymapStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry


@pytest.fixture
def personal_state(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека čítanie')
    workspace = app.create_workspace('Research recovery')
    topic = app.create_topic(workspace, 'Exact notes')
    app.add_text(workspace, 'Original', 'Unchanged authentic original. Україна 🚂.')
    hit, = app.search(workspace, 'Unchanged')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Committed note')
    row, = app.evidence(workspace, topic)
    registry = CommandRegistry(builtin_commands())
    overrides = {'library.new': 'Ctrl+Alt+N', 'reader.next': 'Ctrl+Alt+N'}
    KeymapStore(app.root / 'keyboard-shortcuts.json').save(overrides, registry)
    draft = EvidenceDrafts(app.root).checkpoint(row, 'Unsaved думка — Slovenská poznámka 🚂')
    return app, row, draft, registry, overrides


def _profile_bytes(root):
    return {name: (root / name).read_bytes() for name in ('keyboard-shortcuts.json', 'evidence-drafts.json')}


class _Text:
    def __init__(self, value=''):
        self.value = value
    def GetValue(self):
        return self.value
    def SetValue(self, value):
        self.value = value
    ChangeValue = SetValue
    def SetFocus(self):
        pass


def _recovery_panel(app, row):
    """Run the real recovery/authorization methods with explicit control doubles."""
    chooser = Mock()
    chooser.ShowModal.return_value = 1
    chooser.GetSelection.return_value = 0
    wx = SimpleNamespace(ID_OK=1, SingleChoiceDialog=Mock(return_value=chooser))
    path = Path(backup_sidecars.__file__).resolve().parent / 'ui/desktop_evidence_panel.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    env = dict(wx=wx, EvidencePanel=object)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), env)
    panel = object.__new__(env['DesktopEvidencePanel'])
    panel.workspace, panel.topic = row.anchor.identity.workspace, row.topic_id
    panel.displayed = row
    panel.note, panel.status = _Text(row.note), _Text()
    panel._selected = lambda: panel.displayed
    panel._dirty = lambda: panel.note.GetValue() != panel.displayed.note
    panel._evidence_epoch = 0
    panel.transaction = lambda: app.evidence_transaction(panel.workspace)
    panel.frame = SimpleNamespace(working=False, _closing=False, evidence=panel,
                                  active=(panel.workspace, panel.topic), cancel_event=Event())
    panel._recovery_store = EvidenceDrafts(app.root)
    panel._recovery_disposed = panel._recovery_loading = False
    panel._recovery_seen = set()
    panel._recovery_owned = panel._recovery_pending = None
    panel._recovery_error = ''
    panel._review_recovery_copy = Mock()
    return panel, chooser


def test_common_backup_preserves_profile_bytes_hashes_and_authorized_recovery(personal_state, tmp_path):
    app, row, draft, registry, overrides = personal_state
    before = _profile_bytes(app.root)
    folder = tmp_path / 'Повна копія'
    checksum = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    expected = [dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
                for name, raw in sorted(before.items())]
    assert manifest['version'] == 4 and manifest['sidecars'] == expected
    assert _profile_bytes(folder) == before
    destination = restore_backup(folder, tmp_path / 'Відновлена', checksum)
    assert _profile_bytes(destination) == before and _profile_bytes(folder) == before
    restored = LibraryApp(destination)
    restored_row, = restored.evidence(row.anchor.identity.workspace, row.topic_id)
    assert restored_row == row
    loaded = KeymapStore(destination / 'keyboard-shortcuts.json').load()
    assert registry.validate(loaded) == overrides
    assert EvidenceDrafts(destination).get(*draft.key) == draft
    panel, chooser = _recovery_panel(restored, restored_row)
    panel.recover_draft()
    assert panel.note.GetValue() == draft.text and panel._recovery_owned == draft
    assert chooser.ShowModal.call_count == 1
    assert restored.evidence(panel.workspace, panel.topic)[0].note == 'Committed note'
    assert _profile_bytes(destination) == before


@pytest.mark.parametrize('revoked', ['workspace', 'topic', 'visible_scope'])
def test_restored_draft_never_bypasses_fresh_scope_authorization(personal_state, tmp_path, revoked):
    app, row, draft, _, _ = personal_state
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    root = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(root)
    panel, chooser = _recovery_panel(restored, row)
    if revoked == 'workspace':
        restored.archive_workspace(panel.workspace)
    elif revoked == 'topic':
        with restored.topic_transaction(panel.workspace) as tree:
            tree.archive(panel.topic)
    else:
        panel.frame.active = ('another-workspace', 'another-topic')
    panel.recover_draft()
    chooser.ShowModal.assert_not_called()
    panel._review_recovery_copy.assert_not_called()
    assert panel.note.GetValue() == 'Committed note'
    assert EvidenceDrafts(root).get(*draft.key) == draft


def test_conflicting_saved_note_is_preserved_and_recovery_remains_read_only(personal_state, tmp_path):
    app, row, draft, _, _ = personal_state
    with app.evidence_transaction(row.anchor.identity.workspace) as service:
        service.edit_note(row.topic_id, row.evidence_id, 'A later committed note')
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    root = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(root)
    current, = restored.evidence(row.anchor.identity.workspace, row.topic_id)
    panel, _ = _recovery_panel(restored, current)
    panel.recover_draft()
    panel._review_recovery_copy.assert_called_once_with(draft)
    assert panel.note.GetValue() == 'A later committed note'
    assert panel._recovery_owned is None and EvidenceDrafts(root).get(*draft.key) == draft


def test_archived_scope_draft_is_backed_up_without_authorizing_recovery(personal_state, tmp_path):
    app, row, draft, _, _ = personal_state
    app.archive_workspace(row.anchor.identity.workspace)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    root = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(root)
    panel, chooser = _recovery_panel(restored, row)
    panel.recover_draft()
    chooser.ShowModal.assert_not_called()
    assert EvidenceDrafts(root).get(*draft.key) == draft
    restored.restore_workspace(row.anchor.identity.workspace)
    panel.recover_draft()
    assert panel.note.GetValue() == draft.text


@pytest.mark.parametrize('name', ['keyboard-shortcuts.json', 'evidence-drafts.json'])
def test_same_size_sidecar_tamper_rejects_restore_before_publication(personal_state, tmp_path, name):
    app, _, _, _, _ = personal_state
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    path = folder / name
    raw = path.read_bytes()
    changed = raw.replace(b'Ctrl+Alt+N', b'Ctrl+Alt+M') if name.startswith('keyboard') else raw.replace(b'Unsaved', b'Changed')
    assert changed != raw and len(changed) == len(raw)
    path.write_bytes(changed)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='digest'):
        restore_backup(folder, destination, digest)
    assert not destination.exists()


@pytest.mark.parametrize('identity', ['workspace', 'topic', 'evidence'])
def test_foreign_or_missing_evidence_draft_blocks_complete_backup(personal_state, tmp_path, identity):
    app, row, _, _, _ = personal_state
    if identity == 'workspace':
        other = replace(row, anchor=replace(row.anchor, identity=replace(row.anchor.identity, workspace='missing-library')))
    elif identity == 'topic':
        other = replace(row, topic_id='missing-topic')
    else:
        other = replace(row, evidence_id='missing-evidence')
    foreign = EvidenceDrafts(app.root).checkpoint(other, 'Unbound recovery text')
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='exact saved evidence'):
        create_backup(app, destination)
    assert not destination.exists()
    assert EvidenceDrafts(app.root).get(*foreign.key) == foreign


@pytest.mark.parametrize('name', ['keyboard-shortcuts.json', 'evidence-drafts.json'])
def test_invalid_personal_state_is_not_omitted_from_completed_backup(personal_state, tmp_path, name):
    app, _, _, _, _ = personal_state
    (app.root / name).write_bytes(b'{"version":false}')
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists() and (app.root / name).read_bytes() == b'{"version":false}'


def test_keymap_change_under_its_own_lock_is_detected_during_common_snapshot(personal_state, tmp_path, monkeypatch):
    app, _, _, registry, overrides = personal_state
    original = backup_sidecars._write
    changed = dict(overrides, **{'library.new': 'Ctrl+Alt+M'})
    def mutate(stage, name, raw):
        original(stage, name, raw)
        if name == 'keyboard-shortcuts.json':
            KeymapStore(app.root / name).save(changed, registry)
    monkeypatch.setattr(backup_sidecars, '_write', mutate)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='changed during backup'):
        create_backup(app, destination)
    assert not destination.exists()
    assert KeymapStore(app.root / 'keyboard-shortcuts.json').load() == changed
