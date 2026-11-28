"""Installed native assistant-to-ASR protocol flow; no model or language claim."""
import argparse
import faulthandler
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import enumerate as threads
from time import monotonic
import traceback

FIXTURE_SHA256 = '8fb51637e1d6aba3c2a54374f95bf5ace5844d4dbe2da0fdcf00fa02eface2ac'
MODULES = (
    'carr_library.assistant.transcription_tools', 'carr_library.assistant.models',
    'carr_library.assistant.service', 'carr_library.assistant.validation',
    'carr_library.assistant.snapshots', 'carr_library.assistant.planner',
    'carr_library.assistant.cli', 'carr_library.ui.assistant_dialog',
    'carr_library.transcription_service', 'carr_library.transcription_state',
    'carr_library.supplied_transcription_backup', 'carr_library.runtime_settings',
)


def origins():
    prefix = Path(sys.prefix).resolve(strict=True)
    result = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {p.casefold() for p in path.parts}:
            raise ValueError('The assistant transcription probe imported checkout implementation.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def fixture():
    path = Path(__file__).resolve().parents[1] / 'tests/assistant/transcription_fixture.py'
    if sha256(path.read_bytes()).hexdigest() != FIXTURE_SHA256:
        raise ValueError('The reviewed synthetic protocol fixture changed.')
    spec = importlib.util.spec_from_file_location('_native_assistant_asr_fixture', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, path


def run(work):
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService
    from carr_library.assistant.validation import validate_job
    from carr_library.library_backup import create_backup, verify_backup, restore_backup
    from carr_library.operations import ResourceBroker
    from carr_library.transcription_service import TranscriptionService
    from carr_library.ui.assistant_dialog import show_assistant

    work.mkdir(mode=0o700)
    helper, helper_path = fixture()
    checks, failures, timers = [], [], []
    deadline = monotonic() + 180
    metrics = {}
    accepted_id, accepted_receipt = None, None
    gui = wx.App(False)
    frame = wx.Frame(None, title='Current assistant transcription acceptance')
    frame.active, frame._closing = None, False
    frame.Show()
    gui.SetTopWindow(frame)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('CHECK ' + label, flush=True)

    def close_modals():
        for window in reversed(wx.GetTopLevelWindows()):
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    def failed():
        failures.append(traceback.format_exc())
        print(failures[-1], file=sys.stderr, flush=True)
        close_modals()

    def later(action):
        def invoke():
            try:
                if failures:
                    return
                if monotonic() >= deadline:
                    raise TimeoutError('The owned assistant transcription flow exceeded its deadline.')
                action()
            except Exception:
                failed()
        timers.append(wx.CallLater(25, invoke))

    def until(predicate, action):
        def observe():
            if predicate():
                action()
            else:
                later(observe)
        later(observe)

    def press(control):
        if not isinstance(control, wx.Button) or not control.IsEnabled():
            raise AssertionError('A required native assistant control is unavailable.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        if not control.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The native assistant did not dispatch its own button.')

    def choose(control, index):
        if (not isinstance(control, wx.Choice) or not control.IsEnabled()
                or not control.IsShown() or type(index) is not int
                or not 0 <= index < control.GetCount()):
            raise AssertionError('The current native profile choice is unavailable.')
        control.SetSelection(index)
        event = wx.CommandEvent(wx.EVT_CHOICE.typeId, control.GetId())
        event.SetEventObject(control)
        event.SetInt(index)
        event.SetString(control.GetString(index))
        # The actual invalidate handler deliberately calls event.Skip().
        # ProcessEvent may therefore return False after that handler ran.
        # The observed selected index and the later reviewed-Run revocation
        # check establish the native result, not the propagation return value.
        control.GetEventHandler().ProcessEvent(event)
        if control.GetSelection() != index:
            raise AssertionError('The current native profile choice did not retain its selection.')

    def fill(e, controls):
        draft = e.plan().to_dict()
        draft.pop('scope')
        controls['instruction'].ChangeValue(json.dumps(draft, ensure_ascii=False))
        controls['input'].ChangeValue(str(e.source.parent))
        controls['output'].ChangeValue(str(e.outputs))
        controls['budget'].ChangeValue('16')
        controls['file_budget'].ChangeValue('16')
        controls['deadline'].ChangeValue('45')

    def settled():
        return not any(thread.is_alive() and thread.name.startswith('local-assistant')
                       for thread in threads())

    try:
        with helper.protocol_interpreter():
            accepted = helper.make_environment(work / 'accepted')
            cancelled = helper.make_environment(work / 'cancelled', mode='wait')
            original = accepted.source.read_bytes()
            frame.library = accepted.app
            check('native_actual_windows_isolated_interpreter',
                  sys.platform == 'win32' and wx.Platform == '__WXMSW__' and bool(sys.flags.isolated))
            check('native_fixture_exact_reviewed_protocol_bytes',
                  sha256(helper_path.read_bytes()).hexdigest() == FIXTURE_SHA256)

            def ready(dialog, controls):
                try:
                    check('native_assistant_owned_current_frame', dialog.GetParent() is frame)
                    choice = controls['transcription_profile']
                    check('native_transcription_profile_named_explicit_none',
                          isinstance(choice, wx.Choice) and choice.GetSelection() == 0
                          and choice.GetCount() == 2 and 'explicit' in choice.GetName())
                    check('native_initial_run_disabled', not controls['run'].IsEnabled())
                    fill(accepted, controls)

                    def no_profile():
                        check('native_unconfigured_preview_refused',
                              not controls['run'].IsEnabled()
                              and 'Explicitly select' in controls['review'].GetValue())
                        check('native_unconfigured_preview_creates_no_job',
                              accepted.service.jobs() == ()
                              and not (accepted.app.root / 'transcription-jobs').exists())
                        choose(choice, 1)
                        press(controls['preview'])
                        until(lambda: controls['preview'].IsEnabled(), reviewed)

                    def reviewed():
                        check('native_profile_preview_binds_exact_manifest_and_settings',
                              controls['run'].IsEnabled()
                              and accepted.profile.id in controls['review'].GetValue()
                              and accepted.profile.manifest_sha256 in controls['review'].GetValue()
                              and '"condition_on_previous_text": true' in controls['review'].GetValue())
                        choose(choice, 0)
                        check('native_profile_change_revokes_reviewed_run',
                              not controls['run'].IsEnabled())
                        choose(choice, 1)
                        press(controls['preview'])
                        until(lambda: controls['preview'].IsEnabled(), run_selected)

                    def run_selected():
                        check('native_repreview_remains_passive',
                              controls['run'].IsEnabled() and accepted.service.jobs() == ()
                              and not tuple(accepted.outputs.iterdir()))
                        press(controls['run'])
                        until(lambda: controls['preview'].IsEnabled(), completed)

                    def completed():
                        nonlocal accepted_id, accepted_receipt
                        rows = accepted.service.jobs()
                        check('native_explicit_run_completes_current_assistant_job',
                              len(rows) == 1 and rows[0].status == 'complete'
                              and controls['review'].GetValue().startswith('complete: 1 of 1'))
                        accepted_id = rows[0].id
                        record = accepted.service.store.load(accepted_id)
                        validate_job(record)
                        accepted_receipt = record['steps'][0]['result']['transcription_receipt']
                        verified = TranscriptionService(accepted.app.root).completed(accepted_receipt['job_id'])
                        check('native_actual_child_completion_binds_retained_job',
                              verified['run_id'] == accepted_receipt['run_id']
                              and verified['completion_sha256'] == accepted_receipt['completion_sha256'])
                        check('native_all_four_formats_and_provenance_published',
                              set(path.name for path in (accepted.outputs / 'transcript').iterdir())
                              == {'transcript.txt', 'transcript.srt', 'transcript.vtt',
                                  'transcript.json', 'provenance.json'})
                        check('native_original_source_is_unchanged', accepted.source.read_bytes() == original)
                        check('native_accepted_worker_releases_shared_resources',
                              ResourceBroker.for_app(accepted.app.root).snapshot()['leases'] == [])
                        metrics.update(accepted_domain_jobs=1, accepted_formats=4, exported_files=5)
                        press(controls['close'])

                    press(controls['preview'])
                    until(lambda: controls['preview'].IsEnabled(), no_profile)
                except Exception:
                    failed()

            print('PHASE actual native explicit profile, Preview and Run', flush=True)
            show_assistant(frame, on_ready=ready)
            if failures or accepted_id is None:
                raise AssertionError('\n'.join(failures) or 'The native accepted flow did not complete.')
            before_run = accepted_receipt['run_id']
            replay = accepted.service.resume(accepted_id)
            check('actual_completed_recovery_preserves_exact_run_without_new_inference',
                  replay.status == 'complete'
                  and replay.results[0]['transcription_receipt']['run_id'] == before_run
                  and len(TranscriptionService(accepted.app.root).jobs()) == 1)

            frame.library = cancelled.app
            cancelled_run = {}
            def cancel_ready(dialog, controls):
                try:
                    check('native_second_library_has_its_own_explicit_profile',
                          controls['transcription_profile'].GetSelection() == 0
                          and controls['transcription_profile'].GetCount() == 2)
                    fill(cancelled, controls)
                    choose(controls['transcription_profile'], 1)

                    def prepared():
                        check('native_cancel_preview_does_not_start_worker',
                              controls['run'].IsEnabled() and cancelled.service.jobs() == ())
                        press(controls['run'])
                        until(processing, request_pause)

                    def processing():
                        rows = TranscriptionService(cancelled.app.root).jobs()
                        if not rows:
                            return False
                        job = cancelled.app.root / 'transcription-jobs' / rows[0]['id']
                        try:
                            value = json.loads((job / 'job_status.json').read_bytes())
                        except (FileNotFoundError, ValueError):
                            return False
                        if value.get('state') == 'processing':
                            cancelled_run.update(job=job, run_id=value['run_id'])
                            return True
                        return False

                    def request_pause():
                        check('native_pause_targets_actual_running_protocol_child',
                              controls['pause'].IsEnabled() and bool(cancelled_run))
                        press(controls['pause'])
                        until(lambda: controls['preview'].IsEnabled(), paused)

                    def paused():
                        rows = cancelled.service.jobs()
                        execution = json.loads((cancelled_run['job'] / 'execution.json').read_bytes())
                        check('native_pause_waits_for_owned_child_cleanup',
                              len(rows) == 1 and rows[0].status == 'paused'
                              and execution['state'] == 'cancelled')
                        check('native_cancelled_run_keeps_inputs_without_export',
                              not tuple(cancelled.outputs.iterdir())
                              and len(TranscriptionService(cancelled.app.root).definition(
                                  cancelled_run['job'].name)['inputs']) == 1)
                        check('native_cancelled_worker_releases_shared_resources',
                              ResourceBroker.for_app(cancelled.app.root).snapshot()['leases'] == [])
                        press(controls['run'])
                        until(lambda: controls['preview'].IsEnabled(), no_replay)

                    def no_replay():
                        rows = cancelled.service.jobs()
                        execution = json.loads((cancelled_run['job'] / 'execution.json').read_bytes())
                        check('native_uncertain_dispatch_requires_new_review_not_replay',
                              len(rows) == 1 and rows[0].status == 'attention'
                              and execution['run_id'] == cancelled_run['run_id']
                              and execution['state'] == 'cancelled'
                              and not tuple(cancelled.outputs.iterdir()))
                        metrics.update(cancelled_domain_jobs=1, profiles_explicitly_selected=2)
                        press(controls['close'])

                    press(controls['preview'])
                    until(lambda: controls['preview'].IsEnabled(), prepared)
                except Exception:
                    failed()

            print('PHASE actual native pause and no-replay boundary', flush=True)
            show_assistant(frame, on_ready=cancel_ready)
            if failures or not cancelled_run:
                raise AssertionError('\n'.join(failures) or 'The native cancellation flow did not complete.')

            backup = work / 'backup'
            digest = create_backup(accepted.app, backup)
            inventory = verify_backup(backup, digest)
            paths = {row['path'] for row in inventory['sidecars']}
            check('actual_backup_retains_assistant_and_managed_transcription_producers',
                  any(path.startswith('state/assistant/') for path in paths)
                  and any(path.startswith('transcription-jobs/') for path in paths))
            # Make all original external capabilities unavailable. Restoration
            # must use only copied managed bytes and leave the old job inert.
            (accepted.root / 'runtime').rename(accepted.root / 'offline-runtime')
            (accepted.root / 'input').rename(accepted.root / 'offline-input')
            accepted.outputs.rename(accepted.root / 'offline-output')
            restored = restore_backup(backup, work / 'restored', digest)
            restored_app = LibraryApp(restored)
            restored_service = AssistantService(restored_app)
            check('actual_restored_assistant_requires_new_scope_approval',
                  restored_service.get(accepted_id).status == 'attention')
            refused = False
            try:
                restored_service.resume(accepted_id)
            except PermissionError:
                refused = True
            check('actual_restored_job_cannot_launch_saved_runtime', refused)
            retained = TranscriptionService(restored).completed(accepted_receipt['job_id'])
            check('actual_restored_transcripts_read_without_external_source_or_runtime',
                  retained['run_id'] == before_run and retained['historical'] is True
                  and retained['runtime_current_verified'] is False)
            second = work / 'second-backup'
            verify_backup(second, create_backup(restored_app, second))
            check('actual_rebackup_preserves_passive_cross_binding',
                  restored_service.store.load(accepted_id)['steps'][0]['result']['transcription_receipt']
                  == accepted_receipt)
            metrics.update(restored_asr_jobs=1, rebackup_verified=1)
            check('native_fixture_bytes_remain_unchanged',
                  sha256(helper_path.read_bytes()).hexdigest() == FIXTURE_SHA256)
        # Closing the dialogs requests executor shutdown. Observe actual thread
        # settlement before emitting the receipt, without retaining GUI owners.
        end = monotonic() + 10
        while not settled():
            if monotonic() >= end:
                raise TimeoutError('An assistant-owned worker did not settle after close.')
            wx.YieldIfNeeded()
            wx.MilliSleep(10)
        check('native_all_assistant_workers_settled_after_close', settled())
        return {'status': 'PASS', 'actual_windows': True, 'isolated': True,
                'checks': checks, 'metrics': metrics, 'module_origins': origins(),
                'fixture_sha256': FIXTURE_SHA256, 'synthetic_protocol_worker': True,
                'actual_asr_model_inference': 'NOT_RUN', 'language_model_quality': 'NOT_QUALIFIED',
                'nvda': 'NOT_RUN', 'physical_audio': 'NOT_RUN'}
    finally:
        for timer in timers:
            timer.Stop()
        close_modals()
        frame.Destroy()
        wx.YieldIfNeeded()
        gui.Destroy()
        sys.modules.pop('_native_assistant_asr_fixture', None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    faulthandler.enable()
    faulthandler.dump_traceback_later(210, exit=True)
    handle, report = None, None
    try:
        if sys.platform != 'win32' or not sys.flags.isolated:
            raise ValueError('An actual isolated installed Windows interpreter is required.')
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        before = origins()
        report = run(args.work)
        if report['module_origins'] != before:
            raise ValueError('Installed module identities changed during native execution.')
    except Exception as error:
        traceback.print_exc()
        report = {'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error),
                  'actual_windows': sys.platform == 'win32', 'isolated': bool(sys.flags.isolated),
                  'actual_asr_model_inference': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                report = {'status': 'FAIL', 'reason': 'Native runtime cleanup: ' + str(error),
                          'actual_asr_model_inference': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
