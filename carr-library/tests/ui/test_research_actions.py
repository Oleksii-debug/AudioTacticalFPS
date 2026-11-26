"""Execute menu handlers with real scoped services and controllable dialogs.

Widget/focus doubles are explicit; these checks do not claim Windows or NVDA
acceptance. The import-resume cases execute the actual native worker process.
"""
import sys
import shutil
from types import SimpleNamespace
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.read_views import browse_editions
from carr_library.ui.research_actions import ResearchActions, install_research_actions


class Control:
    def __init__(self, value=''):
        self.value = value
        self.focused = False

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value

    def SetFocus(self):
        self.focused = True

    def SetItems(self, values):
        self.items = list(values)

    def SetSelection(self, value):
        self.selection = value


class CatalogView:
    def __init__(self, library, workspace):
        self.library, self.workspace = library, workspace
        self.ready = True
        self.books = Control()
        self.refreshed = 0
        self.rows = {r.edition_id: r for r in library.browse(workspace)}
        self.selection = (None, None, next(iter(self.rows), None))

    def _selection(self):
        return self.selection

    def transaction(self):
        return self.library.catalog_transaction(self.workspace)

    def _snapshot(self, catalog, collection):
        return {}, {r.edition_id: r for r in browse_editions(catalog, collection=collection)}

    def refresh(self, *, selection=None):
        self.refreshed += 1
        self.rows = {r.edition_id: r for r in self.library.browse(self.workspace)}
        if selection is not None:
            self.selection = selection
        return True


class SearchView:
    def __init__(self):
        self.invalidated = 0

    def refresh_collections(self):
        self.invalidated += 1


class EvidenceView:
    def __init__(self, workspace, topic, saved):
        self.workspace, self.topic = workspace, topic
        self.displayed = saved
        self.note = Control(saved.note)
        self.items = Control()

    def _selected(self):
        return self.displayed

    def _dirty(self):
        return self.note.GetValue() != self.displayed.note

    def refresh(self):
        raise AssertionError('Research actions must not refresh away an unsaved note')


class Frame:
    def __init__(self, library, workspace, topic=None, saved=None):
        self.library = library
        self.active = (workspace, topic)
        self.working = self.busy = self._closing = False
        self.choice, self.status = Control(), Control()
        self.search, self.catalog = SearchView(), CatalogView(library, workspace)
        self.book = SimpleNamespace(GetPage=lambda i: (self.search, self.catalog)[i])
        self.evidence = EvidenceView(workspace, topic, saved) if saved is not None else None
        self.cancel_event = threading.Event()
        self.cancel_on_start = False
        self.jobs = []
        self.messages = []
        _, self.scopes = self._load_scopes()

    def _load_scopes(self):
        rows = tuple(self.library.list_scopes())
        return rows, [(workspace, topic) for workspace, topic, _ in rows]

    def report(self, text):
        self.messages.append(text)
        self.status.SetValue(text)
        self.status.SetFocus()

    def run_job(self, title, operation, done=None):
        self.jobs.append(title)
        self.cancel_event.clear()
        if self.cancel_on_start:
            self.cancel_event.set()
        result = operation()
        if done is not None:
            done(result)


