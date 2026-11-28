"""Actual installed Windows transcription controls; no inference qualification.

The native UI is driven with a synthetic in-memory service so this probe never
loads the supplied private worker, private recordings, or model assets. Real
receipt bytes and supervised child processes have separate contract tests.
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch
import traceback


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.ui import transcription_v43_dialog as ui
    from carr_library.ui import transcription_dialog as entry
    from carr_library.transcription_receipt import validate_settings

    checks, errors, timers = [], [], []
    settled = Event()
    def watchdog():
        if not settled.wait(45):
            print('FAIL: native transcription controls exceeded the watchdog', flush=True)
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
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    check('transcription UI loaded from installed package',
          all('site-packages' in [part.casefold() for part in Path(module.__file__).parts]
              for module in (ui, entry)))

    class FixtureService:
        """Only the presentation of already captured public fixture values."""
        def __init__(self, root):
            self.library_root = root
            self.calls = 0
            self.stopped = Event()
            self.release = Event()
            self.running, self.pause_requested, self.pause_seen = Event(), Event(), Event()
            self.resume_seen, self.finish_run = Event(), Event()
            self.control_calls = []
            self.value = dict(id='a' * 32, title='Транскрипція — Україна',
                settings=validate_settings({'language': 'uk', 'beam_size': 3}),
                runtime_manifest_sha256='b' * 64,
                inputs=[dict(relative='Україна.wav', bytes=32)])
        def jobs(self, *, cancelled):
            self.calls += 1
            if self.calls > 1:
                while not self.release.wait(.01):
                    if cancelled():
                        self.stopped.set()
                        raise InterruptedError('Synthetic UI operation acknowledged stop.')
                if cancelled():
                    self.stopped.set()
                    raise InterruptedError('Synthetic UI operation acknowledged stop.')
            return ({'id': self.value['id'], 'title': self.value['title'], 'inputs': 1},)
        def definition(self, identity, *, verify_inputs, cancelled):
            check('fixture review uses immutable definition without running an engine',
                  identity == self.value['id'] and verify_inputs is False and not cancelled())
            return self.value
        def run(self, identity, manifest, *, cancelled, progress):
            if identity != self.value['id'] or not manifest:
                raise ValueError('Synthetic fixture requires its captured request.')
            self.running.set()
            progress(0, 1, 'running')
            while not self.finish_run.wait(.01):
                if cancelled():
                    raise InterruptedError('Synthetic UI run acknowledged stop.')
                if self.pause_requested.is_set():
                    self.pause_seen.set()
                    progress(0, 1, 'paused')
                elif self.pause_seen.is_set():
                    self.resume_seen.set()
                    progress(0, 1, 'running')
            progress(1, 1, 'finished')
            return dict(input_count=1, output_count=1,
                        outputs=[dict(path='synthetic-output.txt', format='txt', bytes=32)])
        def control(self, identity, action):
            if identity != self.value['id']:
                raise ValueError('Control must keep the selected synthetic job.')
            self.control_calls.append(action)
            if action == 'pause':
                self.pause_requested.set()
            elif action == 'resume':
                self.pause_requested.clear()
            elif action == 'stop':
                self.stopped.set()

    with TemporaryDirectory(prefix='carr-native-transcription-') as directory:
        root = Path(directory)
        frame = wx.Frame(None, title='Synthetic native transcription controls')
        frame.library = type('Library', (), {'root': root})()
        frame.cancel_event = Event()
        frame.working = frame._closing = False
        service = FixtureService(root)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='transcription-ui-fixture')
        futures = []

        def run_job(title, operation, done=None, failed=None, **options):
            if frame.working:
                return False
            frame.working = True
            frame.cancel_event.clear()
            future = executor.submit(operation)
            futures.append(future)
            def finished(future):
                def apply():
                    frame.working = False
                    try:
                        result = future.result()
                    except Exception as exc:
                        if failed is not None:
                            failed(exc)
                    else:
                        if done is not None:
                            done(result)
                wx.CallAfter(apply)
            future.add_done_callback(finished)
        frame.run_job = run_job
        app.SetTopWindow(frame)
        frame.Show()
        state = {'phase': 0, 'active': True, 'view': None, 'complete': False}
        deadline = monotonic() + 20

        def press(view, name):
            button = view.buttons[name]
            check('enabled named native transcription button: ' + name,
                  isinstance(button, wx.Button) and button.IsEnabled() and bool(button.GetName()))
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('native transcription button dispatch: ' + name,
                  button.GetEventHandler().ProcessEvent(event))

        def schedule():
            timers.append(wx.CallLater(25, poll))

        def poll():
            if not state['active']:
                return
            view = state['view']
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native transcription flow did not finish within its bound.')
                if view.session.busy and state['phase'] not in ('run-started', 'pause-requested', 'resume-requested'):
                    schedule()
                    return
                if state['phase'] == 0:
                    check('owned native transcription modal',
                          view.dialog.GetParent() is frame and view.dialog.IsModal()
                          and view.dialog.GetName() == 'Local speech transcription workspace')
                    check('library-wide scope is explicitly named without an active workspace',
                          'All jobs in this library' in view.scope.GetValue()
                          and not getattr(frame, 'active', None))
                    accelerators = [button.GetLabel().split('&', 1)[1][0].casefold()
                                    for button in view.buttons.values() if '&' in button.GetLabel()]
                    check('native transcription action mnemonics are distinct',
                          len(accelerators) == len(set(accelerators)))
                    check('all seventeen native settings have unique accessible names',
                          len(view.fields) == 17 and
                          {field.GetName() for field in view.fields.values()} == set(ui._LABELS.values()))
                    check('native choices, numeric spins and booleans remain typed',
                          all(isinstance(view.fields[name], wx.Choice) for name in ui._CHOICES)
                          and all(isinstance(view.fields[name], wx.SpinCtrl)
                                  for name in ('beam_size', 'parallel_files'))
                          and all(isinstance(view.fields[name], wx.CheckBox)
                                  for name, value in ui.DEFAULT_SETTINGS.items() if type(value) is bool))
                    check('saved synthetic job retains its exact Unicode title',
                          len(view.rows) == 1 and service.value['title'] in view.jobs_list.GetString(0))
                    view.jobs_list.SetSelection(0)
                    view.controls()
                    state['phase'] = 1
                    press(view, 'load')
                    schedule()
                    return
                if state['phase'] == 1:
                    check('reviewed synthetic job settings are frozen',
                          view.current_job == service.value['id']
                          and all(not field.IsEnabled() for field in view.fields.values())
                          and ui.settings_from_controls(view.fields) == service.value['settings'])
                    check('review preserves Ukrainian title and retained source name',
                          view.title.GetValue() == service.value['title']
                          and 'Україна.wav' in view.source_list.GetString(0))
                    press(view, 'new')
                    check('New job re-enables typed settings without starting a worker',
                          view.current_job is None and all(field.IsEnabled() for field in view.fields.values())
                          and not view.manifest_path)
                    view.fields['task'].SetSelection(1)
                    view.fields['language'].SetSelection(
                        tuple(code for code, _ in ui._CHOICES['language']).index('uk'))
                    view.fields['parallel_files'].SetValue(2)
                    chosen = ui.settings_from_controls(view.fields)
                    check('native request values preserve explicit English translation and Ukrainian source',
                          chosen['task'] == 'translate' and chosen['language'] == 'uk'
                          and chosen['translation_target'] == 'en' and chosen['parallel_files'] == 2)
                    view.jobs_list.SetSelection(0)
                    view.controls()
                    state['phase'] = 2
                    press(view, 'load')
                    schedule()
                    return
                if state['phase'] == 2:
                    view.manifest_path = str(root / 'synthetic-presentation-only-manifest.json')
                    view.manifest.ChangeValue(view.manifest_path)
                    view.controls()
                    state['phase'] = 'run-started'
                    press(view, 'start')
                    schedule()
                    return
                if state['phase'] == 'run-started':
                    if not service.running.is_set():
                        schedule()
                        return
                    check('synthetic run enables only its current-job pause and stop controls',
                          view.session.kind == 'run' and view.buttons['pause'].IsEnabled()
                          and view.buttons['stop'].IsEnabled() and not view.buttons['resume'].IsEnabled())
                    press(view, 'pause')
                    state['phase'] = 'pause-requested'
                    schedule()
                    return
                if state['phase'] == 'pause-requested':
                    if not service.pause_seen.is_set():
                        schedule()
                        return
                    check('pause request reaches the same synthetic worker before resume is offered',
                          service.control_calls == ['pause'] and view.pause_requested
                          and view.buttons['resume'].IsEnabled() and not view.buttons['pause'].IsEnabled())
                    press(view, 'resume')
                    state['phase'] = 'resume-requested'
                    schedule()
                    return
                if state['phase'] == 'resume-requested':
                    if not service.resume_seen.is_set():
                        schedule()
                        return
                    check('resume preserves the running synthetic job and frozen settings',
                          service.control_calls == ['pause', 'resume'] and not view.pause_requested
                          and view.current_job == service.value['id']
                          and all(not field.IsEnabled() for field in view.fields.values()))
                    service.finish_run.set()
                    state['phase'] = 'run-complete'
                    schedule()
                    return
                if state['phase'] == 'run-complete':
                    check('returned synthetic verified summary enables reading-copy control',
                          view.summary['input_count'] == 1 and view.summary['output_count'] == 1
                          and view.outputs.GetCount() == 1 and view.buttons['reading'].IsEnabled()
                          and not view.buttons['pause'].IsEnabled() and not view.buttons['resume'].IsEnabled())
                    press(view, 'refresh')
                    check('the public UI fixture operation is active before closing', view.session.busy)
                    press(view, 'close')
                    check('close requests stop and keeps native controls alive until settlement',
                          view.session.close_requested and not view.session.closed and view.dialog.IsModal())
                    service.release.set()
                    state['complete'] = True
                    return
            except Exception:
                errors.append(traceback.format_exc())
                service.release.set()
                state['active'] = False
                view.session.cancel.set()
                if view.dialog.IsModal():
                    view.dialog.EndModal(wx.ID_CANCEL)

        def ready(view):
            state['view'] = view
            schedule()

        try:
            class ProfileAnswer(wx.SingleChoiceDialog):
                def ShowModal(self):
                    labels = []
                    for index in range(3):
                        self.SetSelection(index)
                        labels.append(self.GetStringSelection())
                    check('one actual native profile chooser exposes both workspace contracts and library-wide scope',
                          isinstance(self, wx.SingleChoiceDialog)
                          and all(label.startswith('Workspace jobs') for label in labels[:2])
                          and labels[2].startswith('Library-wide jobs') and self.GetSelection() == 2)
                    return wx.ID_OK
            with patch.object(wx, 'SingleChoiceDialog', ProfileAnswer):
                entry.show_transcription(frame, service=service, on_ready=ready)
            check('shared public entrypoint retains the explicitly chosen supplied profile',
                  frame._transcription_profile == 'supplied-whisper-v43-hardened-1')
            check('native close completed after the synthetic operation acknowledged cancellation',
                  state['complete'] and service.stopped.is_set())
            if errors:
                raise AssertionError('\n'.join(errors))
        finally:
            state['active'] = False
            service.release.set()
            service.finish_run.set()
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
                profile_picker_interaction='NOT_RUN', profile_picker_answer='controlled selection on an actual wx choice dialog',
                scope='Actual Windows controls and lifecycle with a public synthetic presentation fixture.')


def main():
    parser = ArgumentParser()
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if not options.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows native mode is required.'}))
        return 77
    try:
        result = run_native()
    except Exception:
        result = {'status': 'FAIL', 'error': traceback.format_exc(), 'inference': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    if options.output is not None:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
