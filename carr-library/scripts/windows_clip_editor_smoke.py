"""Real Windows wx clip parameters; processing proof lives in strict PCM tests."""
import json
import sys
import traceback


def main():
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows wx is required.'}))
        return 77
    import wx
    from carr_library.audio.production import parse_script
    from carr_library.ui.audio_clips import show_clip_editor

    application = wx.App(False)
    frame = wx.Frame(None, title='Resonance native clip controls acceptance')
    frame.Show()
    plan = parse_script('# First\n[clip retained.wav id=clip-one duration=.5 title="Голос"]\n'
                        '# Second\n[duration 1]')
    checks, errors = [], []

    def run(action):
        timers = []
        def ready(dialog, controls, buttons, status):
            def stop():
                errors.append('Native clip controls watchdog expired.')
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)
            timers.append(wx.CallLater(10000, stop))

            def click(button):
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                event.SetEventObject(button)
                button.GetEventHandler().ProcessEvent(event)

            try:
                assert controls['scene'].GetName() == 'Chapter'
                assert controls['track'].GetName() == 'Track role'
                assert controls['clip'].GetSelection() == 0
                assert buttons['edit'].IsEnabled()
                controls['title'].SetValue('Мікрофон 🙂')
                if action == 'cancel':
                    click(dialog.FindWindowById(wx.ID_CANCEL))
                    checks.append('native_cancel_without_draft_edit')
                    return
                if action == 'edit':
                    controls['duration'].SetValue('0')
                    click(buttons['edit'])
                    assert dialog.IsModal() and 'duration' in status.GetValue().lower()
                    checks.append('invalid_duration_keeps_owned_modal')
                    controls['duration'].SetValue('.3')
                    controls['gain_db'].SetValue('-6')
                    controls['pan'].SetValue('1')
                    controls['fade_in'].SetValue('.05')
                    controls['fade_out'].SetValue('.05')
                    controls['track'].SetSelection(3)
                    controls['gain_db'].SetFocus()
                    wx.YieldIfNeeded()
                    assert wx.Window.FindFocus() is controls['gain_db']
                elif action == 'move':
                    controls['destination'].SetSelection(1)
                elif action == 'duration':
                    controls['minimum'].SetValue('2.5')
                click(buttons[action])
                checks.append('native_' + action)
            except BaseException:
                errors.append(traceback.format_exc())
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)
        try:
            result = show_clip_editor(frame, plan, selected_id='clip-one', on_ready=ready)
        finally:
            for timer in timers:
                timer.Stop()
        if errors:
            raise AssertionError('\n'.join(errors))
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
            elif action == 'move':
                assert result['scene_id'] == 'scene-000002'
            elif action == 'duration':
                assert result['minimum_duration'] == 2.5
        assert plan.scenes[0].clips[0].title == 'Голос'

    try:
        for action in ('edit', 'move', 'duration', 'remove', 'preview', 'cancel'):
            run(action)
        print(json.dumps({'status': 'PASS', 'checks': checks,
                          'actual_pcm_processing': 'SEPARATE_STRICT_SUITE',
                          'native_file_picker': 'NOT_RUN', 'manual_nvda': 'NOT_RUN',
                          'playback_device_acceptance': 'NOT_RUN'}))
        return 0
    finally:
        frame.Destroy()
        application.ProcessPendingEvents()
        application.ProcessIdle()


if __name__ == '__main__':
    raise SystemExit(main())
