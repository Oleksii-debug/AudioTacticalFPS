"""Real service/export boundaries with headless desktop action protocols."""
import threading
import sys
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
    native = SimpleNamespace(YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4, ID_YES=8, ID_NO=16)
    native.dialog = Mock()
    native.dialog.ShowModal.return_value = native.ID_YES
    native.MessageDialog = Mock(return_value=native.dialog)
    monkeypatch.setitem(sys.modules, 'wx', native)
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


def test_late_cancel_does_not_open_an_already_completed_comparison(research):
    frame, actions, dialogs = make_actions(research)
    actions.compare_sources()
    _, operation, done, _ = frame.jobs[0]
    snapshot = operation()
    frame.cancel_event.set()
    done(snapshot)
    dialogs.matrix.assert_not_called()


def test_late_cancel_does_not_open_the_quality_metadata_editor(research, monkeypatch):
    from carr_library.ui.academic_tools import edit_catalog_edition

    frame, _, _ = make_actions(research)
    workspace = research[1]
    edition = frame.library.browse(workspace)[0].edition_id
    editor = Mock()
    monkeypatch.setitem(sys.modules, 'carr_library.ui.catalog_metadata_dialog',
                        SimpleNamespace(MetadataDialog=editor))
    edit_catalog_edition(frame, workspace, edition)
    _, operation, done, _ = frame.jobs[0]
    current = operation()
    frame.cancel_event.set()
    done(current)
    editor.assert_not_called()


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


def test_matrix_exports_subtopics_without_adding_another_storage_schema(research, tmp_path):
    output = tmp_path / 'comparison.md'
    frame, actions, _ = make_actions(research, save_path=output)
    with frame.library.read(research[1]) as db:
        before = tuple(tuple(row) for row in db.execute('SELECT * FROM sqlite_master ORDER BY name'))
    actions.export_comparison()
    frame.complete()
    text = output.read_text(encoding='utf-8')
    assert 'Chapter' in text and research[3] in text and research[4] in text
    assert 'Earlier saved note' in text and 'Child note' in text
    with frame.library.read(research[1]) as db:
        assert tuple(tuple(row) for row in db.execute('SELECT * FROM sqlite_master ORDER BY name')) == before


def test_export_cannot_replace_private_database(research):
    frame, actions, _ = make_actions(research, save_path=research[0].database)
    before = research[0].database.read_bytes()
    actions.export_comparison()
    assert not frame.jobs
    assert 'private library' in frame.report.call_args.args[0]
    assert research[0].database.read_bytes() == before
    assert research[0].search(research[1], 'Exact original quotation.')


def test_cancel_before_export_keeps_existing_file(research, tmp_path):
    output = tmp_path / 'comparison.md'
    output.write_text('Previous export', encoding='utf-8')
    frame, actions, _ = make_actions(research, save_path=output)
    actions.export_comparison()
    frame.cancel_event.set()
    with pytest.raises((InterruptedError, TimeoutError)):
        frame.complete()
    assert output.read_text(encoding='utf-8') == 'Previous export'


def test_scope_change_in_save_dialog_writes_nothing(research, tmp_path):
    frame, actions, dialogs = make_actions(research)
    output = tmp_path / 'comparison.md'
    def choose(*args, **kwargs):
        frame.active = research[2], None
        return output
    dialogs.save_path.side_effect = choose
    actions.export_comparison()
    assert frame.jobs == []
    assert not output.exists()
    assert 'changed' in frame.report.call_args.args[0]


def test_scope_change_after_queuing_export_writes_nothing(research, tmp_path):
    output = tmp_path / 'comparison.md'
    frame, actions, _ = make_actions(research, save_path=output)
    actions.export_comparison()
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


def test_cancelled_matrix_destination_writes_nothing(research):
    frame, actions, dialogs = make_actions(research, save_path=None)
    actions.export_comparison()
    assert frame.jobs == []
    dialogs.save_path.assert_called_once()


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


@pytest.mark.parametrize('kind', ['matrix'])
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
    export_confirmation.dialog.ShowModal.return_value = export_confirmation.ID_NO
    frame, actions, _ = make_actions(research, save_path=output)
    actions.export_comparison()
    assert not frame.jobs
    assert output.read_bytes() == b'Keep existing work'
    assert 'cancelled' in frame.report.call_args.args[0].lower()


