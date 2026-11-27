"""Real database and draft files behind explicit headless native-control doubles."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.anchors import format_citation
from carr_library.evidence_drafts import EvidenceDrafts


class Control:
    def __init__(self, value=''):
        self.value, self.items, self.selection = value, [], 0
        self.focused = False
    def GetValue(self):
        return self.value
    def SetValue(self, value):
        self.value = value
    ChangeValue = SetValue
    def GetSelection(self):
        return self.selection
    def SetSelection(self, value):
        self.selection = value
    def Clear(self):
        self.items.clear()
        self.selection = -1
    def Append(self, value):
        self.items.append(value)
    def Enable(self, value=True):
        pass
    def SetFocus(self):
        self.focused = True


@pytest.fixture
def recovery(tmp_path):
    queued = []
    chooser = Mock()
    chooser.ShowModal.return_value = 1
    chooser.GetSelection.return_value = 0
    wx = SimpleNamespace(Panel=object, YES=1, ID_OK=1, YES_NO=2, NO_DEFAULT=4, ICON_QUESTION=8,
                         EVT_TIMER=object(),
                         MessageBox=Mock(return_value=1), SingleChoiceDialog=Mock(return_value=chooser),
                         CallAfter=queued.append)
    env = dict(wx=wx, replace=replace, format_citation=format_citation)
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui'
    for name in ('evidence_panel.py', 'desktop_evidence_panel.py'):
        tree = ast.parse((source / name).read_text(encoding='utf-8'))
        nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source / name), 'exec'), env)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recovery native workflow')
    topic = app.create_topic(workspace, 'Research')
    app.add_text(workspace, 'Source', 'Exact original passage.')
    hit, = app.search(workspace, 'Exact')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved note')
    panel = object.__new__(env['DesktopEvidencePanel'])
    panel.workspace, panel.topic = workspace, topic
    panel.transaction = lambda: app.evidence_transaction(workspace)
    panel.rows = app.evidence(workspace, topic)
    panel.displayed = panel.rows[0]
    for name in ('items', 'page_choice', 'quote', 'citation', 'note', 'status', 'save_button'):
        setattr(panel, name, Control())
    panel.note.SetValue('Saved note')
    panel._desktop_ready, panel._evidence_epoch = True, 0
    panel.frame = SimpleNamespace(working=False, _closing=False, cancel_event=Event(),
        evidence=panel, active=(workspace, topic), run_job=Mock(), report=Mock(), library=app)
    panel._recovery_store = EvidenceDrafts(app.root)
    panel._recovery_owned = panel._recovery_pending = None
    panel._recovery_seen = set()
    panel._recovery_loading = panel._recovery_disposed = panel._recovery_discarded = False
    panel._recovery_error = ''
    panel._recovery_timer = SimpleNamespace(IsRunning=lambda: False, StartOnce=Mock(), Stop=Mock())
    panel.Unbind = Mock(return_value=True)
    panel._review_recovery_copy = Mock()
    return panel, app, chooser, wx, queued


def checkpoint(panel, text):
    panel.note.SetValue(text)
    panel._recovery_edited(SimpleNamespace(Skip=Mock()))
    panel._flush_recovery()
    return panel._recovery_owned


def execute(panel, *, before_completion=None):
    _, operation, complete, failed = panel.frame.run_job.call_args.args
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            result = pool.submit(operation).result()
        except Exception as error:
            failed(error)
            return False
    if before_completion:
        before_completion()
    complete(result)
    return True


def test_explicit_recover_restores_editor_only_after_authorization(recovery):
    panel, app, _, _, _ = recovery
    draft = panel._recovery_store.checkpoint(panel.displayed, 'Відновлена думка 🚂')
    panel.recover_draft()
    assert panel.note.GetValue() == draft.text and panel._dirty()
    assert panel.note.focused and 'recovered' in panel.status.GetValue()
    assert app.evidence(panel.workspace, panel.topic)[0].note == 'Saved note'
    assert panel._recovery_owned == draft


@pytest.mark.parametrize('choice', [1, 2])
def test_discard_and_keep_choices_do_not_replace_saved_note(recovery, choice):
    panel, app, chooser, _, _ = recovery
    draft = panel._recovery_store.checkpoint(panel.displayed, 'Recovery copy')
    chooser.GetSelection.return_value = choice
    panel.recover_draft()
    assert panel.note.GetValue() == 'Saved note'
    assert app.evidence(panel.workspace, panel.topic)[0].note == 'Saved note'
    assert panel._recovery_store.get(*draft.key) == (None if choice == 1 else draft)


def test_revocation_during_native_confirmation_never_reveals_draft(recovery):
    panel, app, chooser, _, _ = recovery
    draft = panel._recovery_store.checkpoint(panel.displayed, 'Private recovery note')
    chooser.ShowModal.side_effect = lambda: (app.archive_workspace(panel.workspace) or 1)
    panel.recover_draft()
    assert panel.note.GetValue() == 'Saved note'
    assert 'unavailable' in panel.status.GetValue()
    assert panel._recovery_store.get(*draft.key) == draft
    panel._review_recovery_copy.assert_not_called()


def test_changed_note_baseline_can_only_be_reviewed_without_replacing_editor(recovery):
    panel, app, _, _, _ = recovery
    draft = panel._recovery_store.checkpoint(panel.displayed, 'Earlier draft')
    with app.evidence_transaction(panel.workspace) as service:
        service.edit_note(panel.topic, panel.displayed.evidence_id, 'Another editor saved this')
    panel.rows = app.evidence(panel.workspace, panel.topic)
    panel.displayed = panel.rows[0]
    panel.note.SetValue(panel.displayed.note)
    panel.recover_draft()
    panel._review_recovery_copy.assert_called_once_with(draft)
    assert panel.note.GetValue() == 'Another editor saved this'
    assert panel._recovery_owned is None
    assert panel._recovery_store.get(*draft.key) == draft


def test_stale_confirmation_never_overwrites_newer_recovery_revision(recovery):
    panel, _, chooser, _, _ = recovery
    first = panel._recovery_store.checkpoint(panel.displayed, 'Earlier draft')
    saved = []
    def newer():
        saved.append(panel._recovery_store.checkpoint(panel.displayed, 'Newer draft', expected=first.revision))
        return 1
    chooser.ShowModal.side_effect = newer
    panel.recover_draft()
    assert panel.note.GetValue() == 'Saved note'
    assert panel._recovery_store.get(*first.key) == saved[0]
    assert 'Nothing was replaced' in panel.status.GetValue()


def test_successful_save_removes_only_saved_checkpoint(recovery):
    panel, app, _, _, _ = recovery
    draft = checkpoint(panel, 'Ready to commit')
    panel.save_note()
    assert execute(panel)
    assert app.evidence(panel.workspace, panel.topic)[0].note == 'Ready to commit'
    assert panel._recovery_store.get(*draft.key) is None
    assert not panel._dirty()


def test_newer_edit_during_save_is_checkpointed_against_new_saved_baseline(recovery):
    panel, app, _, _, _ = recovery
    first = checkpoint(panel, 'Saved version')
    panel.save_note()
    assert execute(panel, before_completion=lambda: checkpoint(panel, 'Newer unsaved version'))
    current = app.evidence(panel.workspace, panel.topic)[0]
    draft = panel._recovery_store.get(*first.key)
    assert current.note == 'Saved version'
    assert draft.text == panel.note.GetValue() == 'Newer unsaved version'
    assert draft.matches(current) and draft.revision != first.revision


def test_failed_note_commit_preserves_exact_recovery_revision(recovery):
    panel, app, _, _, _ = recovery
    draft = checkpoint(panel, 'Do not lose this')
    original = panel.transaction
    @contextmanager
    def rejected():
        with original() as service:
            yield service
            raise RuntimeError('Synthetic commit failure')
    panel.transaction = rejected
    panel.save_note()
    assert not execute(panel)
    assert panel._recovery_store.get(*draft.key) == draft
    assert panel.note.GetValue() == draft.text
    assert app.evidence(panel.workspace, panel.topic)[0].note == 'Saved note'


def test_panel_destruction_flushes_cached_pending_text_without_reading_controls(recovery):
    panel, _, _, _, _ = recovery
    first = checkpoint(panel, 'First edit')
    panel.note.SetValue('Last edit before destruction')
    panel._recovery_edited(SimpleNamespace(Skip=Mock()))
    assert panel._recovery_store.get(*first.key).text == 'First edit'
    panel.note.GetValue = Mock(side_effect=RuntimeError('Native control already destroyed'))
    panel._recovery_destroy(SimpleNamespace(GetEventObject=lambda: panel, Skip=Mock()))
    assert panel._recovery_store.get(*first.key).text == 'Last edit before destruction'
    assert panel._recovery_disposed


def test_explicit_discard_does_not_recreate_draft_during_destruction(recovery):
    panel, _, _, _, _ = recovery
    draft = checkpoint(panel, 'Intentionally discarded')
    assert panel.discard_recovery()
    panel._recovery_destroy(SimpleNamespace(GetEventObject=lambda: panel, Skip=Mock()))
    assert panel._recovery_store.get(*draft.key) is None


def test_destroy_unbinds_exact_timer_and_queued_callback_cannot_retry_failed_checkpoint(recovery):
    from carr_library.ingest.writer_guard import exclusive_writer
    panel, app, _, wx, _ = recovery
    first = checkpoint(panel, 'Existing durable recovery copy')
    before = panel._recovery_store.path.read_bytes()
    panel.note.SetValue('Pending edit before actual writer contention')
    panel._recovery_edited(SimpleNamespace(Skip=Mock()))
    queued = panel._flush_recovery
    panel._recovery_timer.StartOnce.reset_mock()
    panel.note.GetValue = Mock(side_effect=RuntimeError('Native editor already destroyed'))
    panel.status.SetValue = Mock(side_effect=RuntimeError('Native status already destroyed'))
    event = SimpleNamespace(GetEventObject=lambda: panel, Skip=Mock())
    with exclusive_writer(app.root):
        panel._recovery_destroy(event)
    assert panel._recovery_disposed and 'could not be saved' in panel._recovery_error
    panel.Unbind.assert_called_once_with(wx.EVT_TIMER, handler=queued, source=panel._recovery_timer)
    panel._recovery_timer.Stop.assert_called_once_with()
    assert queued() is True
    panel._recovery_destroy(event)  # Duplicate native disposal cannot retry.
    assert panel._recovery_store.path.read_bytes() == before
    assert panel._recovery_store.get(*first.key) == first
    panel._recovery_timer.StartOnce.assert_not_called()
    panel.note.GetValue.assert_not_called()
    panel.status.SetValue.assert_not_called()
    panel._recovery_timer.Stop.assert_called_once_with()


def test_child_destruction_preserves_live_recovery_timer_and_real_checkpoint(recovery):
    panel, _, _, _, _ = recovery
    first = checkpoint(panel, 'First durable edit')
    panel.note.SetValue('Later edit on a still-live panel')
    panel._recovery_edited(SimpleNamespace(Skip=Mock()))
    event = SimpleNamespace(GetEventObject=lambda: object(), Skip=Mock())
    panel._recovery_destroy(event)
    assert not panel._recovery_disposed
    panel.Unbind.assert_not_called()
    panel._recovery_timer.Stop.assert_not_called()
    assert panel._flush_recovery()
    assert panel._recovery_store.get(*first.key).text == 'Later edit on a still-live panel'
    event.Skip.assert_called_once_with()


def test_corrupt_recovery_storage_reports_error_and_keeps_editor(recovery):
    panel, _, _, _, _ = recovery
    panel._recovery_store.path.write_bytes(b'broken existing file')
    checkpoint(panel, 'Text remains editable')
    assert panel.note.GetValue() == 'Text remains editable'
    assert 'could not be saved' in panel.status.GetValue()
    assert panel._recovery_store.path.read_bytes() == b'broken existing file'


def test_declined_or_failed_discard_keeps_scope_open(recovery):
    panel, _, _, wx, _ = recovery
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/workspace_shell.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_confirm')
    env = dict(wx=wx)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), env)
    frame = SimpleNamespace(evidence=panel, _dirty=lambda: True)
    draft = checkpoint(panel, 'Keep unless explicitly discarded')
    wx.MessageBox.return_value = 0
    assert not env['_confirm'](frame)
    assert panel._recovery_store.get(*draft.key) == draft
    wx.MessageBox.return_value = 1
    panel._recovery_store.discard = Mock(side_effect=OSError('Disk unavailable'))
    assert not env['_confirm'](frame)
    assert panel.note.GetValue() == 'Keep unless explicitly discarded'
