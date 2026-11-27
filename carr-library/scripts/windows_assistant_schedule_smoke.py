"""Actual native foreground schedule review and one current assistant execution.

No OS task, background daemon, remote model, voice or private content is used.
"""
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('Run this native acceptance probe on Windows.')
    import wx
    import carr_library.ui.assistant_dialog as module
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService
    origin = Path(module.__file__).resolve()
    if Path(sys.prefix).resolve() not in origin.parents:
        raise RuntimeError('The native schedule probe must use the isolated installed product.')
    application = wx.App(False)
    failures, controls, result = [], {}, {}
    active, phase, timer = [True], [0], [None]
    deadline = monotonic() + 90
    with TemporaryDirectory(prefix='carr-native-schedule-') as temporary:
        library = LibraryApp(Path(temporary) / 'library')
        frame = wx.Frame(None, title='Local schedule native acceptance')
        frame.library, frame.active = library, None
        frame.working, frame.busy, frame._closing = False, False, False
        frame.Show()
        service = AssistantService(library)
        root_dialog, identity, complete_at = [None], [None], [None]
        def named(parent, name):
            value = wx.FindWindowByName(name, parent)
            if value is None:
                raise AssertionError('Missing native control: ' + name)
            return value
        def press(control):
            if not isinstance(control, wx.Button) or not control.IsEnabled():
                raise AssertionError('The actual native action is not enabled: ' + control.GetName())
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
            event.SetEventObject(control)
            if not control.GetEventHandler().ProcessEvent(event):
                raise AssertionError('The native action was not handled.')
        def modal(name):
            return next((window for window in wx.GetTopLevelWindows()
                         if isinstance(window, wx.Dialog) and window.GetName() == name
                         and window.IsShown() and window.IsModal()), None)
        def drive():
            if not active[0]:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native schedule acceptance exceeded its finite deadline.')
                if not controls:
                    return
                if phase[0] == 0:
                    controls['instruction'].SetValue('Create library "Native scheduled once"')
                    press(controls['preview'])
                    phase[0] = 1
                elif phase[0] == 1 and controls['schedule'].IsEnabled():
                    assert library.workspaces() == () and service.jobs() == ()
                    press(controls['schedule'])
                    phase[0] = 2
                elif phase[0] == 2:
                    chosen = modal('Assistant schedule time')
                    if chosen is None:
                        return
                    detail = named(chosen, 'Exact reviewed plan and scope')
                    assert isinstance(detail, wx.TextCtrl) and not detail.IsEditable()
                    assert 'Native scheduled once' in detail.GetValue()
                    due = (datetime.now(timezone.utc) + timedelta(seconds=8)).isoformat()
                    named(chosen, 'Start time in UTC with Z or +00:00').SetValue(due)
                    named(chosen, 'Start-window allowance in seconds; 1 to 86400').SetValue('60')
                    press(named(chosen, 'Save time only'))
                    phase[0] = 3
                elif phase[0] == 3 and controls['schedules'].IsEnabled():
                    rows = service.jobs()
                    if not rows:
                        return
                    row, = rows
                    record = service.store.load(row.id)
                    if 'schedule' not in record:
                        return
                    assert record['status'] == 'queued' and record['schedule']['state'] == 'pending'
                    assert library.workspaces() == () and not controls['disarm'].IsEnabled()
                    identity[0] = row.id
                    press(controls['schedules'])
                    phase[0] = 4
                elif phase[0] == 4:
                    chosen = modal('Assistant local schedules')
                    if chosen is None:
                        return
                    selected = named(chosen, 'Saved concrete schedules')
                    assert isinstance(selected, wx.ListBox) and selected.GetCount() == 1
                    detail = named(chosen, 'Exact scheduled plan and permission scope')
                    assert not detail.IsEditable() and 'Native scheduled once' in detail.GetValue()
                    assert identity[0] in detail.GetValue()
                    press(named(chosen, 'Arm selected for this window'))
                    phase[0] = 5
                elif phase[0] == 5:
                    record = service.store.load(identity[0])
                    if record['status'] != 'complete' or not controls['run'].IsEnabled():
                        return
                    assert record['schedule']['state'] == 'complete'
                    assert [row.name for row in library.workspaces()] == ['Native scheduled once']
                    assert not controls['disarm'].IsEnabled()
                    complete_at[0] = monotonic()
                    phase[0] = 6
                elif phase[0] == 6 and monotonic() - complete_at[0] >= 1.2:
                    assert len(service.jobs()) == len(library.workspaces()) == 1
                    result.update(schema='carr-native-assistant-schedule/1', status='PASS',
                        installed_module=str(origin), native_controls=True,
                        exact_scope_review=True, saved_without_running=True,
                        separately_armed_foreground=True, ordinary_job_complete=True,
                        repeated_timer_did_not_repeat=True, remote_model='NOT_RUN',
                        os_scheduler_installed=False, nvda='NOT_RUN')
                    active[0] = False
                    press(controls['close'])
            except BaseException as error:
                failures.append(type(error).__name__ + ': ' + str(error))
                active[0] = False
                # Close any nested review first, then the existing assistant.
                for name in ('Assistant schedule time', 'Assistant local schedules'):
                    child = modal(name)
                    if child is not None:
                        child.EndModal(wx.ID_CANCEL)
                if root_dialog[0] is not None and root_dialog[0].IsModal():
                    wx.CallAfter(press, controls['close'])
                else:
                    wx.CallAfter(application.ExitMainLoop)
            finally:
                if active[0]:
                    timer[0] = wx.CallLater(25, drive)
        def ready(dialog, values):
            root_dialog[0] = dialog
            controls.update(values)
        def opened():
            try:
                module.show_assistant(frame, on_ready=ready)
            except BaseException as error:
                failures.append(type(error).__name__ + ': ' + str(error))
            finally:
                wx.CallAfter(application.ExitMainLoop)
        timer[0] = wx.CallLater(25, drive)
        wx.CallAfter(opened)
        import faulthandler
        faulthandler.dump_traceback_later(100)
        try:
            application.MainLoop()
        finally:
            faulthandler.cancel_dump_traceback_later()
            active[0] = False
            if timer[0] is not None:
                timer[0].Stop()
            frame._closing = True
            frame.Destroy()
        if failures:
            raise AssertionError('; '.join(failures))
        if result.get('status') != 'PASS':
            raise AssertionError('The native schedule flow did not finish.')
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
