"""Real Windows wx clip parameters; processing proof lives in strict PCM tests."""
import json
import sys
from time import monotonic
import traceback


def drive_editor(wx, show_editor, frame, plan, action, record):
    """Drive one owned modal from separate native event turns, never nested Yield."""
    state = {'active': True, 'phase': 'waiting-for-modal', 'ready': None, 'submitted': False}
    timer = [None]
    errors = []
    deadline = monotonic() + 10

    def ready(dialog, controls, buttons, status):
        if state['active']:
            state['ready'] = (dialog, controls, buttons, status)

    def schedule():
        if state['active']:
            timer[0] = wx.CallLater(25, poll)

    def press(button):
        assert isinstance(button, wx.Button) and button.IsEnabled(), 'Expected an enabled native button.'
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event), 'The native button event was not handled.'

    def poll():
        if not state['active']:
            return
        dialog = state['ready'][0] if state['ready'] is not None else None
        try:
            if monotonic() >= deadline:
                raise TimeoutError(f'Native clip controls timed out: {action}/{state["phase"]}.')
            if dialog is None or not dialog.IsModal() or not dialog.IsShown():
                schedule()
                return
            assert dialog.GetParent() is frame, 'The clip modal has another owner.'
            _, controls, buttons, status = state['ready']
            if state['phase'] == 'waiting-for-modal':
                assert controls['scene'].GetName() == 'Chapter'
                assert controls['track'].GetName() == 'Track role'
                assert controls['clip'].GetSelection() == 0
                assert buttons['edit'].IsEnabled()
                record(f'{action}: named controls in shown owned modal')
                controls['title'].SetValue('Мікрофон 🙂')
                if action == 'edit':
                    controls['duration'].SetValue('0')
                    press(buttons['edit'])
                    state['phase'] = 'invalid-duration'
                    schedule()
                    return
                if action == 'move':
                    controls['destination'].SetSelection(1)
                elif action == 'duration':
                    controls['minimum'].SetValue('2.5')
                elif action == 'paste':
                    controls['start'].SetValue('1.25')
                elif action == 'split':
                    controls['split_at'].SetValue('.25')
                elif action in ('join', 'consolidate'):
                    controls['join'].SetSelection(1)
            elif state['phase'] == 'invalid-duration':
                assert dialog.IsShown() and 'duration' in status.GetValue().lower()
                record('invalid duration retains owned modal')
                controls['duration'].SetValue('.3')
                controls['gain_db'].SetValue('-6')
                controls['pan'].SetValue('1')
                controls['fade_in'].SetValue('.05')
                controls['fade_out'].SetValue('.05')
                controls['track'].SetSelection(3)
                controls['gain_db'].SetFocus()
                state['phase'] = 'numeric-focus'
                schedule()
                return
            elif state['phase'] == 'numeric-focus':
                assert wx.Window.FindFocus() is controls['gain_db'], 'Numeric edit did not receive native focus.'
                record('numeric field retains native focus on the next event turn')
            else:
                raise AssertionError('Unexpected clip probe phase.')
            state['phase'] = 'submit-' + action
            button = dialog.FindWindowById(wx.ID_CANCEL, dialog) if action == 'cancel' else buttons[action]
            press(button)
            # On MSW EndModal hides synchronously, while IsModal remains true
            # until ShowModal unwinds. Observe visibility and the returned result.
            assert not dialog.IsShown(), f'{action} was not accepted: {status.GetValue()[:512]}'
            state['submitted'] = True
            record('native ' + action)
        except BaseException:
            errors.append(traceback.format_exc())
            state['active'] = False
            if dialog is not None and dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

    schedule()
    try:
        result = show_editor(frame, plan, selected_id='clip-one',
                             clipboard_available=action == 'paste', on_ready=ready)
        if errors:
            raise AssertionError(errors[-1])
        assert state['submitted'], 'The clip modal returned before the native action was submitted.'
        if action == 'cancel':
            assert result is None
        else:
            assert result['action'] == action
            if action == 'edit':
                assert result['clip_id'] == 'clip-one'
                assert result['values']['track'] == 'microphone'
                assert result['values']['title'] == 'Мікрофон 🙂'
                assert result['values']['gain_db'] == -6 and result['values']['pan'] == 1
                assert result['values']['duration'] == .3
                assert result['values']['fade_in'] == result['values']['fade_out'] == .05
            elif action == 'move':
                assert result['scene_id'] == 'scene-000002'
            elif action == 'duration':
                assert result['minimum_duration'] == 2.5
            elif action in ('copy', 'cut'):
                assert result['clip_id'] == 'clip-one'
            elif action == 'paste':
                assert result['position'] == 1.25 and 'clipboard' not in result
            elif action == 'split':
                assert result['split_at'] == .25 and result['clip_id'] == 'clip-one'
            elif action in ('join', 'consolidate'):
                assert result['other_id'] == 'clip-two' and result['clip_id'] == 'clip-one'
        assert plan.scenes[0].clips[0].title == 'Голос'
        return result
    finally:
        state['active'] = False
        if timer[0] is not None:
            timer[0].Stop()
        state['ready'] = None


def main():
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows wx is required.'}))
        return 77
    import wx
    from carr_library.audio.production import parse_script
    from carr_library.ui.audio_clips import show_clip_editor

    application = wx.App(False)
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    frame = wx.Frame(None, title='Resonance native clip controls acceptance')
    application.SetTopWindow(frame)
    frame.Show()
    plan = parse_script('# First\n[clip retained.wav id=clip-one duration=.5 title="Голос"]\n'
                        '[clip retained.wav id=clip-two trim_start=.5 duration=.5 start=.5]\n'
                        '# Second\n[duration 1]')
    checks = []

    def record(label):
        checks.append(label)
        print('PASS:', label, flush=True)

    try:
        for action in ('edit', 'move', 'duration', 'remove', 'preview',
                       'copy', 'cut', 'paste', 'split', 'join', 'consolidate', 'cancel'):
            drive_editor(wx, show_clip_editor, frame, plan, action, record)
        print(json.dumps({'status': 'PASS', 'checks': checks,
                          'actual_pcm_processing': 'SEPARATE_STRICT_SUITE',
                          'native_file_picker': 'NOT_RUN', 'manual_nvda': 'NOT_RUN',
                          'playback_device_acceptance': 'NOT_RUN'}), flush=True)
        return 0
    finally:
        frame.Destroy()
        application.ProcessPendingEvents()
        native_loop.ProcessIdle()
        application.Yield()


if __name__ == '__main__':
    raise SystemExit(main())
