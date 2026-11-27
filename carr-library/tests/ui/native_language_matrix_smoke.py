"""Installed native canonical-language view and current local inventory checks.

Run: python -I tests/ui/native_language_matrix_smoke.py --native
Use --source path/to/carr-library only for an explicitly labelled source run.
No speech synthesis, OCR recognition, downloads, private documents or NVDA.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None, timeout=150):
    if sys.platform not in ('win32', 'darwin') and not os.environ.get('DISPLAY'):
        return not_run('A native display is unavailable.')
    if os.environ.get('GDK_BACKEND') == 'wayland':
        return not_run('wx.UIActionSimulator requires a supported native key backend.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    if not hasattr(wx, 'UIActionSimulator'):
        return not_run('The installed wx build has no native key simulator.')
    if source is not None:
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / 'src'))

    import carr_library.language_qualification.catalog as catalog_module
    import carr_library.language_qualification.runtime_inventory as inventory_module
    import carr_library.ui.language_matrix as matrix_module
    import carr_library.ui.desktop_app as desktop_module
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame

    checks, failures, opened, gates = [], [], [], []
    observations = {}
    deadline = monotonic() + timeout
    frame = None
    phase = None

    def abort(reason):
        failures.append(reason)
        for gate in gates:
            gate.set()
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal() and window.IsShown():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    try:
        application = ProbeApp(False)
    except (Exception, SystemExit) as exc:
        return not_run('The native wx application could not start: ' + str(exc))
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    simulator = wx.UIActionSimulator()
    watchdog = wx.CallLater(int(timeout * 1000), abort, 'Native language view exceeded its deadline.')

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def healthy():
        if failures:
            raise AssertionError('; '.join(failures))
        if monotonic() >= deadline:
            raise TimeoutError('Native language view exceeded its deadline.')

    def pump(predicate, seconds=10):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            application.Yield()
            healthy()
            if predicate():
                return True
            wx.MilliSleep(10)
        healthy()
        return False

    def settle():
        end = min(deadline, monotonic() + .15)
        while monotonic() < end:
            application.Yield()
            healthy()
            wx.MilliSleep(10)

    def press(key, modifiers=wx.MOD_NONE):
        if not simulator.Char(key, modifiers):
            raise RuntimeError('Native key injection is unavailable.')
        settle()

    def focus(control):
        control.SetFocus()
        check('native focus reaches ' + control.GetName(),
              pump(lambda: wx.Window.FindFocus() is control, seconds=3))

    def activate(control):
        check('native action is enabled: ' + control.GetName(), control.IsEnabled())
        focus(control)
        press(wx.WXK_SPACE)

    original_show = matrix_module.show_language_matrix
    original_collect = matrix_module.collect_installed_voices
    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in
               (catalog_module, inventory_module, matrix_module, desktop_module)}
    checkout = Path(__file__).resolve().parents[2]

    def observed_show(parent):
        def ready(dialog):
            opened.append(dialog)
            try:
                phase(dialog)
                if dialog._window_alive and dialog.IsShown():
                    raise AssertionError('The native language phase did not close its window.')
            except BaseException:
                failures.append(traceback.format_exc())
            finally:
                for gate in gates:
                    gate.set()
                if dialog._window_alive and dialog.IsModal() and dialog.IsShown():
                    dialog.EndModal(wx.ID_CANCEL)
        return original_show(parent, on_ready=ready)

    matrix_module.show_language_matrix = observed_show
    with TemporaryDirectory(prefix='carr-native-languages-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic language view scope')
        imported = library.add_text(workspace, 'Synthetic source', 'Незмінний український текст.')
        other = LibraryApp(root / 'other')
        source_path = library.assets.resolve(imported['asset'])
        source_hash = sha256(source_path.read_bytes()).hexdigest()

        def close_keyboard(dialog):
            press(wx.WXK_ESCAPE)
            check('native Escape closes the evidence view', not dialog.IsShown())

        def view_and_inventory(dialog):
            check('native language view has named controls and read-only details',
                  dialog.IsModal() and dialog.search.GetName() == 'Search selected languages or countries'
                  and not dialog.details.IsEditable() and not dialog.inventory.IsEditable()
                  and not dialog.ocr_inventory.IsEditable())
            check('initial native view derives exactly 135 source entries without discovery',
                  dialog.languages.GetCount() == 135 and dialog.controller.inventory is None
                  and dialog.controller.ocr_inventory is None)
            focus(dialog.search)
            dialog.search.SetValue('Українська')
            check('native Unicode filtering retains the exact selected source row',
                  pump(lambda: len(dialog.rows) == 1)
                  and dialog.rows[0].language.label_uk == 'Українська'
                  and dialog.rows[0].language.associations[0].source_text in dialog.details.GetValue())
            check('filter updates preserve the current native input focus', wx.Window.FindFocus() is dialog.search)
            press(wx.WXK_TAB)
            check('native Tab moves away from the search control', wx.Window.FindFocus() is not dialog.search)
            dialog.search.SetValue('')
            focus(dialog.regions)
            press(wx.WXK_HOME)
            press(wx.WXK_DOWN)
            check('native region choice preserves the 37 canonical European memberships',
                  pump(lambda: len(dialog.rows) == 37))
            press(wx.WXK_HOME)
            check('native All regions restores the 135 unique entries', pump(lambda: len(dialog.rows) == 135))
            activate(dialog.refresh_button)
            check('actual configured voice enumeration finishes through the frame worker',
                  pump(lambda: not dialog.pending, seconds=55) and dialog.controller.inventory is not None)
            voices = dialog.controller.inventory.voices
            observations['voice_count'] = len(voices)
            observations['voice_engines'] = sorted({voice.engine for voice in voices})
            check('enumerated voices remain separate from language and acoustic qualification',
                  dialog.controller.matrix.summary()['qualified_entries'] == 0
                  and dialog.controller.matrix.summary()['entries_with_declared_voice_matches'] == 0
                  and dialog.controller.matrix.summary()['unmatched_voices'] == len(voices))
            activate(dialog.ocr_button)
            check('explicit OCR scan returns or reports its optional capability failure',
                  pump(lambda: not dialog.pending, seconds=35))
            if dialog.controller.ocr_inventory is None:
                observations['ocr_inventory'] = 'UNAVAILABLE'
                check('unavailable OCR does not invent an inventory',
                      'not updated' in dialog.status.GetValue() and not dialog.controller.ocr_inventory)
            else:
                observations['ocr_inventory'] = list(dialog.controller.ocr_inventory.identifiers)
                check('OCR model inventory does not certify language recognition',
                      all(row.capability('ocr').status == 'untested' for row in dialog.controller.filter()))
            close_keyboard(dialog)

        def held_scan(dialog):
            entered, release = Event(), Event()
            gates.append(release)
            def collect(*args, **kwargs):
                value = original_collect(*args, **kwargs)
                entered.set()
                if not release.wait(min(20, max(.1, deadline - monotonic()))):
                    raise TimeoutError('Held real inventory was not released.')
                return value
            matrix_module.collect_installed_voices = collect
            activate(dialog.refresh_button)
            check('actual inventory has completed before publication is held', pump(entered.is_set, seconds=55))
            return release

        def cancel_scan(dialog):
            release = held_scan(dialog)
            activate(dialog.cancel_button)
            release.set()
            check('native Cancel discards the late inventory and reenables refresh',
                  pump(lambda: not dialog.pending) and dialog.refresh_button.IsEnabled()
                  and dialog.controller.inventory is None)
            check('resource cancellation does not set another operation cancellation token',
                  not frame.cancel_event.is_set())
            close_keyboard(dialog)

        def destroy_scan(dialog):
            release = held_scan(dialog)
            dialog.Destroy()
            check('forced native destruction invalidates the child lifetime',
                  pump(lambda: not dialog._window_alive))
            release.set()
            check('destroyed view cannot publish an actual late inventory',
                  pump(lambda: not frame.working) and dialog.controller.inventory is None)

        def change_root(dialog):
            release = held_scan(dialog)
            try:
                frame.library = other
                release.set()
                check('root change discards the captured inventory',
                      pump(lambda: not frame.working) and dialog.controller.inventory is None)
                close_keyboard(dialog)
            finally:
                frame.library = library

        try:
            if source is None:
                check('language modules are loaded from the installed package',
                      all(not Path(path).is_relative_to(checkout) for path in origins.values()))
            frame = DesktopFrame(library)
            frame.Show()
            frame.Raise()
            check('Main exposes the stable language command in its existing registry',
                  'language.capabilities' in frame.keyboard_commands.bindings)
            # Only this temporary fixture profile changes. The product does not
            # reserve another shortcut or install a second accelerator table.
            overrides = dict(frame.keyboard_commands.overrides)
            chosen = None
            for shortcut, key, modifiers in (
                ('Ctrl+Alt+L', ord('L'), wx.MOD_CONTROL | wx.MOD_ALT),
                ('Ctrl+Alt+Shift+9', ord('9'), wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT),
            ):
                try:
                    proposed = {**overrides, 'language.capabilities': shortcut}
                    frame.keyboard_commands.validate(proposed)
                except ValueError:
                    continue
                frame.keyboard_commands.apply_overrides(proposed)
                chosen = key, modifiers
                break
            check('native acceptance uses one valid temporary registry shortcut', chosen is not None)
            for phase in (view_and_inventory, cancel_scan, destroy_scan, change_root):
                matrix_module.collect_installed_voices = original_collect
                previous = len(opened)
                frame.Raise()
                focus(frame.choice)
                press(*chosen)
                check('the actual shared Main shortcut opens exactly one language view',
                      pump(lambda: len(opened) == previous + 1))
                check('the current worker settles before another view opens', pump(lambda: not frame.working))
            check('all native language operations preserve exact source bytes',
                  sha256(source_path.read_bytes()).hexdigest() == source_hash)
            healthy()
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                'platform': platform.system(), 'origins': origins, 'observations': observations,
                'source_override': str(source) if source else None,
                'native_key_backend': 'wx.UIActionSimulator', 'qualified_languages': 0,
                'speech_synthesis': 'NOT_RUN', 'ocr_recognition': 'NOT_RUN', 'nvda': 'NOT_RUN'},
                sort_keys=True), flush=True)
            return 0
        except Exception as exc:
            print(json.dumps({'status': 'FAIL', 'checks_passed': len(checks), 'error': str(exc),
                              'nvda': 'NOT_RUN'}), flush=True)
            traceback.print_exc()
            return 1
        finally:
            matrix_module.show_language_matrix = original_show
            matrix_module.collect_installed_voices = original_collect
            watchdog.Stop()
            for gate in gates:
                gate.set()
            if frame is not None:
                frame.library = library
                frame.cancel_event.set()
                frame._closing = True
                frame.executor.shutdown(wait=False, cancel_futures=True)
                frame.Destroy()
                application.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--timeout', type=float, default=150)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 240:
        parser.error('--timeout must be between 10 and 240 seconds')
    return run_native(args.source.resolve() if args.source else None, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
