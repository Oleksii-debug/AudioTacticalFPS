"""Native Windows platform controls and real local SAPI progress synthesis.

ASR service responses are explicitly synthetic. This exercises the installed
native workstation, events, shared run_job, checkpoint UI and derivative flow;
it is not model inference or recognition-quality evidence. SAPI discovery and
PCM synthesis are real; playback is intercepted because CI may lack a device.
Manual NVDA acceptance and physical playback remain NOT_RUN.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import threading
import time
import wave


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

    def read_transcript(self, identity, source, *, edited=False):
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
    import wx
    from carr_library.application import LibraryApp
    from carr_library.operations import ResourceBroker
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.transcription_dialog import build_transcription_dialog, TranscriptionController
    from carr_library.ui.progress_speech import LocalProgressSpeech
    import carr_library.ui.transcription_dialog as transcription_module
    import carr_library.ui.operations_dialog as operations_module

    app = wx.App(False)
    result = {'status': 'RUNNING', 'platform': 'win32', 'checks': [],
              'asr_service': 'SYNTHETIC_UI_FIXTURE', 'model_inference': 'NOT_RUN',
              'manual_nvda': 'NOT_RUN', 'physical_playback': 'NOT_RUN',
              'origins': {module.__name__: str(Path(module.__file__).resolve())
                          for module in (transcription_module, operations_module)}}
    source_root = Path(__file__).resolve().parents[1] / 'src'
    assert all(not Path(path).is_relative_to(source_root) for path in result['origins'].values()), \
        'Platform smoke must exercise the installed distribution.'

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
        errors = []

        def workstation_checks():
            try:
                dialog.refresh()
                check('native ASR profile refresh uses the shared job host', pump(lambda: not dialog.pending))
                controls = {'Registered transcription runtime': wx.Choice,
                    'Source language code or auto': wx.TextCtrl, 'Selected local audio or video': wx.TextCtrl,
                    'Saved transcription jobs': wx.Choice, 'Transcription source result': wx.Choice,
                    'Transcript reading copy': wx.TextCtrl, 'Transcription progress and result': wx.TextCtrl}
                for name, native_type in controls.items():
                    check('ASR accessible native control: ' + name, isinstance(named(dialog, name), native_type))
                check('native ASR pages and Escape route are present',
                      dialog.pages.GetPageCount() == 3 and dialog.GetEscapeId() == wx.ID_CLOSE)
                dialog.pages.SetSelection(1)
                dialog.python_path.SetValue(str(root / 'synthetic-python.exe'))
                dialog.worker_path.SetValue(str(root / 'synthetic-worker.py'))
                dialog.model_path.SetValue(str(root / 'synthetic-model'))
                click(named(dialog, 'Inspect files'))
                check('runtime inspection completes without inference', pump(lambda: not dialog.pending)
                      and service.calls == ['inspect'] and not dialog.trust.GetValue())
                click(named(dialog, 'Register trusted runtime'))
                check('unchecked trust blocks runtime registration', service.calls == ['inspect'])
                dialog.trust.SetValue(True)
                click(named(dialog, 'Register trusted runtime'))
                check('explicit inspected fingerprint trust registers one profile',
                      pump(lambda: not dialog.pending) and service.calls == ['inspect', 'register'])
                dialog.pages.SetSelection(0)
                source = root / 'synthetic.wav'
                source.write_bytes(b'Synthetic UI fixture; not audio inference.')
                dialog.sources = (source,)
                dialog.source_paths.ChangeValue(str(source))
                dialog.language.SetValue('uk')
                click(dialog.start_button)
                check('native Start enters the isolated service boundary', pump(service.running.is_set))
                dialog.help_button.SetFocus()
                check('help retains focus during coalesced progress',
                      pump(lambda: 'Transcribing synthetic fixture' in dialog.status.GetValue())
                      and wx.Window.FindFocus() == dialog.help_button)
                click(dialog.pause_button)
                check('native Pause preserves a real paused UI outcome', pump(lambda: not dialog.pending)
                      and dialog.current_job['status'] == 'paused' and dialog.current_job['done'] == 1)
                click(dialog.start_button)
                check('native Resume reuses the stored job', pump(lambda: not dialog.pending)
                      and service.calls.count('create') == 1 and 'resume' in service.calls
                      and dialog.current_job['status'] == 'complete' and not dialog.start_button.IsEnabled())
                dialog.pages.SetSelection(2)
                click(named(dialog, 'Load transcript'))
                check('native transcript reader opens an independent reading copy',
                      pump(lambda: not dialog.pending) and dialog.document.source_text == service.original
                      and dialog.editor.GetValue() == service.original)
                check('explicit load focuses the transcript editor', pump(lambda: wx.Window.FindFocus() == dialog.editor))
                keyboard = wx.UIActionSimulator()
                keyboard.Char(wx.WXK_TAB)
                check('native Tab leaves the multiline editor for another dialog control',
                      pump(lambda: wx.Window.FindFocus() is not None and wx.Window.FindFocus() != dialog.editor)
                      and dialog.IsDescendant(wx.Window.FindFocus()))
                dialog.editor.SetValue('Corrected synthetic reading copy.')
                click(named(dialog, 'Save edits'))
                check('native edit save preserves original recognition text', pump(lambda: not dialog.pending)
                      and dialog.document.source_text == service.original
                      and service.edits['c' * 32] == 'Corrected synthetic reading copy.')
                click(named(dialog, 'Undo reading edit'))
                check('native Undo restores the independent source copy', dialog.editor.GetValue() == service.original)
                click(named(dialog, 'Redo reading edit'))
                check('native Redo restores the saved correction', dialog.editor.GetValue() == 'Corrected synthetic reading copy.')
                destination = root / 'new-transcript-export'
                dialog.export_to(destination)
                check('native export delegates into a new folder', pump(lambda: not dialog.pending)
                      and (destination / 'synthetic-original.txt').read_text(encoding='utf-8') == service.original)
                dialog.close_button.SetFocus()
                keyboard.Char(wx.WXK_ESCAPE)
                check('native Escape closes an idle ASR dialog and stops its timer',
                      pump(lambda: dialog.closed) and not dialog.timer.IsRunning())
            except Exception as exc:
                errors.append(str(exc))
                service.cancel(service.job['id']) if service.job else None
                dialog.cancel_event.set()
                pump(lambda: not dialog.pending, timeout=5)
            finally:
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CLOSE)

        wx.CallLater(150, workstation_checks)
        try:
            dialog.ShowModal()
        finally:
            dialog.closed = True
            dialog.cancel_event.set()
            dialog.timer.Stop()
            dialog.Destroy()
        if errors:
            raise AssertionError('; '.join(errors))

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

        def dashboard_checks():
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
                keyboard.Char(wx.WXK_ESCAPE)
                check('dashboard native Escape ends its modal lifecycle', pump(lambda: not dashboard.IsModal()))
            except Exception as exc:
                dashboard_errors.append(str(exc))
            finally:
                if dashboard.IsModal():
                    dashboard.EndModal(wx.ID_CLOSE)

        wx.CallLater(200, dashboard_checks)
        try:
            operations_module.show_operations(frame)
        finally:
            operations_module.ThreadPoolExecutor = original_executor
        if dashboard_errors:
            raise AssertionError('; '.join(dashboard_errors))

        measured, played, pcm_paths = {}, threading.Event(), []
        class CapturePlayback:
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
        speech = LocalProgressSpeech(player_factory=CapturePlayback)
        try:
            check('native progress speech queues without blocking the GUI',
                  speech.speak('In progress. Fifty percent. Verifying local transcript files.', volume=.35))
            check('actual native SAPI progress synthesis produces measured non-silent PCM',
                  pump(lambda: played.is_set() or speech.last_error is not None, timeout=45)
                  and played.is_set() and measured['frames'] > 0 and measured['width'] == 2
                  and measured['peak'] > 100 and measured['thread'] != threading.get_ident())
            speech.muted = True
            check('quiet progress speech rejects queued playback', not speech.speak('Muted.', volume=.35))
        finally:
            speech.close()
            if speech._thread is not None:
                speech._thread.join(5)
        check('progress speech worker stops and removes temporary PCM',
              speech._thread is not None and not speech._thread.is_alive()
              and all(not path.exists() for path in pcm_paths))
        result['native_sapi_progress_pcm'] = {key: value for key, value in measured.items() if key != 'thread'}
        frame.Close()
        app.Yield()
    result['status'] = 'PASS'
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
