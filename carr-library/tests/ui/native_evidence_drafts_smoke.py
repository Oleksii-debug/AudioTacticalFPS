"""Installed native wx note recovery, real controls/files and keyboard selection.

Synthetic notes only. Missing wx/display is NOT_RUN (77), not success. This does
not establish NVDA, power-failure or inaccessible-volume acceptance.
Run: python -I tests/ui/native_evidence_drafts_smoke.py --native
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic


def not_run(reason):
    print(json.dumps(dict(status='NOT_RUN', reason=reason, nvda='NOT_RUN')), flush=True)
    return 77


def run_native(source=None):
    if sys.platform not in ('win32', 'darwin') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('No native display is available.')
    if source is not None:
        sys.path[:0] = [str(source / 'src'), str(source)]
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    if source is not None:
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / 'src'))
    import carr_library.evidence_drafts as draft_module
    import carr_library.ui.desktop_evidence_panel as ui_module
    from carr_library.application import LibraryApp
    from carr_library.evidence_drafts import EvidenceDrafts
    from carr_library.ui.desktop_evidence_panel import DesktopEvidencePanel

    try:
        app = wx.App(False)
    except (Exception, SystemExit) as exc:
        return not_run('Native application could not open: ' + str(exc))
    checks, errors = [], []
    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in (draft_module, ui_module)}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    if source is None:
        checkout = Path(__file__).resolve().parents[2]
        check('recovery code is loaded from the installed package',
              all(not Path(origin).is_relative_to(checkout) for origin in origins.values()))

    def pump(predicate, timeout=8):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            app.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    class Host(wx.Frame):
        def __init__(self, library, workspace, topic):
            super().__init__(None, title='Native research draft acceptance', size=(800, 700))
            self.library = library
            self.active = workspace, topic
            self.evidence = None
            self.working = self._closing = False
            self.cancel_event = Event()
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.layout = wx.BoxSizer(wx.VERTICAL)
            self.SetSizer(self.layout)
        def run_job(self, title, operation, done=None, failed=None):
            self.working = True
            future = self.executor.submit(operation)
            def complete(_):
                def apply():
                    self.working = False
                    try:
                        value = future.result()
                    except Exception as error:
                        if failed:
                            failed(error)
                        else:
                            errors.append(str(error))
                    else:
                        if done:
                            done(value)
                wx.CallAfter(apply)
            future.add_done_callback(complete)
        def report(self, message):
            errors.append(message)
        def open_panel(self):
            workspace, topic = self.active
            panel = DesktopEvidencePanel(self, self, workspace, topic,
                                         lambda: self.library.evidence_transaction(workspace))
            self.evidence = panel
            self.layout.Add(panel, 1, wx.EXPAND)
            self.Layout()
            return panel

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def choose_recovery_with_keyboard():
        """Drive the actual native modal chooser, never replace the dialog API."""
        deadline = monotonic() + 5
        def watchdog():
            for window in wx.GetTopLevelWindows():
                if (isinstance(window, wx.Dialog) and window.IsModal()
                        and window.GetTitle() == 'Recover research note'):
                    errors.append('Native keyboard confirmation did not close the recovery dialog.')
                    window.EndModal(wx.ID_CANCEL)
        wx.CallLater(7000, watchdog)
        def attempt():
            dialogs = [window for window in wx.GetTopLevelWindows()
                       if isinstance(window, wx.Dialog) and window.IsModal()
                       and window.GetTitle() == 'Recover research note']
            if dialogs:
                dialog = dialogs[0]
                try:
                    choice = next(child for child in descendants(dialog)
                                  if isinstance(child, wx.ListBox))
                    dialog.Raise()
                    choice.SetFocus()
                    def press():
                        try:
                            simulator = wx.UIActionSimulator()
                            if not simulator.Select('Recover unsaved draft'):
                                raise RuntimeError('Native keyboard selection could not choose Recover.')
                            if not simulator.Char(wx.WXK_RETURN):
                                raise RuntimeError('Native Enter key could not be delivered.')
                        except Exception as exc:
                            errors.append(str(exc))
                            if dialog.IsModal():
                                dialog.EndModal(wx.ID_CANCEL)
                    wx.CallLater(75, press)
                except Exception as exc:
                    errors.append(str(exc))
                    dialog.EndModal(wx.ID_CANCEL)
            elif monotonic() < deadline:
                wx.CallLater(30, attempt)
            else:
                errors.append('Native recovery dialog did not appear.')
        wx.CallLater(30, attempt)

    with TemporaryDirectory(prefix='carr-native-draft-') as temporary:
        library = LibraryApp(Path(temporary) / 'library')
        workspace = library.create_workspace('Synthetic recovery library')
        topic = library.create_topic(workspace, 'Research note recovery')
        library.add_text(workspace, 'Synthetic source', 'Exact authenticated source. Україна.')
        hit, = library.search(workspace, 'Exact')
        evidence = library.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved baseline')
        store = EvidenceDrafts(library.root)
        frame = Host(library, workspace, topic)
        app.SetTopWindow(frame)
        panel = frame.open_panel()
        frame.Show()
        frame.Raise()
        try:
            check('native evidence asynchronously loads its authorized source',
                  pump(lambda: panel.displayed is not None and not frame.working))
            check('note is a named native editable text control',
                  isinstance(panel.note, wx.TextCtrl) and panel.note.IsEditable()
                  and panel.note.GetName() == 'Research note (editable)')
            check('recovery is available as a named native button', any(
                isinstance(child, wx.Button) and child.GetLabel() == 'Recover research note draft...'
                for child in panel.GetChildren()))
            panel.note.SetValue('First native edit — Україна 🚂')
            check('native text event publishes its first recovery checkpoint',
                  pump(lambda: store.get(workspace, topic, evidence) is not None))
            panel.note.SetValue('Final pending edit — Slovenská poznámka 🚂')
            frame.layout.Detach(panel)
            frame.evidence = None
            panel.Destroy()
            check('destroy flushes pending native edit to the actual sidecar', pump(lambda:
                store.get(workspace, topic, evidence).text == 'Final pending edit — Slovenská poznámka 🚂'))
            check('recovery checkpoint does not commit research note',
                  library.evidence(workspace, topic)[0].note == 'Saved baseline')

            choose_recovery_with_keyboard()
            restored = frame.open_panel()
            check('keyboard recovery restores exact text into reopened native editor',
                  pump(lambda: restored.note.GetValue() == 'Final pending edit — Slovenská poznámka 🚂'))
            check('recovery returns keyboard focus to the note',
                  pump(lambda: wx.Window.FindFocus() == restored.note))
            check('restored draft remains unsaved until explicit Save',
                  restored._dirty() and library.evidence(workspace, topic)[0].note == 'Saved baseline')
            button = restored.save_button
            button.SetFocus()
            check('Save note receives actual keyboard focus', pump(lambda: wx.Window.FindFocus() == button))
            check('native Space key activates Save note', wx.UIActionSimulator().Char(wx.WXK_SPACE))
            check('native save commits note and removes its recovery copy', pump(lambda:
                not frame.working and store.get(workspace, topic, evidence) is None
                and library.evidence(workspace, topic)[0].note == 'Final pending edit — Slovenská poznámka 🚂'))
            check('no native background or keyboard error occurred', not errors)
            print(json.dumps(dict(status='PASS', checks=len(checks), wx=wx.version(),
                                  platform=platform.system(), origins=origins, nvda='NOT_RUN',
                                  source_override=str(source) if source is not None else None), sort_keys=True), flush=True)
        finally:
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            frame.Destroy()
            app.Yield()
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path, help='Explicit checkout override for local debugging only.')
    args = parser.parse_args()
    return run_native(args.source.resolve() if args.source else None)


if __name__ == '__main__':
    raise SystemExit(main())
