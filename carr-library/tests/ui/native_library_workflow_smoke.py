"""Installed-package Windows wx acceptance for the real local workflow adapter.

Run: python -I -B -X utf8 tests/ui/native_library_workflow_smoke.py

Real DesktopFrame menu events, widgets, worker jobs, TXT ingestion, scoped search,
topic creation, export and journal reopening are exercised. The only observation
hook delays a progress callback until the native Pause event has been delivered;
it neither replaces a service nor fabricates a result. All files are synthetic.
Non-Windows or unavailable wx returns NOT_RUN / 77. Windows CI must require 0.
This is native-control evidence, not an NVDA speech acceptance claim.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback


def not_run(reason, **extra):
    print(json.dumps(dict(status='NOT_RUN', reason=reason, windows_native='NOT_RUN',
                          windows_nvda='NOT_RUN', **extra)), flush=True)
    return 77


def run_native():
    if sys.platform != 'win32':
        return not_run('This acceptance probe requires native Windows wx controls.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable.', error=str(exc))

    from carr_library.application import LibraryApp
    from carr_library import library_assistant as service_module
    from carr_library.ui import library_assistant as ui_module
    from carr_library.ui import desktop_app

    checks, failures, timers = [], [], []
    progress_threads, report_threads = [], []
    state = dict(view=None, frame=None, phase='new', pause_observed=False)
    release_checkpoint = threading.Event()
    gui_thread = threading.get_ident()
    deadline = monotonic() + 40
    report = {}

    def hard_timeout():
        print(json.dumps(dict(status='FAIL', errors=['Native workflow smoke exceeded 60 seconds.'],
                              windows_native='FAIL', windows_nvda='NOT_RUN')), flush=True)
        os._exit(1)

    hard_watchdog = threading.Timer(60, hard_timeout)
    hard_watchdog.daemon = True
    hard_watchdog.start()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    gui = ProbeApp(False)

    def abort(message):
        failures.append(message)
        release_checkpoint.set()
        frame, view = state['frame'], state['view']
        if frame is not None:
            frame.cancel_event.set()
        if view is not None and view.controller.alive and view.dialog.IsModal():
            view.dialog.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    def guarded(callback):
        if failures:
            return
        try:
            callback()
        except BaseException:
            abort(traceback.format_exc())

    def later(callback, delay=25):
        timers.append(wx.CallLater(delay, lambda: guarded(callback)))

    def wait_for(predicate, callback):
        if monotonic() >= deadline:
            raise AssertionError('Native workflow state did not arrive before its deadline.')
        if predicate():
            callback()
        else:
            later(lambda: wait_for(predicate, callback))

    def click(button):
        assert isinstance(button, wx.Button) and button.IsShown() and button.IsEnabled()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event), 'Native button event was not handled.'

    original_view = ui_module.NativeLibraryWorkflow

    class ObservedNativeWorkflow(original_view):
        """Observe actual control threads and a deterministic cancellation boundary."""
        def __init__(self, frame):
            super().__init__(frame)
            state['view'] = self
            later(prepare_new if state['phase'] == 'new' else prepare_resume)

        def report(self, text, *, focus=False):
            report_threads.append(threading.get_ident())
            assert threading.get_ident() == gui_thread, 'A wx status update left the GUI thread.'
            return super().report(text, focus=focus)

        def progress(self, text):
            progress_threads.append(threading.get_ident())
            assert threading.get_ident() != gui_thread, 'The workflow ran on the GUI thread.'
            super().progress(text)
            if state['phase'] == 'new' and text.startswith('Step 2 '):
                # A public progress callback is the boundary before the next
                # tool. Deliver a real native Pause event while it is waiting.
                wx.CallAfter(lambda: guarded(pause_at_checkpoint))
                if not release_checkpoint.wait(10):
                    raise AssertionError('The native Pause event did not reach the checkpoint.')

    ui_module.NativeLibraryWorkflow = ObservedNativeWorkflow
    try:
        with TemporaryDirectory(prefix='carr-native-library-workflow-') as temporary:
            root = Path(temporary).resolve()
            inputs, outputs = root / 'selected inputs', root / 'selected outputs'
            inputs.mkdir()
            outputs.mkdir()
            source = inputs / 'workflow-source.txt'
            source.write_text('Shared railway excerpt. Український точний текст.\n', encoding='utf-8')
            source_bytes = source.read_bytes()
            library = LibraryApp(root / 'private library')
            workspace = library.create_workspace('Selected workflow library')
            foreign = library.create_workspace('Other private library')
            existing_topic = library.create_topic(workspace, 'Existing research')
            original_source = root / 'historical.txt'
            original_source.write_text('Original historical passage and its exact punctuation.\n', encoding='utf-8')
            original = library.import_file(workspace, original_source, title='Historical source')
            assert original['state'] == 'complete', original
            old_hit, = library.search(workspace, 'historical passage')
            library.save_hit(workspace, existing_topic, old_hit.ref, old_hit.revision,
                             'Separate existing research note.')
            private_source = root / 'other-private.txt'
            private_source.write_text('Shared railway PRIVATE_SCOPE_SENTINEL.\n', encoding='utf-8')
            private_import = library.import_file(foreign, private_source, title='PRIVATE_SCOPE_SENTINEL')
            assert private_import['state'] == 'complete', private_import
            original_evidence = library.evidence(workspace, existing_topic)
            original_bytes = library.assets.resolve(original['asset']).read_bytes()
            original_workspaces = library.workspaces()
            foreign_books, foreign_topics = library.browse(foreign), library.topics(foreign)

            frame = desktop_app.DesktopFrame(library)
            state['frame'] = frame
            frame._open_workspace(workspace, existing_topic)
            assert frame.active == (workspace, existing_topic)
            frame.Show()
            frame.Raise()
            gui.SetTopWindow(frame)
            original_handles = dict(library._selections)
            original_book = frame.book

            def unchanged_sources():
                assert source.read_bytes() == source_bytes
                assert original_source.read_bytes() == original_bytes
                assert library.assets.resolve(original['asset']).read_bytes() == original_bytes
                assert library.evidence(workspace, existing_topic) == original_evidence
                assert library.workspaces() == original_workspaces
                assert library.browse(foreign) == foreign_books
                assert library.topics(foreign) == foreign_topics
                assert library._selections == original_handles
                assert library.copy(workspace, old_hit.ref, old_hit.revision, 'quote') == 'historical passage'
                assert frame.active == (workspace, existing_topic) and frame.book is original_book

            def idle(view):
                return not view.controller.running and not frame.working

            def prepare_new():
                view = state['view']
                assert isinstance(view.dialog, wx.Dialog) and view.dialog.IsModal()
                assert wx.Window.FindFocus() is view.goal
                for control, name in (
                    (view.goal, 'Commands or JSON library plan'),
                    (view.input_root, 'Allowed input folder'),
                    (view.output_root, 'Allowed output folder'),
                    (view.review, 'Reviewed plan and permitted folders'),
                    (view.results, 'Saved step results and recovery status'),
                    (view.status, 'Library workflow status'),
                    (view.saved, 'Saved workflows in the selected library'),
                ):
                    assert control.GetName() == name and control.IsShown()
                assert not view.review.IsEditable() and not view.results.IsEditable()
                assert not view.status.IsEditable() and not view.backup_permission.GetValue()
                assert not view.run.IsEnabled() and not view.pause_button.IsEnabled()
                view.input_root.SetValue(str(inputs))
                view.output_root.SetValue(str(outputs))
                view.goal.SetValue('import: workflow-source.txt\ntopic: Workflow research\n'
                                   'export: @1 -> workflow.md\nsearch: Shared railway')
                click(view.preview)
                assert view.controller.running and not view.goal.IsEnabled()
                wait_for(lambda: idle(view), reviewed)

            def reviewed():
                view = state['view']
                assert view.run.IsEnabled(), view.status.GetValue()
                assert 'Plan ready for review' in view.status.GetValue()
                assert wx.Window.FindFocus() is view.status
                assert 'LOCAL ONLY' in view.review.GetValue()
                assert str(inputs) in view.review.GetValue() and str(outputs) in view.review.GetValue()
                assert 'import_text' in view.review.GetValue() and 'export_topic' in view.review.GetValue()
                assert not view.controller.service.journal.exists()
                assert len(library.browse(workspace)) == 1 and len(library.topics(workspace)) == 1
                assert not tuple(outputs.iterdir())
                unchanged_sources()
                checks.append('real named native menu dialog and read-only reviewed plan before approval')
                click(view.run)
                assert view.controller.running and view.pause_button.IsEnabled()
                assert not view.run.IsEnabled() and all(not control.IsEnabled() for control in view.edit_controls)

            def pause_at_checkpoint():
                view = state['view']
                try:
                    assert view.controller.running and frame.working
                    assert view.status.GetValue().startswith('Step 2 '), view.status.GetValue()
                    assert len(library.browse(workspace)) == 2
                    assert len(library.topics(workspace)) == 1 and not tuple(outputs.iterdir())
                    click(view.pause_button)
                    assert frame.cancel_event.is_set()
                    assert 'Pause requested' in view.status.GetValue()
                    state['pause_observed'] = True
                finally:
                    release_checkpoint.set()
                wait_for(lambda: idle(view), paused)

            def paused():
                view = state['view']
                assert state['pause_observed']
                assert 'Workflow paused' in view.status.GetValue(), view.status.GetValue()
                assert wx.Window.FindFocus() is view.status and view.run.IsEnabled()
                job = view.controller.service.load(view.controller.job_id)
                assert job['state'] == 'paused'
                assert job['steps'][0]['state'] == 'complete'
                assert all(step['state'] != 'complete' and step['result'] is None for step in job['steps'][1:])
                state['job_id'], state['import_result'] = job['id'], job['steps'][0]['result']
                assert 'Completed steps: 1/4' in view.results.GetValue()
                assert len(library.topics(workspace)) == 1 and not tuple(outputs.iterdir())
                unchanged_sources()
                checks.append('native Pause checkpoints the real import before topic or export publication')
                click(view.close_button)

            def prepare_resume():
                view = state['view']
                assert view.controller.job_id is None and not view.run.IsEnabled()
                click(view.refresh)
                wait_for(lambda: idle(view), saved_jobs)

            def saved_jobs():
                view = state['view']
                assert view.job_ids == [state['job_id']], view.status.GetValue()
                assert view.saved.GetCount() == 1 and 'paused' in view.saved.GetString(0)
                view.saved.SetSelection(0)
                click(view.load_button)
                wait_for(lambda: idle(view), loaded)

            def loaded():
                view = state['view']
                assert view.controller.job_id == state['job_id'] and view.run.IsEnabled(), view.status.GetValue()
                assert 'Saved plan loaded' in view.status.GetValue()
                assert view.input_root.GetValue() == str(inputs) and view.output_root.GetValue() == str(outputs)
                assert not view.backup_permission.GetValue()
                assert 'Completed steps: 1/4' in view.results.GetValue()
                assert len(library.browse(workspace)) == 2 and len(library.topics(workspace)) == 1
                checks.append('fresh native dialog reloads the exact scoped job and reviewed folder permissions')
                click(view.run)
                wait_for(lambda: idle(view), completed)

            def completed():
                view = state['view']
                assert 'Workflow complete' in view.status.GetValue(), view.status.GetValue()
                assert wx.Window.FindFocus() is view.status
                assert not view.run.IsEnabled() and not view.pause_button.IsEnabled()
                assert 'Completed steps: 4/4' in view.results.GetValue()
                job = view.controller.service.load(state['job_id'])
                assert job['state'] == 'complete' and all(step['state'] == 'complete' for step in job['steps'])
                assert job['steps'][0]['result'] == state['import_result']
                assert len(library.browse(workspace)) == 2 and len(library.topics(workspace)) == 2
                new_topic = job['steps'][1]['result']['topic_id']
                output = outputs / 'workflow.md'
                assert output.read_text('utf-8') == library.export_topic(workspace, new_topic)
                assert sha256(output.read_bytes()).hexdigest() == job['steps'][2]['result']['sha256']
                assert tuple(path.name for path in outputs.iterdir()) == ('workflow.md',)
                search = job['steps'][3]['result']
                assert search['count'] == 1 and len(search['results']) == 1
                assert search['results'][0]['workspace'] == workspace
                assert 'PRIVATE_SCOPE_SENTINEL' not in json.dumps(search)
                imported = state['import_result']
                assert library.assets.resolve(imported['asset']).read_bytes() == source_bytes
                unchanged_sources()
                assert progress_threads and all(identity != gui_thread for identity in progress_threads)
                assert report_threads and all(identity == gui_thread for identity in report_threads)
                checks.append('real resumed worker publishes exactly one topic and export without repeating import')
                checks.append('scoped source results exclude the other library and preserve quotations, handles and notebook')
                checks.append('native status reports stay on the GUI thread while real tool execution uses the worker')
                state['complete'] = True
                click(view.close_button)

            def open_from_menu():
                item = frame.library_assistant_menu_item
                assert ui_module.install_library_assistant_actions(frame) is item
                tools = [frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                         if frame.menu_bar.GetMenuLabel(index).replace('&', '') == 'Tools']
                assert len(tools) == 1
                assert sum('library workflow assistant' in entry.GetItemLabelText()
                           for entry in tools[0].GetMenuItems()) == 1
                event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
                event.SetEventObject(frame)
                assert frame.GetEventHandler().ProcessEvent(event), 'Native menu event was not handled.'
                if failures:
                    return
                assert not state['view'].controller.alive
                if state['phase'] == 'new':
                    assert state.get('job_id') and state['pause_observed']
                    state['phase'] = 'resume'
                    later(open_from_menu)
                else:
                    assert state.get('complete'), 'Dialog closed before real workflow completion.'
                    report.update(status='PASS', native_platform=platform.system(), wx_version=wx.version(),
                        checks=checks, windows_native='PASS', windows_nvda='NOT_RUN',
                        service_sha256=sha256(Path(service_module.__file__).read_bytes()).hexdigest(),
                        ui_sha256=sha256(Path(ui_module.__file__).read_bytes()).hexdigest(),
                        desktop_sha256=sha256(Path(desktop_app.__file__).read_bytes()).hexdigest())
                    gui.ExitMainLoop()

            timers.append(wx.CallLater(40000, lambda: abort('Native workflow smoke exceeded 40 seconds.')))
            later(open_from_menu)
            try:
                gui.MainLoop()
            finally:
                release_checkpoint.set()
                for timer in timers:
                    timer.Stop()
                frame.cancel_event.set()
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
                gui.ProcessPendingEvents()
    finally:
        ui_module.NativeLibraryWorkflow = original_view
        hard_watchdog.cancel()

    if failures or report.get('status') != 'PASS':
        print(json.dumps(dict(status='FAIL', errors=failures or ['Probe did not complete.'],
                              windows_native='FAIL', windows_nvda='NOT_RUN'), ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


def main():
    if sys.platform != 'win32':
        return not_run('This acceptance probe requires native Windows wx controls.')
    runtime_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        runtime_handle = activate_bundled_runtime()
        return run_native()
    except BaseException:
        print(json.dumps(dict(status='FAIL', errors=[traceback.format_exc()],
                              windows_native='FAIL', windows_nvda='NOT_RUN')), flush=True)
        return 1
    finally:
        if runtime_handle is not None:
            runtime_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
