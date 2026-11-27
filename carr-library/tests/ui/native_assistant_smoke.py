"""Installed native local-assistant acceptance with synthetic local sources.

Run: python -I tests/ui/native_assistant_smoke.py --native
Checkout debugging only: append --source path/to/carr-library.
Real wx keys open Main's Ctrl+Alt+J action, preview, run, review and close.
The optional planner uses the real gateway with a synthetic local transport.
No live model, provider network, model acquisition, private documents or NVDA are involved.
"""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None, timeout=120):
    if sys.platform not in ('win32', 'darwin') and not os.environ.get('DISPLAY'):
        return not_run('A native display is unavailable; wx key simulation requires X11 on Linux.')
    if os.environ.get('GDK_BACKEND') == 'wayland':
        return not_run('wx.UIActionSimulator does not support GTK Wayland.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    if not hasattr(wx, 'UIActionSimulator'):
        return not_run('This wx build has no native key simulator.')
    if source is not None:
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / 'src'))

    import carr_library.application as application_module
    import carr_library.assistant.service as service_module
    import carr_library.ui.assistant_actions as actions_module
    import carr_library.ui.assistant_dialog as dialog_module
    import carr_library.ui.local_planner_actions as planning_actions_module
    import carr_library.assistant.local_planner as planning_module
    import carr_library.providers as providers_module
    import carr_library.ui.desktop_app as desktop_module
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame

    checks, failures, opened = [], [], []
    deadline = monotonic() + timeout
    frame = None

    def abort(reason):
        failures.append(reason)
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal() and window.IsShown():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    try:
        application = ProbeApp(False)
    except (Exception, SystemExit) as exc:
        return not_run('The native wx application could not open: ' + str(exc))
    native_loop = wx.GUIEventLoop()
    loop_activation = wx.EventLoopActivator(native_loop)
    simulator = wx.UIActionSimulator()
    watchdog = wx.CallLater(int(timeout * 1000), abort, 'Native assistant probe exceeded its deadline.')

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def healthy():
        if failures:
            raise AssertionError('; '.join(failures))
        if monotonic() >= deadline:
            raise TimeoutError('Native assistant probe exceeded its deadline.')

    def pump(predicate, seconds=8):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            application.Yield()
            healthy()
            if predicate():
                return True
            wx.MilliSleep(10)
        healthy()
        return False

    def settle(seconds=.2):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            application.Yield()
            healthy()
            wx.MilliSleep(10)

    def press(key, modifiers=wx.MOD_NONE):
        if not simulator.Char(key, modifiers):
            raise RuntimeError('The native input backend could not inject a key.')
        settle()

    def focus(control):
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() == control, seconds=3):
            raise AssertionError('Native focus did not reach ' + control.GetName())

    def activate(control):
        if not control.IsEnabled():
            raise AssertionError('A required native button is disabled: ' + control.GetName())
        focus(control)
        press(wx.WXK_SPACE)

    def named(dialog, name):
        return next(child for child in dialog.GetChildren() if child.GetName() == name)

    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in
               (application_module, service_module, actions_module, dialog_module, desktop_module,
                planning_actions_module, planning_module, providers_module)}
    checkout = Path(__file__).resolve().parents[2]
    original_show = dialog_module.show_assistant
    phase = None
    active_actions = []
    releases = []

    def instrumented_show(parent, **kwargs):
        """Use the existing observational hook; all controls/services are real."""
        def ready(dialog, controls):
            opened.append(dialog)
            active_actions.append(controls['actions'])
            try:
                phase(dialog, controls)
                if dialog.IsShown():
                    raise AssertionError('The native assistant phase did not close its window.')
            except BaseException:
                failures.append(traceback.format_exc())
            finally:
                for gate in releases:
                    gate.set()
                # MSW retains IsModal until ShowModal unwinds. Preserve the
                # return code of a real native Close that already hid it.
                if dialog.IsModal() and dialog.IsShown():
                    dialog.EndModal(wx.ID_CANCEL)
        if phase is reviewed_model:
            kwargs['local_planner'] = planning_module.LocalPlanner(parent.library, transport=model_transport)
        return original_show(parent, on_ready=ready, **kwargs)

    dialog_module.show_assistant = instrumented_show
    with TemporaryDirectory(prefix='carr-native-assistant-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic assistant scope')
        topic = library.create_topic(workspace, 'Synthetic source evidence')
        imported = library.add_text(workspace, 'Exact synthetic source',
            'The railway connected these towns. Український незмінний текст.', authors=('Synthetic author',))
        hit, = library.search(workspace, 'railway')
        saved_note = 'Synthetic saved note.'
        evidence = library.save_hit(workspace, topic, hit.ref, hit.revision, saved_note)
        other = library.create_workspace('Excluded synthetic library')
        library.add_text(other, 'Same title elsewhere', 'This railway belongs to a different library.')
        other_root = LibraryApp(root / 'other-root')
        original_bytes = library.assets.resolve(imported['asset']).read_bytes()
        unsaved_note = 'Synthetic unsaved note — keep this exact text.'
        identities = []
        pending = []

        class ModelTransport:
            def __init__(self):
                self.calls = []
            def post(self, profile, route, body, headers, **options):
                if options['cancelled']():
                    raise InterruptedError('Synthetic model request cancelled before transmission')
                assert profile.local and profile.provider == 'openai-compatible' and route == '/chat/completions'
                self.calls.append(json.loads(body))
                proposal = {'schema':'carr-assistant-plan/1', 'summary':'A proposed source search',
                    'steps':[{'tool':'library.search','arguments':{'query':'railway'}}]}
                return json.dumps({'choices':[{'message':{'content':json.dumps(proposal)}}]}).encode()
        model_transport = ModelTransport()
        providers_module.ProfileStore(library.root/'api-connections.json').save(providers_module.ProviderProfile(
            'native-local-model', 'Synthetic local planner', 'openai-compatible',
            'http://127.0.0.1:8080/v1', 'synthetic-no-live-model', ('llm',), local=True))

        def close_native(dialog, controls):
            activate(controls['close'])
            check('native Close hides the assistant window', not dialog.IsShown())

        def preview_search(dialog, controls):
            controls['instruction'].SetValue('Search "railway"')
            check('editing a task withdraws Run until a new preview', not controls['run'].IsEnabled())
            activate(controls['preview'])
            check('native Preview displays the exact library and approved search',
                  controls['run'].IsEnabled() and workspace in controls['review'].GetValue()
                  and 'library.search' in controls['review'].GetValue())

        def successful_search(dialog, controls):
            actions = controls['actions']
            check('assistant uses a real modal dialog and named native controls',
                  dialog.IsModal() and isinstance(controls['instruction'], wx.TextCtrl)
                  and controls['review'].GetName() == 'Plan preview and actual results')
            check('initial task cannot execute before preview', not controls['run'].IsEnabled()
                  and not actions.service.jobs())
            before = dict(library._selections)
            preview_search(dialog, controls)
            check('preview is read-only and creates no job checkpoint', not actions.service.jobs())
            activate(controls['run'])
            check('native Run completes an actual private-session source search',
                  pump(lambda: not actions.busy and controls['review'].GetValue().startswith('complete:')))
            identity = actions.job_id
            identities.append(identity)
            record = actions.service.store.load(identity)
            result, = record['steps'][0]['result']['results']
            check('result retains exact source identity and no persistent live handle',
                  result['verified'] and result['quote'] == 'railway' and 'ref' not in result
                  and result['workspace'] == workspace
                  and result['source_citation']['identity']['asset_sha256'] == imported['asset'])
            check('assistant search leaves the host selection cache unchanged', library._selections == before)
            close_native(dialog, controls)

        def restart_review(dialog, controls):
            actions = controls['actions']
            identity = identities[0]
            refused = False
            try:
                actions.service.resume(identity)
            except PermissionError:
                refused = True
            check('a reopened task requires the exact reviewed fingerprint', refused and not controls['run'].IsEnabled())

            selection_deadline = min(deadline, monotonic() + 8)
            def select_saved():
                try:
                    chooser = next((window for window in wx.GetTopLevelWindows()
                                    if isinstance(window, wx.SingleChoiceDialog)
                                    and window.GetTitle() == 'Assistant jobs' and window.IsShown()), None)
                    if chooser is None:
                        if monotonic() >= selection_deadline:
                            raise TimeoutError('The native saved-job chooser did not open.')
                        wx.CallLater(20, select_saved)
                        return
                    chooser.SetSelection(0)
                    activate(next(child for child in chooser.GetChildren()
                                  if isinstance(child, wx.Button) and child.GetId() == wx.ID_OK))
                except BaseException:
                    abort(traceback.format_exc())
            wx.CallAfter(select_saved)
            activate(named(dialog, 'Saved jobs'))
            check('native saved-job choice displays full scope before resume',
                  controls['run'].IsEnabled() and 'Review this exact saved plan' in controls['review'].GetValue()
                  and 'max_bytes' in controls['review'].GetValue() and actions.job_id == identity)
            activate(controls['run'])
            check('explicit native resume rechecks the actual source without duplicate jobs',
                  pump(lambda: not actions.busy and controls['review'].GetValue().startswith('complete:'))
                  and len(actions.service.jobs()) == 1)
            close_native(dialog, controls)

        def hold_real_operation(actions):
            entered, release = Event(), Event()
            releases.append(release)
            original_execute = actions.service._execute
            original_start = actions.start
            futures = []
            def execute(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('Synthetic worker barrier exceeded its deadline.')
                return original_execute(*args, **kwargs)
            def start(*args, **kwargs):
                generation, future = original_start(*args, **kwargs)
                futures.append(future)
                return generation, future
            actions.service._execute, actions.start = execute, start
            pending.append(futures)
            return entered, release, futures

        def close_running(dialog, controls):
            actions = controls['actions']
            entered, release, futures = hold_real_operation(actions)
            preview_search(dialog, controls)
            activate(controls['run'])
            check('actual worker reaches a controlled pending source operation', pump(entered.is_set))
            close_native(dialog, controls)
            check('closing a running native assistant latches cancellation', actions.closed and actions.cancel_event.is_set())
            release.set()

        def changed_root(dialog, controls):
            actions = controls['actions']
            entered, release, futures = hold_real_operation(actions)
            choice = named(dialog, 'Library scope')
            choice.SetSelection(0)
            controls['instruction'].SetValue('Create library "Must not be created"')
            activate(controls['preview'])
            check('root-change case first reviews an explicit harmless create plan',
                  controls['run'].IsEnabled() and 'Must not be created' in controls['review'].GetValue())
            activate(controls['run'])
            check('root-change case holds the real worker before its transaction', pump(entered.is_set))
            frame.library = other_root
            try:
                release.set()
                check('changed parent root cancels the real worker before commit',
                      pump(lambda: futures and futures[0].done()) and futures[0].result().status == 'paused')
                check('a stale root cannot accept or display the old completion',
                      not actions.current() and not controls['review'].GetValue().startswith('complete:'))
                close_native(dialog, controls)
            finally:
                frame.library = library

        def reviewed_model(dialog, controls):
            actions = controls['actions']
            model_actions = controls['model_actions']
            before = len(actions.service.jobs())
            controls['instruction'].SetValue('Search the selected library for railway.')
            selection_deadline = min(deadline, monotonic() + 8)
            def select_profile():
                try:
                    chooser = next((window for window in wx.GetTopLevelWindows()
                        if isinstance(window, wx.SingleChoiceDialog) and window.GetParent() is dialog
                        and window.GetTitle() == 'Local planning connection'
                        and window.IsShown() and window.IsModal()), None)
                    if chooser is None:
                        if monotonic() >= selection_deadline:
                            raise TimeoutError('The native local-model profile chooser did not open.')
                        wx.CallLater(20, select_profile)
                        return
                    chooser.SetSelection(0)
                    activate(next(child for child in chooser.GetChildren()
                        if isinstance(child, wx.Button) and child.GetId() == wx.ID_OK))
                except BaseException:
                    abort(traceback.format_exc())
            wx.CallAfter(select_profile)
            activate(controls['model'])
            shown_request = json.JSONDecoder().raw_decode(controls['review'].GetValue().split('\n\n', 1)[1])[0]
            check('native model review exposes exact prompt without request or tool execution',
                model_actions.review is not None and not model_transport.calls
                and len(actions.service.jobs()) == before and not controls['run'].IsEnabled()
                and shown_request == model_actions.review.data)
            check('exact model approval is a distinct named native action',
                controls['model'].GetName() == 'Approve local model request')
            activate(controls['model'])
            check('approved synthetic model request yields only a separately reviewable typed proposal',
                pump(lambda:not model_actions.busy and actions.plan is not None
                    and controls['review'].GetValue().startswith('UNTRUSTED LOCAL MODEL PROPOSAL'))
                and len(model_transport.calls) == 1 and len(actions.service.jobs()) == before
                and controls['run'].IsEnabled() and actions.plan.scope.workspace_id == workspace
                and actions.plan.steps[0].tool == 'library.search')
            activate(controls['run'])
            check('separate native Run executes the actual source search after model proposal review',
                pump(lambda:not actions.busy and controls['review'].GetValue().startswith('complete:'))
                and len(actions.service.jobs()) == before + 1 and len(model_transport.calls) == 1)
            close_native(dialog, controls)

        try:
            frame = DesktopFrame(library)
            frame._open_workspace(workspace, topic)
            application.SetTopWindow(frame)
            frame.SetSize((1100, 900))
            frame.Show()
            frame.Raise()
            check('actual Main window opens the synthetic source topic', pump(lambda:
                frame.active == (workspace, topic) and frame.evidence is not None
                and frame.evidence.displayed is not None
                and frame.evidence.displayed.evidence_id == evidence and not frame.working))
            frame.evidence.note.SetValue(unsaved_note)
            if source is None:
                check('assistant and host load from the installed distribution',
                      all(not Path(path).is_relative_to(checkout) for path in origins.values()))
            check('Main registry owns the actual assistant Ctrl+Alt+J action',
                  frame.keyboard_commands.shortcut('assistant.open') == 'Ctrl+Alt+J'
                  and frame.keyboard_commands.available('assistant.open'))
            for phase in (successful_search, restart_review, close_running, changed_root, reviewed_model):
                previous = len(opened)
                frame.Raise()
                focus(frame.evidence.note)
                press(ord('J'), wx.MOD_CONTROL | wx.MOD_ALT)
                check('actual Main shortcut opens exactly one assistant session',
                      pump(lambda: len(opened) == previous + 1))
                for futures in pending:
                    if futures:
                        check('closed worker settles before the next native phase',
                              pump(futures[0].done) and futures[0].result().status == 'paused')
                pending.clear()
            check('root drift creates no library in either data directory',
                  {row.name for row in library.workspaces()} == {'Synthetic assistant scope', 'Excluded synthetic library'}
                  and not other_root.workspaces())
            check('native assistant preserves source bytes, saved note and unsaved editor text',
                  library.assets.resolve(imported['asset']).read_bytes() == original_bytes
                  and library.evidence(workspace, topic)[0].note == saved_note
                  and frame.evidence.note.GetValue() == unsaved_note)
            healthy()
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                'platform': platform.system(), 'origins': origins, 'native_key_backend': 'wx.UIActionSimulator',
                'operation': 'real source search, cancellation and rollback; native two-stage model approval with synthetic transport',
                'local_model_inference': 'NOT_RUN', 'provider_network': 'NOT_RUN',
                'source_override': str(source) if source is not None else None, 'nvda': 'NOT_RUN'},
                sort_keys=True), flush=True)
            return 0
        except Exception as exc:
            print(json.dumps({'status': 'FAIL', 'checks_passed': len(checks), 'error': str(exc),
                              'nvda': 'NOT_RUN'}), flush=True)
            traceback.print_exc()
            return 1
        finally:
            dialog_module.show_assistant = original_show
            watchdog.Stop()
            for gate in releases:
                gate.set()
            for actions in active_actions:
                actions.close()
            if frame is not None:
                frame.library = library
                frame.cancel_event.set()
                frame._closing = True
                frame.executor.shutdown(wait=False, cancel_futures=True)
                frame.Destroy()
                application.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', required=True)
    parser.add_argument('--source', type=Path, default=None)
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    if not 5 <= args.timeout <= 170:
        parser.error('--timeout must be between 5 and 170 seconds')
    return run_native(args.source.resolve() if args.source is not None else None, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
