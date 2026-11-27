"""Headless controller probe with real scoped SQLite services and export files.

The dialogs are explicit doubles. Jobs execute on a worker thread, while every
dialog/control assertion runs on the caller thread. This is not NVDA acceptance.
Run directly or include this explicit path in pytest collection.
"""
from dataclasses import replace
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from carr_library.application import LibraryApp
from carr_library.ui import catalog_insights_actions as ui


class Control:
    def __init__(self, thread, value=''):
        self.thread, self.value, self.focused = thread, value, False

    def SetFocus(self):
        assert threading.get_ident() == self.thread, 'wx focus used from a worker'
        self.focused = True


class Frame:
    def __init__(self, library, workspace, topic):
        self.main_thread = threading.get_ident()
        self.library = library
        self.active = workspace, topic
        self.book = object()
        self.cancel_event = threading.Event()
        self.choice, self.status = Control(self.main_thread), Control(self.main_thread)
        self.origin = Control(self.main_thread, 'UNSAVED PRIVATE DRAFT')
        self.evidence = SimpleNamespace(note=self.origin)
        self.working = self.busy = self._closing = False
        self.jobs, self.messages, self.errors = [], [], []
        self.before_worker = self.after_worker = None

    def report(self, value):
        assert threading.get_ident() == self.main_thread, 'wx report used from a worker'
        self.messages.append(value)
        self.status.value = value
        self.status.SetFocus()

    def run_job(self, title, operation, done=None, failed=None):
        self.jobs.append(title)
        self.working = self.busy = True
        self.cancel_event.clear()
        if self.before_worker:
            self.before_worker()
        result, errors = [], []

        def execute():
            try:
                result.append(operation())
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute)
        worker.start()
        worker.join(10)
        assert not worker.is_alive(), 'Bounded report worker did not complete'
        self.working = self.busy = False
        if self.after_worker:
            self.after_worker()
        try:
            if errors:
                raise errors[0]
            if done:
                done(result[0])
        except Exception as exc:
            self.errors.append(exc)
            if failed:
                failed(exc)
            else:
                raise


class Dialogs:
    def __init__(self, frame, answers=(0,), destination=None, during=None):
        self.frame = frame
        self.answers = iter(answers)
        self.path = destination
        self.during = dict(during or {})
        self.choices, self.shown, self.destinations = [], [], []

    def _main(self):
        assert threading.get_ident() == self.frame.main_thread, 'Dialog used from a worker'

    def focused_control(self, parent):
        self._main()
        return parent.origin

    def choose(self, parent, title, label, choices):
        self._main()
        self.choices.append((title, label, list(choices)))
        callback = self.during.get(len(self.choices))
        if callback:
            callback()
        return next(self.answers)

    def destination(self, parent, format):
        self._main()
        self.destinations.append(format)
        if self.during.get('destination'):
            self.during['destination']()
        return self.path

    def approve_export(self, parent, path, *, private_root):
        self._main()
        from carr_library.ui.research_actions import NativeDialogs
        return NativeDialogs().approve_export(parent, path, private_root=private_root)

    def show(self, parent, title, value):
        self._main()
        self.shown.append((title, value))
        if self.during.get('show'):
            self.during['show']()


class CatalogInsightsActionsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='catalog-insights-actions-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = LibraryApp(self.root / 'private library')
        self.workspace = self.library.create_workspace('Дослідження')
        self.topic = self.library.create_topic(self.workspace, 'Visible research')
        self.library.add_text(self.workspace, 'Історія', 'Private railway quotation — exact source text.')
        self.library.add_text(self.workspace, 'Complete source', 'Another source.',
                              authors=('Known author',), language='en')
        hit, = self.library.search(self.workspace, 'railway')
        self.library.save_hit(self.workspace, self.topic, hit.ref, hit.revision, 'PRIVATE SAVED NOTE')
        self.edition = hit.edition_id
        self.collection = self.library.create_collection(self.workspace, 'Selected collection')
        with self.library.catalog_transaction(self.workspace) as catalog:
            catalog.assign(self.collection, self.edition)
        other = self.library.create_workspace('Foreign library')
        self.library.add_text(other, 'FOREIGN SECRET TITLE', 'Not part of this report.')
        self.other_workspace = other
        self.frame = Frame(self.library, self.workspace, self.topic)
        self.destination = self.root / 'matrix.csv'
        self.approve_overwrite = True
        owner = self
        def confirmation(*args, **kwargs):
            assert threading.get_ident() == owner.frame.main_thread
            return 1 if owner.approve_overwrite else 0
        wx = SimpleNamespace(MessageBox=confirmation, YES=1, NO=0,
                             YES_NO=2, NO_DEFAULT=4, ICON_QUESTION=8)
        missing = object()
        previous_wx = sys.modules.get('wx', missing)
        sys.modules['wx'] = wx
        def restore_wx():
            if previous_wx is missing:
                sys.modules.pop('wx', None)
            else:
                sys.modules['wx'] = previous_wx
        self.addCleanup(restore_wx)

    def actions(self, answers=(0,), *, during=None, destination=None):
        dialogs = Dialogs(self.frame, answers,
                          self.destination if destination is None else destination, during)
        return ui.CatalogInsightsActions(self.frame, dialogs), dialogs

    def clone_library(self):
        destination = self.root / 'same identities in another root'
        shutil.copytree(self.library.root, destination)
        return LibraryApp(destination)

    def archive_topic(self):
        with self.library.topic_transaction(self.workspace) as tree:
            tree.archive(self.topic)

    def archive_collection(self):
        with self.library.catalog_transaction(self.workspace) as catalog:
            catalog.archive(self.collection)

    def assert_private_content_absent(self, value):
        self.assertNotIn('Private railway quotation', value)
        self.assertNotIn('PRIVATE SAVED NOTE', value)
        self.assertNotIn('UNSAVED PRIVATE DRAFT', value)
        self.assertNotIn('FOREIGN SECRET TITLE', value)
        self.assertNotIn(str(self.library.root), value)

    def test_overview_real_service_keeps_scope_and_draft(self):
        before = self.library.evidence(self.workspace, self.topic)
        actions, dialogs = self.actions()
        with patch.object(ui, 'analyze_catalog', wraps=ui.analyze_catalog) as analyze:
            actions.overview()
        self.assertEqual(self.frame.errors, [])
        self.assertEqual(len(self.frame.jobs), 1)
        self.assertEqual(dialogs.shown[0][0], 'Library overview')
        text = dialogs.shown[0][1]
        self.assertIn('Source editions: 2.', text)
        self.assertIn('Saved evidence links in visible topics: 1.', text)
        self.assertIn('not a fresh check', text)
        self.assert_private_content_absent(text)
        self.assertIs(analyze.call_args.args[0], self.library)
        self.assertEqual(analyze.call_args.kwargs['authorized_topic'], self.topic)
        self.assertIs(analyze.call_args.kwargs['cancelled'].__self__, self.frame.cancel_event)
        self.assertEqual(self.frame.origin.value, 'UNSAVED PRIVATE DRAFT')
        self.assertTrue(self.frame.origin.focused)
        self.assertEqual(self.library.evidence(self.workspace, self.topic), before)

    def test_collection_scope_and_metadata_checklist(self):
        actions, dialogs = self.actions((1,))
        actions.checklist()
        self.assertEqual(self.frame.errors, [])
        self.assertIn('Collection: Selected collection', dialogs.shown[0][1])
        self.assertIn('Історія', dialogs.shown[0][1])
        self.assertIn(self.edition, dialogs.shown[0][1])
        self.assertIn('Add author names if known', dialogs.shown[0][1])
        self.assertNotIn('Complete source', dialogs.shown[0][1])
        self.assert_private_content_absent(dialogs.shown[0][1])

    def test_empty_collection_report_is_explicit(self):
        with self.library.catalog_transaction(self.workspace) as catalog:
            catalog.remove(self.collection, self.edition)
        actions, dialogs = self.actions((1,))
        actions.overview()
        self.assertIn('Source editions: 0.', dialogs.shown[0][1])
        self.assertIn('No sources in this scope', dialogs.shown[0][1])

    def test_scope_cancel_restores_original_focus_without_job(self):
        actions, dialogs = self.actions((None,))
        actions.overview()
        self.assertEqual(self.frame.jobs, [])
        self.assertEqual(dialogs.shown, [])
        self.assertTrue(self.frame.origin.focused)

    def test_format_and_destination_cancel_preserve_file_and_focus(self):
        self.destination.write_text('Existing output', encoding='utf-8')
        for answers, destination in (((0, None), self.destination), ((0, 0), None)):
            with self.subTest(answers=answers):
                dialogs = Dialogs(self.frame, answers, destination)
                ui.CatalogInsightsActions(self.frame, dialogs).export_matrix()
                self.assertEqual(self.frame.jobs, [])
                self.assertTrue(self.frame.origin.focused)
                self.assertEqual(self.destination.read_text(encoding='utf-8'), 'Existing output')

    def test_busy_or_missing_scope_never_opens_dialog(self):
        for busy in (True, False):
            with self.subTest(busy=busy):
                self.frame.working = busy
                self.frame.active = (self.workspace, self.topic) if busy else None
                actions, dialogs = self.actions()
                actions.overview()
                self.assertEqual(dialogs.choices, [])
                self.assertEqual(self.frame.jobs, [])

    def test_closed_frame_does_not_report_or_open_dialog(self):
        self.frame._closing = True
        actions, dialogs = self.actions()
        actions.overview()
        self.assertEqual(dialogs.choices, [])
        self.assertEqual(self.frame.messages, [])

    def test_scope_change_during_choice_blocks_work(self):
        actions, dialogs = self.actions(during={1: lambda: setattr(
            self.frame, 'active', (self.other_workspace, None))})
        actions.overview()
        self.assertEqual(self.frame.jobs, [])
        self.assertEqual(dialogs.shown, [])
        self.assertIn('changed', self.frame.messages[-1])

    def test_same_ids_other_library_object_during_dialog_are_rejected(self):
        other = self.clone_library()
        actions, dialogs = self.actions(during={1: lambda: setattr(self.frame, 'library', other)})
        actions.overview()
        self.assertEqual(self.frame.jobs, [])
        self.assertEqual(dialogs.shown, [])
        self.assertIn('changed', self.frame.messages[-1])

    def test_rebuilt_panel_with_same_ids_is_rejected(self):
        actions, _ = self.actions(during={1: lambda: setattr(self.frame, 'book', object())})
        actions.checklist()
        self.assertEqual(self.frame.jobs, [])
        self.assertIn('changed', self.frame.messages[-1])

    def test_archived_topic_during_dialog_is_reauthorized(self):
        actions, dialogs = self.actions(during={1: self.archive_topic})
        actions.overview()
        self.assertEqual(self.frame.jobs, [])
        self.assertEqual(dialogs.shown, [])
        self.assertIn('archived topic', self.frame.messages[-1])

    def test_renamed_collection_during_dialog_is_rejected(self):
        actions, dialogs = self.actions((1,), during={1: lambda: self.library.rename_collection(
            self.workspace, self.collection, 'Changed collection')})
        actions.overview()
        self.assertEqual(self.frame.jobs, [])
        self.assertEqual(dialogs.shown, [])
        self.assertIn('collection changed', self.frame.messages[-1])

    def test_worker_rejects_same_id_data_root_replacement(self):
        other = self.clone_library()
        self.frame.before_worker = lambda: setattr(self.frame, 'library', other)
        actions, dialogs = self.actions()
        with patch.object(ui, 'analyze_catalog', wraps=ui.analyze_catalog) as analyze:
            actions.overview()
        analyze.assert_not_called()
        self.assertEqual(dialogs.shown, [])
        self.assertIn('changed', self.frame.messages[-1])

    def test_worker_rejects_archived_workspace(self):
        self.frame.before_worker = lambda: self.library.archive_workspace(self.workspace)
        actions, dialogs = self.actions()
        actions.overview()
        self.assertEqual(dialogs.shown, [])
        self.assertIn('archived', self.frame.messages[-1])

    def test_cancellation_before_analysis_suppresses_output(self):
        self.frame.before_worker = self.frame.cancel_event.set
        actions, dialogs = self.actions()
        with patch.object(ui, 'analyze_catalog', wraps=ui.analyze_catalog) as analyze:
            actions.overview()
        analyze.assert_not_called()
        self.assertEqual(dialogs.shown, [])
        self.assertIn('cancelled', self.frame.messages[-1])

    def test_callback_reauthorizes_topic_before_showing(self):
        self.frame.after_worker = self.archive_topic
        actions, dialogs = self.actions()
        actions.checklist()
        self.assertEqual(dialogs.shown, [])
        self.assertIn('archived topic', self.frame.messages[-1])

    def test_callback_rejects_same_ids_other_root(self):
        other = self.clone_library()
        self.frame.after_worker = lambda: setattr(self.frame, 'library', other)
        actions, dialogs = self.actions()
        actions.overview()
        self.assertEqual(dialogs.shown, [])
        self.assertIn('changed', self.frame.messages[-1])

    def test_callback_rejects_archived_collection(self):
        self.frame.after_worker = self.archive_collection
        actions, dialogs = self.actions((1,))
        actions.overview()
        self.assertEqual(dialogs.shown, [])
        self.assertIn('collection changed', self.frame.messages[-1])

    def test_callback_observes_late_cancellation(self):
        self.frame.after_worker = self.frame.cancel_event.set
        actions, dialogs = self.actions()
        actions.checklist()
        self.assertEqual(dialogs.shown, [])
        self.assertIn('cancelled', self.frame.messages[-1])

    def test_foreign_analysis_is_not_displayed(self):
        original = ui.analyze_catalog

        def foreign(*args, **kwargs):
            return replace(original(*args, **kwargs), workspace=self.other_workspace)

        actions, dialogs = self.actions()
        with patch.object(ui, 'analyze_catalog', foreign):
            actions.overview()
        self.assertEqual(dialogs.shown, [])
        self.assertIn('does not match', self.frame.messages[-1])

    def test_collection_labels_escape_direction_controls(self):
        self.library.rename_collection(self.workspace, self.collection, 'А\u202eB\nC')
        actions, dialogs = self.actions((None,))
        actions.overview()
        label = dialogs.choices[0][2][1]
        self.assertNotIn('\u202e', label)
        self.assertNotIn('\n', label)
        self.assertIn('А\\u202eB C', label)

    def test_all_matrix_formats_use_real_renderer_without_notes_or_quotes(self):
        for index, format in enumerate(('csv', 'markdown', 'text')):
            with self.subTest(format=format):
                actions, dialogs = self.actions((1, index))
                actions.export_matrix()
                self.assertEqual(self.frame.errors, [])
                text = self.destination.read_text(encoding='utf-8')
                self.assertIn('Історія', text)
                self.assertNotIn('Complete source', text)
                self.assert_private_content_absent(text)
                self.assertNotIn(self.edition, text)
                self.assertNotIn('asset_sha256', text)
                self.assertEqual(dialogs.destinations, [format])
                self.assertIn('matrix saved', self.frame.messages[-1])
                self.assertEqual(self.frame.origin.value, 'UNSAVED PRIVATE DRAFT')

    def test_changed_library_after_save_dialog_never_writes(self):
        other = self.clone_library()
        self.destination.write_text('Keep existing', encoding='utf-8')
        actions, _ = self.actions((0, 0), during={'destination': lambda: setattr(
            self.frame, 'library', other)})
        actions.export_matrix()
        self.assertEqual(self.frame.jobs, [])
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'Keep existing')

    def test_matrix_rechecks_topic_after_rendering_before_publication(self):
        from carr_library.research import literature_matrix

        original = literature_matrix.render_literature_matrix

        def revoke(*args, **kwargs):
            text = original(*args, **kwargs)
            self.archive_topic()
            return text

        self.destination.write_text('Keep existing', encoding='utf-8')
        actions, _ = self.actions((0, 0))
        with patch.object(literature_matrix, 'render_literature_matrix', revoke):
            actions.export_matrix()
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'Keep existing')
        self.assertIn('archived topic', self.frame.messages[-1])

    def test_matrix_late_cancellation_before_publication_keeps_existing(self):
        from carr_library.research import literature_matrix

        original = literature_matrix.render_literature_matrix

        def cancel(*args, **kwargs):
            text = original(*args, **kwargs)
            self.frame.cancel_event.set()
            return text

        self.destination.write_text('Keep existing', encoding='utf-8')
        actions, _ = self.actions((0, 0))
        with patch.object(literature_matrix, 'render_literature_matrix', cancel):
            actions.export_matrix()
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'Keep existing')
        self.assertIn('cancelled', self.frame.messages[-1])

    def test_matrix_render_cannot_replace_competing_destination_bytes(self):
        from carr_library.research import literature_matrix
        original = literature_matrix.render_literature_matrix
        def competing(*args, **kwargs):
            value = original(*args, **kwargs)
            self.destination.write_bytes(b'Newer independently saved matrix')
            return value
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.destination.unlink(missing_ok=True)
                if existing:
                    self.destination.write_bytes(b'Previously approved matrix')
                actions, _ = self.actions((0, 0))
                with patch.object(literature_matrix, 'render_literature_matrix', competing):
                    actions.export_matrix()
                self.assertEqual(self.destination.read_bytes(), b'Newer independently saved matrix')
                self.assertIn('destination changed', self.frame.messages[-1])
                self.assertEqual(list(self.root.glob('.research-export-*')), [])

    def test_matrix_overwrite_decline_prevents_worker_and_restores_focus(self):
        self.destination.write_bytes(b'Keep existing matrix')
        self.approve_overwrite = False
        actions, _ = self.actions((0, 0))
        actions.export_matrix()
        self.assertEqual(self.frame.jobs, [])
        self.assertEqual(self.destination.read_bytes(), b'Keep existing matrix')
        self.assertTrue(self.frame.origin.focused)

    def test_cancellation_after_publication_is_reported_as_saved(self):
        original = ui.write_export

        def cancel_after_write(*args, **kwargs):
            original(*args, **kwargs)
            self.frame.cancel_event.set()

        actions, _ = self.actions((0, 0))
        with patch.object(ui, 'write_export', cancel_after_write):
            actions.export_matrix()
        self.assertTrue(self.destination.is_file())
        self.assertEqual(self.frame.errors, [])
        self.assertIn('file was saved before cancellation', self.frame.messages[-1])

    def test_cancellation_during_file_write_preserves_destination_and_removes_temporary(self):
        from carr_library.research import export_file

        original_fsync = export_file.os.fsync
        synced_sizes = []

        def cancel_after_sync(descriptor):
            original_fsync(descriptor)
            synced_sizes.append(export_file.os.fstat(descriptor).st_size)
            self.frame.cancel_event.set()

        self.destination.write_text('Keep existing', encoding='utf-8')
        actions, _ = self.actions((0, 0))
        with (patch.object(export_file.os, 'fsync', side_effect=cancel_after_sync),
              patch.object(export_file.os, 'replace', wraps=export_file.os.replace) as publish,
              patch.object(ui, 'write_export', wraps=ui.write_export) as write):
            actions.export_matrix()
        self.assertEqual(len(synced_sizes), 1)
        self.assertGreater(synced_sizes[0], 0)
        self.assertIs(write.call_args.kwargs['cancelled'].__self__, self.frame.cancel_event)
        publish.assert_not_called()
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'Keep existing')
        self.assertEqual(list(self.root.glob('.research-export-*')), [])
        self.assertIn('cancelled; the destination was not changed', self.frame.messages[-1])

    def test_completed_export_does_not_claim_new_scope_owns_it(self):
        other = self.clone_library()
        self.frame.after_worker = lambda: setattr(self.frame, 'library', other)
        actions, _ = self.actions((0, 0))
        actions.export_matrix()
        self.assertTrue(self.destination.is_file())
        self.assertIn('file was saved', self.frame.messages[-1])
        self.assertNotIn(str(self.destination), self.frame.messages[-1])

    def test_checked_export_rejects_private_library_destination(self):
        protected = self.library.root / 'matrix.csv'
        actions, _ = self.actions((0, 0), destination=protected)
        actions.export_matrix()
        self.assertFalse(protected.exists())
        self.assertIn('outside the private library', self.frame.messages[-1])

    def test_checked_export_failure_keeps_existing_destination(self):
        self.destination.write_text('Keep existing', encoding='utf-8')
        actions, _ = self.actions((0, 0))
        with patch('carr_library.research.export_file.os.replace', side_effect=OSError('Synthetic disk error')):
            actions.export_matrix()
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'Keep existing')
        self.assertIn('Synthetic disk error', self.frame.messages[-1])
        self.assertEqual(list(self.root.glob('.research-export-*')), [])

    def test_cancelled_dialog_after_root_change_does_not_focus_old_control(self):
        actions, _ = self.actions((None,), during={1: lambda: setattr(
            self.frame, 'library', self.clone_library())})
        actions.overview()
        self.assertFalse(self.frame.origin.focused)
        self.assertEqual(self.frame.jobs, [])


