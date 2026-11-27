"""Native Windows platform controls and real local SAPI progress synthesis.

ASR service responses are explicitly synthetic. This exercises the installed
native workstation, events, shared run_job, checkpoint UI and derivative flow;
it is not model inference or recognition-quality evidence. SAPI discovery and
PCM synthesis are real; playback is intercepted because CI may lack a device.
Manual NVDA acceptance and physical playback remain NOT_RUN.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import os
import sysconfig
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import threading
import time
import wave


def request_modal_escape(dialog, keyboard, *, escape_key, close_code, pump):
    """Observe native close handling without waiting inside its own modal loop.

    On wxMSW, IsModal stays true until this callback returns and ShowModal
    unwinds. Check the window and return code here, then check ShowModal's
    actual return in the caller outside the callback.
    """
    if not dialog.IsShown() or not dialog.IsModal():
        return False
    keyboard.Char(escape_key)
    return pump(lambda: not dialog.IsShown()) and dialog.GetReturnCode() == close_code


class SyntheticTranscriptionService:
    """Deterministic protocol-independent UI fixture, with no model execution."""

    def __init__(self):
        self.profiles, self.job, self.edits = [], None, {}
        self.running = threading.Event()
        self.original = 'Synthetic recognition text. Українська копія.'
        self.calls = []

    def list_runtimes(self): return deepcopy(self.profiles)
    def list_jobs(self): return [deepcopy(self.job)] if self.job else []
    def get_job(self, identity): return deepcopy(self.job)

    def inspect_runtime(self, *paths, **options):
        self.calls.append('inspect')
        return {'fingerprint': 'synthetic-inspection-only', 'model_name': 'synthetic-model',
                'python_executable': paths[0], 'worker_path': paths[1], 'model_directory': paths[2]}

    def register_runtime(self, name, *paths, **options):
        assert options['owner_trusted'] is True
        assert options['expected_fingerprint'] == 'synthetic-inspection-only'
        self.calls.append('register')
        profile = {'id': 'a' * 32, 'name': name, 'trusted': True,
                   'model_name': 'synthetic-model', 'qualification': 'unqualified'}
        self.profiles = [profile]
        return deepcopy(profile)

    def create_job(self, profile, sources, **options):
        self.calls.append('create')
        self.running.clear()
        self.job = {'id': 'b' * 32, 'title': options['title'], 'profile_id': profile,
            'status': 'pending', 'done': 0, 'total': 2, 'settings': options['settings'],
            'sources': [{'id': 'c' * 32, 'original_path': str(sources[0])},
                        {'id': 'd' * 32, 'original_path': str(sources[0])}],
            'results': [], 'error': ''}
        return deepcopy(self.job)

    def run(self, identity, *, cancel, progress):
        self.calls.append('run')
        self.running.set()
        deadline = time.monotonic() + 20
        while not cancel():
            if time.monotonic() >= deadline:
                raise TimeoutError('The synthetic UI worker was not paused by the test.')
            progress({'phase': 'Transcribing synthetic fixture', 'done': 1, 'total': 2,
                      'current': 'No recognition engine is running'})
            threading.Event().wait(.03)
        return deepcopy(self.job)

    def pause(self, identity):
        self.calls.append('pause')
        self.job.update(status='paused', done=1, results=[{'source_id': 'c' * 32}])

    def cancel(self, identity):
        self.calls.append('cancel')
        self.job.update(status='cancelled', done=1)

    def resume(self, identity, *, cancel, progress):
        self.calls.append('resume')
        self.job.update(status='complete', done=2,
                        results=[{'source_id': 'c' * 32}, {'source_id': 'd' * 32}])
        progress({'phase': 'Synthetic fixture complete', 'done': 2, 'total': 2})
        return deepcopy(self.job)

    def read_transcript(self, identity, source, *, edited=False, cancel=None):
        if cancel is not None and cancel():
            raise InterruptedError('Synthetic transcript load cancelled.')
        if edited and source not in self.edits:
            raise FileNotFoundError('No synthetic saved edit.')
        return {'text': self.edits[source] if edited else self.original, 'source_id': source,
                'edited': edited, 'provenance': {'fixture': True, 'citation_authority': False}}

    def save_edit(self, identity, source, text):
        self.calls.append('save-edit')
        self.edits[source] = text
        return {'text': text, 'source_id': source, 'edited': True,
                'provenance': {'fixture': True, 'citation_authority': False}}

    def export(self, identity, directory, *, formats, include_edits, cancel):
        assert not cancel() and include_edits
        self.calls.append('export')
        target = directory / 'synthetic-original.txt'
        target.write_text(self.original, encoding='utf-8')
        return {'status': 'complete', 'fixture': True, 'files': [str(target)], 'citation_authority': False}


def main():
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows is required.',
                          'manual_nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return run_native()
    finally:
        if native_handle is not None:
            native_handle.close()


def run_native():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.operations import ResourceBroker
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.transcription_dialog import build_transcription_dialog, TranscriptionController
    from carr_library.speech import VoiceRegistry
    from carr_library.speech.progress_speech import LocalProgressSpeech
    from carr_library.speech.runtime import NativeSapiProvider
    from dataclasses import replace
    from carr_library.progress import ProgressHub
    from carr_library.progress_feedback import ProgressFeedback
    from carr_library.progress_preferences import ProgressPreferences
    from carr_library.speech.progress_speech import snapshot_speech_provider
    import carr_library.ui.transcription_dialog as transcription_module
    import carr_library.ui.transcription_workstation as workstation_module
    import carr_library.transcription.service as service_module
    import carr_library.transcription.runtime as runtime_module
    import carr_library.audio.transcription as execution_module
    import carr_library.ui.operations_dialog as operations_module

    app = wx.App(False)
    result = {'status': 'RUNNING', 'platform': 'win32', 'checks': [],
              'asr_service': 'SYNTHETIC_UI_FIXTURE', 'model_inference': 'NOT_RUN',
              'manual_nvda': 'NOT_RUN', 'physical_playback': 'NOT_RUN',
              'origins': {module.__name__: str(Path(module.__file__).resolve())
                          for module in (transcription_module, workstation_module, service_module,
                                         runtime_module, execution_module, operations_module)}}
    source_root = Path(__file__).resolve().parents[1] / 'src'
    installed = (Path(os.environ['CARR_PROGRAM_DIR']) if os.environ.get('CARR_PROGRAM_DIR')
                 else Path(sysconfig.get_path('purelib'))).resolve()
    assert sys.flags.isolated and sys.dont_write_bytecode and sys.flags.utf8_mode
    for name, path in result['origins'].items():
        actual = Path(path).resolve()
        expected = source_root / Path(*name.split('.')).with_suffix('.py')
        assert actual.is_relative_to(installed) and not actual.is_relative_to(source_root)
        assert sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest(), name

    def check(name, value):
        if not value:
            raise AssertionError(name)
        result['checks'].append(name)
        print('PASS:', name, flush=True)

    def pump(predicate, timeout=12):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def click(control):
        assert isinstance(control, wx.Button) and control.IsEnabled(), control.GetName()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        assert control.GetEventHandler().ProcessEvent(event), control.GetName()

    def named(parent, name):
        control = wx.Window.FindWindowByName(name, parent)
        if control is None:
            raise AssertionError('Missing native control: ' + name)
        return control

    with TemporaryDirectory(prefix='carr-native-platform-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic platform acceptance')
        frame = DesktopFrame(library)
        frame.Show()
        app.Yield()
        service = SyntheticTranscriptionService()
        dialog = build_transcription_dialog(frame, TranscriptionController(library.root, service=service))
        view = dialog.profile_view
        errors = []

        def workstation_checks():
            try:
                view.refresh()
                check('native ASR profile refresh uses the shared job host', pump(lambda: not view.pending))
                controls = {'Registered transcription runtime': wx.Choice,
                    'Source language code or auto': wx.TextCtrl, 'Selected local audio or video': wx.TextCtrl,
                    'Saved transcription jobs': wx.Choice, 'Transcription source result': wx.Choice,
                    'Transcript reading copy': wx.TextCtrl, 'Transcription progress and result': wx.TextCtrl}
                for name, native_type in controls.items():
                    check('ASR accessible native control: ' + name, isinstance(named(view, name), native_type))
                check('native ASR pages and Escape route are present',
                      view.pages.GetPageCount() == 3 and dialog.pages.GetPageCount() == 3
                      and dialog.GetEscapeId() == dialog.close_button.GetId())
                view.pages.SetSelection(1)
                view.python_path.SetValue(str(root / 'synthetic-python.exe'))
                view.worker_path.SetValue(str(root / 'synthetic-worker.py'))
                view.model_path.SetValue(str(root / 'synthetic-model'))
                click(named(view, 'Inspect files'))
                check('runtime inspection completes without inference', pump(lambda: not view.pending)
                      and service.calls == ['inspect'] and not view.trust.GetValue())
                click(named(view, 'Register trusted runtime'))
                check('unchecked trust blocks runtime registration', service.calls == ['inspect'])
                view.trust.SetValue(True)
                click(named(view, 'Register trusted runtime'))
                check('explicit inspected fingerprint trust registers one profile',
                      pump(lambda: not view.pending) and service.calls == ['inspect', 'register'])
                view.pages.SetSelection(0)
                source = root / 'synthetic.wav'
                source.write_bytes(b'Synthetic UI fixture; not audio inference.')
                view.sources = (source,)
                view.source_paths.ChangeValue(str(source))
                view.language.SetValue('uk')
                click(view.start_button)
                check('native Start enters the isolated service boundary', pump(service.running.is_set))
                view.help_button.SetFocus()
                check('help retains focus during coalesced progress',
                      pump(lambda: 'Transcribing synthetic fixture' in view.status.GetValue())
                      and wx.Window.FindFocus() == view.help_button)
                dialog.pages.SetSelection(0)
                dialog.query_button.SetFocus()
                keyboard = wx.UIActionSimulator()
                keyboard.Char(wx.WXK_F8)
                check('shared native F8 pauses the pending profile owner even on a manifest page', pump(lambda: not view.pending)
                      and view.current_job['status'] == 'paused' and view.current_job['done'] == 1)
                dialog.pages.SetSelection(2)
                click(dialog.start_button)
                check('native Resume reuses the stored job', pump(lambda: not view.pending)
                      and service.calls.count('create') == 1 and 'resume' in service.calls
                      and view.current_job['status'] == 'complete' and not view.start_button.IsEnabled())
                view.pages.SetSelection(2)
                click(named(view, 'Load transcript'))
                check('native transcript reader opens an independent reading copy',
                      pump(lambda: not view.pending) and view.document.source_text == service.original
                      and view.editor.GetValue() == service.original)
                check('explicit load focuses the transcript editor', pump(lambda: wx.Window.FindFocus() == view.editor))
                keyboard = wx.UIActionSimulator()
                keyboard.Char(wx.WXK_TAB)
                check('native Tab leaves the multiline editor for another dialog control',
                      pump(lambda: wx.Window.FindFocus() is not None and wx.Window.FindFocus() != view.editor)
                      and view.IsDescendant(wx.Window.FindFocus()))
                view.editor.SetValue('Corrected synthetic reading copy.')
                click(named(view, 'Save edits'))
                check('native edit save preserves original recognition text', pump(lambda: not view.pending)
                      and view.document.source_text == service.original
                      and service.edits['c' * 32] == 'Corrected synthetic reading copy.')
                click(named(view, 'Undo reading edit'))
                check('native Undo restores the independent source copy', view.editor.GetValue() == service.original)
                click(named(view, 'Redo reading edit'))
                check('native Redo restores the saved correction', view.editor.GetValue() == 'Corrected synthetic reading copy.')
                destination = root / 'new-transcript-export'
                view.export_to(destination)
                check('native export delegates into a new folder', pump(lambda: not view.pending)
                      and (destination / 'synthetic-original.txt').read_text(encoding='utf-8') == service.original)
                view.new_job()
                click(dialog.start_button)
                check('shared Start reaches the profile page owner', pump(service.running.is_set))
                dialog.pages.SetSelection(0)
                click(dialog.cancel_button)
                check('shared Cancel reaches the pending profile owner on another page',
                      pump(lambda: not view.pending) and view.current_job['status'] == 'cancelled')
                dialog.pages.SetSelection(2)
                view.close_button.SetFocus()
                keyboard.Char(wx.WXK_ESCAPE)
                check('native Escape closes an idle ASR dialog and stops its timer',
                      pump(lambda: view.closed) and not view.timer.IsRunning())
            except Exception as exc:
                errors.append(str(exc))
                service.cancel(service.job['id']) if service.job else None
                view.cancel_event.set()
                pump(lambda: not view.pending, timeout=5)
            finally:
                if dialog.IsModal() and dialog.IsShown():
                    dialog.EndModal(wx.ID_CANCEL)

        wx.CallLater(150, workstation_checks)
        try:
            dialog.ShowModal()
        finally:
            view.dispose()
            dialog.controller.dispose()
            if dialog:
                dialog.Destroy()
        if errors:
            raise AssertionError('; '.join(errors))


        # Parent-driven native deletion bypasses Python Destroy. Run these
        # deferred lifetimes through the actual main loop, holding only an
        # authored UI-service result until its real owner is gone.
        for destruction in ('direct', 'parent'):
            release, worker_finished = threading.Event(), threading.Event()
            returned, queued, seen, delivered, lifecycle_errors = [], [], [], [], []
            original_run = frame.run_job
            class HeldService(SyntheticTranscriptionService):
                def run(self, identity, *, cancel, progress):
                    self.calls.append('run')
                    self.running.set()
                    if not release.wait(12):
                        raise TimeoutError('Native lifecycle fixture did not release its owned result.')
                    cancel()
                    progress({'phase': 'Late real synthetic result', 'done': 1, 'total': 2})
                    return deepcopy(self.job)
            held = HeldService()
            held.profiles = [{'id': 'a' * 32, 'name': 'Synthetic UI owner',
                              'trusted': True, 'model_name': 'synthetic-model',
                              'qualification': 'unqualified'}]
            class Host(wx.Frame):
                @property
                def working(self):
                    return frame.working
            host = Host(frame, title='Owned transcription lifetime fixture')
            host.library, host.cancel_event, host._closing = library, frame.cancel_event, False
            def observe_job(title, operation, *, done, failed):
                queued.append((done, failed))
                def observed_operation():
                    try:
                        value = operation()
                        returned.append(value)
                        return value
                    finally:
                        worker_finished.set()
                return original_run(title, observed_operation, done=done, failed=failed)
            host.run_job = observe_job
            owner = build_transcription_dialog(host, TranscriptionController(library.root, service=held))
            page = owner.profile_view
            page.profiles = tuple(held.profiles)
            page.runtime.SetItems([held.profiles[0]['name']])
            page.runtime.SetSelection(0)
            page.sources = (source,)
            page.source_paths.ChangeValue(str(source))
            identity = owner.GetId()
            def destroyed(event, *, selected_id=identity):
                if event.GetId() == selected_id:
                    seen.append((event.GetId(), event.GetEventObject() is owner))
                event.Skip()
            owner.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
            phase = ['start']
            deadline = time.monotonic() + 15
            def finish_lifetime():
                app.ExitMainLoop()
            def tick():
                try:
                    if time.monotonic() >= deadline:
                        raise AssertionError('Timed out awaiting actual ASR native deletion and worker completion.')
                    if phase[0] == 'start':
                        owner.Show()
                        click(owner.start_button)
                        phase[0] = 'running'
                    elif phase[0] == 'running' and held.running.is_set():
                        assert page.pending and not frame.cancel_event.is_set()
                        if destruction == 'direct':
                            owner.Destroy()
                            assert owner.controller.closed and page.closed
                        else:
                            host.DestroyChildren()
                        phase[0] = 'deleted'
                    elif phase[0] == 'deleted' and not owner and seen:
                        assert len(seen) == 1 and seen[0][0] == identity
                        assert page.closed and page.cancel_event.is_set()
                        assert owner.controller.closed and not frame.cancel_event.is_set()
                        release.set()
                        phase[0] = 'finished'
                    elif phase[0] == 'finished' and worker_finished.is_set() and not frame.working:
                        assert len(returned) == 1 and returned[0]['status'] == 'cancelled'
                        assert queued and not owner and not page
                        def deliver():
                            try:
                                # Replay the actual registered completion with
                                # its actual returned value after native deletion.
                                queued[0][0](returned[0])
                                page.flush_progress()
                                page.report('Late coalesced progress after native deletion.')
                                delivered.append(True)
                            except BaseException as error:
                                lifecycle_errors.append(repr(error))
                            finally:
                                finish_lifetime()
                        wx.CallAfter(deliver)
                        phase[0] = 'delivering'
                    if phase[0] != 'delivering':
                        wx.CallLater(10, tick)
                except BaseException as error:
                    lifecycle_errors.append(repr(error))
                    release.set()
                    finish_lifetime()
            wx.CallAfter(tick)
            try:
                app.MainLoop()
                check('profile pending ' + destruction + ' destruction keeps late callbacks detached',
                      not lifecycle_errors and delivered == [True] and len(seen) == 1
                      and not owner and not page and worker_finished.is_set()
                      and page.closed and not frame.cancel_event.is_set())
                result.setdefault('profile_lifetimes', []).append({
                    'destruction': destruction, 'native_events': len(seen),
                    'original_wrapper': seen[0][1], 'actual_worker_finished': True,
                    'actual_callback_replayed_after_deletion': True})
            finally:
                release.set()
                if owner:
                    owner.Destroy()
                if host:
                    host.Destroy()
            if lifecycle_errors:
                raise AssertionError('; '.join(lifecycle_errors))

        # A real dashboard executor with one deterministic failed submission
        # exercises recovery without a competing fake native widget layer.
        original_executor = operations_module.ThreadPoolExecutor
        class RejectFirstSubmission:
            def __init__(self, **options):
                self.actual, self.first = ThreadPoolExecutor(**options), True
            def submit(self, operation):
                if self.first:
                    self.first = False
                    raise RuntimeError('Synthetic executor admission failure')
                return self.actual.submit(operation)
            def shutdown(self, **options): return self.actual.shutdown(**options)
        operations_module.ThreadPoolExecutor = RejectFirstSubmission
        dashboard_errors = []
        dashboard_escape_received = False

        def dashboard_checks():
            nonlocal dashboard_escape_received
            dashboard = next((window for window in wx.GetTopLevelWindows()
                              if isinstance(window, wx.Dialog) and window.GetTitle() == 'Tasks and resources'), None)
            if dashboard is None:
                dashboard_errors.append('The native Tasks and resources dialog did not open.')
                return
            try:
                for name in ('Saved platform operations', 'Operation details and resource status',
                        'Allowed CPU slots', 'Declared memory allowance in MiB',
                        'Declared temporary disk allowance in GiB', 'Keep free disk space in MiB'):
                    check('dashboard accessible native control: ' + name, isinstance(named(dashboard, name), wx.Control))
                refresh = named(dashboard, 'Refresh')
                detail = named(dashboard, 'Operation details and resource status')
                click(refresh)
                check('dashboard submission failure restores usable controls',
                      'Synthetic executor admission failure' in detail.GetValue() and refresh.IsEnabled())
                detail.SetFocus()
                click(refresh)
                check('dashboard asynchronous refresh preserves details focus', pump(refresh.IsEnabled)
                      and wx.Window.FindFocus() == detail)
                named(dashboard, 'Allowed CPU slots').SetValue(2)
                named(dashboard, 'Declared memory allowance in MiB').SetValue(1024)
                named(dashboard, 'Declared temporary disk allowance in GiB').SetValue(1)
                named(dashboard, 'Keep free disk space in MiB').SetValue(64)
                click(named(dashboard, 'Save allowances'))
                check('native resource policy saves through the shared broker',
                      pump(lambda: 'Resource allowances saved.' in detail.GetValue())
                      and ResourceBroker.for_app(library.root).limits.cpu_slots == 2)
                check('dashboard Escape routes to explicit close', dashboard.GetEscapeId() == wx.ID_CLOSE)
                named(dashboard, 'Close').SetFocus()
                keyboard = wx.UIActionSimulator()
                dashboard_escape_received = request_modal_escape(dashboard, keyboard,
                    escape_key=wx.WXK_ESCAPE, close_code=wx.ID_CLOSE, pump=pump)
                check('dashboard native Escape requests close and hides its window', dashboard_escape_received)
            except Exception as exc:
                dashboard_errors.append(str(exc))
            finally:
                if dashboard.IsModal() and dashboard.IsShown():
                    dashboard.EndModal(wx.ID_CLOSE)

        wx.CallLater(200, dashboard_checks)
        try:
            dashboard_close_code = operations_module.show_operations(frame)
        finally:
            operations_module.ThreadPoolExecutor = original_executor
        if dashboard_errors:
            raise AssertionError('; '.join(dashboard_errors))
        check('dashboard native Escape ends its modal lifecycle',
              dashboard_escape_received and dashboard_close_code == wx.ID_CLOSE)

        measured, played, pcm_paths = {}, threading.Event(), []
        class CapturePlayback:
            available = True
            state = 'stopped'
            def play(self, path):
                pcm_paths.append(Path(path))
                with wave.open(str(path), 'rb') as pcm:
                    measured.update(frames=pcm.getnframes(), rate=pcm.getframerate(), width=pcm.getsampwidth())
                    raw = pcm.readframes(pcm.getnframes())
                measured['peak'] = max((abs(value[0]) for value in struct.iter_unpack('<h', raw)), default=0)
                measured['thread'] = threading.get_ident()
                played.set()
            def close(self): pass
        registry = VoiceRegistry((NativeSapiProvider(),))
        progress_voices = tuple(voice for voice in registry.voices()
            if voice.language.replace('_', '-').split('-')[0].lower() == 'en'
            and 'volume' in voice.capabilities)
        check('native progress fixture explicitly selects an installed English SAPI voice', bool(progress_voices))
        selected_voice = progress_voices[0]
        provider = LocalProgressSpeech(registry, selected_voice.id,
            library_root=library.root, _player_factory=CapturePlayback)
        preferences = ProgressPreferences(mode='speech', volume=35)
        adapter = snapshot_speech_provider(provider)
        speech = ProgressFeedback(preferences, speech_provider=adapter)
        reporter = ProgressHub().start('Verifying local transcript files', total=2)
        reporter.update(completed=1, phase='Verifying local transcript files')
        try:
            check('native progress speech queues without blocking the GUI',
                  speech.update(reporter.snapshot()) is not None)
            check('actual native SAPI progress synthesis produces measured non-silent PCM',
                  pump(lambda: played.is_set() or bool(speech.last_error), timeout=45)
                  and played.is_set() and measured['frames'] > 0 and measured['width'] == 2
                  and measured['peak'] > 100 and measured['thread'] != threading.get_ident())
            speech.set_preferences(replace(preferences, muted=True))
            reporter.update(completed=2)
            check('quiet progress speech rejects queued playback', speech.update(reporter.snapshot()) is None)
        finally:
            speech.close()
            if speech._worker is not None:
                speech._worker.join(5)
            adapter.close()
        check('progress speech worker stops and removes temporary PCM',
              speech._worker is not None and not speech._worker.is_alive()
              and all(not path.exists() for path in pcm_paths))
        result['native_sapi_progress_pcm'] = {key: value for key, value in measured.items() if key != 'thread'}
        result['native_sapi_progress_voice'] = {'id': selected_voice.id, 'language': selected_voice.language}
        frame.Close()
        app.Yield()
    result['status'] = 'PASS'
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
