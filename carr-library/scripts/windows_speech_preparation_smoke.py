"""Installed native speech-text preparation; no speech/model quality claim."""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, get_ident
from time import monotonic
import traceback


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader import speech_normalization
    from carr_library.reader.narration import NarrationStore, encode_project, new_project
    from carr_library.ui import speech_preparation
    from carr_library.ui.narration_editor import open_narration_editor
    from carr_library.ui.live_reading import native_offset, selected_source_offsets

    finished = Event()
    checks, errors, timers, futures, worker_threads = [], [], [], [], []
    gui_thread = get_ident()
    def watchdog():
        if not finished.wait(60):
            print('FAIL: native speech-preparation watchdog expired', flush=True)
            os._exit(124)
    Thread(target=watchdog, daemon=True).start()
    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    app = wx.App(False)
    event_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(event_loop)
    frame = None
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='native-speech-preparation')
    try:
        check('actual Windows wx controls', wx.Platform == '__WXMSW__')
        for module in (speech_normalization, speech_preparation):
            check('installed module ' + module.__name__,
                  'site-packages' in [part.casefold() for part in Path(module.__file__).parts])
        with TemporaryDirectory(prefix='carr-native-speech-text-') as temporary:
            base = Path(temporary)
            library = LibraryApp(base / 'library')
            library.create_workspace('Speech text fixture')
            root = library.root
            original_path = base / 'Оригінал Україна.txt'
            source = 'Retained 😀.\r\nOutside 12; selected 21 кг; 2026-11-27.\r\nEnd e\u0301.'
            original_bytes = source.encode('utf-8')
            original_path.write_bytes(original_bytes)
            selected = '21 кг; 2026-11-27.'
            first, last = source.index(selected), source.index(selected) + len(selected)
            prepared_text = 'двадцять один кілограм; двадцять сьомого листопада, рік дві тисячі двадцять шість.'
            store = NarrationStore(root)
            project = new_project(source, title='Native speech text', original_path=str(original_path))
            saved = store.save(project)
            frame = wx.Frame(None, title='Installed speech-preparation acceptance')
            frame.library = library
            frame.working = frame._closing = False
            frame.cancel_event = Event()

            def run_job(title, operation, done=None, failed=None, **kwargs):
                if frame.working:
                    return False
                frame.working = True
                frame.cancel_event.clear()
                def perform():
                    worker_threads.append(get_ident())
                    return operation()
                future = executor.submit(perform)
                futures.append(future)
                def arrived(future):
                    def settle():
                        frame.working = False
                        try:
                            result = future.result()
                        except Exception as exc:
                            if failed is not None:
                                failed(exc)
                        else:
                            if done is not None:
                                done(result)
                    wx.CallAfter(settle)
                future.add_done_callback(arrived)
                return True
            frame.run_job = run_job
            frame.Show()
            app.SetTopWindow(frame)
            state = dict(active=True, phase='reader', reader=None, controller=None,
                         controls=None, view=None, session=None)
            deadline = monotonic() + 45

            def press(button):
                check('enabled native button ' + button.GetName(), isinstance(button, wx.Button) and button.IsEnabled())
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                event.SetEventObject(button)
                check('handled native button ' + button.GetName(), button.GetEventHandler().ProcessEvent(event))

            def choose(control, selection):
                control.SetSelection(selection)
                event = wx.CommandEvent(wx.EVT_CHOICE.typeId, control.GetId())
                event.SetEventObject(control)
                control.GetEventHandler().ProcessEvent(event)

            def toggle(control, value):
                control.SetValue(value)
                event = wx.CommandEvent(wx.EVT_CHECKBOX.typeId, control.GetId())
                event.SetEventObject(control)
                control.GetEventHandler().ProcessEvent(event)

            def schedule():
                timers.append(wx.CallLater(20, poll))

            def failure(error):
                errors.append(str(error))
                print('FAIL:', str(error), flush=True)
                state['phase'] = 'failed'
                view = state.get('view')
                if view is not None and view.alive:
                    view.on_close()
                schedule()

            def poll():
                if not state['active']:
                    return
                try:
                    phase, view = state['phase'], state['view']
                    if phase != 'failed' and monotonic() >= deadline:
                        raise TimeoutError('The native speech-preparation flow exceeded its deadline.')
                    if phase == 'reader':
                        if state['reader'] is None:
                            schedule()
                            return
                        controls = state['controls']
                        check('existing actions retained', all(key in controls for key in
                              ('normalize', 'spelling', 'appearance', 'paced_reading', 'live_reading', 'speech_preparation')))
                        controls['editor'].SetSelection(native_offset(controls['editor'], source, first),
                                                        native_offset(controls['editor'], source, last))
                        state['phase'] = 'dialog'
                        schedule()
                        press(controls['speech_preparation'])
                        return
                    if phase == 'dialog':
                        if view is None or not view.IsModal():
                            schedule()
                            return
                        check('actual owned preparation modal', view.GetParent() is state['reader'])
                        check('verbatim default with explicit selected scope', view.mode.GetSelection() == 0 and view.scope.GetSelection() == 1)
                        check('language is not silently selected', view.locale.GetSelection() == 0)
                        state['phase'] = 'verbatim'
                        press(view.buttons['preview'])
                    elif phase == 'verbatim':
                        if view.busy or view.proposal is None:
                            schedule()
                            return
                        check('exact selected source at native Unicode offsets', view.proposal.snapshot.selection == (first, last))
                        check('verbatim cannot apply a fabricated change', view.proposal.result.text == selected and not view.buttons['apply'].IsEnabled())
                        check('preview did not edit reading copy', state['controller'].pending_text == source)
                        choose(view.mode, 1)
                        choose(view.locale, 1)
                        for name in ('numbers', 'dates', 'units'):
                            toggle(view.expansions[name], True)
                        view.mode.SetFocus()
                        state['phase'] = 'focus'
                    elif phase == 'focus':
                        if wx.Window.FindFocus() is not view.mode:
                            schedule()
                            return
                        check('native preparation mode receives focus', True)
                        state['phase'] = 'speech'
                        press(view.buttons['preview'])
                    elif phase == 'speech':
                        if view.busy or view.proposal is None:
                            schedule()
                            return
                        check('actual finite Ukrainian proposal', view.proposal.result.text == prepared_text)
                        check('before and after are separate native text controls', view.before.GetValue() == selected and view.after.GetValue() == prepared_text)
                        check('proposal did not pre-apply', state['controller'].pending_text == source)
                        press(view.buttons['apply'])
                        expected = source[:first] + prepared_text + source[last:]
                        check('accepted selected text only', state['controller'].pending_text == expected)
                        check('exact original remains readable', state['controller'].project.original_text == source)
                        check('accepted native selection matches prepared range',
                              selected_source_offsets(state['controls']['editor'], expected) == (first, first + len(prepared_text)))
                        draft, _ = store.load(project.id, draft=True)
                        check('current recovery store retains accepted text', draft.current_text == expected)
                        check('current revision history contains one accepted preparation', len(state['controller'].project.revisions) == 2)
                        state['phase'] = 'undo'
                    elif phase == 'undo':
                        press(view.buttons['undo'])
                        check('actual Undo restores the exact source text', state['controller'].pending_text == source)
                        check('Undo retains review history', len(state['controller'].project.revisions) == 2)
                        choose(view.mode, 0)
                        choose(view.scope, 0)
                        state['phase'] = 'whole-verbatim'
                        press(view.buttons['preview'])
                    elif phase == 'whole-verbatim':
                        if view.busy or view.proposal is None:
                            schedule()
                            return
                        check('verbatim remains exact after changing options and Undo', view.proposal.result.text == source)
                        check('no hidden speech options survive verbatim mode', view.proposal.result.options == speech_normalization.SpeechOptions())
                        state['phase'] = 'save'
                        schedule()
                        press(view.buttons['close'])
                        return
                    elif phase == 'save':
                        if view is not None and view.alive:
                            schedule()
                            return
                        press(state['controls']['save'])
                        saved_project, _ = store.load(project.id)
                        check('saved project preserves history and original', saved_project.original_text == source
                              and saved_project.current_text == source and len(saved_project.revisions) == 2)
                        state['phase'] = 'done'
                        press(state['controls']['close'])
                        return
                    elif phase == 'failed':
                        if view is not None and view.alive:
                            schedule()
                            return
                        reader = state.get('reader')
                        if reader and reader.IsModal():
                            # Failure cleanup only. The successful path above
                            # requires the actual Save and Close handlers.
                            reader.EndModal(wx.ID_CANCEL)
                        return
                    if state['phase'] != 'done':
                        schedule()
                except Exception as exc:
                    failure(exc)

            def ready(reader, controller, controls):
                state.update(reader=reader, controller=controller, controls=controls)
                schedule()

            def factory(parent, **kwargs):
                return speech_preparation.show_speech_preparation(parent, **kwargs,
                    on_ready=lambda view, session: state.update(view=view, session=session))

            try:
                result = open_narration_editor(frame, store, project, expected_digest=saved,
                    speech_factory=factory, on_ready=ready)
                state['active'] = False
                check('native flow completed through reviewed handlers', not errors and state['phase'] == 'done')
                check('actual current worker settled', futures and all(future.done() for future in futures))
                check('preview ran outside GUI thread', worker_threads and all(thread != gui_thread for thread in worker_threads))
                check('original file bytes stayed unchanged', original_path.read_bytes() == original_bytes)
                check('saved and returned reading projects agree', encode_project(store.load(project.id)[0]) == encode_project(result))
                check('no implicit voice or audio job', not (root / 'audio-projects').exists()
                      and not (root / 'transcription-jobs').exists())
            finally:
                state['active'] = False
                frame.cancel_event.set()
                for timer in timers:
                    timer.Stop()
                executor.shutdown(wait=True)
                # Settle any already queued current-frame callbacks before
                # deleting its real controls or temporary storage.
                for _ in range(100):
                    if not event_loop.Pending():
                        break
                    event_loop.Dispatch()
    finally:
        executor.shutdown(wait=True)
        for timer in timers:
            timer.Stop()
        if frame:
            frame.Destroy()
        if runtime is not None:
            runtime.close()
        finished.set()
    return dict(status='PASS', checks=len(checks), native_profile='uk-UA',
                speech_or_voice='NOT_RUN', physical_keys='NOT_RUN', nvda='NOT_RUN')


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps(dict(status='NOT_RUN', reason='Actual Windows required.')))
        return 77
    try:
        result = run_native()
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
