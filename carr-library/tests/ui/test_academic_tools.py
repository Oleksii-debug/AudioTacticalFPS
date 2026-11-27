"""Real service/export boundaries with headless desktop action protocols."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.academic_tools import AcademicActions, AcademicScope
from carr_library.ui.literature_matrix import MatrixCommand


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Research')
    other = app.create_workspace('Another library')
    topic = app.create_topic(workspace, 'Chapter')
    child = app.create_topic(workspace, 'Subsection', parent=topic)
    app.add_text(workspace, 'Source', 'Exact original quotation.\r\nAnother sentence.', authors=('An Author',))
    hit, = app.search(workspace, 'Exact original quotation.')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, 'Earlier saved note')
    child_evidence = app.save_hit(workspace, child, hit.ref, hit.revision, 'Child note')
    app.set_evidence_tags(workspace, topic, evidence, ('Theme',))
    return app, workspace, other, topic, child, evidence, child_evidence


@pytest.fixture(autouse=True)
def export_confirmation(monkeypatch):
    from carr_library.ui import academic_tools
    from carr_library.ui.export_destination import confirm_export_destination

    native = SimpleNamespace(YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4, YES=8, NO=16)
    native.MessageBox = Mock(return_value=native.YES)
    monkeypatch.setattr(academic_tools, 'confirm_export_destination',
        lambda *args, **kwargs: confirm_export_destination(*args, **kwargs, wx_module=native))
    return native


class Frame:
    def __init__(self, app, workspace, topic):
        self.library = app
        self.active = workspace, topic
        self.working = self.busy = self._closing = False
        self.cancel_event = threading.Event()
        self.jobs = []
        self.report = Mock()

    def run_job(self, title, operation, done=None, failed=None):
        self.cancel_event.clear()
        self.jobs.append((title, operation, done, failed))

    def complete(self, index=0):
        _, operation, done, _ = self.jobs[index]
        result = operation()
        if done is not None:
            done(result)
        return result


def make_actions(research, **dialog_options):
    app, workspace, _, topic, *_ = research
    frame = Frame(app, workspace, topic)
    dialogs = SimpleNamespace(matrix=Mock(return_value=None),
        outline_options=Mock(return_value=(True, None, 'markdown')),
        save_path=Mock(return_value=None))
    for key, value in dialog_options.items():
        getattr(dialogs, key).return_value = value
    return frame, AcademicActions(frame, dialogs), dialogs


def test_comparison_uses_fresh_service_and_preserves_both_topic_identities(research):
    frame, actions, dialogs = make_actions(research)
    actions.compare_sources(include_descendants=False, tag_filter='Theme')
    snapshot = frame.complete()
    assert len(snapshot.sources) == 1
    assert {row.topic_id for row in snapshot.sources[0].evidence} == {research[3], research[4]}
    dialogs.matrix.assert_called_once_with(frame, snapshot, include_descendants=False, tag_filter='Theme')


def test_queued_comparison_cannot_show_results_in_another_library(research):
    frame, actions, dialogs = make_actions(research)
    actions.compare_sources()
    frame.active = research[2], None
    frame.complete()
    dialogs.matrix.assert_not_called()


def test_closed_frame_does_not_receive_comparison_result(research):
    frame, actions, dialogs = make_actions(research)
    actions.compare_sources()
    frame._closing = True
    frame.complete()
    dialogs.matrix.assert_not_called()


def test_matrix_export_reloads_notes_after_display_instead_of_exporting_cache(research, tmp_path):
    output = tmp_path / 'review.csv'
    frame, actions, dialogs = make_actions(research,
        matrix=MatrixCommand('matrix_csv'), save_path=output)
    actions.compare_sources()
    frame.complete()
    assert len(frame.jobs) == 2
    app, workspace, _, topic, _, evidence, _ = research
    with app.evidence_transaction(workspace) as store:
        store.edit_note(topic, evidence, 'Current note after the displayed snapshot')
    frame.complete(1)
    text = output.read_text(encoding='utf-8')
    assert 'Current note after the displayed snapshot' in text
    assert 'Earlier saved note' not in text
    assert 'Exact original quotation.' in text


def test_matrix_export_rechecks_retained_original_before_writing(research, tmp_path):
    output = tmp_path / 'review.md'
    frame, actions, _ = make_actions(research,
        matrix=MatrixCommand('matrix_markdown'), save_path=output)
    actions.compare_sources()
    snapshot = frame.complete()
    source = snapshot.sources[0]
    original = frame.library.assets.resolve(source.asset_sha256)
    original.write_bytes(b'Changed original')
    with pytest.raises((ValueError, OSError)):
        frame.complete(1)
    assert not output.exists()


def test_outline_exports_subtopics_without_adding_another_storage_schema(research, tmp_path):
    output = tmp_path / 'outline.md'
    frame, actions, _ = make_actions(research, save_path=output)
    with frame.library.read(research[1]) as db:
        before = tuple(tuple(row) for row in db.execute('SELECT * FROM sqlite_master ORDER BY name'))
    actions.outline()
    frame.complete()
    text = output.read_text(encoding='utf-8')
    assert 'Chapter' in text and 'Subsection' in text
    assert 'Earlier saved note' in text and 'Child note' in text
    with frame.library.read(research[1]) as db:
        assert tuple(tuple(row) for row in db.execute('SELECT * FROM sqlite_master ORDER BY name')) == before


def test_export_cannot_replace_private_database(research):
    frame, actions, _ = make_actions(research, save_path=research[0].database)
    before = research[0].database.read_bytes()
    actions.outline()
    assert not frame.jobs
    assert 'private library' in frame.report.call_args.args[0]
    assert research[0].database.read_bytes() == before
    assert research[0].search(research[1], 'Exact original quotation.')


def test_cancel_before_export_keeps_existing_file(research, tmp_path):
    output = tmp_path / 'outline.md'
    output.write_text('Previous export', encoding='utf-8')
    frame, actions, _ = make_actions(research, save_path=output)
    actions.outline()
    frame.cancel_event.set()
    with pytest.raises((InterruptedError, TimeoutError)):
        frame.complete()
    assert output.read_text(encoding='utf-8') == 'Previous export'


def test_scope_change_in_save_dialog_writes_nothing(research, tmp_path):
    frame, actions, dialogs = make_actions(research)
    output = tmp_path / 'outline.md'
    def choose(*args, **kwargs):
        frame.active = research[2], None
        return output
    dialogs.save_path.side_effect = choose
    actions.outline()
    assert frame.jobs == []
    assert not output.exists()
    assert 'changed' in frame.report.call_args.args[0]


def test_scope_change_after_queuing_export_writes_nothing(research, tmp_path):
    output = tmp_path / 'outline.md'
    frame, actions, _ = make_actions(research, save_path=output)
    actions.outline()
    frame.active = research[2], None
    with pytest.raises(ValueError, match='changed'):
        frame.complete()
    assert not output.exists()


@pytest.mark.parametrize('busy', ['working', 'busy', '_closing'])
def test_comparison_does_not_start_during_another_operation(research, busy):
    frame, actions, dialogs = make_actions(research)
    setattr(frame, busy, True)
    actions.compare_sources()
    assert frame.jobs == []
    dialogs.matrix.assert_not_called()


def test_cancelled_outline_options_writes_nothing(research):
    frame, actions, dialogs = make_actions(research, outline_options=None)
    actions.outline()
    assert frame.jobs == []
    dialogs.save_path.assert_not_called()


def test_note_draft_survives_matrix_drilldown(research):
    frame, actions, _ = make_actions(research)
    note = Mock()
    note.GetValue.return_value = 'Unsaved draft'
    panel = SimpleNamespace(workspace=research[1], topic=research[3],
                            note=note, refresh=Mock(return_value=False))
    frame.evidence = panel
    frame.book = Mock()
    frame.book.GetPageCount.return_value = 4
    frame.book.GetPage.side_effect = [object(), object(), object(), panel]
    selected = SimpleNamespace(topic_id=research[3], evidence_id=research[5])
    actions.open_evidence(AcademicScope(frame.library, *frame.active), selected)
    panel.refresh.assert_called_once_with(evidence_id=research[5], focus=True)
    note.SetValue.assert_not_called()
    note.SetFocus.assert_called_once()
    frame.book.SetSelection.assert_called_once_with(3)


def test_declined_scope_change_does_not_replace_the_existing_note_editor(research):
    frame, actions, _ = make_actions(research)
    frame.evidence = Mock()
    frame._open_workspace = Mock()
    selected = SimpleNamespace(topic_id=research[4], evidence_id=research[6])
    actions.open_evidence(AcademicScope(frame.library, *frame.active), selected)
    frame._open_workspace.assert_called_once_with(research[1], research[4])
    frame.evidence.refresh.assert_not_called()


@pytest.mark.parametrize('kind', ['outline', 'matrix'])
@pytest.mark.parametrize('existing', [False, True])
def test_queued_academic_export_preserves_changed_destination(
        research, tmp_path, kind, existing):
    output = tmp_path / 'approved.md'
    if existing:
        output.write_bytes(b'Previously approved export')
    frame, actions, _ = make_actions(research, save_path=output)
    scope = AcademicScope(frame.library, *frame.active)
    actions._export(scope, kind=kind, format='markdown',
                    include_descendants=True, tag_filter=None)
    assert len(frame.jobs) == 1
    independent = b'Independent work saved while the export was queued'
    output.write_bytes(independent)
    with pytest.raises(ValueError, match='destination changed'):
        frame.complete()
    assert output.read_bytes() == independent
    assert not list(tmp_path.glob('.research-export-*'))


def test_declined_academic_replacement_never_starts_render(
        research, tmp_path, export_confirmation):
    output = tmp_path / 'approved.md'
    output.write_bytes(b'Keep existing work')
    export_confirmation.MessageBox.return_value = export_confirmation.NO
    frame, actions, _ = make_actions(research, save_path=output)
    actions.outline()
    assert not frame.jobs
    assert output.read_bytes() == b'Keep existing work'
    assert 'cancelled' in frame.report.call_args.args[0].lower()
