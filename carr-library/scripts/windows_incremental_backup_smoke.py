"""Installed Windows native backup commands over synthetic local libraries.

The actual command, action/hash/name dialogs, shared worker and readable result
are exercised. System folder selections are supplied as explicit fixture paths.
This is not manual NVDA, removable-media or power-loss qualification.
"""
import argparse
from hashlib import sha256
import importlib
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch


def run_native(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1] / 'src')
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args(argv)
    if not args.native or sys.platform != 'win32':
        raise RuntimeError('The native incremental backup gate requires actual Windows.')
    import wx
    modules = (
        'carr_library.incremental_backup', 'carr_library.incremental_backup_cli',
        'carr_library.sync.vault', 'carr_library.file_publication',
        'carr_library.ui.incremental_backup_dialog',
    )
    origins = {}
    for name in modules:
        module = importlib.import_module(name)
        installed = Path(module.__file__).resolve()
        expected = args.source_root / Path(*name.split('.')).with_suffix('.py')
        if installed.is_relative_to(args.source_root.resolve()) or not installed.is_relative_to(Path(sys.prefix).resolve()):
            raise AssertionError('Native backup probe must import the installed wheel: ' + name)
        checksum = sha256(installed.read_bytes()).hexdigest()
        assert checksum == sha256(expected.read_bytes()).hexdigest(), 'Installed source differs: ' + name
        origins[name] = checksum
    from carr_library.application import LibraryApp
    from carr_library.progress import ProgressPreferences, save_preferences
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import incremental_backup_dialog as ui
    from carr_library.library_backup import verify_backup

    app = wx.App(False)
    result = {'status': 'NOT_RUN', 'checks': [], 'origins': origins, 'manual_nvda': 'NOT_RUN',
              'physical_media': 'NOT_RUN', 'power_loss': 'NOT_RUN',
              'folder_picker': 'fixture selection; system picker not automated'}
    with TemporaryDirectory(prefix='carr-native-incremental-') as folder:
        root = Path(folder)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native incremental backup')
        library.add_text(workspace, 'Перший оригінал', 'Railway original: Україна.')
        save_preferences(library.root, ProgressPreferences(muted=True))
        frame = DesktopFrame(library)
        frame.Show()
        app.Yield()
        try:
            items = [item for index in range(frame.menu_bar.GetMenuCount())
                     for item in frame.menu_bar.GetMenu(index).GetMenuItems()]
            item = next(item for item in items if item.GetItemLabel().replace('&', '').startswith(
                'Incremental backup repository'))
            assert frame.keyboard_commands.registry.get(ui.COMMAND_ID).command_id == ui.COMMAND_ID

            def descendants(parent):
                for child in parent.GetChildren():
                    yield child
                    yield from descendants(child)

            def click(button):
                assert button is not None and button.IsEnabled()
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                event.SetEventObject(button)
                assert button.GetEventHandler().ProcessEvent(event)

            def wait_for(predicate, message):
                deadline = monotonic() + 60
                while not predicate():
                    if monotonic() >= deadline:
                        raise TimeoutError(message)
                    app.Yield()
                    wx.MilliSleep(10)

            def command(action, folders=(), texts=(), *, cancel_action=False):
                paths, values = list(folders), list(texts)
                completed, errors, output, timers = [], [], [], []
                seen_action, seen_texts = [], []
                deadline = monotonic() + 60
                def visit():
                    try:
                        if monotonic() >= deadline:
                            raise TimeoutError('Incremental native command did not complete.')
                        dialogs = [window for window in wx.GetTopLevelWindows()
                                   if isinstance(window, wx.Dialog) and window.IsShown()
                                   and window.GetParent() is frame]
                        if dialogs:
                            dialog = dialogs[-1]
                            if isinstance(dialog, wx.SingleChoiceDialog):
                                if not seen_action:
                                    seen_action.append(True)
                                    assert dialog.GetName() == 'Incremental backup action'
                                    if cancel_action:
                                        click(dialog.FindWindowById(wx.ID_CANCEL))
                                        completed.append(True)
                                    else:
                                        dialog.SetSelection(action)
                                        click(dialog.FindWindowById(wx.ID_OK))
                            elif isinstance(dialog, wx.TextEntryDialog):
                                if dialog.GetId() not in seen_texts:
                                    seen_texts.append(dialog.GetId())
                                    assert values, 'Unexpected native text prompt'
                                    value = values.pop(0)
                                    if value is None:
                                        click(dialog.FindWindowById(wx.ID_CANCEL))
                                        completed.append(True)
                                    else:
                                        dialog.SetValue(value)
                                        click(dialog.FindWindowById(wx.ID_OK))
                            elif dialog.GetTitle() == 'Incremental backup result':
                                readers = [control for control in descendants(dialog)
                                           if isinstance(control, wx.TextCtrl)
                                           and control.GetName() == 'Incremental backup result']
                                assert len(readers) == 1 and not readers[0].IsEditable()
                                assert wx.Window.FindFocus() is readers[0], 'Native result is not focused for reading'
                                output.append(readers[0].GetValue())
                                click(dialog.FindWindowById(wx.ID_CANCEL))
                                completed.append(True)
                        if not completed:
                            timers.append(wx.CallLater(25, visit))
                    except BaseException as exc:
                        errors.append(exc)
                        completed.append(False)
                        frame.cancel_event.set()
                        for dialog in wx.GetTopLevelWindows():
                            if isinstance(dialog, wx.Dialog) and dialog.IsShown() and dialog.IsModal():
                                dialog.EndModal(wx.ID_CANCEL)
                def selected_folder(parent, title):
                    assert parent is frame and paths, 'Unexpected folder selection'
                    return paths.pop(0)
                with patch.object(ui, '_directory', side_effect=selected_folder):
                    timers.append(wx.CallLater(25, visit))
                    event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
                    event.SetEventObject(frame)
                    try:
                        assert frame.GetEventHandler().ProcessEvent(event)
                        wait_for(lambda: bool(completed), 'Native backup modal did not finish')
                        wait_for(lambda: not frame.working, 'Native backup worker did not finish')
                    finally:
                        for timer in timers:
                            timer.Stop()
                if errors:
                    raise errors[0]
                assert completed == [True] and not paths and not values
                return output[0] if output else None

            repository = root / 'Знімки'
            assert command(0, [root], ['Знімки']).startswith('Local backup repository created')
            first_text = command(1, [repository], [''])
            first = re.search(r'Snapshot SHA256: ([0-9a-f]{64})', first_text).group(1)
            library.add_text(workspace, 'Другий оригінал', 'Railway second native revision.')
            second_text = command(1, [repository], [first])
            second = re.search(r'Snapshot SHA256: ([0-9a-f]{64})', second_text).group(1)
            assert first != second and 'Reused chunks:' in second_text
            assert 'were verified' in command(3, [repository], [second])
            listing = command(2, [repository])
            assert first in listing and second in listing and 'not a verification' in listing
            command(4, [repository, root], [second, 'ordinary-full'])
            manifest = sha256((root / 'ordinary-full/manifest.json').read_bytes()).hexdigest()
            assert verify_backup(root / 'ordinary-full', manifest)['version'] in (1, 2, 3, 4, 5)
            command(5, [repository, root], [second, 'restored'])
            recovered = LibraryApp(root / 'restored')
            assert len(recovered.search(workspace, 'Railway')) == 2
            assert frame.library is library, 'Backup restoration silently switched the active library'
            before = (repository / 'head.json').read_bytes()
            assert command(0, cancel_action=True) is None
            assert command(3, [repository], [None]) is None
            assert (repository / 'head.json').read_bytes() == before
            result['checks'] += [
                'shared command registry and real native menu dispatch',
                'actual action and checksum/name dialogs; readable result focus',
                'two canonical snapshots with reused chunks and explicit parent token',
                'native complete verification, ordinary full export and new-root restore',
                'actual original quotations survive restored data',
                'native action/hash cancellation has no new publication',
                'restoration leaves the existing library active',
            ]
        finally:
            frame.cancel_event.set()
            if frame.working:
                wait_for(lambda: not frame.working, 'Closing native backup worker')
            frame.Close()
            app.Yield()
    result['status'] = 'PASS'
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

def main(argv=None):
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    try:
        return run_native(argv)
    finally:
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    main()
