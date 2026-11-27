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


def drive_chain(wx, show_editor, frame, editor, *, private_root, check, cancel=False):
    """Drive visible controls on separate event turns, including nested dialogs."""
    state = {'active': True, 'dialog': None, 'fields': None, 'buttons': None,
             'status': None, 'phase': 'ready', 'complete': False, 'error': None}
    timers = []
    deadline = monotonic() + 15

    def schedule():
        timers.append(wx.CallLater(25, poll))

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('The effect editor action is not an enabled native button.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The effect editor button event was not handled.')

    def choose(control, index, event_type):
        control.SetSelection(index)
        event = wx.CommandEvent(event_type.typeId, control.GetId())
        event.SetEventObject(control)
        if not control.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The effect editor selection event was not handled.')

    def nested():
        return [window for window in wx.GetTopLevelWindows()
                if isinstance(window, wx.Dialog) and window.IsModal()
                and window.GetParent() is state['dialog']]

    def dismiss():
        for window in nested():
            window.EndModal(wx.ID_CANCEL)
        dialog = state['dialog']
        if dialog is not None and dialog.IsModal():
            dialog.EndModal(wx.ID_CANCEL)

    def poll():
        if not state['active']:
            return
        try:
            if monotonic() >= deadline:
                raise TimeoutError('The native effect-chain sequence exceeded its deadline.')
            dialog, fields, buttons, phase = (
                state['dialog'], state['fields'], state['buttons'], state['phase'])
            if dialog is None or not dialog.IsModal() or not dialog.IsShown():
                schedule()
                return
            if dialog.GetParent() is not frame:
                raise AssertionError('The effect editor belongs to another native parent.')
            if phase == 'ready':
                check('named native chain window', dialog.GetName() == 'Role and master effect chains')
                check('native role and effect-order controls',
                      isinstance(fields['role'], wx.Choice) and isinstance(fields['chain'], wx.ListBox)
                      and fields['role'].GetName() == 'Effect chain target'
                      and fields['chain'].GetName() == 'Effects in processing order')
                check('preset actions are named and require explicit apply',
                      not buttons['apply_preset'].IsEnabled()
                      and fields['preset_name'].GetName() == 'Effect preset name'
                      and fields['preset_preview'].GetName() == 'Loaded effect preset preview')
                choose(fields['role'], 1, wx.EVT_CHOICE)
                fields['role'].SetFocus()
                state['phase'] = 'focus'
            elif phase == 'focus':
                if wx.Window.FindFocus() is not fields['role']:
                    schedule()
                    return
                check('effect role receives actual native keyboard focus', True)
                check('selected Unicode role has two effects', fields['chain'].GetCount() == 2)
                check('first effect cannot move above the chain', not buttons['up'].IsEnabled())
                press(buttons['bypass'])
                check('native bypass retains the selected step', 'bypassed' in fields['chain'].GetString(0))
                press(buttons['down'])
                check('native reorder changes position and retains bypass',
                      fields['chain'].GetString(0).startswith('1. highpass')
                      and 'bypassed' in fields['chain'].GetString(1))
                state['phase'] = 'parameters'
                schedule()  # Must run in the nested modal event loop.
                press(buttons['edit'])
                return
            elif phase == 'parameters':
                children = nested()
                if not children:
                    schedule()
                    return
                if len(children) != 1 or children[0].GetName() != 'Parameters for gain on Ірина':
                    raise AssertionError('A different native parameter dialog opened.')
                child = children[0]
                pending, entries = list(child.GetChildren()), []
                while pending:
                    control = pending.pop()
                    if isinstance(control, wx.TextCtrl):
                        entries.append(control)
                    pending.extend(control.GetChildren())
                    if len(pending) + len(entries) > 128:
                        raise AssertionError('The parameter dialog exceeded its control budget.')
                check('editing shows the previously selected numeric value',
                      len(entries) == 1 and float(entries[0].GetValue()) == -6)
                entries[0].SetValue('-9')
                state['phase'] = 'edited'
                schedule()
                press(child.FindWindowById(wx.ID_OK, parent=child))
                return
            elif phase == 'edited':
                if nested():
                    schedule()
                    return
                check('native edit keeps bypass and changes only numeric settings',
                      'bypassed' in fields['chain'].GetString(1)
                      and 'gain db -9' in fields['chain'].GetString(1))
                state['phase'] = 'kind'
                schedule()
                press(buttons['add'])
                return
            elif phase == 'kind':
                children = nested()
                if not children:
                    schedule()
                    return
                if len(children) != 1 or not isinstance(children[0], wx.SingleChoiceDialog):
                    raise AssertionError('A different native effect-kind dialog opened.')
                child = children[0]
                from carr_library.audio.models import effect_controls
                child.SetSelection(tuple(effect_controls()).index('declick'))
                state['phase'] = 'added'
                schedule()
                press(child.FindWindowById(wx.ID_OK, parent=child))
                return
            elif phase == 'added':
                if nested():
                    schedule()
                    return
                check('native add inserts one selected typed effect', fields['chain'].GetCount() == 3
                      and fields['chain'].GetString(2).startswith('3. declick'))
                press(buttons['remove'])
                check('native removal affects only the selected step', fields['chain'].GetCount() == 2
                      and fields['chain'].GetString(0).startswith('1. highpass'))
                choose(fields['role'], len(editor.roles), wx.EVT_CHOICE)
                check('master has a distinct actual native target',
                      fields['role'].GetStringSelection() == 'Master: entire programme'
                      and fields['chain'].GetCount() == 2)
                check('dry wet control is named and enabled only for master',
                      isinstance(fields['master_mix'], wx.TextCtrl)
                      and fields['master_mix'].GetName() == 'Master dry wet mix from zero to one'
                      and fields['master_mix'].IsEnabled())
                fields['master_mix'].ChangeValue('nan')
                press(buttons['accept'])
                check('invalid master mix keeps the actual modal open',
                      dialog.IsModal() and 'finite' in state['status'].GetValue())
                fields['master_mix'].ChangeValue('0.5')
                press(buttons['bypass'])
                press(buttons['down'])
                check('native master chain has actual bypass and order controls',
                      fields['chain'].GetString(0).startswith('1. limiter')
                      and 'bypassed' in fields['chain'].GetString(1))
                state['phase'] = 'accepted'
                schedule()
                press(buttons['cancel' if cancel else 'accept'])
                return
            elif phase == 'accepted':
                # Returning from ShowModal, rather than a click alone, is the
                # success criterion. This turn is reached only on a refusal.
                raise AssertionError('The native chain confirmation did not close: ' + state['status'].GetValue())
            schedule()
        except BaseException as exc:
            state['error'] = exc
            state['active'] = False
            dismiss()

    def ready(dialog, fields, buttons, status):
        state.update(dialog=dialog, fields=fields, buttons=buttons, status=status)
        schedule()

    try:
        result = show_editor(frame, editor, private_root=private_root, on_ready=ready)
        state['active'] = False
        if state['error'] is not None:
            raise state['error']
        check('native chain confirmation settled', state['phase'] == 'accepted')
        check('native cancel discards pending chain result' if cancel else 'native apply returns a validated script',
              result is None if cancel else isinstance(result, str) and bool(result))
        return result
    finally:
        state['active'] = False
        for timer in timers:
            timer.Stop()


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.audio import models
    from carr_library.ui import audio_effects
    from carr_library.audio.effect_editing import RoleEffectEditor, load_preset, save_preset
    from carr_library.audio.production import parse_script
    from carr_library.reader.narration_text import NarrationDraft
    from carr_library.research.export_file import capture_export_destination
    from tempfile import TemporaryDirectory

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

    def selected_choice(dialog, expected):
        # wx.SingleChoiceDialog caches its public string result on acceptance.
        # Inspect the real list while modal; verify the returned role after OK.
        lists = [control for control in descendants(dialog) if isinstance(control, wx.ListBox)]
        check('one native choice list retains its exact Unicode inventory',
              len(lists) == 1 and tuple(lists[0].GetStrings()) == tuple(expected))
        return lists[0].GetStringSelection()

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
                    check('Unicode role selection is retained', selected_choice(dialog, roles) == roles[1])
                elif phase == 'effect':
                    check('named effect selector', isinstance(dialog, wx.SingleChoiceDialog)
                          and dialog.GetName() == 'Audio effect type')
                    dialog.SetSelection(tuple(definitions).index(kind))
                    check('selected typed effect kind', selected_choice(dialog, tuple(definitions)) == kind)
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
        source = ('[master mix=1]\r\n[master-effect gain gain_db=-6]\r\n'
                  '[master-effect limiter ceiling_db=-3]\r\n'
                  '[role Ірина]\r\n[effect Ірина gain gain_db=-6]\r\n'
                  '[effect Ірина highpass frequency=80]\r\nІрина: Точні слова.')
        with TemporaryDirectory(prefix='carr-native-effect-chain-') as folder:
            private = Path(folder) / 'library'
            private.mkdir()
            for cancel in (False, True):
                draft = NarrationDraft(source, source_id='synthetic-native-effects')
                editor = RoleEffectEditor(draft.text)
                result = drive_chain(wx, audio_effects.show_effect_editor, frame, editor,
                                     private_root=private, check=check, cancel=cancel)
                if result is not None:
                    draft.edit_text(result, reason='Native effect-chain acceptance')
                    plan = parse_script(result)
                    selected = next(role for role in plan.roles if role.name == 'Ірина')
                    check('accepted typed chain keeps order, settings and bypass',
                          selected.effects == (models.Effect('highpass', (('frequency', 80.0),)),
                              models.Effect('gain', (('gain_db', -9.0),), enabled=False)))
                    check('unselected native role remains unchanged', plan.roles[0].effects == ())
                    check('native master apply retains typed mix, order and bypass',
                          plan.schema_version == 3 and plan.master_mix == .5
                          and plan.master_effects == (models.Effect('limiter', (('ceiling_db', -3.0),)),
                              models.Effect('gain', (('gain_db', -6.0),), enabled=False)))
                    check('native chain retains exact spoken source',
                          plan.scenes == parse_script(source).scenes and draft.source_text == source)
                    path = Path(folder) / 'synthetic-preset.json'
                    preset = editor.preset('Ірина', 'Синтетичний голос')
                    approval = capture_export_destination(path, private_root=private)
                    save_preset(path, preset, private_root=private, approval=approval)
                    check('actual bounded preset file roundtrip preserves typed chain', load_preset(path) == preset)
                else:
                    check('native cancel leaves the source draft without edits', draft.text == source and not draft.edits)
        check('no native callback failed', not errors)
        print(json.dumps(dict(status='PASS', checks=len(checks), wx=wx.version(),
                              native_controls='PASS', typed_parameters='PASS', cancellation='PASS',
                              role_chain='PASS', master_chain='PASS', master_mix='PASS',
                              preset_file_roundtrip='PASS', preset_native_picker='NOT_RUN',
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
