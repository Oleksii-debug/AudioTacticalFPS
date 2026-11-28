"""Installed native wx Save All and evidence recovery with real durable writes.

Synthetic notes only. Missing wx/display is NOT_RUN (77), not success. This does
not establish NVDA, power-failure or inaccessible-volume acceptance.
Run: python -I scripts/windows_save_all_recovery_smoke.py --native --report result.json
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import faulthandler
import json
import os
from pathlib import Path
import platform
import sys
import traceback
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic


def not_run(reason):
    return dict(status='NOT_RUN', reason=reason, nvda='NOT_RUN')


def run_native(source=None):
    if sys.platform not in ('win32', 'darwin') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('No native display is available.')
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
    from carr_library.ui import audio_studio, save_actions, speech_dialog
    from carr_library.ui.save_actions import install_save_coordinator
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.audio.voices import Voice

    try:
        app = wx.App(False)
    except (Exception, SystemExit) as exc:
        return not_run('Native application could not open: ' + str(exc))
    # App.Yield alone does not dispatch MSW input without an active event loop.
    native_loop = wx.GUIEventLoop()
    loop_activation = wx.EventLoopActivator(native_loop)
    checks, errors = [], []
    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in
               (draft_module, ui_module, audio_studio, save_actions, speech_dialog)}

    def check(name, condition):
        if not condition:
            raise AssertionError(name + (': ' + '; '.join(errors) if errors else ''))
        checks.append(name)
        print('PASS:', name, flush=True)

    if source is None:
        checkout = Path(__file__).resolve().parents[1]
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
            self.working = self.busy = self._closing = False
            self.hold_completions = False
            self.pending_ui, self.messages = [], []
            self.cancel_event = Event()
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.layout = wx.BoxSizer(wx.VERTICAL)
            self.SetSizer(self.layout)
            install_save_coordinator(self)
        def _dirty(self):
            return self.evidence is not None and self.evidence._dirty()
        def _restore_note_focus(self):
            if self.evidence is not None:
                self.evidence.note.SetFocus()
        def release_completions(self):
            self.hold_completions = False
            pending, self.pending_ui = self.pending_ui, []
            for apply in pending:
                wx.CallAfter(apply)
        def run_job(self, title, operation, done=None, failed=None):
            if self.working:
                raise RuntimeError('A native probe operation already owns this host.')
            self.working = self.busy = True
            self.cancel_event.clear()
            future = self.executor.submit(operation)
            def complete(_):
                def apply():
                    self.working = self.busy = False
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
                if self.hold_completions:
                    self.pending_ui.append(apply)
                else:
                    wx.CallAfter(apply)
            future.add_done_callback(complete)
        def report(self, message):
            self.messages.append(str(message))
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

    def find_button(window, label):
        return next(child for child in descendants(window)
                    if isinstance(child, wx.Button) and child.GetLabel().replace('&', '') == label)

    def press(control):
        control.SetFocus()
        check('native keyboard focus reaches ' + control.GetLabel().replace('&', ''),
              pump(lambda: wx.Window.FindFocus() == control))
        check('native Space activates ' + control.GetLabel().replace('&', ''),
              wx.UIActionSimulator().Char(wx.WXK_SPACE))

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
            # This registry declares one fixture identity for project parsing.
            # No synthesizer, neural model, or listening-quality result is faked.
            registry = SimpleNamespace(list_voices=lambda: (
                Voice('flite:kal', 'flite', 'Synthetic project voice; no audio generated',
                      ('en',), (), '', ''),))
            controller = audio_studio.StudioController(library.root, registry)
            studio = audio_studio.StudioDialog(frame, controller)

            def exercise_studio():
                try:
                    check('actual integrated Studio exposes Save All and cancellation',
                          find_button(studio.dialog, 'Save all open drafts').IsEnabled()
                          and find_button(studio.dialog, 'Cancel Save All').IsEnabled())
                    restored.note.SetValue('Native Save All first note snapshot')
                    studio.script.SetValue('Narrator: Native Studio project committed by Save All.')
                    frame.hold_completions = True
                    press(find_button(studio.dialog, 'Save all open drafts'))
                    check('Save All waits for actual note completion on its native owner',
                          pump(lambda: bool(frame.pending_ui)) and frame._save_coordinator.busy)
                    check('real note transaction committed before its deferred UI acknowledgement',
                          library.evidence(workspace, topic)[0].note == 'Native Save All first note snapshot')
                    restored.note.SetValue('Newer native note remains editable')
                    frame.release_completions()
                    check('Save All completes both actual participants',
                          pump(lambda: not frame._save_coordinator.busy and not frame.working
                               and controller.project is not None))
                    saved_project = controller.store.load(controller.project.id)
                    check('native Studio participant wrote a real validated project',
                          saved_project.script == 'Narrator: Native Studio project committed by Save All.'
                          and not studio.unsaved())
                    check('newer native note is retained and overall success is withheld',
                          restored.note.GetValue() == 'Newer native note remains editable'
                          and restored._dirty()
                          and 'Unsaved changes remain' in studio.status.GetValue())
                    check('newer note checkpoint matches the newly committed baseline',
                          store.get(workspace, topic, evidence).matches(library.evidence(workspace, topic)[0])
                          and store.get(workspace, topic, evidence).text == 'Newer native note remains editable')

                    studio.script.SetValue('Narrator: This later Studio draft must await another Save.')
                    prior_revision = controller.project.revision
                    frame.hold_completions = True
                    press(find_button(studio.dialog, 'Save all open drafts'))
                    check('second request reaches a committed note before the next participant',
                          pump(lambda: bool(frame.pending_ui)) and frame._save_coordinator.busy)
                    press(find_button(studio.dialog, 'Cancel Save All'))
                    check('actual modal Cancel signals the shared request',
                          pump(frame.cancel_event.is_set) and frame._save_coordinator.busy)
                    frame.release_completions()
                    check('cancelled Save All releases its request',
                          pump(lambda: not frame._save_coordinator.busy and not frame.working))
                    check('late cancellation preserves the committed note and unsaved next project',
                          library.evidence(workspace, topic)[0].note == 'Newer native note remains editable'
                          and controller.store.load(controller.project.id).revision == prior_revision
                          and studio.unsaved() and 'cancelled' in studio.status.GetValue())
                    press(find_button(studio.dialog, 'Save project'))
                    check('a separate explicit native Save commits the remaining Studio draft',
                          pump(lambda: not studio.unsaved())
                          and controller.store.load(controller.project.id).script ==
                              'Narrator: This later Studio draft must await another Save.')
                    press(find_button(studio.dialog, 'Close'))
                except Exception as exc:
                    errors.append('Studio Save All: ' + repr(exc))
                    traceback.print_exc()
                    if studio.dialog.IsModal():
                        studio.dialog.EndModal(wx.ID_CANCEL)

            wx.CallLater(100, exercise_studio)
            studio.show()
            check('closing Studio unregisters only its own Save All participant',
                  tuple(frame._save_coordinator.participants) == ('research.note',))
            check('native Studio Save All assertions completed', not errors)

            reader_slot = 'native-save-all-reader'
            reader_store = ReaderDraftStore(library.root, slot=reader_slot)

            def exercise_reader():
                dialog = None
                try:
                    dialog = next(window for window in wx.GetTopLevelWindows()
                        if isinstance(window, wx.Dialog) and window.IsModal()
                        and window.GetTitle() == 'Voice reader and reading copy')
                    editor = next(child for child in descendants(dialog)
                        if isinstance(child, wx.TextCtrl)
                        and child.GetName() == 'Reading copy (editable; Ctrl+S saves)')
                    check('reader voice discovery finishes before its explicit Save All',
                          pump(lambda: find_button(dialog, 'Reload voices').IsEnabled(), timeout=20))
                    editor.SetValue('Native reader edit committed by Save All.')
                    press(find_button(dialog, 'Save all open drafts'))
                    check('native reader participant writes actual reading recovery state',
                          pump(lambda: reader_store.path.exists() and
                               reader_store.load().document.text == 'Native reader edit committed by Save All.'))
                    saved = reader_store.load()
                    check('reader Save All preserves immutable original text',
                          saved.document.source_text == 'Immutable native reading source.')
                    check('reader shares the same owner coordinator',
                          any(key.startswith('reading.') for key in frame._save_coordinator.participants)
                          and not frame._save_coordinator.busy)
                    press(find_button(dialog, 'Close'))
                except Exception as exc:
                    errors.append('Reader Save All: ' + repr(exc))
                    traceback.print_exc()
                    if dialog is not None and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            wx.CallLater(150, exercise_reader)
            speech_dialog.show_speech_reader(frame, text='Immutable native reading source.',
                                             recovery_slot=reader_slot)
            check('closing reader unregisters its participant and keeps the saved file',
                  tuple(frame._save_coordinator.participants) == ('research.note',)
                  and reader_store.load().document.text == 'Native reader edit committed by Save All.')
            check('no native background or keyboard error occurred', not errors)
            result = dict(status='PASS', checks=len(checks), check_names=checks, wx=wx.version(),
                          platform=platform.system(), origins=origins, nvda='NOT_RUN',
                          audio_quality='NOT_RUN', source_override=str(source) if source is not None else None)
        finally:
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            frame.Destroy()
            app.Yield()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path, help='Explicit checkout override for local debugging only.')
    parser.add_argument('--report', type=Path, help='JSON acceptance receipt outside the installed package.')
    args = parser.parse_args()
    if not args.native:
        parser.error('Pass --native to execute real native controls.')
    def record(value):
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    record(dict(status='FAILED', reason='Native Save All probe has not completed.', nvda='NOT_RUN'))
    faulthandler.enable()
    # Unlike GUI callbacks this deadline remains independent of a blocked
    # modal event loop, and remains armed through native/worker cleanup.
    faulthandler.dump_traceback_later(90, exit=True)
    try:
        result = run_native(args.source.resolve() if args.source else None)
        record(result)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result['status'] == 'PASS' else 77
    except BaseException as exc:
        result = dict(status='FAILED', error=type(exc).__name__ + ': ' + str(exc), nvda='NOT_RUN')
        record(result)
        print(json.dumps(result, sort_keys=True), flush=True)
        traceback.print_exc()
        return 1
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
