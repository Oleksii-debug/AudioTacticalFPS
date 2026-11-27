"""Exercise the installed academic UI with real Windows wx controls and SQLite.

Run with the installed interpreter: python -I scripts/windows_academic_smoke.py.
The external viewer is a recording callback. Manual key presses, native file
pickers and NVDA speech remain separate acceptance work, never inferred here.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'A native Windows session is required.',
                          'nvda': 'NOT_RUN'}), flush=True)
        return 77

    from carr_library.native_runtime import activate_bundled_runtime
    activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.research.academic_library import AcademicLibrary
    from carr_library.ui import academic_library_dialog as ui
    from carr_library.ui.desktop_app import DesktopFrame

    checks, failures = [], []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def focused(control):
        current = wx.Window.FindFocus()
        while current is not None:
            if current is control:
                return True
            current = current.GetParent()
        return False

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)

    def pump_until(predicate, explanation, timeout=12):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError(explanation)
            gui.Yield()
            sleep(0.01)

    def drive_modal(dialog, action, *, expected=wx.ID_OK):
        errors = []

        def perform():
            try:
                action()
                # Native Windows exits the modal loop after this callback returns.
                # The watchdog and ShowModal return-code check verify completion.
            except Exception:
                errors.append(traceback.format_exc())
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)

        def expired():
            if dialog.IsModal():
                errors.append('Native modal operation timed out.')
                dialog.EndModal(wx.ID_CANCEL)

        watchdog = wx.CallLater(20000, expired)
        wx.CallAfter(perform)
        try:
            result = dialog.ShowModal()
        finally:
            watchdog.Stop()
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('native modal action returned its expected result', result == expected)

    source = Path(ui.__file__).resolve()
    check('academic UI comes from the installed package',
          'site-packages' in tuple(part.casefold() for part in source.parts))
    check('wx uses real native Windows controls', wx.Platform == '__WXMSW__')
    frame = academic = None
    with TemporaryDirectory(prefix='research-academic-native-') as temporary:
        root = Path(temporary)
        try:
            library = LibraryApp(root / 'private library')
            workspace = library.create_workspace('Native academic library')
            topic = library.create_topic(workspace, 'Source evidence')
            imported = library.add_text(workspace, 'Original native source',
                'Exact native source wording about railways. Український текст.', authors=('Source Author',))
            hit, = library.search(workspace, 'railways')
            evidence_id = library.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved source note')
            original = library.assets.resolve(imported['asset']).read_bytes()
            frame = DesktopFrame(library)
            gui.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            frame._open_workspace(workspace, topic)
            pump_until(lambda: frame.evidence is not None and frame.evidence.displayed is not None
                       and not frame.working, 'Initial native evidence did not become ready.')
            from carr_library.citations.csl import ATTRIBUTION
            check('normal desktop startup visibly exposes named citation processor attribution',
                  frame.citation_credit.IsShownOnScreen()
                  and frame.citation_credit.GetLabel() == ATTRIBUTION
                  and frame.citation_credit.GetName() ==
                  'Citation processor attribution. ' + ATTRIBUTION.replace('\n', ' '))
            check('real desktop exposes the Academic menu', frame.menu_bar.FindMenu('Academic') >= 0)
            menu = frame.menu_bar.GetMenu(frame.menu_bar.FindMenu('Academic'))
            check('academic library has its keyboard shortcut',
                  any('Ctrl+Shift+B' in item.GetItemLabel() for item in menu.GetMenuItems()))
            before_book, before_scope, before_note = frame.book, frame.active, frame.evidence.note
            draft = 'Unsaved parent note — retain exactly.'
            before_note.SetValue(draft)
            frame.book.ChangeSelection(3)
            before_note.SetFocus()
            gui.Yield()

            controller = ui.AcademicController(library, workspace)
            _, ReferenceEditor, ImportPreviewDialog, _, _, _ = ui._native_types()
            academic = ui.create_academic_dialog(frame, controller)
            scenario_errors = []

            def scenarios():
                try:
                    check('native academic filters have readable names',
                          academic.query.GetName() == 'Search references'
                          and academic.references.GetName() == 'Academic references')
                    check('native reference details are read only',
                          bool(academic.details.GetWindowStyleFlag() & wx.TE_READONLY))
                    initial = {**controller.from_book(imported['edition']), 'title': 'Native academic article',
                               'authors': ('Native Author', 'Другий автор'), 'item_type': 'article',
                               'year': '2026', 'doi': '10.5555/native-academic', 'reading_status': 'reading',
                               'priority': 'high', 'rating': 4, 'progress': 25,
                               'tags': ('Seminar',), 'notes': 'NativePrivateNote'}
                    editor = ReferenceEditor(academic, save=controller.create, books=controller.books(),
                                             initial=initial, executor=frame.executor)
                    try:
                        check('native reference editor has separate bibliography and reading pages',
                              isinstance(editor.book, wx.Notebook) and editor.book.GetPageCount() == 2)
                        drive_modal(editor, editor.on_save)
                        first = editor.saved
                    finally:
                        editor.Destroy()
                    check('native create saved real metadata and reading state',
                          first.title == initial['title'] and first.authors == initial['authors']
                          and first.year == '2026' and first.progress == 25 and first.edition_id == imported['edition'])
                    academic.refresh(selected_id=first.id)
                    academic.references.SetFocus()
                    gui.Yield()
                    check('native reference list accepts actual keyboard focus', focused(academic.references))
                    check('native detail view displays the selected saved reference',
                          'Native academic article' in academic.details.GetValue()
                          and 'NativePrivateNote' in academic.details.GetValue())

                    editor = ReferenceEditor(academic, save=lambda values: controller.update(first, values),
                                             books=controller.books(), reference=first,
                                             executor=frame.executor, confirm_discard=lambda: False)
                    def edit_with_validation():
                        editor.book.ChangeSelection(1)
                        editor.controls['notes'].SetValue('NativeEditedNote\nУкраїнська нотатка.')
                        editor.controls['progress'].SetValue('20.5')
                        editor.on_save()
                        gui.Yield()
                        check('validation failure focuses actual native status', focused(editor.status))
                        check('validation failure preserves typed notes and invalid field',
                              editor.controls['notes'].GetValue().startswith('NativeEditedNote')
                              and editor.controls['progress'].GetValue() == '20.5')
                        editor.on_cancel()
                        check('declining discard keeps the native editor and draft',
                              editor.IsModal() and 'NativeEditedNote' in editor.controls['notes'].GetValue())
                        editor.controls['progress'].SetValue('100')
                        editor.controls['reading_status'].SetSelection(2)
                        editor.on_save()
                    try:
                        drive_modal(editor, edit_with_validation)
                        current = editor.saved
                    finally:
                        editor.Destroy()
                    check('native edit persists its explicit reading choices',
                          current.progress == 100 and current.reading_status == 'read'
                          and current.notes.startswith('NativeEditedNote'))

                    editor = ReferenceEditor(academic, save=lambda values: controller.update(first, values),
                                             books=controller.books(), reference=first,
                                             confirm_discard=lambda: True)
                    def stale_edit():
                        editor.controls['notes'].SetValue('Older unsaved draft')
                        editor.on_save()
                        gui.Yield()
                        check('stale native form keeps its draft and exposes the conflict',
                              'changed' in editor.status.GetValue()
                              and editor.controls['notes'].GetValue() == 'Older unsaved draft'
                              and focused(editor.status))
                        editor.on_cancel()
                    try:
                        drive_modal(editor, stale_edit, expected=wx.ID_CANCEL)
                    finally:
                        editor.Destroy()
                    check('stale native edit cannot overwrite saved notes', controller.get(first.id) == current)

                    academic.query.SetValue('Native academic')
                    academic.tag.SetValue('Seminar')
                    academic.reading_status.SetSelection(3)
                    academic.on_filter()
                    gui.Yield()
                    check('native filters use real scoped reading and tag queries',
                          len(academic.rows) == 1 and academic.selected().id == current.id
                          and focused(academic.references))
                    controller.create({'title': 'Alternative native description', 'authors': ('Other Author',),
                                       'doi': 'https://doi.org/10.5555/NATIVE-ACADEMIC'})
                    report_text = controller.duplicate_report()
                    check('duplicate suggestions retain both references and explain a reason',
                          'Reason:' in report_text and 'Native academic article' in report_text
                          and 'Alternative native description' in report_text and controller.statistics()['active'] == 2)

                    data = ('TY  - JOUR\nTI  - Unselected native import\nER  - \n\n'
                            'TY  - BOOK\nTI  - Selected native import\nAU  - Synthetic Author\nPY  - 2025\nER  - \n').encode()
                    preview = controller.preview(data, 'ris')
                    review = ImportPreviewDialog(academic, controller, preview)
                    try:
                        check('native import preview does not preselect any reference',
                              isinstance(review.entries, wx.CheckListBox)
                              and not any(review.entries.IsChecked(i) for i in range(review.entries.GetCount())))
                        def select_import():
                            review.entries.SetSelection(1)
                            review.entries.Check(1, True)
                            review.on_selected()
                            check('native import details correspond to the selected entry',
                                  'Selected native import' in review.details.GetValue())
                            review.on_import()
                        drive_modal(review, select_import)
                        imported_reference = controller.get(review.result.imported_ids[0])
                    finally:
                        review.Destroy()
                    check('only the explicitly checked import was saved',
                          imported_reference.title == 'Selected native import'
                          and not controller.list(query='Unselected native import'))
                    exported = academic.export_to(root / 'bibliography.ris', format='ris')
                    check('native export action exports all active references despite visible filters',
                          len(controller.preview(exported.encode(), 'ris').entries) == 3)
                    check('native export action excludes private notes by default', 'NativeEditedNote' not in exported)
                    included = academic.export_to(root / 'bibliography-with-notes.txt', include_notes=True)
                    check('explicit personal-note export includes the requested notes', 'NativeEditedNote' in included)

                    launched, original_launcher = [], wx.LaunchDefaultApplication
                    wx.LaunchDefaultApplication = lambda path: launched.append(Path(path)) or True
                    try:
                        academic.refresh(selected_id=current.id)
                        academic.on_document()
                    finally:
                        wx.LaunchDefaultApplication = original_launcher
                    check('linked-document action passes a verified viewing copy to the launcher callback',
                          len(launched) == 1 and launched[0].read_bytes() == original
                          and launched[0] != library.assets.resolve(imported['asset']))
                    academic.references.SetSelection(wx.NOT_FOUND)
                    academic.on_copy()
                    gui.Yield()
                    check('missing native selection does not copy an arbitrary last reference',
                          'Select a reference' in academic.status.GetValue() and focused(academic.status))

                    with library.write(workspace) as db:
                        service = AcademicLibrary(db, workspace)
                        for number in range(ui.PAGE_SIZE + 3):
                            service.create(title=f'Pagination reference {number:03}')
                    academic.on_clear_filters()
                    first_page = {row.id for row in academic.rows}
                    check('native first page reports more results without hiding them',
                          len(first_page) == ui.PAGE_SIZE and academic.next.IsEnabled()
                          and 'Next page' in academic.status.GetValue())
                    academic.on_next()
                    second_page = {row.id for row in academic.rows}
                    check('native next page reaches the remaining references exactly once',
                          not first_page.intersection(second_page)
                          and len(first_page | second_page) == controller.statistics()['active']
                          and academic.previous.IsEnabled() and not academic.next.IsEnabled())
                    academic.on_previous()
                    check('native previous page returns to the same records',
                          {row.id for row in academic.rows} == first_page)
                    academic.query.SetValue('Selected native import')
                    academic.on_filter()
                    archived = controller.archive(imported_reference)
                    academic.refresh()
                    check('archived references leave the default native results', not academic.rows)
                    academic.include_archived.SetValue(True)
                    academic.on_filter()
                    check('native archive view exposes a restore action for the correct reference',
                          academic.selected().id == archived.id
                          and 'Restore' in academic.actions['archive'].GetLabel())
                    controller.archive(archived, archived=False)
                    academic.refresh()
                    check('restored reference returns with active metadata', not academic.selected().archived)
                    check('academic operations preserve retained original bytes and saved source evidence',
                          library.assets.resolve(imported['asset']).read_bytes() == original
                          and library.evidence(workspace, topic)[0].evidence_id == evidence_id
                          and library.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote)
                except Exception:
                    scenario_errors.append(traceback.format_exc())
                finally:
                    if academic.IsModal():
                        academic.EndModal(wx.ID_OK if not scenario_errors else wx.ID_CANCEL)

            wx.CallAfter(scenarios)
            academic.ShowModal()
            if scenario_errors or failures:
                raise RuntimeError('\n'.join(scenario_errors + failures))
            check('closing the academic modal preserves the parent notebook, scope and draft',
                  frame.book is before_book and frame.active == before_scope
                  and frame.evidence.note is before_note and before_note.GetValue() == draft)
            frame._restore_note_focus()
            gui.Yield()
            check('keyboard focus can return to the retained native research draft', focused(before_note))
            print(json.dumps({'result': 'PASS', 'checks': len(checks), 'platform': platform.system(),
                              'wx': wx.version(), 'installed_ui_sha256': sha256(source.read_bytes()).hexdigest(),
                              'nvda': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN', 'native_file_pickers': 'NOT_RUN',
                              'external_viewer': 'recording callback only'}, sort_keys=True), flush=True)
        finally:
            if academic is not None:
                academic.Destroy()
            if frame is not None:
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
            gui.Yield()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({'result': 'FAIL', 'error': traceback.format_exc(), 'nvda': 'NOT_RUN'}), flush=True)
        raise SystemExit(1)
