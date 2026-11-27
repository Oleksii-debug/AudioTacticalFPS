"""Native saved workflow review, rename, load and fresh scoped execution.

The installed product owns every store and operation. No model, credential,
background scheduler or private input is used by this synthetic acceptance flow.
"""
from argparse import ArgumentParser
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import enumerate as threads
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
    from carr_library.assistant import AssistantService, Scope
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    origin = Path(module.__file__).resolve()
    if Path(sys.prefix).resolve() not in origin.parents:
        raise RuntimeError('The native workflow probe requires the isolated installed product.')
    application = wx.App(False)
    before_threads = set(threads())
    controls, failures, result = {}, [], {}
    active, phase, timer = [True], [0], [None]
    root_dialog, selected_template = [None], [None]
    deadline = monotonic() + 90
    with TemporaryDirectory(prefix='carr-native-workflow-') as temporary:
        base = Path(temporary)
        library = LibraryApp(base / 'library')
        first = library.create_workspace('A original workspace')
        second = library.create_workspace('B newly chosen workspace')
        library.add_text(first, 'Original scope', 'Original first source.')
        library.add_text(second, 'Current scope', 'Current second source.')
        service = AssistantService(library)
        store = service.workflow_templates()
        frame = wx.Frame(None, title='Saved workflow native acceptance')
        frame.library, frame.active = library, first
        frame.working, frame.busy, frame._closing = False, False, False
        frame.Show()

        def press(control):
            if not isinstance(control, wx.Button) or not control.IsEnabled():
                raise AssertionError('The actual native action is not enabled.')
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
            event.SetEventObject(control)
            if not control.GetEventHandler().ProcessEvent(event):
                raise AssertionError('The native action was not handled.')

        def modal(*, name=None, title=None):
            return next((window for window in wx.GetTopLevelWindows()
                if isinstance(window, wx.Dialog) and window.IsShown() and window.IsModal()
                and (name is None or window.GetName() == name)
                and (title is None or window.GetTitle() == title)), None)

        def named(parent, name):
            value = wx.FindWindowByName(name, parent)
            if value is None:
                raise AssertionError('Missing native control: ' + name)
            return value

        def choose_scope(identity):
            ids = [row.id for row in library.workspaces()]
            controls['scope'].SetSelection(ids.index(identity) + 1)
            event = wx.CommandEvent(wx.EVT_CHOICE.typeId, controls['scope'].GetId())
            event.SetEventObject(controls['scope'])
            controls['scope'].GetEventHandler().ProcessEvent(event)

        def drive():
            if not active[0]:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native workflow acceptance exceeded its deadline.')
                # The next driver already exists when a native button opens
                # a nested synchronous wx modal loop.
                timer[0] = wx.CallLater(25, drive)
                if not controls:
                    return
                if phase[0] == 0:
                    choose_scope(first)
                    controls['instruction'].SetValue('List books')
                    phase[0] = 1
                    press(controls['preview'])
                elif phase[0] == 1 and controls['run'].IsEnabled():
                    assert service.jobs() == () and not store.root.exists()
                    phase[0] = 2
                    press(controls['templates'])
                elif phase[0] == 2:
                    child = modal(name='Assistant workflow templates')
                    if child is None or not named(child, 'Save plan as new').IsEnabled():
                        return
                    assert child.GetParent() is root_dialog[0]
                    phase[0] = 3
                    press(named(child, 'Save plan as new'))
                elif phase[0] == 3:
                    child = modal(title='Save workflow template')
                    if child is None:
                        return
                    assert isinstance(child, wx.TextEntryDialog)
                    child.SetValue('Native reusable catalog')
                    phase[0] = 4
                    press(child.FindWindow(wx.ID_OK))
                elif phase[0] == 4:
                    child = modal(name='Assistant workflow templates')
                    if child is None or not named(child, 'Rename selected').IsEnabled():
                        return
                    rows = named(child, 'Saved workflow templates; selected rows form a batch in list order')
                    if rows.GetCount() != 1:
                        return
                    saved, = store.list()
                    assert saved.name == 'Native reusable catalog' and saved.revision == 1
                    assert 'scope' not in saved.to_dict()['draft'] and service.jobs() == ()
                    selected_template[0] = saved.id
                    rows.SetSelection(0)
                    phase[0] = 5
                    press(named(child, 'Rename selected'))
                elif phase[0] == 5:
                    child = modal(title='Rename workflow template')
                    if child is None:
                        return
                    assert isinstance(child, wx.TextEntryDialog)
                    child.SetValue('Native renamed catalog')
                    phase[0] = 6
                    press(child.FindWindow(wx.ID_OK))
                elif phase[0] == 6:
                    child = modal(name='Assistant workflow templates')
                    if child is None or not named(child, 'Load selected as draft').IsEnabled():
                        return
                    rows = named(child, 'Saved workflow templates; selected rows form a batch in list order')
                    if rows.GetCount() != 1 or not rows.GetString(0).startswith('Native renamed catalog; revision 2;'):
                        return
                    assert store.get(selected_template[0]).revision == 2
                    rows.SetSelection(0)
                    phase[0] = 7
                    press(named(child, 'Load selected as draft'))
                elif phase[0] == 7 and controls['templates'].IsEnabled():
                    draft = json.loads(controls['instruction'].GetValue())
                    assert set(draft) == {'schema', 'steps', 'summary'} and 'scope' not in draft
                    assert not controls['run'].IsEnabled() and not controls['schedule'].IsEnabled()
                    assert service.jobs() == ()
                    choose_scope(second)
                    phase[0] = 8
                    press(controls['preview'])
                elif phase[0] == 8 and controls['run'].IsEnabled():
                    assert service.jobs() == ()
                    phase[0] = 9
                    press(controls['run'])
                elif phase[0] == 9 and controls['templates'].IsEnabled():
                    jobs = service.jobs()
                    if not jobs or jobs[0].status != 'complete':
                        return
                    job, = jobs
                    record = service.store.load(job.id)
                    assert record['plan']['scope']['workspace_id'] == second
                    assert len(library.workspaces()) == 2
                    assert 'schedule' not in record
                    result.update(schema='carr-native-assistant-templates/1',
                        installed_module=str(origin), native_controls=True,
                        native_save_rename_load=True, saved_without_scope_or_approval=True,
                        separate_fresh_scope_preview_and_run=True,
                        model_inference='NOT_RUN', nvda='NOT_RUN')
                    phase[0] = 10
                    active[0] = False
                    press(controls['close'])
            except BaseException as error:
                failures.append(type(error).__name__ + ': ' + str(error))
                active[0] = False
                child = modal(title='Save workflow template') or modal(title='Rename workflow template')
                if child is not None:
                    child.EndModal(wx.ID_CANCEL)
                picker = modal(name='Assistant workflow templates')
                if picker is not None:
                    picker.template_handlers['close']()
                if root_dialog[0] is not None and root_dialog[0].IsModal():
                    wx.CallAfter(press, controls['close'])
                else:
                    wx.CallAfter(application.ExitMainLoop)

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
            for thread in set(threads()) - before_threads:
                if thread.name.startswith('local-assistant'):
                    thread.join(timeout=5)
                    if thread.is_alive():
                        failures.append('The actual assistant worker did not settle after close.')
        if failures or phase[0] != 10:
            raise AssertionError('; '.join(failures or ['The native workflow flow did not finish.']))
        relative = 'state/assistant/templates/' + selected_template[0] + '.json'
        original = (library.root / relative).read_bytes()
        backup = base / 'backup'
        pin = create_backup(library, backup)
        verify_backup(backup, pin)
        restored = restore_backup(backup, base / 'restored', pin)
        assert (restored / relative).read_bytes() == original
        recovered = AssistantService(LibraryApp(restored))
        saved = recovered.workflow_templates().get(selected_template[0])
        try:
            recovered.preview(saved.instruction(), Scope())
        except PermissionError:
            pass
        else:
            raise AssertionError('A restored template inherited its old workspace authority.')
        result.update(status='PASS', exact_bytes_backup_restore=True,
                      restored_scope_inert=True, ordinary_job_complete=True)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
