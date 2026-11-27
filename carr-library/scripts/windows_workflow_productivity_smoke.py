"""Actual native named workflow reuse and redacted diagnostics; no NVDA claim."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import get_ident
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'nvda': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Plan, Scope, Step
    from carr_library.assistant.workflow_access import WorkflowAccess
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.assistant_dialog import show_assistant
    from carr_library.ui import diagnostics_dialog as diagnostics

    checks, errors = [], []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def fail():
        errors.append(traceback.format_exc())
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail()
            return False

    application = ProbeApp(False)
    def pump(predicate):
        deadline = monotonic() + 45
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() > deadline:
                raise TimeoutError('Native workflow or diagnostics did not complete.')
            application.Yield()
            sleep(0.01)

    def later(predicate, action, deadline=None):
        deadline = deadline or monotonic() + 30
        try:
            if errors:
                return
            if predicate():
                action()
            elif monotonic() >= deadline:
                raise TimeoutError('A native workflow control did not reach its expected state.')
            else:
                wx.CallLater(25, later, predicate, action, deadline)
        except Exception:
            fail()

    def named(dialog, name):
        result = [control for control in dialog.GetChildren() if control.GetName() == name]
        if len(result) != 1:
            raise AssertionError('Missing or ambiguous native control: ' + name)
        return result[0]

    def button(dialog, label):
        result = [control for control in dialog.GetChildren()
                  if isinstance(control, wx.Button) and control.GetLabel().replace('&', '') == label]
        if len(result) != 1:
            raise AssertionError('Missing native action: ' + label)
        return result[0]

    def click(control):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        check('native button handler: ' + control.GetLabel().replace('&', ''),
              control.GetEventHandler().ProcessEvent(event))

    check('diagnostics loads from the installed wheel',
          'site-packages' in [part.casefold() for part in Path(diagnostics.__file__).parts])
    check('native Windows wx is active', wx.Platform == '__WXMSW__')
    with TemporaryDirectory(prefix='research-workflow-productivity-native-') as directory:
        base = Path(directory)
        library = LibraryApp(base / 'library')
        old_workspace = library.create_workspace('Old original workspace')
        current_workspace = library.create_workspace('New chosen workspace')
        topic = library.create_topic(current_workspace, 'Native workflow')
        library.add_text(old_workspace, 'Private old original', 'Railway private old source.')
        library.add_text(current_workspace, 'Chosen original', 'Railway chosen source.')
        old_input, old_output = base / 'old-input', base / 'old-output'
        old_input.mkdir()
        old_output.mkdir()
        service = AssistantService(library)
        access = WorkflowAccess(service)
        original = access.save(Plan(Scope(old_workspace, (old_input,), old_output, allow_full_backup=True),
            (Step('library.catalog'), Step('library.search', {'query': 'Railway'}))), 'Saved typed macro')
        original_bytes = service.store.path(original).read_bytes()
        frame = DesktopFrame(library)
        try:
            application.SetTopWindow(frame)
            frame.Show()
            pump(lambda: not frame.working)
            frame._open_workspace(current_workspace, topic)
            pump(lambda: not frame.working and frame.active == (current_workspace, topic))
            created = []

            def workflow_ready(dialog):
                try:
                    check('named workflow has a native Escape close binding', dialog.GetEscapeId() == wx.ID_CANCEL)
                    inputs = named(dialog, 'Allowed input folder (optional; no other folders are readable)')
                    outputs = named(dialog, 'Chosen output folder (required for audio, extraction or backup)')
                    scope = named(dialog, 'Library scope')
                    review = named(dialog, 'Plan preview and actual results')
                    goal = named(dialog, 'Goal — guided English/Ukrainian commands or typed JSON plan')
                    name = named(dialog, 'Workflow name (optional until Save named workflow)')
                    full, = [control for control in dialog.GetChildren()
                             if isinstance(control, wx.CheckBox)
                             and control.GetLabel().startswith('Allow a complete backup')]
                    inputs.SetValue(str(old_input))
                    outputs.SetValue(str(old_output))
                    full.SetValue(True)
                    check('named workflow controls are native and accessible',
                          isinstance(scope, wx.Choice) and isinstance(name, wx.TextCtrl)
                          and isinstance(review, wx.TextCtrl)
                          and bool(review.GetWindowStyleFlag() & wx.TE_READONLY))
                    click(button(dialog, 'Reuse saved workflow'))

                    def chooser():
                        return next((window for window in wx.GetTopLevelWindows()
                                     if isinstance(window, wx.SingleChoiceDialog)
                                     and window.GetTitle() == 'Reusable typed workflows' and window.IsModal()), None)
                    def selected():
                        choice = chooser()
                        choice.SetSelection(0)
                        choice.EndModal(wx.ID_OK)
                        later(lambda: 'Saved typed macro' in goal.GetValue() and inputs.GetValue() == '', loaded)
                    def loaded():
                        check('reuse clears old filesystem and full-backup grants',
                              inputs.GetValue() == outputs.GetValue() == '' and not full.GetValue())
                        check('reuse alone cannot run a plan', not button(dialog, 'Run / resume').IsEnabled())
                        check('reuse preserves the original saved job', service.store.path(original).read_bytes() == original_bytes)
                        scope.SetStringSelection('New chosen workspace')
                        click(button(dialog, 'Preview plan'))
                        check('preview binds typed steps to the newly selected library',
                              current_workspace in review.GetValue() and old_workspace not in review.GetValue()
                              and button(dialog, 'Run / resume').IsEnabled())
                        click(button(dialog, 'Save named workflow'))
                        later(lambda: review.GetValue().startswith('Named workflow saved.'), saved)
                    def saved():
                        identities = set(service.store.identities()) - {original}
                        check('native Save creates exactly one new pending typed job', len(identities) == 1)
                        identity = identities.pop()
                        created.append(identity)
                        record = service.store.load(identity)
                        check('saving runs no operation and uses only the fresh scope',
                              record['status'] == 'queued' and all(row['status'] == 'pending' for row in record['steps'])
                              and record['plan']['scope']['workspace_id'] == current_workspace
                              and record['plan']['scope']['input_roots'] == []
                              and record['plan']['scope']['output_root'] is None
                              and record['plan']['scope']['allow_full_backup'] is False)
                        click(button(dialog, 'Run / resume'))
                        later(lambda: review.GetValue().startswith('complete: 2 of 2'), completed)
                    def completed():
                        report = service.get(created[0])
                        hits = report.results[1]['results']
                        check('explicit native Run executes both typed steps in the chosen library',
                              report.status == 'complete' and report.completed == 2
                              and len(hits) == 1 and hits[0]['workspace'] == current_workspace)
                        check('completed reuse leaves the original job unchanged',
                              service.store.path(original).read_bytes() == original_bytes)
                        click(button(dialog, 'Close'))
                    later(lambda: chooser() is not None, selected)
                except Exception:
                    fail()

            watchdog = wx.CallLater(45000, lambda: (
                errors.append('Native named workflow exceeded its deadline.'),
                [window.EndModal(wx.ID_CANCEL) for window in wx.GetTopLevelWindows()
                 if isinstance(window, wx.Dialog) and window.IsModal()]))
            try:
                show_assistant(frame, on_ready=workflow_ready)
            finally:
                watchdog.Stop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            check('native named workflow completed without a second job store', len(created) == 1)

            observed_threads, preview_closed = [], []
            original_collect = diagnostics.collect_diagnostics
            def observed_collect(*, cancelled):
                observed_threads.append(get_ident())
                return original_collect(cancelled=cancelled)
            diagnostics.collect_diagnostics = observed_collect
            exported = base / 'diagnostics.json'
            def diagnostics_ready(dialog):
                try:
                    check('diagnostics preview is native read-only text with an accessible name',
                          isinstance(dialog.preview, wx.TextCtrl)
                          and bool(dialog.preview.GetWindowStyleFlag() & wx.TE_READONLY)
                          and dialog.preview.GetName() == 'Exact redacted diagnostics report to export')
                    value = json.loads(dialog.preview.GetValue())
                    check('actual synthetic installation check passed without model or NVDA claims',
                          value['installation_check']['status'] == 'PASS'
                          and value['windows_nvda'] == value['model_inference'] == 'NOT_RUN')
                    check('private workspace names and paths are absent',
                          'Old original workspace' not in dialog.preview.GetValue()
                          and str(base) not in dialog.preview.GetValue())
                    dialog.preview.SetFocus()
                    dialog.preview.Navigate()
                    application.Yield()
                    check('Tab reaches the native diagnostics status', wx.Window.FindFocus() is dialog.status)
                    dialog.controller.export(exported)
                    check('the reviewed report is exported exactly',
                          exported.read_bytes() == dialog.controller.payload
                          == dialog.preview.GetValue().encode('utf-8'))
                    try:
                        dialog.controller.export(exported)
                    except FileExistsError:
                        check('an existing diagnostics export is preserved', exported.read_bytes() == dialog.controller.payload)
                    else:
                        raise AssertionError('Existing diagnostics export was overwritten.')
                    check('native diagnostics has an Escape close binding', dialog.GetEscapeId() == wx.ID_CANCEL)
                    preview_closed.append(True)
                    dialog.EndModal(wx.ID_CANCEL)
                except Exception:
                    fail()
            try:
                main_thread = get_ident()
                check('diagnostics is installed in the shared native command inventory',
                      any(command.identity == 'Help/Diagnostics preview and export'
                          for command in frame.keyboard_actions.commands))
                frame.diagnostics_actions.show(on_ready=diagnostics_ready)
                pump(lambda: bool(preview_closed) and not frame.working)
                check('real diagnostics runs off the native GUI thread',
                      len(observed_threads) == 1 and observed_threads[0] != main_thread)
            finally:
                diagnostics.collect_diagnostics = original_collect
        finally:
            if not frame.IsBeingDeleted():
                frame.Close(force=True)
                application.Yield()
            if native is not None:
                native.close()
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(json.dumps({'status': 'PASS', 'count': len(checks), 'checks': checks,
                      'nvda': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
