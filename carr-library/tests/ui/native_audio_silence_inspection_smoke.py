"""Installed native Studio inspection of real completed synthetic PCM.

The current producer creates one explicit four-second pause before the probe
forbids any subsequent render. Inspection and report export use their real
owners. Controlled status barriers make cancellation deterministic; the report
destination chooser supplies one explicit temporary path. No model, listening,
physical playback, manual file-picker or NVDA quality claim is made.
"""
from argparse import ArgumentParser
import faulthandler
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from time import monotonic
import traceback


MODULES = (
    'carr_library.ui.audio_studio', 'carr_library.audio.production',
    'carr_library.audio.ffmpeg', 'carr_library.audio.jobs', 'carr_library.audio.models',
    'carr_library.research.export_file', 'carr_library.ui.export_destination',
    'carr_library.reader.narration_text', 'carr_library.native_file_metadata',
    'carr_library.ui.operation_progress', 'carr_library.ui.save_all',
    'carr_library.audio.managed', 'carr_library.application',
)


def identities():
    prefix = Path(sys.prefix).resolve(strict=True)
    result = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('The native inspection imported an uninstalled implementation: ' + name)
        result[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


def run(work):
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import production
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.audio.jobs import RenderCancelled
    from carr_library.audio.runtime import resolve_ffmpeg
    from carr_library.ui import audio_studio
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController

    checks, failures, timers, errors = [], [], [], []
    metrics = {'setup_cancelled': 0, 'invalid_criteria_refused': 0,
        'completed_inspections': 0, 'exported_reports': 0, 'copied_reports': 0,
        'native_cancelled_inspections': 0, 'owner_cancelled_inspections': 0,
        'stale_setup_refusals': 0, 'inspection_worker_calls': 0,
        'export_worker_calls': 0, 'pcm_frames': 32000, 'pause_overlap_frames': 32000}
    original_identities = identities()
    work.mkdir(parents=False, exist_ok=False)
    owner_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    state = {'phase': 'initial', 'active': True, 'complete': False,
             'abort_started': None, 'barrier': None}
    deadline = monotonic() + 150

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    class Registry:
        def __init__(self):
            self.discoveries = 0
        def voices(self, *, cancelled=lambda: False):
            if cancelled():
                raise RenderCancelled('Synthetic metadata discovery cancelled')
            self.discoveries += 1
            return ({'id': 'synthetic:pause-only', 'name': 'Synthetic unused voice metadata',
                     'languages': ('uk',), 'available': True, 'controls': ()},)
        def capabilities(self):
            return ({'engine': 'synthetic', 'available': True},)
        def synthesize(self, *args, **kwargs):
            raise AssertionError('The pause-only fixture or inspection attempted synthesis')

    class NativeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            abort()
            return True

    gui = NativeApp(False)
    frame = studio = None
    registry = Registry()
    original_inspect = production.inspect_completed_silence
    original_render = production.render_project
    original_write = audio_studio.write_export
    rendered = original_files = None
    report_text = None
    report = None
    source = '[output sample_rate=8000 stems=false]\r\n# Точна синтетична пауза\r\n[pause 4]\r\n'

    def alive(window):
        return bool(window) and not window.IsBeingDeleted() and window.IsShown()

    def descendants(root):
        pending, values = list(root.GetChildren()), []
        while pending:
            value = pending.pop()
            values.append(value)
            if len(values) > 1024:
                raise ValueError('The native inspection control inventory exceeded its bound.')
            pending.extend(value.GetChildren())
        return values

    def named(root, name, kind):
        found = [item for item in descendants(root) if isinstance(item, kind) and item.GetName() == name]
        if len(found) != 1:
            raise AssertionError('Expected one named native control: ' + name)
        return found[0]

    def press(button):
        if not isinstance(button, wx.Button) or not alive(button) or not button.IsEnabled():
            raise AssertionError('The requested native inspection button is unavailable.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.GetEventHandler().ProcessEvent(event)

    def modals():
        names = {'Completed audio inspection criteria', 'Completed audio inspection report window'}
        found = [window for window in wx.GetTopLevelWindows()
                 if isinstance(window, wx.Dialog) and alive(window) and window.IsModal()
                 and window.GetName() in names]
        if any(window.GetParent() is not studio.dialog for window in found) or len(found) > 1:
            raise AssertionError('An active inspection dialog is duplicated or belongs to another owner.')
        return found

    def current_form(name):
        active = modals()
        if not active:
            return None
        if active[0].GetName() != name:
            raise AssertionError('A different owned inspection modal is active.')
        return active[0]

    def fixture_bytes():
        values = {'project': studio.controller.project_path.read_bytes(),
                  'original': original_path.read_bytes()}
        for item in sorted(output.rglob('*')):
            if item.is_file():
                values['output/' + item.relative_to(output).as_posix()] = item.read_bytes()
        return values

    def schedule(callback=None):
        timers.append(wx.CallLater(25, callback or poll))

    def abort():
        if not state['active']:
            return
        release.set()
        if state['abort_started'] is None:
            state['abort_started'] = monotonic()
        state['phase'] = 'aborting'
        schedule(cleanup_poll)

    def cleanup_poll():
        try:
            if studio is None or studio.closed:
                return
            active = modals()
            if active:
                active[0].EndModal(wx.ID_CANCEL)
                schedule(cleanup_poll)
                return
            if studio.busy:
                studio.cancelled.set()
                if monotonic() - state['abort_started'] > 10:
                    raise TimeoutError('The owned inspection worker did not settle during cleanup.')
                schedule(cleanup_poll)
                return
            if studio.dialog.IsModal():
                studio.dialog.EndModal(wx.ID_CANCEL)
        except Exception:
            failures.append(traceback.format_exc())
            raise

    def open_inspection(phase, barrier=None):
        state['barrier'] = barrier
        entered.clear()
        release.clear()
        state['phase'] = phase
        schedule()
        press(named(studio.dialog, 'Inspect completed audio', wx.Button))

    def accept(form, phase):
        named(form, 'Inspection threshold in dBFS', wx.TextCtrl).ChangeValue('-50')
        named(form, 'Inspection minimum duration in seconds', wx.TextCtrl).ChangeValue('3')
        state['phase'] = phase
        schedule()
        press(named(form, 'Inspect using these criteria', wx.Button))

    def inspect(*args, **kwargs):
        if threading.get_ident() == owner_thread:
            raise AssertionError('Inspection blocked the native event thread')
        metrics['inspection_worker_calls'] += 1
        observer = kwargs['on_status']
        mode = state['barrier']
        def status(message):
            if mode and not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('The explicit native cancellation barrier did not release.')
            observer(message)
        result = original_inspect(*args, **{**kwargs, 'on_status': status})
        if result['status'] == 'complete':
            state['last_report'] = result
        return result

    def write(path, text, **kwargs):
        if threading.get_ident() == owner_thread:
            raise AssertionError('Inspection export blocked the native event thread')
        metrics['export_worker_calls'] += 1
        return original_write(path, text, **kwargs)

    def no_render(*args, **kwargs):
        raise AssertionError('Inspection or report export attempted fresh rendering')

    def poll():
        nonlocal report_text
        if not state['active'] or state['phase'] == 'aborting':
            return
        try:
            if monotonic() >= deadline:
                raise TimeoutError('The native inspection sequence exceeded its finite deadline.')
            if studio is None or not alive(studio.dialog) or not studio.dialog.IsModal():
                schedule()
                return
            phase = state['phase']
            if phase == 'initial':
                if studio.busy:
                    schedule()
                    return
                studio.notebook.SetSelection(2)
                check('actual installed Windows inspection owner', sys.platform == 'win32'
                      and sys.flags.isolated and studio.dialog.GetParent() is frame)
                check('inspection is reachable from the named Production-page button',
                      alive(named(studio.dialog, 'Inspect completed audio', wx.Button))
                      and studio.notebook.GetPageText(2) == 'Production')
                state['discoveries'] = registry.discoveries
                check('fixture is actual current-producer stereo PCM with a recorded four-second pause',
                      wav_info(rendered['output']).frames == 32000
                      and wav_info(rendered['output']).rate == 8000
                      and wav_info(rendered['output']).channels == 2)
                open_inspection('cancel_setup')
                return
            if phase == 'cancel_setup':
                form = current_form('Completed audio inspection criteria')
                if form is None:
                    schedule()
                    return
                threshold = named(form, 'Inspection threshold in dBFS', wx.TextCtrl)
                minimum = named(form, 'Inspection minimum duration in seconds', wx.TextCtrl)
                check('native criteria expose explicit dBFS and minimum-second units',
                      threshold.GetValue() == '-50' and minimum.GetValue() == '3')
                check('criteria begin with native focus on the threshold', wx.Window.FindFocus() is threshold)
                threshold.ChangeValue('nan')
                press(named(form, 'Inspect using these criteria', wx.Button))
                check('nonfinite native criteria remain in the owned modal with a visible refusal',
                      form.IsModal() and 'Use a threshold' in named(
                          form, 'Inspection criteria validation', wx.TextCtrl).GetValue()
                      and metrics['inspection_worker_calls'] == 0)
                metrics['invalid_criteria_refused'] += 1
                state['phase'] = 'after_cancel_setup'
                schedule()
                press(named(form, 'Cancel inspection setup', wx.Button))
                return
            if phase == 'after_cancel_setup':
                if modals():
                    schedule()
                    return
                check('native setup Cancel starts no worker and preserves the completed render',
                      not studio.busy and metrics['inspection_worker_calls'] == 0
                      and studio.controller.result is rendered and fixture_bytes() == original_files)
                metrics['setup_cancelled'] += 1
                open_inspection('accept_setup')
                return
            if phase == 'accept_setup':
                form = current_form('Completed audio inspection criteria')
                if form is None:
                    schedule()
                    return
                accept(form, 'report')
                return
            if phase == 'report':
                form = current_form('Completed audio inspection report window')
                if form is None:
                    schedule()
                    return
                value = state['last_report']
                report_text = production.completed_silence_report_text(value)
                reader = named(form, 'Completed audio inspection report', wx.TextCtrl)
                check('actual worker completion opens one owned read-only native report',
                      not studio.busy and not reader.IsEditable()
                      and reader.GetValue() == report_text.replace('\r\n', '\n').replace('\r', '\n'))
                check('real inspection reports the exact retained PCM frames and explicit criteria',
                      value['sample_rate'] == 8000 and value['channels'] == 2 and value['frames'] == 32000
                      and value['criteria']['threshold_dbfs'] == -50
                      and value['criteria']['minimum_seconds'] == 3
                      and value['interval_count'] == 1 and value['qualifying_frames'] == 32000)
                check('recorded authored pause overlap is context while background music remains unknown',
                      value['intervals'][0]['annotations'] == {
                          'authored_pause_overlap_frames': 32000, 'spoken_line_overlap_frames': 0,
                          'declared_music_clip_overlap_frames': 0}
                      and value['context']['background_music'] == 'not-recorded-in-timeline'
                      and 'Review warnings only' in reader.GetValue()
                      and 'Background scene music is unknown' in reader.GetValue())
                check('inspection keeps the exact source and prior completed result',
                      studio.controller.result is rendered and studio.controller.script == source
                      and fixture_bytes() == original_files)
                metrics['completed_inspections'] += 1
                press(named(form, 'Copy inspection report', wx.Button))
                copied = wx.TextDataObject()
                if not wx.TheClipboard.Open():
                    raise AssertionError('The native clipboard could not be read after Copy.')
                try:
                    accepted = wx.TheClipboard.GetData(copied)
                finally:
                    wx.TheClipboard.Close()
                check('native Copy transfers the complete inspection text',
                      accepted and copied.GetText().replace('\r\n', '\n').replace('\r', '\n')
                      == report_text.replace('\r\n', '\n').replace('\r', '\n'))
                metrics['copied_reports'] += 1
                check('report actions retain named Copy Export and Close controls',
                      all(named(form, name, wx.Button).IsEnabled() for name in (
                          'Copy inspection report', 'Export inspection report', 'Close inspection report')))
                state['phase'] = 'after_export'
                schedule()
                press(named(form, 'Export inspection report', wx.Button))
                return
            if phase == 'after_export':
                if studio.busy or modals() or not export_path.exists():
                    schedule()
                    return
                check('approved report export completes on the existing Studio worker',
                      metrics['export_worker_calls'] == 1
                      and export_path.read_text('utf-8') == report_text
                      and 'Inspection report saved:' in studio.status.GetValue())
                check('export preserves every completed production and project byte',
                      studio.controller.result is rendered and fixture_bytes() == original_files)
                metrics['exported_reports'] += 1
                open_inspection('stale_setup')
                return
            if phase == 'stale_setup':
                form = current_form('Completed audio inspection criteria')
                if form is None:
                    schedule()
                    return
                frame.library = object()
                press(named(form, 'Inspect using these criteria', wx.Button))
                check('changed current library refuses the still-open criteria before dispatch',
                      form.IsModal() and 'changed' in named(
                          form, 'Inspection criteria validation', wx.TextCtrl).GetValue()
                      and metrics['inspection_worker_calls'] == 1)
                metrics['stale_setup_refusals'] += 1
                state['phase'] = 'after_stale_setup'
                schedule()
                press(named(form, 'Cancel inspection setup', wx.Button))
                return
            if phase == 'after_stale_setup':
                if modals():
                    schedule()
                    return
                frame.library = library
                open_inspection('cancel_accept', 'cancel')
                return
            if phase == 'cancel_accept':
                form = current_form('Completed audio inspection criteria')
                if form is None:
                    schedule()
                    return
                accept(form, 'cancel_running')
                return
            if phase == 'cancel_running':
                if not entered.is_set():
                    schedule()
                    return
                check('native Cancel remains available while real inspection owns its inputs',
                      studio.busy and named(studio.dialog, 'Cancel studio operation', wx.Button).IsEnabled()
                      and studio.controller._render_lock.locked())
                press(named(studio.dialog, 'Cancel studio operation', wx.Button))
                release.set()
                state['phase'] = 'cancel_settled'
            elif phase == 'cancel_settled':
                if studio.busy:
                    schedule()
                    return
                check('native Cancel reaches the actual reader and settles without a report',
                      len(errors) == 1 and isinstance(errors[0], RenderCancelled)
                      and not modals() and not studio.controller._render_lock.locked())
                check('cancelled inspection preserves the prior result and completed files',
                      studio.controller.result is rendered and fixture_bytes() == original_files)
                metrics['native_cancelled_inspections'] += 1
                open_inspection('owner_accept', 'owner')
                return
            elif phase == 'owner_accept':
                form = current_form('Completed audio inspection criteria')
                if form is None:
                    schedule()
                    return
                accept(form, 'owner_running')
                return
            elif phase == 'owner_running':
                if not entered.is_set():
                    schedule()
                    return
                frame.library = object()
                release.set()
                state['phase'] = 'owner_settled'
            elif phase == 'owner_settled':
                if studio.busy:
                    schedule()
                    return
                check('changed library ownership cancels the actual worker before late display',
                      len(errors) == 2 and isinstance(errors[1], RenderCancelled)
                      and not modals() and not studio.controller._render_lock.locked())
                check('owner cancellation cannot overwrite the current completed production',
                      studio.controller.result is rendered and fixture_bytes() == original_files)
                metrics['owner_cancelled_inspections'] += 1
                frame.library = library
                check('inspection and export never request another voice inventory or synthesis',
                      registry.discoveries == state['discoveries']
                      and metrics['inspection_worker_calls'] == 3 and metrics['export_worker_calls'] == 1)
                check('native inspection restores its ordinary controls after owned work',
                      named(studio.dialog, 'Inspect completed audio', wx.Button).IsEnabled()
                      and not named(studio.dialog, 'Cancel studio operation', wx.Button).IsEnabled())
                state['active'] = False
                state['complete'] = True
                press(named(studio.dialog, 'Close Audio Studio', wx.Button))
                return
            schedule()
        except Exception:
            failures.append(traceback.format_exc())
            abort()

    try:
        library = LibraryApp(work / 'library')
        library.create_workspace('Synthetic completed-audio inspection qualification')
        frame = wx.Frame(None, title='Completed-audio inspection qualification owner', size=(1000, 860))
        frame.library = library
        frame.Show()
        output = work / 'production'
        output.mkdir()
        original_path = work / 'original-script.txt'
        original_path.write_bytes(source.encode('utf-8'))
        controller = StudioController(private_root=library.root, registry=registry,
                                      ffmpeg=os.environ.get('CARR_FFMPEG'))
        controller.new(source, input_kind='script')
        controller.configure(voice='synthetic:pause-only', language='uk', output_dir=output)
        controller.save(work / 'inspection-studio.json')
        ffmpeg = resolve_ffmpeg(controller.ffmpeg)
        if ffmpeg is None:
            raise ValueError('The native fixture requires the existing pinned CARR_FFMPEG.')
        rendered = original_render(controller.plan(), output, engine=registry, ffmpeg=ffmpeg)
        controller.result = rendered
        studio = AudioStudioDialog(frame, controller)
        original_files = fixture_bytes()
        export_path = work / 'inspection-report.txt'
        studio._inspection_report_path = lambda parent: export_path
        original_error = studio._error
        def observed_error(error):
            errors.append(error)
            original_error(error)
        studio._error = observed_error
        production.inspect_completed_silence = inspect
        production.render_project = no_render
        audio_studio.write_export = write
        schedule()
        studio.show()
        if failures:
            raise AssertionError('\n'.join(failures))
        check('native Close settles the existing Studio inspection executor',
              state['complete'] and studio.closed)
        studio.executor.shutdown(wait=True, cancel_futures=True)
        check('no inspection or export worker survives native close',
              all(not thread.is_alive() for thread in studio.executor._threads))
        check('exact Unicode source and original file survive every native inspection action',
              controller.script == source and controller.draft.source_text == source
              and original_path.read_bytes() == source.encode('utf-8'))
        check('completed output manifest timeline PCM and project remain byte-for-byte unchanged',
              fixture_bytes() == original_files and controller.result is rendered)
        check('only two acknowledged cancellation errors occurred',
              len(errors) == 2 and all(isinstance(error, RenderCancelled) for error in errors))
        check('installed inspection implementations remain unchanged', identities() == original_identities)
        report = {'status': 'PASS', 'actual_windows': True, 'isolated': True,
            'native_controls': True, 'native_events': 'PASS', 'source_preserved': True,
            'completion_preserved': True, 'read_only_inspection': True, 'cleanup_complete': True,
            'report_export': 'CURRENT_APPROVED_PUBLISHER',
            'fixture': 'REAL_SYNTHETIC_PCM_CURRENT_PRODUCER',
            'native_file_picker': 'NOT_RUN', 'physical_audio': 'NOT_RUN',
            'nvda': 'NOT_RUN', 'acoustic_accuracy': 'NOT_QUALIFIED',
            'checks': checks, 'total_checks': len(checks), 'metrics': metrics,
            'module_origins': original_identities, 'wx': wx.version()}
    finally:
        state['active'] = False
        release.set()
        production.inspect_completed_silence = original_inspect
        production.render_project = original_render
        audio_studio.write_export = original_write
        for timer in timers:
            timer.Stop()
        if studio is not None:
            studio.cancelled.set()
            studio.timer.Stop()
            studio.playback_timer.Stop()
            studio.executor.shutdown(wait=True, cancel_futures=True)
            studio.playback.close()
            getattr(studio, '_save_unregister', lambda: None)()
            getattr(studio, '_progress_unregister_player', lambda: None)()
            if studio.dialog and not studio.dialog.IsBeingDeleted():
                if studio.dialog.IsModal():
                    raise RuntimeError('An owned inspection modal did not unwind before final cleanup.')
                studio.dialog.Destroy()
        if frame:
            frame.Destroy()
        gui.Yield()
        gui.Destroy()
        if failures:
            raise AssertionError('\n'.join(failures))
    return report


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32' or not sys.flags.isolated:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Use actual isolated installed Windows Python.',
                          'nvda': 'NOT_RUN', 'acoustic_accuracy': 'NOT_QUALIFIED'}), flush=True)
        return 77
    handle, report = None, {'status': 'FAIL', 'reason': 'The native inspection probe did not finish.'}
    faulthandler.enable()
    faulthandler.dump_traceback_later(210, exit=True)
    print('PHASE installed completed-audio inspection started', flush=True)
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        report = run(args.work.absolute())
    except Exception as error:
        traceback.print_exc()
        report = {'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error),
                  'actual_windows': sys.platform == 'win32', 'isolated': bool(sys.flags.isolated),
                  'nvda': 'NOT_RUN', 'physical_audio': 'NOT_RUN', 'acoustic_accuracy': 'NOT_QUALIFIED'}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                report = {'status': 'FAIL', 'reason': 'Native runtime cleanup: ' + str(error),
                          'nvda': 'NOT_RUN', 'physical_audio': 'NOT_RUN',
                          'acoustic_accuracy': 'NOT_QUALIFIED'}
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
