"""Exercise integrated native save/shortcut controls and reading-state recovery.

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
    from carr_library.keybindings import KeybindingStore
    from carr_library.library_backup import restore_backup
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.ui.desktop_app import DesktopFrame

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

    from carr_library.ui import productivity
    check('productivity loads from the installed wheel', all(
        'site-packages' in [part.casefold() for part in Path(module.__file__).parts]
        for module in (productivity,)))
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
            keyboard = frame.keyboard_actions
            save_binding = next(binding for binding in keyboard.bindings
                                if binding.command.identity == 'File/Save current changes')
            check('main Save exposes Ctrl+S', save_binding.item.GetItemLabel().endswith('\tCtrl+S'))
            frame.evidence.note.SetValue('Saved through the native File command.')
            event = wx.CommandEvent(wx.EVT_MENU.typeId, save_binding.item.GetId())
            check('native File Save dispatches', frame.GetEventHandler().ProcessEvent(event))
            pump(lambda: not frame.working)
            check('File Save commits the selected evidence note',
                  library.evidence(workspace, topic)[0].note == 'Saved through the native File command.')
            remapped_save = 'Ctrl+Alt+Shift+S'
            check('remap fixture is unused in the actual native command inventory',
                  all(binding.command.shortcut.casefold() != remapped_save.casefold()
                      for binding in keyboard.bindings))
            keyboard.save({save_binding.command.identity: remapped_save})
            check('shortcut change updates native menu',
                  save_binding.item.GetItemLabel().endswith('\t' + remapped_save))
            check('shortcut persists after reopening preferences',
                  KeybindingStore(library.root).load()[save_binding.command.identity] == remapped_save)
            old_labels = tuple(binding.item.GetItemLabel() for binding in keyboard.bindings)
            old_preferences = KeybindingStore(library.root).path.read_bytes()
            for occupied in ('Ctrl+Alt+S', 'Ctrl+I'):
                check('collision fixture belongs to another native command: ' + occupied,
                      any(binding.command.identity != save_binding.command.identity
                          and binding.command.shortcut.casefold() == occupied.casefold()
                          for binding in keyboard.bindings))
                try:
                    keyboard.save({save_binding.command.identity: occupied})
                except ValueError:
                    pass
                else:
                    raise AssertionError('Conflicting shortcut was accepted: ' + occupied)
                check('conflicting key leaves native menu unchanged: ' + occupied,
                      tuple(binding.item.GetItemLabel() for binding in keyboard.bindings) == old_labels)
                check('conflicting key leaves saved preferences unchanged: ' + occupied,
                      KeybindingStore(library.root).path.read_bytes() == old_preferences)

            desk = ReadingDesk(library, workspace, imported['edition'])
            page = desk.page(1)
            state = ReadingState(library.root)
            state.remember(page.location)
            state.add_bookmark(page.location, 'Synthetic start')
            check('source bytes and authenticated citation remain unchanged',
                  library.assets.resolve(imported['asset']).read_bytes() == original_bytes
                  and library.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote)

            backup = root / 'backup'
            checksum = library.backup(backup)
            restored = root / 'restored'
            restore_backup(backup, restored, expected_manifest_sha256=checksum)
            check('complete backup restores page resume and bookmark',
                  ReadingState(restored).resume(workspace, imported['edition']) == page.location
                  and len(ReadingState(restored).bookmarks(workspace, imported['edition'])) == 1)
            check('complete backup restores keyboard preferences',
                  KeybindingStore(restored).load() == KeybindingStore(library.root).load())
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
