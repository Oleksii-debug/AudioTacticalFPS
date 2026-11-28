"""Exercise installed primary assistant controls and durable jobs on Windows.

The documents, library and jobs are temporary synthetic fixtures. Native buttons
run the production planner, importer, search and checkpoint recovery. This does
not claim manual screen-reader acceptance or execute paid provider operations.
"""
from argparse import ArgumentParser
import faulthandler
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


SOURCE_FILES = (
    'assistant/__init__.py', 'assistant/models.py', 'assistant/planner.py',
    'assistant/service.py', 'assistant/state.py', 'assistant/backup_receipt.py',
    'assistant/templates.py', 'assistant/template_bindings.py',
    'ui/assistant_dialog.py', 'ui/workflow_templates.py',
)
EXPECTED_CHECKS = (
    'native preview is non-executing and shows exact operations',
    'native Run executes real import and scoped search',
    'native progress reports completion only after durable verification',
    'reopened native saved job resumes without duplicate imports',
    'unsupported native instructions execute nothing',
    'original Unicode document bytes remain unchanged',
)


def trace_phase(phase, **details):
    """Emit bounded text evidence before a native stage can stall."""
    print(json.dumps({'probe': 'native-assistant', 'phase': phase, **details},
                     ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)


def exercise_controls(root, wx, *, timeout=60):
    """Run actual native controls; also callable by a separate local GUI preflight."""
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise ValueError('The native assistant stage timeout must be between 1 and 120 seconds.')
    application = wx.GetApp()
    if application is None or application.IsMainLoopRunning():
        raise RuntimeError('Start the native assistant probe with a fresh wx application outside MainLoop.')
    trace_phase('prepare-fixture')
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService
    from carr_library.ui import assistant_dialog

    inputs, outputs = root / 'Українські джерела', root / 'Обраний результат'
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / 'залізниця Ω.txt'
    raw = 'Railway transport connected cities.\r\nЗалізниця з’єднувала міста.'.encode('utf-8')
    source.write_bytes(raw)
    library = LibraryApp(root / 'library')
    other = library.create_workspace('Separate private scope')
    library.add_text(other, 'Excluded source', 'Railway transport belongs to another library.')
    baseline_ids = tuple(row.edition_id for row in library.browse(other))
    checks, progress, completed_state = [], [], {}
    template_checks = []

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def button(dialog, label):
        return next(control for control in descendants(dialog)
                    if isinstance(control, wx.Button) and control.GetLabel().replace('&', '') == label)

    def click(control):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        control.GetEventHandler().ProcessEvent(event)

    def run_dialog(reopened):
        # A new application object and native dialog must recover the durable job.
        trace_phase('open-dialog', reopened=reopened)
        faulthandler.dump_traceback_later(timeout + 15, exit=True)
        frame = wx.Frame(None, title='Synthetic native assistant acceptance')
        frame.library = LibraryApp(library.root) if reopened else library
        frame.active = None
        frame.Show()
        observer = AssistantService(frame.library)
        started, failures = monotonic(), []
        state = {'phase': 'open', 'dialog': None}

        def close():
            dialog = state['dialog']
            if dialog is not None and not state.get('closing'):
                state['closing'] = True
                trace_phase('close-dialog', reopened=reopened, failures=len(failures))
                try:
                    click(button(dialog, 'Close'))
                except BaseException:
                    failures.append(traceback.format_exc())
                    trace_phase('close-failed', reopened=reopened, error=failures[-1])
                    # Failure cleanup must not strand the native modal. This
                    # never counts as a successful Close-control assertion.
                    if dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

        def observe(review):
            change = review.ChangeValue
            def record(value):
                change(value)  # Preserve the real native control update.
                match = re.match(r'^(\d+)/(\d+):', str(value))
                final = str(value).startswith('complete:')
                if match or final:
                    try:
                        jobs = observer.jobs()
                        assert len(jobs) == 1
                        job = jobs[0]
                        done, total = map(int, match.groups()) if match else (job.completed, job.total)
                        assert total == job.total == 3 and 0 <= done <= job.completed
                        if done == total or final:
                            assert job.status == 'complete' and job.completed == job.total
                        progress.append({'reopened': reopened, 'displayed_completed': done,
                                         'total': total, 'durable_completed': job.completed,
                                         'durable_status': job.status, 'final_text': final})
                    except BaseException as exc:
                        failures.append(traceback.format_exc())
                        trace_phase('progress-failed', reopened=reopened, error=failures[-1])
                        wx.CallAfter(close)
            # Observation only: no service, result, thread or native event is mocked.
            review.ChangeValue = record

        def select_saved():
            try:
                if monotonic() - started > timeout:
                    raise RuntimeError('The native saved-job selector did not open.')
                choices = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.SingleChoiceDialog)
                           and window.IsModal() and window.IsShown()
                           and window.GetTitle() == 'Assistant jobs']
                if not choices:
                    wx.CallLater(25, select_saved)
                    return
                choice = choices[0]
                assert len(observer.jobs()) == 1
                choice.SetSelection(0)
                assert choice.GetSelection() == 0
                trace_phase('choose-saved-job', reopened=reopened)
                click(choice.FindWindowById(wx.ID_OK))
            except BaseException as exc:
                failures.append(traceback.format_exc())
                trace_phase('saved-selector-failed', reopened=reopened, error=failures[-1])
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.SingleChoiceDialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                wx.CallAfter(close)

        def select_named_template():
            try:
                if monotonic() - started > timeout:
                    raise RuntimeError('The native template selector did not open.')
                choices = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.SingleChoiceDialog)
                           and window.IsModal() and window.IsShown()
                           and window.GetTitle() == 'Named assistant templates']
                if not choices:
                    wx.CallLater(25, select_named_template)
                    return
                choice = choices[0]
                choice.SetSelection(0)
                click(choice.FindWindowById(wx.ID_OK))
            except BaseException:
                failures.append(traceback.format_exc())
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.SingleChoiceDialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                wx.CallAfter(close)

        def poll():
            try:
                if monotonic() - started > timeout:
                    raise RuntimeError('Native assistant controls exceeded the test deadline.')
                dialog = state['dialog']
                if dialog is None:
                    dialog = next((window for window in wx.GetTopLevelWindows()
                                   if isinstance(window, wx.Dialog)
                                   and window.IsModal() and window.IsShown()
                                   and window.GetTitle().startswith('Local assistant')), None)
                    if dialog is None:
                        wx.CallLater(25, poll)
                        return
                    state['dialog'] = dialog
                    trace_phase('dialog-ready', reopened=reopened)
                controls = {control.GetName(): control for control in descendants(dialog)}
                review = controls['Plan preview and actual results']
                if state['phase'] == 'open':
                    observe(review)
                    if reopened:
                        trace_phase('open-saved-jobs', reopened=reopened)
                        wx.CallLater(25, select_saved)
                        click(button(dialog, 'Saved jobs'))
                        if failures:
                            close()
                            return
                        assert button(dialog, 'Run / resume').IsEnabled()
                        assert 'original reviewed scope' in review.GetValue()
                    else:
                        controls['Allowed input folder (optional; no other folders are readable)'].SetValue(str(inputs))
                        controls['Chosen output folder (required for audio, extraction or backup)'].SetValue(str(outputs))
                        controls['Goal — guided English/Ukrainian commands or typed JSON plan'].SetValue(
                            'Create library "Native assistant"\nImport "' + str(source) + '"\nSearch "Railway transport"')
                        assert not button(dialog, 'Run / resume').IsEnabled()
                        click(button(dialog, 'Preview plan'))
                        assert button(dialog, 'Run / resume').IsEnabled()
                        assert 'library.import' in review.GetValue() and 'library.search' in review.GetValue()
                        assert [row.id for row in frame.library.workspaces()] == [other]
                        assert not observer.jobs() and source.read_bytes() == raw
                        checks.append(EXPECTED_CHECKS[0])
                        from carr_library.assistant.templates import TemplateStore
                        controls['Workflow template name (saved steps carry no permissions)'].SetValue(
                            'Native repeated import')
                        controls['Template category'].SetSelection(1)
                        click(button(dialog, 'Save reviewed template'))
                        record, = TemplateStore(frame.library.root).list()[0]
                        assert record.name == 'Native repeated import' and record.kind == 'batch-preset'
                        assert 'scope' not in record.proposal()
                        assert [step.tool for step in record.steps] == [
                            'library.create', 'library.import', 'library.search']
                        assert not observer.jobs() and source.read_bytes() == raw
                        template_checks.append('native template save persists typed steps without executing')
                        trace_phase('preview-verified', reopened=reopened)
                    trace_phase('run-reviewed-plan', reopened=reopened)
                    click(button(dialog, 'Run / resume'))
                    state['phase'] = 'running'
                # Saved jobs already display "complete:" before resume starts.
                # Wait for the real completion callback to enable Run again;
                # the old summary is not the resumed and reverified result.
                elif (state['phase'] == 'running' and button(dialog, 'Run / resume').IsEnabled()
                      and review.GetValue().startswith('complete:')):
                    trace_phase('verify-durable-completion', reopened=reopened)
                    assert '3 of 3 verified operations' in review.GetValue()
                    job, = observer.jobs()
                    assert job.status == 'complete' and job.completed == job.total == 3
                    workspace = job.results[0]['workspace']
                    assert workspace != other
                    imported = tuple(row.edition_id for row in frame.library.browse(workspace))
                    assert len(imported) == 1
                    hits = job.results[2]['results']
                    assert hits and all(hit['workspace'] == workspace and hit['verified'] for hit in hits)
                    assert [hit['quote'] for hit in hits] == ['Railway transport']
                    assert tuple(row.edition_id for row in frame.library.browse(other)) == baseline_ids
                    assert len(frame.library.workspaces()) == 2
                    if reopened:
                        assert job.id == completed_state['id'] and imported == completed_state['editions']
                        assert job.results == completed_state['results']
                        checks.append(EXPECTED_CHECKS[3])
                        controls['Goal — guided English/Ukrainian commands or typed JSON plan'].SetValue(
                            'Upload every file to a remote service')
                        click(button(dialog, 'Preview plan'))
                        assert not button(dialog, 'Run / resume').IsEnabled()
                        assert 'unsupported' in review.GetValue().lower()
                        assert len(observer.jobs()) == 1 and len(frame.library.browse(workspace)) == 1
                        checks.append(EXPECTED_CHECKS[4])
                        assert source.read_bytes() == raw
                        checks.append(EXPECTED_CHECKS[5])
                        dialog.assistant_controls['full_backup'].SetValue(True)
                        wx.CallLater(25, select_named_template)
                        click(button(dialog, 'Named templates...'))
                        if failures:
                            close()
                            return
                        assert not button(dialog, 'Run / resume').IsEnabled()
                        assert controls['Allowed input folder (optional; no other folders are readable)'].GetValue() == ''
                        assert controls['Chosen output folder (required for audio, extraction or backup)'].GetValue() == ''
                        proposal = json.loads(controls['Goal — guided English/Ukrainian commands or typed JSON plan'].GetValue())
                        assert 'scope' not in proposal
                        assert dialog.assistant_controls['full_backup'].GetValue() is False
                        assert len(observer.jobs()) == 1 and source.read_bytes() == raw
                        click(button(dialog, 'Run / resume'))
                        assert len(observer.jobs()) == 1
                        controls['Allowed input folder (optional; no other folders are readable)'].SetValue(str(inputs))
                        controls['Chosen output folder (required for audio, extraction or backup)'].SetValue(str(outputs))
                        click(button(dialog, 'Preview plan'))
                        assert button(dialog, 'Run / resume').IsEnabled()
                        assert len(observer.jobs()) == 1
                        template_checks.append('native restored template requires fresh scope preview and separate Run')
                    else:
                        completed_state.update(id=job.id, editions=imported, results=job.results)
                        observed = [row for row in progress if not row['reopened']]
                        assert {row['displayed_completed'] for row in observed} == {0, 1, 2, 3}
                        assert any(row['final_text'] for row in observed)
                        checks.extend(EXPECTED_CHECKS[1:3])
                    close()
                    return
                if not state.get('closing'):
                    wx.CallLater(25, poll)
            except BaseException as exc:
                failures.append(traceback.format_exc())
                trace_phase('controls-failed', reopened=reopened, error=failures[-1])
                close()

        wx.CallAfter(poll)
        try:
            assistant_dialog.show_assistant(frame)
        finally:
            frame.Destroy()
            faulthandler.cancel_dump_traceback_later()
        if failures:
            raise RuntimeError('; '.join(failures))

    failures = []
    previous_exit = application.GetExitOnFrameDelete()
    previous_top = application.GetTopWindow()
    # MSW can return from MainLoop before CallAfter runs when no top-level
    # window exists. Keep a real shown host alive across both modal scenarios;
    # their separate frames still reopen the durable application state.
    loop_host = wx.Frame(None, title='Native assistant acceptance host')
    application.SetTopWindow(loop_host)
    loop_host.Show()
    application.SetExitOnFrameDelete(False)

    def run_scenarios():
        try:
            # Start the real modal workflows only after MainLoop is active.
            # Native Windows dispatch and timers must have a running GUI loop.
            trace_phase('main-loop-running')
            if not application.IsMainLoopRunning():
                raise RuntimeError('The native application event loop is not active.')
            run_dialog(False)
            run_dialog(True)
        except BaseException:
            failures.append(traceback.format_exc())
            trace_phase('scenario-failed', error=failures[-1])
        finally:
            application.ExitMainLoop()

    wx.CallAfter(run_scenarios)
    try:
        trace_phase('enter-main-loop', host_shown=loop_host.IsShown())
        faulthandler.dump_traceback_later(timeout + 15, exit=True)
        application.MainLoop()
    finally:
        faulthandler.cancel_dump_traceback_later()
        application.SetTopWindow(previous_top)
        loop_host.Destroy()
        application.SetExitOnFrameDelete(previous_exit)
    if failures:
        raise RuntimeError('; '.join(failures))
    if tuple(checks) != EXPECTED_CHECKS:
        raise RuntimeError('Native assistant did not complete every required operation.')
    if len(template_checks) != 2:
        raise RuntimeError('Native assistant template checks did not complete.')
    trace_phase('all-required-checks-passed', check_count=len(checks) + len(template_checks))
    return {'checks': checks, 'check_count': len(checks), 'template_checks': template_checks,
            'template_check_count': len(template_checks), 'progress_observations': progress,
            'saved_job_reopened': True, 'unrelated_workspace_unchanged': True}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--result', type=Path,
                        help='Write final acceptance JSON separately from native diagnostic output.')
    args = parser.parse_args()
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'windows_execution': 'NOT_RUN'}))
        return 77
    bundle = args.bundle.resolve()
    import carr_library
    installed = Path(carr_library.__file__).resolve().parent
    if not installed.is_relative_to(bundle / '.venv'):
        raise ValueError('The probe must use the actual installed application.')
    manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    if any(not re.fullmatch('[0-9a-f]{40}', manifest.get(key, '')) for key in ('source_commit', 'source_tree')):
        raise ValueError('The installed bundle must identify the exact committed source.')
    from carr_library.native_runtime import activate_bundled_runtime
    from carr_library.audio_runtime import activate_bundled_audio_runtime
    native_handle = activate_bundled_runtime(bundle_root=bundle)
    assert native_handle is not None and activate_bundled_audio_runtime(bundle_root=bundle)
    trace_phase('import-native-toolkit')
    import wx
    source = Path(__file__).resolve().parents[1] / 'src' / 'carr_library'
    hashes = {}
    for name in SOURCE_FILES:
        raw = (source / name).read_bytes()
        if (installed / name).read_bytes() != raw:
            raise ValueError('Installed assistant source differs from the acceptance checkout.')
        hashes[name] = sha256(raw).hexdigest()
    trace_phase('create-native-application')
    application = wx.App(False)
    with TemporaryDirectory(prefix='carr-native-assistant-Архів-') as temporary:
        result = exercise_controls(Path(temporary), wx)
    for name, expected in hashes.items():
        if sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Acceptance source changed during execution.')
    result.update(status='PASS', windows_execution='PASS', installed_modules='PASS',
                  source_commit=manifest['source_commit'], source_tree=manifest['source_tree'],
                  source_hashes=hashes, manual_nvda='NOT_RUN', paid_api='NOT_RUN')
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if args.result is not None:
        args.result.write_text(raw + '\n', encoding='utf-8')
    print(raw, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
