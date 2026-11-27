"""Installed Windows audio-effect dialogs with real named wx controls.

Run with the installed application's Python: -I this_file.py --native.
Only synthetic role names and numeric values are used. Signal rendering and
screen-reader speech have their separate acceptance checks.
"""
from argparse import ArgumentParser
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
    from carr_library.audio import models
    from carr_library.ui import audio_effects

    checks, errors = [], []

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
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    check('effect implementation loaded from installed package', all(
        'site-packages' in [part.casefold() for part in Path(module.__file__).parts]
        for module in (audio_effects, models)))
    frame = wx.Frame(None, title='Synthetic local audio-effect controls')
    gui.SetTopWindow(frame)
    frame.Show()
    roles = ('Narrator', 'Ірина')
    kind = 'compressor'
    values = dict(threshold_db=-24, ratio=2.5, attack_ms=15,
                  release_ms=180, knee=3, makeup_db=2, mix=.75)
    definitions = models.effect_controls()

    def owned_modals():
        return [window for window in wx.GetTopLevelWindows()
                if isinstance(window, wx.Dialog) and window.IsModal()
                and window.GetParent() is frame]

    def descendants(window):
        pending, result = list(window.GetChildren()), []
        while pending:
            child = pending.pop()
            result.append(child)
            if len(result) > 128:
                raise AssertionError('The effect dialog has an unexpected control inventory.')
            pending.extend(child.GetChildren())
        return result

    def press(dialog, identifier):
        button = dialog.FindWindow(identifier)
        check('actual enabled native dialog button',
              isinstance(button, wx.Button) and button.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native dialog button dispatch', button.GetEventHandler().ProcessEvent(event))

    def drive(*, cancel_at=None, invalid_number=False):
        state = dict(active=True, phase=0, complete=False)
        timer = [None]
        deadline = monotonic() + 12
        phases = ('role', 'effect', 'parameters')

        def schedule():
            timer[0] = wx.CallLater(25, poll)

        def poll():
            if not state['active']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('The native effect dialog did not complete in time.')
                dialogs = owned_modals()
                if not dialogs:
                    schedule()
                    return
                check('one modal belongs to the synthetic parent', len(dialogs) == 1)
                dialog, phase = dialogs[0], phases[state['phase']]
                if phase == 'role':
                    check('named role selector', isinstance(dialog, wx.SingleChoiceDialog)
                          and dialog.GetName() == 'Role for the new audio effect')
                    dialog.SetSelection(1)
                    check('Unicode role selection is retained', dialog.GetStringSelection() == roles[1])
                elif phase == 'effect':
                    check('named effect selector', isinstance(dialog, wx.SingleChoiceDialog)
                          and dialog.GetName() == 'Audio effect type')
                    dialog.SetSelection(tuple(definitions).index(kind))
                    check('selected typed effect kind', dialog.GetStringSelection() == kind)
                else:
                    check('named parameter dialog', dialog.GetName() == f'Parameters for {kind} on {roles[1]}')
                    fields = [control for control in descendants(dialog) if isinstance(control, wx.TextCtrl)]
                    expected = {
                        f'{name.replace("_", " ")} ({spec["unit"]}; {spec["minimum"]} to {spec["maximum"]})': name
                        for name, spec in definitions[kind].items()}
                    check('every numeric control has its name, unit and bounds',
                          len(fields) == len(expected) and {field.GetName() for field in fields} == set(expected))
                    for field in fields:
                        name = expected[field.GetName()]
                        check('numeric control is editable', field.IsEnabled() and field.IsEditable())
                        field.SetValue('not a number' if invalid_number and name == 'threshold_db'
                                       else str(values[name]))
                if phase == cancel_at:
                    state['complete'] = True
                    press(dialog, wx.ID_CANCEL)
                    return
                state['phase'] += 1
                if state['phase'] < len(phases):
                    schedule()
                else:
                    state['complete'] = True
                press(dialog, wx.ID_OK)
            except Exception:
                errors.append(traceback.format_exc())
                state['active'] = False
                for dialog in owned_modals():
                    dialog.EndModal(wx.ID_CANCEL)

        schedule()
        try:
            try:
                result = audio_effects.choose_role_effect(frame, roles)
            except ValueError:
                if not invalid_number:
                    raise
                result = 'invalid-number-rejected'
            if errors:
                raise AssertionError(errors[-1][-2000:])
            check('native modal sequence completed', state['complete'])
            check('no effect modal remains after return', not owned_modals())
            return result
        finally:
            state['active'] = False
            if timer[0] is not None:
                timer[0].Stop()

    try:
        result = drive()
        check('confirmed dialog returns the selected role and typed Effect',
              isinstance(result, tuple) and len(result) == 2 and result[0] == roles[1]
              and type(result[1]) is models.Effect and result[1].kind == kind
              and result[1].enabled is True)
        check('typed parameters match the actual edited controls', dict(result[1].parameters) == values)
        for phase in ('role', 'effect', 'parameters'):
            check('Cancel returns no effect from ' + phase, drive(cancel_at=phase) is None)
        check('invalid numeric entry is rejected after native confirmation',
              drive(invalid_number=True) == 'invalid-number-rejected')
        check('no native callback failed', not errors)
        print(json.dumps(dict(status='PASS', checks=len(checks), wx=wx.version(),
                              native_controls='PASS', typed_parameters='PASS', cancellation='PASS',
                              nvda='NOT_RUN', signal_rendering='NOT_RUN'), sort_keys=True), flush=True)
    finally:
        for dialog in owned_modals():
            dialog.EndModal(wx.ID_CANCEL)
        frame.Destroy()
        gui.Yield()
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps(dict(status='NOT_RUN', reason='An explicit native Windows run is required.',
                              nvda='NOT_RUN')), flush=True)
        return 77
    finished = Event()

    def watchdog():
        if not finished.wait(60):
            print(json.dumps(dict(status='FAIL', reason='The native effects probe exceeded 60 seconds.',
                                  nvda='NOT_RUN')), flush=True)
            os._exit(1)  # Standalone synthetic probe only.

    Thread(target=watchdog, name='native-effects-watchdog', daemon=True).start()
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