@pytest.mark.parametrize('change', ['none', 'scope', 'metadata'])
def test_quality_editor_reuses_actual_metadata_guard_and_preserves_saved_sources(
        research, monkeypatch, change):
    from carr_library.ui.academic_tools import edit_catalog_edition

    app, workspace, other, topic, *_ = research
    frame, _, _ = make_actions(research)
    edition = app.browse(workspace)[0].edition_id
    before = app.edition_metadata(workspace, edition)
    saved = app.evidence(workspace, topic)
    assets = {path.name: path.read_bytes() for path in app.assets_dir.iterdir()}
    frame.executor, frame.book = Mock(), Mock()
    opened, destroyed = [], []

    class Editor:
        def __init__(self, parent, current, *, save, executor):
            assert parent is frame and current == before and executor is frame.executor
            self.save = save
            opened.append(current)

        def ShowModal(self):
            if change == 'scope':
                frame.active = other, None
            elif change == 'metadata':
                app.update_metadata(workspace, edition, expected_metadata=before,
                    title='Independent metadata edit', authors=before.authors,
                    language=before.language, edition=before.edition, volume=before.volume)
            self.save(title=before.title, authors=before.authors, language='sk',
                      edition=before.edition, volume=before.volume)
            return 1

        def Destroy(self):
            destroyed.append(True)

    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1))
    monkeypatch.setitem(sys.modules, 'carr_library.ui.catalog_metadata_dialog',
                        SimpleNamespace(MetadataDialog=Editor))
    edit_catalog_edition(frame, workspace, edition)
    assert len(frame.jobs) == 1 and not opened
    if change == 'none':
        frame.complete()
        assert app.edition_metadata(workspace, edition).language == 'sk'
        frame.book.GetPage.assert_any_call(1)
    else:
        with pytest.raises(ValueError, match='changed'):
            frame.complete()
        current = app.edition_metadata(workspace, edition)
        assert current.language == before.language
        assert current.title == ('Independent metadata edit' if change == 'metadata' else before.title)
        frame.book.GetPage.assert_not_called()
    assert opened == [before] and destroyed == [True]
    assert app.evidence(workspace, topic) == saved
    assert {path.name: path.read_bytes() for path in app.assets_dir.iterdir()} == assets


@pytest.mark.parametrize('existing', [False, True])
def test_additive_menu_installer_preserves_existing_routes(research, monkeypatch, existing):
    from carr_library.ui.academic_tools import install_academic_comparison_actions

    class Menu:
        def __init__(self, *titles):
            self.items = list(titles)
        def Append(self, identity, title):
            self.items.append(title)
            return title

    old_menu = Menu('Academic library...', 'DOI and ISBN lookup and open-access sources...')
    outline = Menu('Export topic and subtopics\tCtrl+Shift+E')
    bibliography = Menu('Review reference metadata for selected book')
    menus = [('Research', outline), ('Bibliography', bibliography)]
    if existing:
        menus.append(('Academic', old_menu))
    help_menu = Menu('Keyboard guide')
    menus.append(('Help', help_menu))
    frame, _, _ = make_actions(research)
    frame.Bind = Mock()
    frame.menu_bar = SimpleNamespace(
        FindMenu=lambda title: next((i for i, (name, _) in enumerate(menus) if name == title), -1),
        GetMenu=lambda index: menus[index][1], GetMenuCount=lambda: len(menus),
        Insert=lambda index, menu, title: menus.insert(index, (title.replace('&', ''), menu)))
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU='menu'))
    quality = Mock()
    monkeypatch.setitem(sys.modules, 'carr_library.ui.catalog_quality', SimpleNamespace(open_catalog_quality=quality))
    actions = install_academic_comparison_actions(frame)
    assert frame.academic_comparison_actions is actions
    selected = dict(menus)['Academic']
    assert selected.items[-2:] == ['Compare sources in active topic', 'Check catalog quality']
    if existing:
        assert selected is old_menu and selected.items[:2] == ['Academic library...', 'DOI and ISBN lookup and open-access sources...']
    assert menus[0] == ('Research', outline) and menus[1] == ('Bibliography', bibliography)
    assert menus[-1] == ('Help', help_menu)
    assert frame.Bind.call_count == 2
    frame.Bind.call_args_list[-1].args[1](None)
    assert quality.call_args.args == (frame,)
    assert callable(quality.call_args.kwargs['edit_edition'])


def test_new_unassigned_actions_keep_previously_saved_shortcut_owners(research):
    from carr_library.ui.keyboard_settings import MenuCommand, load_shortcuts, save_shortcuts

    root = research[0].root
    (root / 'state' / 'ui').mkdir(parents=True, exist_ok=True)
    previous = (MenuCommand('academic/academic library', 'Academic library', 1,
                           'Ctrl+Shift+B', 'Academic library...', None),)
    save_shortcuts(root, previous, {'academic/academic library': 'Ctrl+Alt+M'}, expected_token=None)
    path = root / 'state' / 'ui' / 'shortcuts.json'
    saved = path.read_bytes()
    added = tuple(MenuCommand('academic/' + label.casefold(), label, index, '', label, None)
                  for index, label in enumerate(('Compare sources in active topic', 'Check catalog quality'), 2))
    overrides, _ = load_shortcuts(root, previous + added)
    assert overrides == {'academic/academic library': 'Ctrl+Alt+M'}
    assert path.read_bytes() == saved
