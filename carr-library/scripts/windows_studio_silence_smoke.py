"""Actual native Studio silence criteria and read-only completed-audio report.

A small synthetic Tone voice creates a genuine completed FFmpeg production.
The actual saved-job picker, Studio controls, keys, shared resource admission,
inspection, result and existing Studio executor are exercised without replacing
any producer result. No physical output, speech quality or NVDA claim is made.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
from time import monotonic, sleep
from types import SimpleNamespace as N
import traceback
import wave


SCENARIOS = (
    'actual_completed_production',
    'native_saved_job_selection',
    'invalid_criteria_no_admission',
    'native_explicit_criteria',
    'actual_readonly_inspection_report',
    'same_studio_worker_and_no_playback',
    'second_criteria_and_complete_scan',
    'unchanged_job_source_and_artifacts',
    'confirmed_native_and_worker_cleanup',
)


def _sibling(name, identity):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(identity, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToneVoices:
    """Disclosed authored PCM provider; real current production handles its WAV."""
    def voices(self):
        return (N(id='tone', name='Synthetic fixture', language='und', engine='fixture'),)

    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        if cancelled():
            raise InterruptedError('Synthetic fixture cancelled')
        with wave.open(str(destination), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            output.writeframes(b''.join(
                struct.pack('<h', round(3000 * math.sin(index * math.tau * 440 / 24000)))
                for index in range(6000)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, required=True)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps(dict(schema='carr-native-studio-silence/1', result='NOT_RUN',
            reason='Actual Windows desktop is required.', checks=0)), flush=True)
        return 77
    if not args.native_child:
        from carr_library.audio.models import AudioBudget
        from carr_library.audio.process import run_process
        result = run_process(
            [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
             '--audio-evidence', str(args.audio_evidence.resolve()), '--native-child'],
            budget=AudioBudget(timeout=360, max_log_bytes=2 * 1024 * 1024),
            owned_tree=True, allow_failure=True)
        sys.stdout.write(result.stdout.decode('utf-8', errors='replace'))
        sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
        sys.stdout.flush()
        sys.stderr.flush()
        return result.returncode
    faulthandler.dump_traceback_later(330, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _native(args)
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _native(args):
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application, durable_state, runtime_settings
    from carr_library.operations import broker
    from carr_library.audio import models, process, cleanup
    from carr_library.backup_compat import pcm
    from carr_library.studio import render, project, publishing, interchange, silence
    from carr_library.ui import desktop_app, studio_dialog, operation_progress, progress_panel

    binding = _sibling('windows_publishing_smoke.py', '_silence_installed_bindings')
    helpers = _sibling('windows_speech_preparation_smoke.py', '_silence_owned_modals')
    modules = (application, durable_state, broker, runtime_settings, models, process,
               cleanup, pcm, render, project, publishing, interchange, silence,
               desktop_app, studio_dialog, operation_progress, progress_panel)
    before = binding.verify_installed_sources(*modules)
    script_paths = tuple(Path(__file__).with_name(name) for name in (
        Path(__file__).name, 'windows_publishing_smoke.py', 'windows_speech_preparation_smoke.py'))
    scripts = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in script_paths}
    evidence = durable_state.read_json(args.audio_evidence, maximum=64 * 1024)
    selected_audio = N(ffmpeg=evidence['ffmpeg']['path'], ffprobe=evidence['ffprobe']['path'])
    audio_before = binding.verify_audio_evidence(selected_audio, args.audio_evidence)
    evidence_sha = sha256(args.audio_evidence.read_bytes()).hexdigest()
    checks, errors, passed = [], [], {}
    frame = timer = failure_handler = None
    state = N(waiting=None, description='', entered=False, finished=False,
              returned=True, started=monotonic())

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    def completed(name):
        if name not in SCENARIOS or name in passed:
            raise AssertionError('Unknown or repeated native scenario: ' + name)
        passed[name] = 'PASS'

    def failed(detail):
        errors.append(detail)
        print('FAIL: native Studio silence\n' + detail, file=sys.stderr, flush=True)

    root = durable_state.local_path(Path(tempfile.mkdtemp(prefix='carr-studio-silence-')), exists=True)
    durable_state.regular(root, directory=True)
    print('AUTHORED_NATIVE_FIXTURE ' + str(root), flush=True)
    library = application.LibraryApp(root / 'library')
    output = root / 'outputs'
    output.mkdir()
    setup = render.ProductionService(library.root / 'state' / 'studio', voices=ToneVoices())
    original_runtime = setup._runtime
    runtime_checks = []
    def checked_runtime(*arguments, **options):
        actual = original_runtime(*arguments, **options)
        runtime_checks.append(binding.verify_audio_evidence(actual, args.audio_evidence))
        return actual
    setup._runtime = checked_runtime
    script = '[Scene: Checked audio — Архів]\nNarrator: Exact retained line.\n[Pause: 0.5]\nNarrator: Final exact line.\n'
    try:
        selected_project = setup.create(script, title='Native silence inspection', voice_ids=('tone',))
        job_id = setup.submit(selected_project, output, stems=False)
        saved = setup.run(job_id)
        check('the real current producer completed its authored Tone/FFmpeg job',
              saved['status'] == 'complete' and runtime_checks
              and all(value == audio_before for value in runtime_checks))
        artifact_bytes = {row['path']: Path(row['path']).read_bytes() for row in saved['artifacts']}
        for row in saved['artifacts']:
            check('a real completed artifact matches its exact saved receipt',
                  len(artifact_bytes[row['path']]) == row['size']
                  and sha256(artifact_bytes[row['path']]).hexdigest() == row['sha256'])
        receipt_path = setup.jobs_root / job_id / 'job.json'
        receipt_bytes = receipt_path.read_bytes()
        # ProjectStore keeps its typed source in its public saved document.
        project_before = setup.projects.load(selected_project.id).to_dict()
        check('the completed project retains its exact original Unicode script',
              project_before['original_script'] == script)
    finally:
        check('the setup service confirms closure before the real Studio opens', setup.close() is True)
    completed('actual_completed_production')

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failed(detail)
                if timer is not None:
                    timer.Stop()
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = (wintypes.HWND,), wintypes.BOOL
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = (), wintypes.HWND

    def inside(peer, ancestor):
        for _ in range(64):
            if peer is None or not peer:
                return False
            if peer is ancestor:
                return True
            peer = peer.GetParent()
        raise ValueError('Native owner chain exceeds its bound.')

    def owners():
        windows = tuple(wx.GetTopLevelWindows())
        if len(windows) > 256:
            raise ValueError('Native owner inventory exceeds its bound.')
        modals = [p for p in windows if p and isinstance(p, wx.Dialog) and p.IsModal()
                  and frame and inside(p, frame)]
        hidden = [p for p in modals if not p.IsShown()]
        if hidden:
            chosen = hidden[0]
        elif modals:
            focus = wx.Window.FindFocus()
            top = wx.GetTopLevelParent(focus) if focus else None
            active = [p for p in modals if p.IsEnabled()]
            chosen = next((p for p in active if p is top), None)
            if chosen is None:
                if len(active) != 1:
                    raise RuntimeError('The current owned modal is still returning.')
                chosen = active[0]
        else:
            return ()
        return (N(dialog=chosen, close=lambda value=chosen: value.EndModal(wx.ID_CANCEL)),)

    abort = helpers._OwnedModalAbort(wx, gui, owners, failed)
    def select_failure(value):
        nonlocal failure_handler
        failure_handler = value
    helpers._nested_modal_failure_acceptance(wx, gui, check, select_failure)

    def pump(predicate, message, seconds=8):
        deadline = monotonic() + seconds
        while not predicate():
            if errors:
                raise RuntimeError('A real native callback failed.')
            if monotonic() >= deadline:
                raise TimeoutError(message)
            gui.Yield()
            sleep(.01)

    def focus(control):
        check('named control is alive, visible and enabled',
              bool(control) and control.IsShownOnScreen() and control.IsEnabled())
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle()
             and control.GetParent().GetClientRect().Contains(control.GetRect()),
             'The complete named Studio control did not receive visible foreground focus.')

    def key(code, modifiers=()):
        pressed = []
        try:
            for modifier in modifiers:
                check('native modifier down', simulator.KeyDown(modifier))
                pressed.append(modifier)
            check('native key down', simulator.KeyDown(code))
            pressed.append(code)
            sleep(.015)
        finally:
            for code in reversed(pressed):
                check('native key released', simulator.KeyUp(code))

    def press(control):
        focus(control)
        key(wx.WXK_SPACE)

    def enter(control, value):
        focus(control)
        key(ord('A'), (wx.WXK_CONTROL,))
        check('native ASCII numeric text accepted', simulator.Text(str(value).encode('ascii')))
        key(wx.WXK_TAB)
        pump(lambda: control.GetValue() == str(value), 'Actual native numeric entry did not arrive.')

    def descendants(parent):
        pending, result = list(parent.GetChildren()), []
        while pending:
            if len(result) + len(pending) > 256:
                raise ValueError('Native Studio control inventory exceeds its bound.')
            peer = pending.pop()
            if not peer:
                raise ValueError('Native Studio control disappeared.')
            result.append(peer)
            pending.extend(peer.GetChildren())
        return result

    def named(parent, name):
        found = [peer for peer in descendants(parent) if peer.GetName() == name]
        if len(found) != 1:
            raise AssertionError('Expected one exact named control: ' + name)
        return found[0]

    def button(parent, label):
        found = [peer for peer in descendants(parent) if isinstance(peer, wx.Button)
                 and peer.GetLabel().replace('&', '') == label]
        if len(found) != 1:
            raise AssertionError('Expected one exact native button: ' + label)
        return found[0]

    def titled(title):
        rows = [p for p in wx.GetTopLevelWindows() if p and isinstance(p, wx.Dialog)
                and p.GetTitle() == title and p.IsShown() and p.IsModal()]
        if len(rows) > 1:
            raise ValueError('Native dialog title is ambiguous.')
        return rows[0] if rows else None

    def waiting(predicate, message, seconds=60):
        return predicate, message, monotonic() + seconds

    actual_executor = studio_dialog.ThreadPoolExecutor
    actual_controller = studio_dialog.StudioController
    actual_inspect = silence.inspect_completed_silence
    executors, futures, controllers, inspections, deaths = [], [], [], [], []
    def observed_executor(*arguments, **options):
        check('the actual Studio requests its existing single-worker executor',
              options.get('max_workers') == 1 and options.get('thread_name_prefix') == 'audio-studio')
        executor = actual_executor(*arguments, **options)
        submit = executor.submit
        def submitted(operation, *args, **kwargs):
            future = submit(operation, *args, **kwargs)
            futures.append(future)
            return future
        executor.submit = submitted
        executors.append(executor)
        return executor
    def observed_controller(*arguments, **options):
        value = actual_controller(*arguments, **options)
        controllers.append(value)
        return value
    def observed_inspection(*arguments, **options):
        result = actual_inspect(*arguments, **options)
        inspections.append(N(result=result, service=arguments[0], job=arguments[1],
                             options=dict(options), thread=threading.get_ident()))
        return result

    studio_dialog.ThreadPoolExecutor = observed_executor
    studio_dialog.StudioController = observed_controller
    silence.inspect_completed_silence = observed_inspection
    frame = desktop_app.DesktopFrame(library)
    frame.SetSize((1180, 980))
    frame.Show()
    gui.SetTopWindow(frame)
    desktop_executor = frame.executor

    def scenario():
        focus(frame.status)
        key(ord('A'), (wx.WXK_CONTROL, wx.WXK_ALT))
        yield waiting(lambda: titled('Audio Studio — script, voices and production') is not None,
                      'The existing native Studio command did not open.')
        studio = titled('Audio Studio — script, voices and production')
        studio_id = studio.GetId()
        studio.Bind(wx.EVT_WINDOW_DESTROY,
            lambda event: (deaths.append(event.GetId()) if event.GetId() == studio_id else None, event.Skip()))
        yield waiting(lambda: not studio.working and button(studio, 'Saved jobs').IsEnabled(),
                      'Actual current Studio voice discovery did not settle.')
        check('one actual Studio controller and executor own the view',
              len(controllers) == len(executors) == 1
              and frame.executor is desktop_executor
              and controllers[0].service.root == library.root / 'state' / 'studio')
        press(button(studio, 'Saved jobs'))
        yield waiting(lambda: titled('Production jobs') is not None, 'The actual saved-job picker did not open.')
        picker = titled('Production jobs')
        lists = [peer for peer in descendants(picker) if isinstance(peer, wx.ListBox)]
        check('the actual saved-job picker contains the genuine completed job',
              len(lists) == 1 and lists[0].GetCount() == 1
              and 'Native silence inspection; complete;' in lists[0].GetString(0))
        focus(lists[0])
        key(wx.WXK_HOME)
        key(wx.WXK_RETURN)
        yield waiting(lambda: not picker and button(studio, 'Inspect silence...').IsEnabled(),
                      'The actual saved-job selection did not return and enable inspection.')
        check('selected native project text remains the actual saved original',
              named(studio, 'Script — prose or named roles; Help explains directions').GetValue() == script
              and controllers[0].project.to_dict() == project_before)
        completed('native_saved_job_selection')
        threshold = named(studio, 'Silence threshold in dBFS; dot decimal')
        minimum = named(studio, 'Minimum quiet duration in seconds; dot decimal')
        check('native silence defaults are explicit', threshold.GetValue() == '-60' and minimum.GetValue() == '2')
        enter(threshold, 'nan')
        prior = studio.status.GetValue()
        count = len(futures)
        press(button(studio, 'Inspect silence...'))
        yield waiting(lambda: studio.status.GetValue() != prior and not studio.working,
                      'Invalid criteria did not produce the actual native refusal.')
        check('invalid native criteria admit no inspection or Studio worker',
              not inspections and len(futures) == count
              and 'Review warnings only' not in studio.status.GetValue())
        completed('invalid_criteria_no_admission')
        enter(threshold, '-80')
        enter(minimum, '0.3')
        completed('native_explicit_criteria')
        press(button(studio, 'Inspect silence...'))
        yield waiting(lambda: len(inspections) == 1 and not studio.working
                      and studio.status.GetValue().startswith('Completed audio — silence inspection\n'),
                      'The real completed inspection did not reach the native report.')
        first = inspections[0]
        check('actual inspection uses the selected job and typed criteria',
              first.job == job_id and first.service is controllers[0].service
              and first.result['status'] == 'complete'
              and first.result['criteria']['threshold_dbfs'] == -80
              and first.result['criteria']['minimum_seconds'] == .3
              and first.result['interval_count'] >= 1
              and any(row['frames'] >= .3 * first.result['sample_rate']
                      for row in first.result['intervals']))
        focus(studio.status)
        check('the exact genuine inspection report is focused and read-only',
              studio.status.GetValue() == silence.silence_report_text(first.result)
              and not studio.status.IsEditable() and wx.Window.FindFocus() is studio.status)
        check('the report binds actual master and timeline bytes',
              first.result['master_sha256'] == sha256((Path(saved['destination']) / 'master.wav').read_bytes()).hexdigest()
              and first.result['timeline_sha256'] == sha256((Path(saved['destination']) / 'timeline.json').read_bytes()).hexdigest()
              and first.result['source_sha256'] == sha256(script.encode('utf-8')).hexdigest())
        completed('actual_readonly_inspection_report')
        check('inspection uses the real existing worker and releases the actual resource lease',
              first.thread != threading.get_ident() and len(executors) == 1
              and not first.service.broker._active()
              and getattr(studio, '_audio_player', None) is None)
        completed('same_studio_worker_and_no_playback')
        enter(threshold, '-60')
        enter(minimum, '3')
        press(button(studio, 'Inspect silence...'))
        yield waiting(lambda: len(inspections) == 2 and not studio.working
                      and studio.status.GetValue() == silence.silence_report_text(inspections[-1].result),
                      'The actual second criteria did not produce its new complete report.')
        second = inspections[1]
        check('the second real scan uses the new criteria without partial or stale intervals',
              second.result['criteria']['threshold_dbfs'] == -60
              and second.result['criteria']['minimum_seconds'] == 3
              and second.result['interval_count'] == 0
              and second.result['frames'] == first.result['frames']
              and second.result['master_sha256'] == first.result['master_sha256'])
        completed('second_criteria_and_complete_scan')
        check('completed job, original script and every actual published artifact remain byte-exact',
              receipt_path.read_bytes() == receipt_bytes
              and controllers[0].service.get(job_id) == saved
              and controllers[0].service.projects.load(selected_project.id).to_dict() == project_before
              and all(Path(path).read_bytes() == raw for path, raw in artifact_bytes.items()))
        completed('unchanged_job_source_and_artifacts')
        press(button(studio, 'Close'))
        yield waiting(lambda: not studio and deaths == [studio_id]
                      and all(future.done() for future in futures),
                      'Actual Studio closure did not finish its native peer and existing worker cleanup.')
        check('the genuine current Studio service and all submitted operations have completed',
              controllers[0].service._closed.is_set()
              and all(future.exception() is None for future in futures)
              and not owners())
        state.finished = True
        gui.ExitMainLoop()

    sequence = scenario()
    def drive(event=None):
        if state.entered or state.finished:
            return
        state.entered = True
        try:
            if monotonic() - state.started > 270:
                raise TimeoutError('Native Studio silence scenario expired: ' + state.description)
            if state.waiting is not None:
                predicate, state.description, deadline = state.waiting
                if not predicate():
                    if monotonic() > deadline:
                        raise TimeoutError(state.description)
                    return
                state.waiting = None
            try:
                state.waiting = next(sequence)
            except StopIteration:
                if not state.finished:
                    raise AssertionError('Native silence scenario ended before final proof.')
        except (Exception, KeyboardInterrupt):
            failed(traceback.format_exc())
            timer.Stop()
            abort.request()
        finally:
            state.entered = False

    timer = wx.Timer(frame)
    frame.Bind(wx.EVT_TIMER, drive, timer)
    timer.Start(25)
    try:
        gui.MainLoop()
    finally:
        timer.Stop()
        timer.SetOwner(None)
        abort.stop()
        studio_dialog.ThreadPoolExecutor = actual_executor
        studio_dialog.StudioController = actual_controller
        silence.inspect_completed_silence = actual_inspect
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=False)
        if frame:
            frame._closing = True
            frame.cancel_event.set()
        desktop_executor.shutdown(wait=True, cancel_futures=False)
        if not library.close_search():
            raise RuntimeError('The actual Studio fixture retains its search cursor owner.')
        if frame:
            if getattr(frame, 'progress_panel', None) is not None:
                frame.progress_panel.close()
            frame.Destroy()
        gui.Yield()
    if errors or not state.finished:
        print(json.dumps(dict(schema='carr-native-studio-silence/1', result='FAIL',
            checks=len(checks), scenarios=passed, errors=errors, fixture=str(root),
            waiting=state.description)), flush=True)
        return 1
    completed('confirmed_native_and_worker_cleanup')
    check('all fixed native silence scenarios completed', tuple(passed) == SCENARIOS)
    check('all selected installed sources remain byte-exact', binding.verify_installed_sources(*modules) == before)
    check('all native scripts and reused modal helpers remain byte-exact',
          {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in script_paths} == scripts)
    check('the exact selected actual audio evidence and executable bytes remain unchanged',
          sha256(args.audio_evidence.read_bytes()).hexdigest() == evidence_sha
          and binding.verify_audio_evidence(selected_audio, args.audio_evidence) == audio_before)
    shutil.rmtree(root)
    print(json.dumps(dict(schema='carr-native-studio-silence/1', result='PASS',
        checks=len(checks), check_names=checks, scenarios=passed, selected_sources=before, scripts=scripts,
        actual_saved_production='PASS', actual_native_criteria='PASS', actual_readonly_report='PASS',
        source_sha256=sha256(script.encode('utf-8')).hexdigest(), audio_runtime=audio_before,
        synthetic_voice='DISCLOSED_TONE_PCM_FIXTURE', physical_output='NOT_RUN',
        speech_quality='NOT_RUN', NVDA='NOT_RUN'), ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
