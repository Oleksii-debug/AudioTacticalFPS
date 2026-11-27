"""Installed native recovery controls over real synthetic retained input bytes.

The selected current runtime is a nonexecutable inventory fixture. Actual
preparation/backup schemas are used; inference and NVDA remain NOT_RUN.
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import traceback


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library import legacy_transcription as recovery
    from carr_library.transcription_service import TranscriptionService
    from carr_library.transcription_state import read_definition
    from carr_library.transcription_receipt import validate_settings
    from carr_library.ui import transcription_dialog as ui

    checks, errors, timers = [], [], []
    settled = Event()
    def watchdog():
        if not settled.wait(50):
            print('FAIL: native historical transcription recovery exceeded its watchdog', flush=True)
            os._exit(124)
    Thread(target=watchdog, daemon=True).start()

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    app = App(False)
    event_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(event_loop)
    check('actual Windows controls', wx.Platform == '__WXMSW__')
    check('installed current recovery module',
          'site-packages' in [part.casefold() for part in Path(recovery.__file__).parts])
    fixture_path = Path(__file__).resolve().parents[1] / 'tests/storage/legacy_transcription_fixture.py'
    spec = importlib.util.spec_from_file_location('public_legacy_transcription_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    with TemporaryDirectory(prefix='carr-native-legacy-transcription-') as directory:
        root = Path(directory).resolve()
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic recovery proof')
        saved = fixture.canonical_data(library.root, complete=True)
        recovery.retain_live(library.root)
        runtime = fixture.current_runtime(root / 'current-reviewed-inventory')
        original = saved.source_path.read_bytes()
        frame = wx.Frame(None, title='Earlier transcription native acceptance')
        frame.library = library
        frame.cancel_event = Event()
        frame.working = frame._closing = False
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='legacy-transcription-native')
        state = dict(view=None, active=True, choice=0, focused=None, opened=False,
                     prepared=False, cancel_opened=False, complete=False)
        expected = [
            ('Earlier transcription projects', 0),
            ('Select earlier transcription', 0),
            ('Earlier transcription action', 3),
            ('Retained sources for a new job', (0,)),
            ('Earlier transcription projects', None),
        ]

        def run_job(title, operation, done=None, failed=None, **options):
            if frame.working:
                return False
            frame.working = True
            frame.cancel_event.clear()
            future = executor.submit(operation)
            def finished(result):
                def deliver():
                    frame.working = False
                    try:
                        value = result.result()
                    except Exception as exc:
                        if failed:
                            failed(exc)
                    else:
                        if done:
                            done(value)
                wx.CallAfter(deliver)
            future.add_done_callback(finished)
            return True
        frame.run_job = run_job

        def children(window):
            pending, result = list(window.GetChildren()), []
            while pending:
                child = pending.pop()
                result.append(child)
                if len(result) > 1000:
                    raise AssertionError('Native control inventory exceeded its bound.')
                pending.extend(child.GetChildren())
            return result

        def owned(control, dialog):
            for _ in range(20):
                if control is dialog:
                    return True
                if control is None:
                    return False
                control = control.GetParent()
            return False

        def press(button):
            check('native button enabled and visible', button.IsEnabled() and button.IsShownOnScreen())
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('native button event handled', button.GetEventHandler().ProcessEvent(event))

        def schedule():
            if state['active']:
                timers.append(wx.CallLater(40, tick))

        def tick():
            if not state['active']:
                return
            view = state['view']
            try:
                if view is None:
                    schedule()
                    return
                nested = [window for window in wx.GetTopLevelWindows()
                          if isinstance(window, wx.Dialog) and window.GetParent() is view.dialog
                          and window.IsModal() and window.IsShown()]
                if nested:
                    check('one owned recovery modal', len(nested) == 1)
                    dialog = nested[0]
                    title, answer = expected[state['choice']]
                    check('exact native recovery step', dialog.GetTitle() == title)
                    key = (int(dialog.GetHandle()), title, state['choice'])
                    if state['focused'] != key:
                        if answer is not None:
                            cls = wx.CheckListBox if isinstance(answer, tuple) else wx.ListBox
                            candidates = [control for control in children(dialog) if isinstance(control, cls)]
                            check('one actual native choice list', len(candidates) == 1)
                            control = candidates[0]
                            if isinstance(answer, tuple):
                                for index in answer:
                                    control.Check(index, True)
                            else:
                                control.SetSelection(answer)
                            control.SetFocus()
                        else:
                            dialog.FindWindowById(wx.ID_CANCEL, dialog).SetFocus()
                        state['focused'] = key
                        schedule()
                        return
                    button = dialog.FindWindowById(wx.ID_CANCEL if answer is None else wx.ID_OK, dialog)
                    check('dialog-scoped native action', button is not None and owned(button, dialog))
                    state['choice'] += 1
                    schedule()  # Nested modal delivery continues while this real event is active.
                    press(button)
                    return
                if view.session.busy:
                    schedule()
                    return
                if not state['opened']:
                    view.manifest_path = str(runtime)
                    view.manifest.ChangeValue(str(runtime))
                    view.title.ChangeValue('Recovered native synthetic job')
                    ui.apply_settings(view.fields, validate_settings(dict(language='uk')))
                    view.controls()
                    view.buttons['historical'].SetFocus()
                    state['opened'] = True
                    schedule()
                    return
                if state['choice'] == 0:
                    schedule()
                    press(view.buttons['historical'])
                    return
                if state['choice'] == 4 and not state['prepared']:
                    check('current immutable definition was prepared', view.current_job is not None
                          and view.current_job != saved.job['id'] and view.definition is not None)
                    current = read_definition(library.root / 'transcription-jobs' / view.current_job)
                    check('old source bytes retained independently',
                          (library.root / 'transcription-jobs' / current['id'] / current['inputs'][0]['path']).read_bytes() == original
                          and saved.source_path.read_bytes() == original)
                    check('preparation does not start or complete inference',
                          not (library.root / 'transcription-jobs' / current['id'] / 'execution.json').exists()
                          and not (library.root / 'transcription-jobs' / current['id'] / 'completion-v1.json').exists())
                    check('current settings are frozen and Start stays separate',
                          current['settings']['language'] == 'uk'
                          and all(not control.IsEnabled() for control in view.fields.values())
                          and view.buttons['start'].IsEnabled())
                    state['id'] = current['id']
                    state['prepared'] = True
                    view.buttons['historical'].SetFocus()
                    schedule()
                    return
                if state['choice'] == 4 and state['prepared'] and not state['cancel_opened']:
                    state['cancel_opened'] = True
                    schedule()
                    press(view.buttons['historical'])
                    return
                if state['choice'] == 5:
                    check('native Cancel preserved the prepared job', view.current_job == state['id'])
                    state['complete'] = True
                    state['active'] = False
                    press(view.buttons['close'])
                    return
                schedule()
            except Exception:
                errors.append(traceback.format_exc())
                state['active'] = False
                frame.cancel_event.set()
                if view is not None:
                    view.session.cancel.set()
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)

        def ready(view):
            state['view'] = view
            schedule()

        try:
            ui.show_transcription(frame, service=TranscriptionService(library.root), on_ready=ready)
            check('all native recovery steps acknowledged', state['complete'] and state['choice'] == 5)
            if errors:
                raise AssertionError('\n'.join(errors))
        finally:
            state['active'] = False
            frame.cancel_event.set()
            if state['view'] is not None:
                state['view'].session.cancel.set()
            for timer in timers:
                timer.Stop()
            executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            app.ProcessPendingEvents()
            settled.set()
    return dict(status='PASS', checks=checks, inference='NOT_RUN', nvda='NOT_RUN',
                scope='Actual Windows recovery controls and current preparation over public synthetic data.')


def main():
    parser = ArgumentParser()
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if not options.native or os.name != 'nt':
        print(json.dumps(dict(status='NOT_RUN', reason='Actual Windows native mode is required.')))
        return 77
    try:
        result = run_native()
    except Exception:
        result = dict(status='FAIL', error=traceback.format_exc(), inference='NOT_RUN', nvda='NOT_RUN')
    if options.output is not None:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
