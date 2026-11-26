"""Independent native Main keymap persistence and transaction acceptance.

Run in the existing Windows lane with the exact package installed:
    python -I -B tests/ui/native_main_keymap_acceptance.py --native
An explicit --source carr-library override is for checkout debugging only.

Complements native_keyboard_settings_smoke.py with three scenarios: late-module
reopen persistence, export/reset/import plus concurrent-save rejection, and a
data-root switch while settings are open. Application menu accelerators, Save,
Import/Export/Reset buttons and Escape use wx.UIActionSimulator. The file-picker
destination and text-control contents are supplied by the probe; manual file
picker operation and NVDA are NOT_RUN. Only synthetic research data is used.
"""
from argparse import ArgumentParser
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from unittest.mock import patch


sys.dont_write_bytecode = True


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None, timeout=150):
    if sys.platform not in ('win32', 'darwin') and not os.environ.get('DISPLAY'):
        return not_run('Native key simulation requires an available display; none is configured.')
    if os.environ.get('GDK_BACKEND') == 'wayland':
        return not_run('wx.UIActionSimulator does not support the GTK Wayland backend.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    if not hasattr(wx, 'UIActionSimulator'):
        return not_run('This wxPython build has no UIActionSimulator.')
    if source is not None:
        sys.path[:0] = [str(source / 'src'), str(source)]

    from carr_library.application import LibraryApp
    from carr_library.keyboard_preferences import KeymapStore
    from carr_library.ui.command_registry import CommandSpec
    from carr_library.ui.desktop_app import DesktopFrame

    checks, scenarios, errors = [], [], []
    frame = None
    deadline = monotonic() + timeout
    expected = source or Path(__file__).resolve().parents[2]
    module_names = ('application', 'keyboard_preferences', 'ui.command_registry',
                    'ui.builtin_keyboard_commands', 'ui.keyboard_settings',
                    'ui.desktop_app', 'ui.workspace_shell', 'ui.desktop_evidence_panel')
    origins, hashes = {}, {}
    for name in module_names:
        module = importlib.import_module('carr_library.' + name)
        path = Path(module.__file__).resolve()
        reference = expected / 'src' / 'carr_library' / (name.replace('.', '/') + '.py')
        if not reference.is_file() or path.read_bytes() != reference.read_bytes():
            raise AssertionError('Imported code differs from the exact acceptance source: ' + name)
        if source is None and path.is_relative_to(expected.resolve()):
            raise AssertionError('Installed acceptance imported checkout code: ' + name)
        origins[name], hashes[name] = str(path), sha256(path.read_bytes()).hexdigest()

    def abort(message):
        errors.append(message)
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    try:
        app = ProbeApp(False)
    except (Exception, SystemExit) as exc:
        return not_run('Native application could not open: ' + str(exc))
    simulator = wx.UIActionSimulator()
    watchdog = wx.CallLater(int(timeout * 1000), abort, 'Native acceptance exceeded its deadline.')

    def check(label, value):
        if not value:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def pump(predicate, seconds=5):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            app.Yield()
            if errors:
                raise AssertionError('; '.join(errors))
            if predicate():
                return True
            wx.MilliSleep(10)
        if monotonic() >= deadline:
            raise TimeoutError('Native acceptance exceeded its deadline.')
        return False

    def settle():
        end = monotonic() + 0.2
        pump(lambda: monotonic() >= end, seconds=0.6)

    def press(code, modifiers=wx.MOD_NONE):
        if not simulator.Char(code, modifiers):
            raise RuntimeError('Native keyboard backend rejected input.')
        settle()

    def focus(control):
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() == control):
            raise AssertionError('Focus did not reach ' + control.GetName())

    def button(dialog, caption=None, identifier=None):
        return next(c for c in dialog.GetChildren() if isinstance(c, wx.Button)
                    and (c.GetId() == identifier if identifier is not None
                         else c.GetLabel().replace('&', '') == caption))

    def activate(control):
        if not control.IsEnabled():
            raise AssertionError('Native button is disabled: ' + control.GetLabel())
        focus(control)
        press(wx.WXK_SPACE)

    def controls(dialog):
        return {c.GetName(): c for c in dialog.GetChildren()}

    def settings(operation):
        """Open the real menu action through its actual Main accelerator."""
        done, operation_errors = [], []
        wait_until = monotonic() + 8

        def drive():
            dialogs = [w for w in wx.GetTopLevelWindows() if isinstance(w, wx.Dialog)
                       and w.GetTitle() == 'Keyboard shortcuts' and w.IsModal()]
            if not dialogs:
                if monotonic() < wait_until:
                    wx.CallLater(20, drive)
                    return
                operation_errors.append(AssertionError('Actual shortcut did not open Keyboard shortcuts.'))
                done.append(True)
                return
            dialog = dialogs[0]
            try:
                fields = controls(dialog)
                fields['Filter keyboard commands'].SetValue('Acceptance late module')
                if not pump(lambda: fields['Commands and their current shortcuts'].GetCount() == 1):
                    raise AssertionError('The native command filter did not select the registered module.')
                operation(dialog, fields)
            except BaseException as exc:
                operation_errors.append(exc)
            finally:
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)
                done.append(True)

        wx.CallLater(25, drive)
        frame.Raise()
        focus(frame.evidence.note)
        press(ord('K'), wx.MOD_CONTROL | wx.MOD_ALT)
        if not pump(lambda: bool(done), seconds=10):
            raise AssertionError('Native settings interaction did not finish.')
        if operation_errors:
            raise operation_errors[0]
        settle()

    def chooser(path):
        class Destination:
            def __init__(self, *args, **kwargs):
                pass
            def ShowModal(self):
                return wx.ID_OK
            def GetPath(self):
                return str(path)
            def Destroy(self):
                pass
        return patch.object(wx, 'FileDialog', Destination)

    def destroy_frame():
        nonlocal frame
        if frame is not None:
            frame.cancel_event.set()
            frame._closing = True
            frame.executor.shutdown(wait=False, cancel_futures=True)
            frame.Destroy()
            frame = None
            app.Yield()

    try:
        with TemporaryDirectory(prefix='carr-native-keymap-transaction-', ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            library = LibraryApp(root / 'library-A-Україна')
            workspace = library.create_workspace('Synthetic keymap library')
            topic = library.create_topic(workspace, 'Synthetic keymap topic')
            imported = library.add_text(workspace, 'Synthetic source', 'Exact keyboard acceptance source.')
            hit, = library.search(workspace, 'acceptance source')
            saved_note, draft = 'Saved note.', 'Unsaved exact draft — Україна.'
            evidence = library.save_hit(workspace, topic, hit.ref, hit.revision, saved_note)
            original = library.assets.resolve(imported['asset']).read_bytes()
            calls = []
            command_id = 'acceptance.late_module'
            caption = 'Acceptance late module'

            def open_frame():
                nonlocal frame
                frame = DesktopFrame(LibraryApp(library.root))
                frame._open_workspace(workspace, topic)
                frame.SetSize((1100, 900))
                app.SetTopWindow(frame)
                frame.Show()
                frame.Raise()
                if not pump(lambda: frame.evidence is not None and frame.evidence.displayed is not None
                            and frame.evidence.displayed.evidence_id == evidence):
                    raise AssertionError('The real evidence view did not load.')
                menu = wx.Menu()
                item = menu.Append(wx.ID_ANY, caption + '\tCtrl+Shift+J')
                frame.GetMenuBar().Append(menu, '&Acceptance')
                frame.Bind(wx.EVT_MENU, lambda event: calls.append(event.GetId()), item)
                manager = frame.keyboard_commands
                manager.register_menu_command(CommandSpec(command_id, caption, 'main', 'Ctrl+Shift+J'), item)
                frame.evidence.note.SetValue(draft)
                focus(frame.evidence.note)
                return manager, item

            def intact():
                return (frame.evidence.note.GetValue() == draft and frame._dirty()
                        and library.evidence(workspace, topic)[0].note == saved_note
                        and library.assets.resolve(imported['asset']).read_bytes() == original)

            def host_key(letter):
                frame.Raise()
                focus(frame.evidence.note)
                press(ord(letter), wx.MOD_CONTROL | wx.MOD_SHIFT)

            manager, item = open_frame()
            keymap = manager.store.path
            shortcut_name = 'Shortcut for selected command; leave empty to unassign'

            def remap(dialog, fields):
                fields[shortcut_name].SetValue('Ctrl+Shift+K')
                activate(button(dialog, identifier=wx.ID_OK))

            settings(remap)
            before = keymap.read_bytes()
            check('native Save preserves the complete unsaved note and exact focus',
                  intact() and wx.Window.FindFocus() == frame.evidence.note)
            # Reopen a deliberately clean fixture. Draft recovery has its own
            # acceptance lane and would otherwise open an unrelated chooser.
            frame.evidence.note.ChangeValue(saved_note)
            if hasattr(frame.evidence, 'discard_recovery'):
                check('synthetic reopen fixture clears only its own recovery copy',
                      frame.evidence.discard_recovery())
            destroy_frame()
            manager, item = open_frame()
            check('fresh frame recovers a late module by stable ID without rewriting its keymap',
                  keymap.read_bytes() == before and manager.shortcut(command_id) == 'Ctrl+Shift+K'
                  and item.GetItemLabel().endswith('\tCtrl+Shift+K') and not manager.load_notice)
            host_key('J')
            check('reopened frame does not dispatch the old default', not calls)
            host_key('K')
            check('reopened frame dispatches the persisted native accelerator exactly once',
                  calls == [item.GetId()])
            count = frame.GetMenuBar().GetMenuCount()
            manager.refresh()
            manager.refresh()
            host_key('K')
            check('menu refresh retains the existing plugin, handler, binding and draft',
                  len(calls) == 2 and frame.GetMenuBar().GetMenuCount() == count and intact())
            scenarios.append('fresh_frame_late_module_persistence')

            exported = root / 'exported-keymap.json'

            def export(dialog, fields):
                with chooser(exported):
                    activate(button(dialog, caption='Export keymap...'))
                check('native Export writes the validated draft without changing live settings',
                      KeymapStore(exported).load() == manager.overrides and keymap.read_bytes() == before)
                press(wx.WXK_ESCAPE)

            settings(export)

            def reset(dialog, fields):
                activate(button(dialog, caption='Reset all defaults'))
                activate(button(dialog, identifier=wx.ID_OK))

            settings(reset)
            check('native Reset restores the module default', manager.shortcut(command_id) == 'Ctrl+Shift+J')

            def restore(dialog, fields):
                with chooser(exported):
                    activate(button(dialog, caption='Import keymap...'))
                check('native Import remains a draft until Save',
                      fields[shortcut_name].GetValue() == 'Ctrl+Shift+K'
                      and manager.shortcut(command_id) == 'Ctrl+Shift+J')
                activate(button(dialog, identifier=wx.ID_OK))

            settings(restore)
            previous_calls = len(calls)
            host_key('J')
            host_key('K')
            check('export-reset-import restores real dispatch and the exact unsaved note',
                  len(calls) == previous_calls + 1 and intact())
            external_bytes = []

            def concurrent_save(dialog, fields):
                external = KeymapStore(keymap)
                external.load()
                external.save({command_id: 'Ctrl+Shift+H'}, manager.registry)
                external_bytes.append(keymap.read_bytes())
                fields[shortcut_name].SetValue('Ctrl+Shift+U')
                activate(button(dialog, identifier=wx.ID_OK))
                check('concurrent native Save stays open and preserves the other writer and live binding',
                      dialog.IsModal() and 'another window' in fields['Keyboard settings status'].GetValue()
                      and keymap.read_bytes() == external_bytes[0]
                      and manager.shortcut(command_id) == 'Ctrl+Shift+K'
                      and item.GetItemLabel().endswith('\tCtrl+Shift+K'))
                press(wx.WXK_ESCAPE)

            settings(concurrent_save)
            previous_calls = len(calls)
            host_key('U')
            host_key('K')
            check('failed compare-and-save rolls back the native accelerator and restores note focus',
                  len(calls) == previous_calls + 1 and intact()
                  and wx.Window.FindFocus() == frame.evidence.note)
            scenarios.append('native_export_import_and_concurrent_save')

            other = LibraryApp(root / 'library-B')
            other_workspace = other.create_workspace('Another synthetic library')
            other_topic = other.create_topic(other_workspace, 'Another synthetic topic')
            other.add_text(other_workspace, 'Other source', 'Other exact source.')
            other_hit, = other.search(other_workspace, 'exact source')
            other.save_hit(other_workspace, other_topic, other_hit.ref, other_hit.revision, 'Other saved note.')
            other_path = other.root / 'keyboard-shortcuts.json'
            KeymapStore(other_path).save({command_id: 'Ctrl+Shift+U'}, manager.registry)
            other_bytes = other_path.read_bytes()
            # This scenario deliberately leaves a clean note before changing
            # roots. Earlier settings scenarios already proved draft retention.
            frame.evidence.note.ChangeValue(saved_note)
            check('root-switch fixture has no unsaved draft to discard', not frame._dirty())

            def stale_root(dialog, fields):
                fields[shortcut_name].SetValue('Ctrl+Shift+J')
                check('production data-root transition succeeds', frame._open_data_root(other.root))
                frame._open_workspace(other_workspace, other_topic)
                activate(button(dialog, identifier=wx.ID_OK))
                check('old settings cannot save into the new data root',
                      dialog.IsModal() and 'folder changed' in fields['Keyboard settings status'].GetValue()
                      and keymap.read_bytes() == external_bytes[0] and other_path.read_bytes() == other_bytes
                      and manager.store.path == other_path and manager.shortcut(command_id) == 'Ctrl+Shift+U')
                press(wx.WXK_ESCAPE)

            settings(stale_root)
            if not pump(lambda: frame.evidence is not None and frame.evidence.displayed is not None):
                raise AssertionError('The new root evidence view did not load.')
            previous_calls = len(calls)
            host_key('K')
            host_key('U')
            check('new root owns actual keyboard dispatch without altering either saved profile',
                  len(calls) == previous_calls + 1 and keymap.read_bytes() == external_bytes[0]
                  and other_path.read_bytes() == other_bytes
                  and library.evidence(workspace, topic)[0].note == saved_note)
            scenarios.append('stale_settings_data_root_change')
            check('exact imported module bytes remained unchanged throughout acceptance',
                  all(sha256(Path(origins[n]).read_bytes()).hexdigest() == hashes[n] for n in hashes))
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'scenarios': scenarios,
                              'platform': platform.system(), 'wx': wx.version(), 'origins': origins,
                              'module_sha256': hashes, 'installed_package': source is None,
                              'source_override': str(source) if source is not None else None,
                              'native_key_backend': 'wx.UIActionSimulator',
                              'file_picker': 'controlled destination callback; manual chooser NOT_RUN',
                              'text_entry': 'native control values supplied by probe',
                              'data_root_transition': 'production method during open native settings',
                              'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
            return 0
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'checks_passed': len(checks), 'scenarios': scenarios,
                          'error': str(exc), 'nvda': 'NOT_RUN'}), flush=True)
        traceback.print_exc()
        return 1
    finally:
        watchdog.Stop()
        destroy_frame()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', required=True)
    parser.add_argument('--source', type=Path, help='Explicit source override, never installed-package evidence.')
    parser.add_argument('--timeout', type=float, default=150)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 170:
        parser.error('--timeout must be between 10 and 170 seconds.')
    return run_native(args.source.resolve() if args.source is not None else None, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
