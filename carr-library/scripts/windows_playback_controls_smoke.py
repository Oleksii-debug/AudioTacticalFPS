"""Installed native playback controls with real local FFmpeg preparation.

This probe does not start an output device. Existing native player acceptance
covers that owner separately; physical sound and screen-reader use are NOT_RUN.
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import sysconfig
import tempfile
from time import monotonic
import traceback
import wave


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows required.'}))
        return 77
    import wx
    from carr_library.audio import playback_processing
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.audio.player import WavePlayer
    from carr_library.audio.runtime import resolve_ffmpeg
    from carr_library.operations import ResourceBroker
    from carr_library.ui.playback_controls import PlaybackControlsDialog, PlaybackWorkspace

    checks, failures, timers = [], [], []
    state = {'window': None, 'phase': 'ready', 'active': True, 'done': False}
    deadline = monotonic() + 90
    gui = wx.App(False)
    frame = wx.Frame(None, title='Synthetic independent playback acceptance')
    frame.Show()
    gui.SetTopWindow(frame)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('Expected an enabled native playback button.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('Native playback action was not handled.')

    def schedule():
        timers.append(wx.CallLater(25, drive))

    with tempfile.TemporaryDirectory(prefix='carr-native-independent-playback-') as temporary:
        root = Path(temporary).resolve()
        library = root / 'library'
        library.mkdir()
        original = root / 'Україна — original.wav'
        with wave.open(str(original), 'wb') as stream:
            stream.setparams((1, 2, 8000, 8000, 'NONE', 'PCM'))
            stream.writeframes(b'\x20\x00' * 8000)
        before = sha256(original.read_bytes()).hexdigest()
        player = WavePlayer()
        player.set_playlist([original])
        ffmpeg = resolve_ffmpeg()
        check('the prepared reviewed local FFmpeg runtime is available', ffmpeg is not None)
        installed = Path(sysconfig.get_paths()['purelib']).resolve()
        check('the actual installed playback core is used',
              installed in Path(playback_processing.__file__).resolve().parents)
        workspace = PlaybackWorkspace(player, original, library_root=library, ffmpeg=ffmpeg)

        def drive():
            if not state['active']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native playback preparation exceeded its bounded acceptance deadline.')
                window, phase = state['window'], state['phase']
                if window is None or not window.dialog.IsModal():
                    schedule()
                    return
                if phase == 'ready':
                    check('owned native modal and Unicode source label',
                          window.dialog.GetParent() is frame
                          and window.dialog.GetName() == 'Independent playback controls for the selected recording'
                          and window.workspace.source.name == original.name)
                    check('six typed numeric controls have accessible names',
                          len(window.fields) == 6 and all(isinstance(field, wx.SpinCtrlDouble)
                          and field.GetName() for field in window.fields.values()))
                    check('all eight actions are real named native buttons',
                          len(window.buttons) == 8 and all(isinstance(button, wx.Button)
                          and button.GetName() for button in window.buttons.values()))
                    window.fields['speed'].SetValue(1.25)
                    window.fields['pitch'].SetValue(3)
                    window.fields['volume'].SetValue(.5)
                    window.fields['pan'].SetValue(-.25)
                    window.fields['bytes'].SetValue(8)
                    window.fields['seconds'].SetValue(20)
                    press(window.buttons['prepare'])
                    state['phase'] = 'prepared'
                elif phase == 'prepared':
                    if window.busy:
                        schedule()
                        return
                    selected = workspace.selection
                    if selected is None:
                        raise AssertionError('Actual preparation did not produce a checked derivative: ' +
                                             window.status.GetValue()[:1000])
                    pcm = selected.prepared
                    check('actual FFmpeg PCM has the exact independent speed frame mapping',
                          (wav_info(pcm.audio).frames, wav_info(pcm.audio).channels) == (6400, 2))
                    check('typed requested pitch volume and balance survived native controls',
                          (pcm.controls.pitch_semitones, pcm.controls.volume, pcm.controls.pan) == (3, .5, -.25))
                    check('prepared PCM is pinned without automatic playback',
                          pcm.ready() == pcm.audio and player.path is None
                          and player.status().state == 'stopped')
                    check('original-time endpoints remain inclusive with an honest qualification',
                          pcm.source_to_audio(1) == .8 and pcm.audio_to_source(.8) == 1
                          and pcm.source_time_accuracy == 'estimated_after_time_stretch')
                    check('prepared fields freeze until explicit New settings',
                          all(not field.IsEnabled() for field in window.fields.values()))
                    window.position.ChangeValue('.25')
                    press(window.buttons['seek'])
                    check('seeking before explicit Play is rejected through native status',
                          'Start the prepared recording' in window.status.GetValue())
                    state['pcm'] = pcm.audio
                    press(window.buttons['reset'])
                    state['phase'] = 'reset'
                elif phase == 'reset':
                    if window.busy:
                        schedule()
                        return
                    check('discard waits for owned Stop then releases private PCM',
                          workspace.selection is None and not state['pcm'].exists())
                    check('New settings re-enables all six typed fields',
                          all(field.IsEnabled() for field in window.fields.values()))
                    press(window.buttons['prepare'])
                    check('second preparation starts as an owned background operation', window.busy)
                    state['phase'] = 'closing'
                    press(window.buttons['close'])
                    check('Close waits for the actual background operation',
                          window.dialog.IsModal() and window.close_requested)
                    state['done'] = True
                    return
                schedule()
            except BaseException:
                failures.append(traceback.format_exc())
                state['active'] = False
                if state['window'] is not None:
                    state['window'].close()

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='native-playback-controls') as executor:
            window = PlaybackControlsDialog(frame, workspace, executor=executor)
            state['window'] = window
            try:
                window.show(on_ready=lambda _: schedule())
            finally:
                state['active'] = False
                for timer in timers:
                    timer.Stop()
        check('native Close settles file and worker owners before returning',
              window.closed and workspace.closed and workspace.selection is None and not window.busy)
        check('the exact original queue is restored without an automatic start',
              player.playlist == (original,) and player.path is None
              and player.status().state == 'stopped')
        check('the source recording is byte-identical',
              sha256(original.read_bytes()).hexdigest() == before)
        check('preparation released the shared resource reservation',
              ResourceBroker.for_app(library).snapshot()['leases'] == [])
        player.close()
    frame.Destroy()
    if failures:
        print('\n'.join(failures), flush=True)
    result = {'status': 'PASS' if state['done'] and not failures else 'FAIL',
              'checks': checks, 'actual_ffmpeg_pcm': 'EXECUTED', 'actual_wx_controls': 'EXECUTED',
              'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN', 'word_alignment': 'NOT_CLAIMED'}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
