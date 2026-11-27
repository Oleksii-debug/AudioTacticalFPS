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
    check('productivity and editor load from the installed wheel', all(
        'site-packages' in [part.casefold() for part in Path(module.__file__).parts]
        for module in (productivity, narration_editor)))
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
            recovered = changed + '\nAutosaved recovery sentence.'
            modal_errors, later_calls = [], []

            def safely(dialog, action):
                try:
                    action()
                except Exception:
                    modal_errors.append(traceback.format_exc())
                    if dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def ready(dialog, controller, controls):
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
                    controls['editor'].SetValue(recovered)

                    def finish():
                        draft, _ = store.load(project.id, draft=True)
                        check('native timer publishes a recoverable draft', draft.current_text == recovered)
                        check('autosave does not silently replace saved version',
                              store.load(project.id)[0].current_text == changed)
                        click(controls['save'])
                        click(controls['close'])

                    later_calls.append(wx.CallLater(1800, lambda: safely(dialog, finish)))
                safely(dialog, begin)

            def expired():
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        modal_errors.append('Native reading-copy dialog timed out.')
                        window.EndModal(wx.ID_CANCEL)

            watchdog = wx.CallLater(15000, expired)
            try:
                open_narration_editor(frame, store, project, on_ready=ready)
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
                  KeymapStore(restored / 'keyboard-shortcuts.json').load() ==
                  KeymapStore(library.root / 'keyboard-shortcuts.json').load())
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
