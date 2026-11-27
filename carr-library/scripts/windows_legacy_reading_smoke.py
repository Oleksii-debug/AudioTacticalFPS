"""Installed Windows controls for explicit historical reading-copy import.

The input is synthetic public text. This checks native revision selection,
named read-only previews, Unicode titles and Cancel; it does not claim NVDA
acceptance or source quotation authority.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Event, Thread
from time import monotonic
import traceback


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.reader import legacy_reading
    from carr_library.ui import reading_import

    errors, checks = [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    check('reading import loaded from installed package', all(
        'site-packages' in [part.casefold() for part in Path(module.__file__).parts]
        for module in (legacy_reading, reading_import)))
    frame = wx.Frame(None, title='Synthetic retained reading-history controls')
    gui.SetTopWindow(frame)
    frame.Show()
    source = 'Київ — pôvodný text.'
    value = dict(schema='carr.reading-copy.v1', title='Історія — čítanie',
        source_text=source, source_sha256=sha256(source.encode()).hexdigest(),
        edits=[dict(start=0, end=4, replacement='Львів', label='Історична правка'),
               dict(start=0, end=5, replacement='Київ', label='Збережене повернення')], revision=1)
    snapshot = legacy_reading.decode_snapshot(json.dumps(value, ensure_ascii=False).encode())
    dialogs = reading_import.ReadingImportDialogs()
    def owned():
        return [window for window in wx.GetTopLevelWindows()
                if isinstance(window, wx.Dialog) and window.IsModal() and window.GetParent() is frame]
    def children(dialog):
        pending, result = list(dialog.GetChildren()), []
        while pending:
            item = pending.pop()
            result.append(item)
            if len(result) > 80:
                raise AssertionError('Unexpected reading preview control inventory.')
            pending.extend(item.GetChildren())
        return result
    def press(dialog, identifier):
        button = dialog.FindWindow(identifier)
        check('actual enabled native dialog button', isinstance(button, wx.Button) and button.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native button event dispatched', button.GetEventHandler().ProcessEvent(event))
    def drive(call, *, phase, cancel=False):
        state = dict(active=True, done=False)
        timer = [None]
        deadline = monotonic() + 8
        def poll():
            if not state['active']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Historical reading dialog did not complete.')
                windows = owned()
                if not windows:
                    timer[0] = wx.CallLater(25, poll)
                    return
                check('one owned reading modal', len(windows) == 1)
                dialog = windows[0]
                controls = children(dialog)
                if phase == 'revision':
                    check('actual historical revision selector', isinstance(dialog, wx.SingleChoiceDialog))
                    lists = [item for item in controls if isinstance(item, wx.ListBox)]
                    check('complete Unicode undo and redo inventory',
                          len(lists) == 1 and tuple(lists[0].GetStrings()) == snapshot.revision_labels)
                    dialog.SetSelection(2)
                    check('actual list selects retained redo revision', lists[0].GetSelection() == 2)
                else:
                    fields = {item.GetName(): item for item in controls if isinstance(item, wx.TextCtrl)}
                    check('all native reading-preview controls named', set(fields) == {
                        'New reading-copy title', 'Retained source positions and historical revision',
                        'Historical reading text preview'})
                    selected = snapshot.at(2)
                    body = fields['Historical reading text preview']
                    history = fields['Retained source positions and historical revision']
                    title = fields['New reading-copy title']
                    check('full Unicode historical text remains read-only',
                          body.GetValue() == selected.text and not body.IsEditable())
                    check('compact provenance remains read-only',
                          history.GetValue() == legacy_reading.history_description(selected)
                          and not history.IsEditable())
                    check('new explicit title is editable', title.IsEditable())
                    title.SetValue('Нова копія — čítanie')
                state['done'] = True
                press(dialog, wx.ID_CANCEL if cancel else wx.ID_OK)
            except Exception:
                errors.append(traceback.format_exc())
                state['active'] = False
                for dialog in owned():
                    dialog.EndModal(wx.ID_CANCEL)
        timer[0] = wx.CallLater(25, poll)
        try:
            result = call()
            if errors:
                raise AssertionError(errors[-1][-1800:])
            check('historical reading modal settled', state['done'] and not owned())
            return result
        finally:
            state['active'] = False
            if timer[0] is not None:
                timer[0].Stop()
    try:
        selected = drive(lambda: dialogs.choose(frame, 'Choose historical reading revision',
                         'Select retained undo or redo history.', snapshot.revision_labels), phase='revision')
        check('accepted selector returns exact retained revision', selected == 2)
        result = drive(lambda: dialogs.preview(frame, snapshot.at(selected)), phase='preview')
        check('native review returns explicit Unicode title', result == 'Нова копія — čítanie')
        check('native Cancel creates no project', drive(
            lambda: dialogs.preview(frame, snapshot.at(2)), phase='preview', cancel=True) is None)
        print(json.dumps(dict(status='PASS', native_controls='PASS', unicode='PASS',
            historical_revision='PASS', checks=len(checks), check_names=checks,
            nvda='NOT_RUN'), sort_keys=True), flush=True)
        return 0
    finally:
        for dialog in owned():
            dialog.EndModal(wx.ID_CANCEL)
        frame.Destroy()
        gui.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps(dict(status='NOT_RUN', reason='Explicit native Windows execution required.',
                              nvda='NOT_RUN')), flush=True)
        return 77
    finished = Event()
    def watchdog():
        if not finished.wait(45):
            print(json.dumps(dict(status='FAIL', reason='Native reading probe exceeded 45 seconds.',
                                  nvda='NOT_RUN')), flush=True)
            os._exit(1)
    Thread(target=watchdog, name='reading-import-probe-watchdog', daemon=True).start()
    try:
        return run_native()
    except BaseException as error:
        print(json.dumps(dict(status='FAIL', error=type(error).__name__ + ': ' + str(error)[:600],
                              nvda='NOT_RUN')), flush=True)
        return 1
    finally:
        finished.set()


if __name__ == '__main__':
    raise SystemExit(main())