class Dialogs:
    def __init__(self, answer=None, *, during=None):
        self.answer, self.during = answer, during
        self.calls, self.shown = [], []

    def _answer(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.during is not None:
            action, self.during = self.during, None
            action()
        return self.answer

    fields = text = choose = _answer

    def show(self, parent, title, value):
        self.shown.append((title, value))


@pytest.fixture
def frame(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Research', 'Original description')
    topic = library.create_topic(workspace, 'Evidence')
    library.add_text(workspace, 'Original title', 'Original railway history — Україна.',
                     authors=('Original Author',), language='en', edition='First', volume='I')
    hit, = library.search(workspace, 'railway history')
    library.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved note')
    saved, = library.evidence(workspace, topic)
    return Frame(library, workspace, topic, saved)


def metadata_values(**changes):
    values = dict(title='Updated title', authors='Author One\nАвтор Два',
                  language='uk', edition='Second', volume='II')
    values.update(changes)
    return values


def test_metadata_action_preserves_original_and_saved_citations(frame):
    scope = frame.active
    old_edition = next(iter(frame.catalog.rows.values()))
    before, = frame.library.evidence(*scope)
    original = frame.library.assets.resolve(old_edition.asset).read_bytes()
    frame.evidence.note.SetValue('Unsaved draft — keep me')
    actions = ResearchActions(frame, Dialogs(metadata_values()))
    actions.edit_book_metadata()

    current, = frame.library.browse(scope[0])
    assert current.title == 'Updated title' and current.authors == ('Author One', 'Автор Два')
    assert (current.language, current.edition, current.volume) == ('uk', 'Second', 'II')
    assert current.asset == old_edition.asset
    assert frame.library.assets.resolve(current.asset).read_bytes() == original
    after, = frame.library.evidence(*scope)
    assert after == before
    new, = frame.library.search(scope[0], 'railway history')
    assert new.revision != before.anchor.identity.revision and new.title == current.title
    assert frame.evidence.note.GetValue() == 'Unsaved draft — keep me'
    assert frame.catalog.refreshed == frame.search.invalidated == 1
    assert frame.catalog.books.focused


def test_metadata_cancel_and_stale_selection_do_not_write(frame):
    previous = next(iter(frame.catalog.rows.values()))
    ResearchActions(frame, Dialogs()).edit_book_metadata()
    assert frame.library.browse(frame.active[0])[0] == previous
    assert frame.catalog.books.focused
    dialogs = Dialogs(metadata_values(), during=lambda: setattr(frame.catalog, 'selection', (None, None, 'foreign')))
    ResearchActions(frame, dialogs).edit_book_metadata()
    assert frame.library.browse(frame.active[0])[0] == previous
    assert frame.catalog.refreshed == 0
    assert 'changed' in frame.messages[-1]


def test_metadata_rejects_concurrent_catalog_change(frame):
    workspace = frame.active[0]
    edition = next(iter(frame.catalog.rows))
    def other_edit():
        frame.library.update_metadata(workspace, edition, title='Other edit', authors=('Other Author',))
    ResearchActions(frame, Dialogs(metadata_values(), during=other_edit)).edit_book_metadata()
    current, = frame.library.browse(workspace)
    assert current.title == 'Other edit' and current.authors == ('Other Author',)
    assert frame.catalog.refreshed == 0 and 'changed' in frame.messages[-1]


def test_metadata_uses_atomic_expected_value_guard(frame, monkeypatch):
    original = frame.library.update_metadata

    def concurrent_edit(workspace, edition_id, **changes):
        assert changes['expected_metadata'] == frame.library.edition_metadata(workspace, edition_id)
        original(workspace, edition_id, title='Concurrent edit at write boundary', authors=('Other Author',))
        return original(workspace, edition_id, **changes)

    monkeypatch.setattr(frame.library, 'update_metadata', concurrent_edit)
    ResearchActions(frame, Dialogs(metadata_values())).edit_book_metadata()
    current, = frame.library.browse(frame.active[0])
    assert current.title == 'Concurrent edit at write boundary'
    assert current.authors == ('Other Author',)
    assert frame.catalog.refreshed == 0 and 'changed' in frame.messages[-1]


def test_metadata_rejects_library_revoked_during_dialog(frame):
    workspace = frame.active[0]
    ResearchActions(frame, Dialogs(metadata_values(), during=lambda: frame.library.archive_workspace(workspace))).edit_book_metadata()
    assert 'not completed' in frame.messages[-1]
    frame.library.restore_workspace(workspace)
    assert frame.library.browse(workspace)[0].title == 'Original title'


def test_description_updates_without_replacing_panels_or_draft(frame):
    frame.evidence.note.SetValue('Unsaved description-related note')
    ResearchActions(frame, Dialogs('Новий опис\nSecond line')).edit_library_description()
    workspace, = frame.library.workspaces()
    assert workspace.name == 'Research' and workspace.description == 'Новий опис\nSecond line'
    assert frame.evidence.note.GetValue() == 'Unsaved description-related note'
    assert frame.catalog.refreshed == frame.search.invalidated == 0
    assert frame.choice.focused


def test_description_cancel_and_concurrent_change_are_preserved(frame):
    ResearchActions(frame, Dialogs()).edit_library_description()
    assert frame.library.workspaces()[0].description == 'Original description'
    def other_edit():
        frame.library.rename_workspace(frame.active[0], 'Changed elsewhere', 'Fresh description')
    ResearchActions(frame, Dialogs('Old dialog value', during=other_edit)).edit_library_description()
    current, = frame.library.workspaces()
    assert (current.name, current.description) == ('Changed elsewhere', 'Fresh description')
    assert 'changed' in frame.messages[-1]


def test_combined_rename_describe_keeps_notebook_and_unsaved_note(frame):
    old_book = frame.book
    frame.evidence.note.SetValue('Unfinished research note')
    values = {'name': 'Renamed research library', 'description': 'Updated description'}
    ResearchActions(frame, Dialogs(values)).rename_or_describe_library()
    current, = frame.library.workspaces()
    assert (current.name, current.description) == tuple(values.values())
    assert frame.book is old_book and frame.evidence.note.GetValue() == 'Unfinished research note'
    assert frame.choice.items[0] == current.name
    assert frame.choice.selection == frame.scopes.index(frame.active)


@pytest.mark.parametrize('method, answer', [
    ('edit_book_metadata', metadata_values()),
    ('edit_library_description', 'Unexpected description'),
    ('rename_or_describe_library', {'name': 'Wrong root name', 'description': 'Wrong root description'}),
    ('edit_evidence_tags', 'Wrong root tag'),
    ('find_evidence_by_tag', 'tag'),
])
def test_same_ids_in_different_data_root_do_not_retarget_modal_actions(frame, method, answer):
    original = frame.library
    copied_root = original.root.with_name('other-data-root')
    shutil.copytree(original.root, copied_root)
    other = LibraryApp(copied_root)
    assert other.workspaces()[0].id == original.workspaces()[0].id
    dialogs = Dialogs(answer, during=lambda: setattr(frame, 'library', other))
    getattr(ResearchActions(frame, dialogs), method)()
    assert 'changed' in frame.messages[-1]
    for library in (original, other):
        assert library.workspaces()[0].name == 'Research'
        assert library.workspaces()[0].description == 'Original description'
        assert library.browse(frame.active[0])[0].title == 'Original title'
        assert library.evidence_tags(*frame.active, frame.evidence.displayed.evidence_id) == ()
    assert dialogs.shown == []


def test_tag_edit_preserves_unsaved_note_and_persists_unicode(frame):
    saved = frame.evidence.displayed
    frame.evidence.note.SetValue('Unsaved draft with several paragraphs.\nKeep this line.')
    ResearchActions(frame, Dialogs('історія\nRailways\n\nісторія')).edit_evidence_tags()
    assert set(frame.library.evidence_tags(*frame.active, saved.evidence_id)) == {'історія', 'Railways'}
    assert frame.library.evidence(*frame.active) == (saved,)
    assert frame.evidence.note.GetValue().startswith('Unsaved draft') and frame.evidence.note.focused
    reopened = LibraryApp(frame.library.root)
    assert reopened.evidence_tags(*frame.active, saved.evidence_id) == frame.library.evidence_tags(*frame.active, saved.evidence_id)
    assert reopened.evidence(*frame.active) == (saved,)


def test_tag_cancel_and_explicit_clear_have_distinct_effects(frame):
    key = frame.evidence.displayed.evidence_id
    frame.library.set_evidence_tags(*frame.active, key, ('Keep',))
    ResearchActions(frame, Dialogs()).edit_evidence_tags()
    assert frame.library.evidence_tags(*frame.active, key) == ('Keep',)
    assert frame.evidence.items.focused
    ResearchActions(frame, Dialogs('')).edit_evidence_tags()
    assert frame.library.evidence_tags(*frame.active, key) == ()


def test_tags_reject_concurrent_edits_and_scopes(frame):
    scope, key = frame.active, frame.evidence.displayed.evidence_id
    def edit_tags():
        frame.library.set_evidence_tags(*scope, key, ('New external tag',))
    ResearchActions(frame, Dialogs('Stale tag', during=edit_tags)).edit_evidence_tags()
    assert frame.library.evidence_tags(*scope, key) == ('New external tag',)
    assert 'changed' in frame.messages[-1]
    other = frame.library.create_topic(scope[0], 'Other topic')
    ResearchActions(frame, Dialogs('Wrong topic', during=lambda: setattr(frame, 'active', (scope[0], other)))).edit_evidence_tags()
    assert frame.library.evidence_tags(*scope, key) == ('New external tag',)
    assert 'changed' in frame.messages[-1]


def test_tag_validation_failure_keeps_previous_tags_and_draft(frame):
    key = frame.evidence.displayed.evidence_id
    frame.library.set_evidence_tags(*frame.active, key, ('Existing',))
    frame.evidence.note.SetValue('Unsaved note')
    ResearchActions(frame, Dialogs('\n'.join('tag-' + str(i) for i in range(101)))).edit_evidence_tags()
    assert frame.library.evidence_tags(*frame.active, key) == ('Existing',)
    assert frame.evidence.note.GetValue() == 'Unsaved note'
    assert 'not completed' in frame.messages[-1]


def test_view_and_find_tags_use_verified_scoped_evidence_without_refresh(frame):
    scope, key = frame.active, frame.evidence.displayed.evidence_id
    frame.library.set_evidence_tags(*scope, key, ('Shared tag',))
    other = frame.library.create_workspace('Foreign library')
    other_topic = frame.library.create_topic(other, 'Other evidence')
    frame.library.add_text(other, 'Foreign book', 'Foreign secret quotation.')
    hit, = frame.library.search(other, 'Foreign secret')
    other_key = frame.library.save_hit(other, other_topic, hit.ref, hit.revision, 'Foreign note')
    frame.library.set_evidence_tags(other, other_topic, other_key, ('Shared tag',))
    frame.evidence.note.SetValue('Unsaved active note')
    dialogs = Dialogs('Shared tag')
    actions = ResearchActions(frame, dialogs)
    actions.view_evidence_tags()
    assert dialogs.shown[0][1] == '1. Shared tag'
    actions.find_evidence_by_tag()
    title, text = dialogs.shown[-1]
    assert title == 'Evidence tagged Shared tag'
    assert 'railway history' in text and 'Saved note' in text and 'Original Author' in text
    assert 'Foreign' not in text and 'SavedEvidence(' not in text
    assert frame.evidence.note.GetValue() == 'Unsaved active note' and frame.evidence.note.focused
    assert frame.library.evidence(*scope)[0].note == 'Saved note'


def test_find_tag_cancel_retains_note_focus(frame):
    frame.evidence.note.SetValue('Still editing')
    dialogs = Dialogs()
    ResearchActions(frame, dialogs).find_evidence_by_tag()
    assert dialogs.shown == [] and frame.evidence.note.GetValue() == 'Still editing'
    assert frame.evidence.note.focused


def pending_docx(frame, tmp_path):
    from docx import Document
    path = tmp_path / 'interrupted.docx'
    document = Document()
    document.add_paragraph('Source resumed through an actual worker process.')
    document.save(path)
    result = frame.library.import_file(frame.active[0], path, cancelled=lambda: True)
    assert result['state'] == 'cancelled'
    return result


def test_resume_menu_executes_existing_request_in_real_worker(frame, tmp_path):
    result = pending_docx(frame, tmp_path)
    before = len(frame.library.browse(frame.active[0]))
    ResearchActions(frame, Dialogs(0)).resume_import()
    requests = frame.library.import_requests(frame.active[0])
    assert len(requests) == 1 and requests[0]['request_id'] == result['request_id']
    assert requests[0]['state'] == 'complete'
    assert len(frame.library.browse(frame.active[0])) == before + 1
    assert len(frame.jobs) == 1 and frame.catalog.refreshed == frame.search.invalidated == 1
    assert frame.messages[-1].startswith('Import of interrupted.docx: complete.')
    assert 'unverified' in frame.messages[-1]
    dialogs = Dialogs(0)
    ResearchActions(frame, dialogs).resume_import()
    assert dialogs.calls == [] and len(frame.jobs) == 1


def test_resume_uses_live_cancellation_and_reports_noncompletion(frame, tmp_path):
    pending_docx(frame, tmp_path)
    before = tuple(frame.library.browse(frame.active[0]))
    frame.cancel_on_start = True
    ResearchActions(frame, Dialogs(0)).resume_import()
    assert frame.cancel_event.is_set()
    assert frame.library.import_requests(frame.active[0])[0]['state'] == 'cancelled'
    assert tuple(frame.library.browse(frame.active[0])) == before
    assert 'cancelled' in frame.messages[-1] and 'complete.' not in frame.messages[-1]
    assert frame.catalog.refreshed == frame.search.invalidated == 0


def test_resume_cancel_and_scope_change_do_not_start_worker(frame, tmp_path):
    pending_docx(frame, tmp_path)
    ResearchActions(frame, Dialogs()).resume_import()
    assert frame.choice.focused and frame.jobs == []
    old_scope = frame.active
    other = frame.library.create_workspace('Other library')
    ResearchActions(frame, Dialogs(0, during=lambda: setattr(frame, 'active', (other, None)))).resume_import()
    assert frame.jobs == [] and 'changed' in frame.messages[-1]
    assert frame.library.import_requests(old_scope[0])[0]['state'] == 'cancelled'


def test_resume_worker_and_callback_are_bound_to_original_data_root(frame, tmp_path):
    original = frame.library
    copied_root = original.root.with_name('other-data-root')
    shutil.copytree(original.root, copied_root)
    other = LibraryApp(copied_root)
    pending_docx(frame, tmp_path)
    queued = []
    frame.run_job = lambda title, operation, done: queued.append((operation, done))
    ResearchActions(frame, Dialogs(0)).resume_import()
    operation, done = queued[0]
    frame.library = other
    with pytest.raises(ValueError, match='library changed'):
        operation()
    done({'state': 'complete'})
    assert frame.messages == [] and frame.catalog.refreshed == frame.search.invalidated == 0
    assert original.import_requests(frame.active[0])[0]['state'] == 'cancelled'
    assert other.import_requests(frame.active[0]) == ()


def test_busy_actions_do_not_open_dialogs_or_mutate(frame):
    frame.working = True
    dialogs = Dialogs(metadata_values())
    actions = ResearchActions(frame, dialogs)
    for method in (actions.edit_book_metadata, actions.edit_library_description,
                   actions.rename_or_describe_library,
                   actions.edit_evidence_tags, actions.view_evidence_tags,
                   actions.find_evidence_by_tag, actions.resume_import):
        method()
    assert dialogs.calls == dialogs.shown == []
    assert all('already running' in message for message in frame.messages)


@pytest.mark.parametrize('existing', [False, True])
def test_install_reuses_existing_menu_ids_and_registers_commands_once(monkeypatch, existing):
    class Item:
        next_identity = 1

        def __init__(self, label):
            self.label, self.identity = label, self.next_identity
            type(self).next_identity += 1

        def GetId(self): return self.identity
        def IsSeparator(self): return self.label is None
        def GetItemLabel(self): return self.label
        def SetItemLabel(self, label): self.label = label

    class Menu:
        def __init__(self):
            self.items = []

        @property
        def labels(self):
            return [item.label for item in self.items]

        def AppendSeparator(self):
            self.items.append(Item(None))

        def Append(self, identity, label):
            item = Item(label)
            self.items.append(item)
            return item

        def GetMenuItems(self):
            return tuple(self.items)

        def Delete(self, identity):
            self.items = [item for item in self.items if item.identity != identity]

    menus = [Menu(), Menu()]
    original_ids = []
    if existing:
        for label in ('Resume interrupted import', 'Edit selected book metadata\tCtrl+Shift+M',
                      'Rename or describe library'):
            original_ids.append(menus[0].Append(-1, label).identity)
        original_ids.append(menus[1].Append(-1, 'Edit selected evidence tags').identity)
    bindings = []
    unbound = []
    frame = SimpleNamespace(menu_bar=SimpleNamespace(
        FindMenu=lambda title: ('Library', 'Research').index(title),
        GetMenu=lambda index: menus[index]),
        Bind=lambda event, callback, item: bindings.append((event, callback, item)),
        Unbind=lambda event, id: unbound.append((event, id)))
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU='menu'))
    actions = install_research_actions(frame)
    assert install_research_actions(frame) is actions
    assert len(bindings) == 6
    assert 'Resume interrupted import...\tCtrl+Shift+I' in menus[0].labels
    assert 'Edit selected book metadata...\tCtrl+Shift+M' in menus[0].labels
    assert 'Rename or describe library...' in menus[0].labels
    assert 'Edit tags for selected evidence...\tCtrl+Shift+T' in menus[1].labels
    assert all(callable(callback) for _, callback, _ in bindings)
    assert len([label for menu in menus for label in menu.labels if label is not None]) == 6
    assert [identity for event, identity in unbound] == original_ids
    assert set(original_ids) <= {item.identity for _, _, item in bindings}
