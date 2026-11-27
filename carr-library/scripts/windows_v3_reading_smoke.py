"""Exercise installed V3 reading/save/shortcut controls on native Windows.

Synthetic documents only. This records real wx event handling, native controls,
durable state and backup round trips. NVDA speech and listening are NOT_RUN.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.keyboard_preferences import KeymapStore
    from carr_library.library_backup import restore_backup
    from carr_library.reader.narration import NarrationStore, new_project
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.narration_editor import open_narration_editor
    from carr_library.ui.research_actions import NativeDialogs

    checks, failures = [], []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)
    native_loop = wx.GUIEventLoop()
    loop_activation = wx.EventLoopActivator(native_loop)

    def pump(predicate, timeout=15):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() > deadline:
                raise TimeoutError('Native reading smoke did not complete.')
            gui.Yield()
            sleep(0.01)

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native button event: ' + button.GetLabel(), button.GetEventHandler().ProcessEvent(event))

    from carr_library.ui import productivity, narration_editor
    import carr_library.reader.regex_preview as regex_adapter
    import carr_library.reader.regex_worker as regex_worker
    check('productivity and editor load from the installed wheel', all(
        'site-packages' in [part.casefold() for part in Path(module.__file__).parts]
        for module in (productivity, narration_editor, regex_adapter, regex_worker)))
    check('real Windows wx runtime', wx.Platform == '__WXMSW__')

    with TemporaryDirectory(prefix='research-v3-reading-native-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic V3 reading')
        topic = library.create_topic(workspace, 'Reading notes')
        source = 'Original railway text. Українська мова. Slovenská veta.'
        imported = library.add_text(workspace, 'Synthetic V3 source', source)
        hit, = library.search(workspace, 'railway')
        identity = library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original note')
        original_bytes = library.assets.resolve(imported['asset']).read_bytes()
        frame = DesktopFrame(library)
        gui.SetTopWindow(frame)
        frame.Show()
        frame._open_workspace(workspace, topic)
        pump(lambda: not frame.working and frame.evidence is not None and frame.evidence.displayed is not None)
        try:
            check('File is the first Alt menu', frame.menu_bar.GetMenuLabel(0).replace('&', '') == 'File')
            check('Options and Reading remain accessible', frame.menu_bar.FindMenu('Options') >= 0
                  and frame.menu_bar.FindMenu('Reading') >= 0)
            keyboard = frame.keyboard_commands
            inventory = {identity: keyboard.shortcut(identity) for identity in keyboard.bindings}
            check('one authoritative Main keyboard manager is installed',
                  not hasattr(frame, 'keyboard_actions')
                  and sum(frame.menu_bar.GetMenuLabel(i).replace('&', '') == 'Options'
                          for i in range(frame.menu_bar.GetMenuCount())) == 1)
            expected = {
                'file.save': 'Ctrl+S',
                'file.save_all': 'Ctrl+Shift+S',
                'research.advanced_query': 'Ctrl+Alt+Q',
                'research.search_center': 'Ctrl+Alt+F',
                'academic.compare_sources': 'Ctrl+Alt+M',
                'academic.open': 'Ctrl+Shift+B',
                'reading.open': 'Ctrl+R',
                'library.epub_reader': 'Ctrl+Shift+R',
            }
            check('native academic, search, reader and save menus compose',
                  all(inventory.get(name) == shortcut for name, shortcut in expected.items()))
            shortcuts = [value.casefold() for value in inventory.values() if value]
            check('complete native command inventory has unique shortcuts', len(shortcuts) == len(set(shortcuts)))
            print(json.dumps({'menu_inventory': inventory}, ensure_ascii=False, sort_keys=True), flush=True)
            save_binding = keyboard.bindings['file.save']
            check('main Save exposes Ctrl+S', save_binding.item.GetItemLabel().endswith('\tCtrl+S'))
            frame.evidence.note.SetValue('Saved through the native File command.')
            event = wx.CommandEvent(wx.EVT_MENU.typeId, save_binding.item.GetId())
            check('native File Save dispatches', frame.GetEventHandler().ProcessEvent(event))
            pump(lambda: not frame.working)
            check('File Save commits the selected evidence note',
                  library.evidence(workspace, topic)[0].note == 'Saved through the native File command.')
            keyboard.apply_overrides({'file.save': 'Ctrl+Alt+S'})
            check('shortcut change updates native menu', save_binding.item.GetItemLabel().endswith('\tCtrl+Alt+S'))
            check('shortcut persists after reopening preferences',
                  KeymapStore(library.root / 'keyboard-shortcuts.json').load()['file.save'] == 'Ctrl+Alt+S')
            old_labels = tuple(binding.item.GetItemLabel() for binding in keyboard.bindings.values())
            try:
                keyboard.apply_overrides({'file.save': 'Ctrl+I'})
            except ValueError:
                pass
            else:
                raise AssertionError('Conflicting shortcut was accepted.')
            check('conflicting key leaves native menu unchanged',
                  tuple(binding.item.GetItemLabel() for binding in keyboard.bindings.values()) == old_labels)

            desk = ReadingDesk(library, workspace, imported['edition'])
            page = desk.page(1)
            state = ReadingState(library.root)
            state.remember(page.location)
            state.add_bookmark(page.location, 'Synthetic start')
            store = NarrationStore(library.root)
            project = new_project(page.raw_text, title='Synthetic reading copy', source_location=page.location)
            changed = source + '\nExplicit reading-copy edit.'
            regex_text = changed.replace('Explicit reading-copy', 'reading-copy Explicit')
            recovered = regex_text + '\nAutosaved recovery sentence.'
            modal_errors, later_calls = [], []
            reading = {}

            def descendants(window):
                for child in window.GetChildren():
                    yield child
                    yield from descendants(child)

            def schedule_modal(parent, title, action):
                parent_id, deadline = parent.GetId(), monotonic() + 8
                def respond():
                    try:
                        windows = [window for window in wx.GetTopLevelWindows()
                            if isinstance(window, wx.Dialog) and not window.IsBeingDeleted()
                            and window.IsModal() and window.GetTitle() == title
                            and window.GetParent() is not None
                            and window.GetParent().GetId() == parent_id]
                        if windows:
                            if len(windows) != 1:
                                raise AssertionError('Expected exactly one owned regex dialog.')
                            action(windows[0])
                        elif monotonic() >= deadline:
                            raise TimeoutError('Owned regex dialog was not shown: ' + title)
                        else:
                            later_calls.append(wx.CallLater(20, respond))
                    except Exception:
                        modal_errors.append(traceback.format_exc())
                        for window in wx.GetTopLevelWindows():
                            if isinstance(window, wx.Dialog) and window.IsModal():
                                window.EndModal(wx.ID_CANCEL)
                later_calls.append(wx.CallLater(20, respond))

            def modal_button(window, identity):
                matches = [child for child in descendants(window)
                           if isinstance(child, wx.Button) and child.GetId() == identity]
                if len(matches) != 1:
                    raise AssertionError('Expected one native modal action.')
                click(matches[0])

            class RegexDialogs(NativeDialogs):
                def fields(self, parent, title, fields, explanation=''):
                    def enter(window):
                        controls = {child.GetName():child for child in descendants(window)
                                    if isinstance(child, wx.TextCtrl)}
                        check('native regex request exposes both named fields',
                              all(label in controls for _,label,_,_ in fields))
                        controls[fields[0][1]].SetValue(r'(Explicit) (reading-copy)')
                        controls[fields[1][1]].SetValue(r'\2 \1')
                        modal_button(window, wx.ID_OK)
                    schedule_modal(parent,title,enter)
                    return super().fields(parent,title,fields,explanation=explanation)

                def choose(self, parent, title, label, choices):
                    def select(window):
                        if title == 'Regular-expression replacement':
                            check('native review has not yet applied the regex result',
                                  reading['controller'].pending_text == changed)
                        lists = [child for child in descendants(window) if isinstance(child, wx.ListBox)]
                        if len(lists) != 1:
                            raise AssertionError('Expected one native regex choice list.')
                        lists[0].SetSelection(0)
                        modal_button(window, wx.ID_OK)
                    schedule_modal(parent,title,select)
                    return super().choose(parent,title,label,choices)

                def show(self, parent, title, value):
                    def read(window):
                        texts = [child for child in descendants(window) if isinstance(child, wx.TextCtrl)]
                        check('actual regex proposal is readable before explicit apply',
                              len(texts) == 1 and bool(texts[0].GetWindowStyleFlag() & wx.TE_READONLY)
                              and texts[0].GetValue() == regex_text)
                        modal_button(window, wx.ID_CANCEL)
                    schedule_modal(parent,title,read)
                    return super().show(parent,title,value)

            def safely(dialog, action):
                try:
                    action()
                except Exception:
                    modal_errors.append(traceback.format_exc())
                    if dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def ready(dialog, controller, controls):
                reading.update(controller=controller, controls=controls)
                def begin():
                    check('original text is a native read-only multiline control',
                          isinstance(controls['original'], wx.TextCtrl)
                          and bool(controls['original'].GetWindowStyleFlag() & wx.TE_READONLY)
                          and controls['original'].GetValue() == source)
                    check('editor permits Tab navigation',
                          not bool(controls['editor'].GetWindowStyleFlag() & wx.TE_PROCESS_TAB))
                    check('provenance distinguishes a reading copy from quotation evidence',
                          'reading' in controls['origin'].GetValue().lower())
                    controls['editor'].SetValue(changed)
                    click(controls['save'])
                    saved, _ = store.load(project.id)
                    check('native Save persists edits but retains exact original',
                          saved.current_text == changed and saved.original_text == source)
                    click(controls['regex'])
                    check('regex uses a pending worker and leaves source text intact',
                          controls['cancel_regex'].IsEnabled()
                          and controller.pending_text == changed
                          and controls['original'].GetValue() == source)
                    deadline = monotonic() + 10

                    def finish():
                        draft, _ = store.load(project.id, draft=True)
                        check('native timer publishes a recoverable draft', draft.current_text == recovered)
                        check('autosave does not silently replace saved version',
                              store.load(project.id)[0].current_text == regex_text)
                        click(controls['save'])
                        click(controls['close'])

                    def regex_finished():
                        if modal_errors:
                            raise RuntimeError('\n'.join(modal_errors))
                        owned_modal = any(isinstance(window, wx.Dialog)
                            and not window.IsBeingDeleted() and window.IsModal()
                            and window.GetParent() is not None
                            and window.GetParent().GetId() == dialog.GetId()
                            for window in wx.GetTopLevelWindows())
                        # Worker settlement reenables controls before the real
                        # nested read/apply dialogs finish. Wait for the actual
                        # applied text and their return, not just a disabled
                        # Cancel button, while retaining the native assertions.
                        if (controls['cancel_regex'].IsEnabled() or owned_modal
                                or controls['editor'].GetValue() != regex_text):
                            if monotonic() >= deadline:
                                raise TimeoutError('Native regex review and explicit apply did not complete.')
                            later_calls.append(wx.CallLater(20, lambda: safely(dialog, regex_finished)))
                            return
                        check('native explicit regex apply changes only the reading copy',
                              controls['editor'].GetValue() == regex_text
                              and controller.project.original_text == source)
                        click(controls['undo'])
                        check('one native undo restores the text before regex apply',
                              controls['editor'].GetValue() == changed)
                        click(controls['redo'])
                        check('native redo restores the same regex derivative',
                              controls['editor'].GetValue() == regex_text)
                        click(controls['save'])
                        check('saved regex revision retains original source location',
                              store.load(project.id)[0].source_location == page.location)
                        controls['editor'].SetValue(recovered)
                        later_calls.append(wx.CallLater(1800, lambda: safely(dialog, finish)))
                    later_calls.append(wx.CallLater(20, lambda: safely(dialog, regex_finished)))
                safely(dialog, begin)

            def expired():
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        modal_errors.append('Native reading-copy dialog timed out.')
                        window.EndModal(wx.ID_CANCEL)

            watchdog = wx.CallLater(30000, expired)
            try:
                open_narration_editor(frame, store, project, on_ready=ready, dialogs=RegexDialogs())
            finally:
                watchdog.Stop()
                for call in later_calls:
                    call.Stop()
            if modal_errors:
                raise RuntimeError('\n'.join(modal_errors))
            saved, _ = store.load(project.id)
            check('reading copy survives closing and reopening the store',
                  NarrationStore(library.root).load(project.id)[0] == saved and saved.current_text == recovered)
            check('source bytes and authenticated citation remain unchanged',
                  library.assets.resolve(imported['asset']).read_bytes() == original_bytes
                  and library.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote)

            backup = root / 'backup'
            checksum = library.backup(backup)
            restored = root / 'restored'
            restore_backup(backup, restored, expected_manifest_sha256=checksum)
            check('complete backup restores saved reading-copy revisions',
                  NarrationStore(restored).load(project.id)[0] == saved)
            check('complete backup restores page resume and bookmark',
                  ReadingState(restored).resume(workspace, imported['edition']) == page.location
                  and len(ReadingState(restored).bookmarks(workspace, imported['edition'])) == 1)
            check('complete backup restores keyboard preferences',
                  KeymapStore(restored / 'keyboard-shortcuts.json').load()
                  == KeymapStore(library.root / 'keyboard-shortcuts.json').load())
        finally:
            if not frame.IsBeingDeleted():
                frame.Close()
                gui.Yield()
            if native_handle is not None:
                native_handle.close()
    if failures:
        raise RuntimeError('\n'.join(failures))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'count': len(checks), 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