class MenuRegistrationTests(unittest.TestCase):
    def test_reuses_existing_item_and_installs_only_once(self):
        class Item:
            sequence = 0

            def __init__(self, label):
                Item.sequence += 1
                self.identity, self.label = Item.sequence, label

            def GetId(self): return self.identity
            def GetItemLabel(self): return self.label
            def SetItemLabel(self, label): self.label = label
            def IsSeparator(self): return self.label is None

        class Menu:
            def __init__(self): self.items = []
            def GetMenuItems(self): return tuple(self.items)
            def Append(self, identity, label):
                item = Item(label)
                self.items.append(item)
                return item
            def AppendSeparator(self): self.items.append(Item(None))
            def Delete(self, identity):
                self.items = [item for item in self.items if item.identity != identity]

        menu, bound, unbound = Menu(), [], []
        keep = menu.Append(-1, 'Library overview')
        duplicate = menu.Append(-1, 'Library overview...')
        unrelated = menu.Append(-1, 'Save a topic search')
        frame = SimpleNamespace(menu_bar=SimpleNamespace(
            FindMenu=lambda title: 1 if title == 'Research' else -1,
            GetMenu=lambda index: menu),
            Bind=lambda event, callback, item: bound.append((callback, item)),
            Unbind=lambda event, id: unbound.append(id))
        with patch.dict(sys.modules, {'wx': SimpleNamespace(ID_ANY=-1, EVT_MENU='menu')}):
            installed = ui.install_catalog_insights_actions(frame)
            self.assertIs(ui.install_catalog_insights_actions(frame), installed)
        self.assertEqual(len(bound), 3)
        self.assertIs(bound[0][1], keep)
        self.assertEqual(unbound, [keep.identity, duplicate.identity])
        self.assertIn(unrelated, menu.items)
        self.assertNotIn(duplicate, menu.items)
        self.assertEqual([callback.__name__ for callback, _ in bound],
                         ['overview', 'checklist', 'export_matrix'])
        self.assertEqual(bound[-1][1].GetItemLabel(), 'Export source comparison &matrix...')


if __name__ == '__main__':
    unittest.main()
