"""Actual reader output selection through native keys and the existing MCI player.

The caller explicitly chooses an enumerated WinMM index for its qualification
machine. --expected-device-identity also pins that choice to the reviewed
inventory's stable identity before playback; CI supplies both values. Generated
PCM proves transport behavior, not human listening or NVDA.
Source mode requires the caller's source PYTHONPATH; --expect-installed uses -I.
"""
import argparse
from array import array
import faulthandler
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
import wave


def run(args, report):
    if os.name != 'nt':
        raise RuntimeError('Actual Windows MCI and native controls are required.')
    from carr_library.application import LibraryApp
    from carr_library.native_runtime import activate_bundled_runtime
    native = activate_bundled_runtime()
    import wx
    from carr_library.speech import output_devices, player as player_module
    from carr_library.speech.progress_speech import play_when_quiet
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.ui import reader_output, speech_dialog
    from carr_library.ui.operation_progress import OperationProgress

    checks, failures, timers, players, views = report['checks'], [], [], [], []
    active, deadline = {}, monotonic() + 90
    original_player, original_controls = speech_dialog.WavePlayer, speech_dialog.ReaderOutputControls
    hashes = {}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def abort():
        for window in reversed(wx.GetTopLevelWindows()):
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            abort()
            return False

    app = ProbeApp(False)
    event_loop = wx.GUIEventLoop()
    loop_activation = wx.EventLoopActivator(event_loop)
    simulator = wx.UIActionSimulator()
    frame = wx.Frame(None, title='Reader output routing acceptance')
    layout = wx.BoxSizer(wx.VERTICAL)
    frame.status = wx.TextCtrl(frame, style=wx.TE_READONLY)
    gauge, label = wx.Gauge(frame, range=100), wx.StaticText(frame, label='')
    layout.Add(frame.status, 0, wx.EXPAND)
    layout.Add(gauge, 0, wx.EXPAND)
    layout.Add(label, 0, wx.EXPAND)
    frame.SetSizer(layout)
    frame.report = lambda message: failures.append(str(message))
    frame._closing = False
    progress = None

    def guarded(callback):
        if failures:
            abort()
            return
        try:
            if monotonic() >= deadline:
                raise TimeoutError('Native output routing exceeded its deadline.')
            callback()
        except BaseException:
            failures.append(traceback.format_exc())
            abort()

    def later(callback, milliseconds=25):
        timer = wx.CallLater(milliseconds, lambda: guarded(callback))
        timers.append(timer)
        return timer

    def wait(predicate, callback, message, seconds=10):
        until = min(deadline, monotonic() + seconds)
        def tick():
            if predicate():
                callback()
            elif monotonic() >= until:
                raise TimeoutError(message)
            else:
                later(tick)
        later(tick)

    def children(parent):
        for child in parent.GetChildren():
            yield child
            yield from children(child)

    def named(name):
        return next(child for child in children(active['dialog']) if child.GetName() == name)

    def key(control, code, callback=lambda: None):
        control.SetFocus()
        def dispatch():
            check('actual key ' + str(code) + ' to ' + control.GetName(), simulator.Char(code))
            later(callback)
        wait(lambda: wx.Window.FindFocus() is control, dispatch, 'Native focus did not reach a control.')

    def choose(index, callback):
        choice = named('Reader output device')
        def step(remaining):
            if remaining:
                key(choice, wx.WXK_DOWN, lambda: step(remaining - 1))
            else:
                check('explicit native output selection', choice.GetSelection() == index)
                callback()
        key(choice, wx.WXK_HOME, lambda: step(index))

    def admit(action, callback):
        play_when_quiet((progress,), action, schedule=wx.CallLater,
            alive=lambda: bool(active.get('dialog')) and not failures,
            on_error=lambda error: (_ for _ in ()).throw(error))
        callback()

    def observe_player():
        value = original_player()
        players.append(value)
        return value

    def observe_controls(*values, **options):
        value = original_controls(*values, **options)
        views.append(value)
        return value

    def close_saved():
        def saved():
            draft = ReaderDraftStore(frame.library.root, slot='output-probe').load()
            check('routing retains the exact original and editable text',
                  draft is not None and draft.document.text == active['text']
                  and draft.document.source_text == active['text'])
            close = next(child for child in children(active['dialog'])
                         if isinstance(child, wx.Button) and child.GetId() == wx.ID_CANCEL)
            key(close, wx.WXK_SPACE)
        save = next(child for child in children(active['dialog'])
                    if isinstance(child, wx.Button)
                    and child.GetLabel().replace('&', '') == 'Save reading copy')
        key(save, wx.WXK_SPACE, lambda: wait(
            lambda: 'saved for recovery' in named('Speech and save status').GetValue(),
            saved, 'The actual reader did not save its disposable reading draft.'))

    def stopped():
        player = active['player']
        check('F8 stops selected routing and disarms policy',
              player.state == 'stopped' and not player.repeat_enabled
              and player.sleep_remaining_seconds is None)
        check('a separate player retains its independent selection',
              active['other_player'].output_device == active['selected'])
        close_saved()

    def final_playing():
        player = active['player']
        check('explicit resume opens the selected endpoint after the default round trip',
              player.active_output_index == active['selected'].index)
        def after_seek():
            wait(lambda: player.state == 'playing' and player.position_seconds >= 1.25,
                 lambda: key(active['editor'], wx.WXK_F8,
                     lambda: wait(lambda: player.state == 'stopped', stopped, 'F8 did not stop native output.')),
                 'Actual selected-route seek did not advance.')
        admit(lambda: player.seek(1.25), after_seek)

    def selected_again():
        player = active['player']
        check('applying output while paused never opens or starts a device',
              player.state == 'paused' and not player._open)
        key(active['editor'], wx.WXK_F7, lambda: wait(
            lambda: player.state == 'playing', final_playing, 'F7 did not resume selected output.'))

    def default_applied():
        player = active['player']
        check('System default is explicit and remains paused',
              player.output_device is None and player.state == 'paused' and not player._open)
        check('selection preserves the paused position without automatic output',
              abs(player.position_seconds - active['paused']) <= .002)
        check('only the current reader route changed',
              active['other_player'].output_device == active['selected'])
        choose(active['choice_index'], lambda: key(named('Use selected reader output device'), wx.WXK_SPACE,
            lambda: wait(lambda: player.output_device == active['selected'],
                         selected_again, 'The explicit route was not restored.')))

    def paused():
        player = active['player']
        active['paused'] = player.position_seconds
        check('actual F7 pauses the selected native endpoint',
              player.state == 'paused' and player.active_output_index == active['selected'].index)
        choose(0, lambda: key(named('Use selected reader output device'), wx.WXK_SPACE,
            lambda: wait(lambda: player.output_device is None and not views[-1]._pending,
                         default_applied, 'Explicit default output was not applied.')))

    def playing():
        player = active['player']
        check('MCI reports the actual selected numeric output',
              player.active_output_index == active['selected'].index)
        check('reader remains registered with shared progress', player in progress._players)
        key(active['editor'], wx.WXK_F7, lambda: wait(
            lambda: player.state == 'paused', paused, 'F7 did not pause native playback.'))

    def applied():
        player = active['player']
        check('explicit device Apply starts no output',
              not player._open and player.state == 'stopped')
        check('native status names the selected session output',
              active['selected'].name in named('Reader output device status').GetValue())
        admit(lambda: player.play(active['source']), lambda: wait(
            lambda: player.state == 'playing' and player.position_seconds > .1,
            playing, 'Actual selected MCI endpoint did not play PCM.'))

    def refreshed():
        values = views[-1]._devices
        matches = [(index + 1, value) for index, value in enumerate(values)
                   if value.interface == active['selected'].interface]
        check('refreshed identity matches exactly one endpoint', len(matches) == 1)
        active['choice_index'], active['selected'] = matches[0]
        check('refresh did not silently select a physical endpoint',
              named('Reader output device').GetSelection() == 0
              and active['player'].output_device is None and not active['player']._open)
        choose(active['choice_index'], lambda: key(named('Use selected reader output device'), wx.WXK_SPACE,
            lambda: wait(lambda: active['player'].output_device == active['selected'],
                         applied, 'Native Apply did not retain the exact device identity.')))

    def ready():
        window = next((value for value in wx.GetTopLevelWindows()
                       if isinstance(value, wx.Dialog) and value.IsModal()
                       and value.GetTitle() == 'Voice reader and reading copy'), None)
        if window is None or not players or not views:
            later(ready)
            return
        active['dialog'], active['player'] = window, players[-1]
        active['editor'] = named('Reading copy (editable; Ctrl+S saves)')
        check('real Windows reader output controls exist',
              isinstance(named('Reader output device'), wx.Choice)
              and isinstance(named('Use selected reader output device'), wx.Button))
        check('opening the reader does not select an explicit output',
              active['player'].output_device is None and not active['player']._open)
        wait(named('Refresh reader output devices').IsEnabled,
             lambda: key(named('Refresh reader output devices'), wx.WXK_SPACE,
                lambda: wait(lambda: not views[-1]._pending and len(views[-1]._devices) > 0,
                             refreshed, 'Read-only native output discovery did not complete.')),
             'Initial voice discovery did not leave the shared reader worker ready.', seconds=45)

    try:
        checkout = Path(__file__).resolve().parents[1]
        for module in (output_devices, player_module, reader_output, speech_dialog):
            actual = Path(module.__file__).resolve()
            expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
            check('exact module origin: ' + module.__name__,
                  (not actual.is_relative_to(checkout) if args.expect_installed else actual == expected)
                  and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())
            hashes[module.__name__] = sha256(actual.read_bytes()).hexdigest()
        check('actual Windows widgets', wx.Platform == '__WXMSW__')
        outputs = output_devices.output_devices()
        matches = [value for value in outputs if value.index == args.device_index]
        check('caller explicitly selected an available WinMM output index', len(matches) == 1)
        selected = matches[0]
        if args.expected_device_identity is not None:
            check('selected output matches the reviewed stable endpoint identity before playback',
                  selected.identity == args.expected_device_identity)
        report['device'] = {'index': selected.index, 'name': selected.name, 'identity': selected.identity}
        with TemporaryDirectory(prefix='resonance-reader-output-') as temporary:
            root = Path(temporary)
            library = root / 'library'
            library.mkdir()
            frame.library = LibraryApp(library)
            progress = OperationProgress(frame, gauge, label)
            frame.operation_progress = progress
            source = root / 'synthetic PCM.wav'
            samples = array('h', (round(600 * math.sin(2 * math.pi * 220 * index / 16000))
                                 for index in range(64000)))
            if sys.byteorder != 'little':
                samples.byteswap()
            with wave.open(str(source), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                audio.writeframes(samples.tobytes())
            source_hash = sha256(source.read_bytes()).hexdigest()
            other = player_module.WavePlayer()
            other.set_output_device(selected)
            active.update(source=source, selected=selected, other_player=other,
                          text='Disposable reader routing fixture. Original quotations remain unchanged.')
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            speech_dialog.WavePlayer = observe_player
            speech_dialog.ReaderOutputControls = observe_controls
            later(ready)
            speech_dialog.show_speech_reader(frame, text=active['text'], recovery_slot='output-probe')
            if failures:
                raise AssertionError('\n'.join(failures))
            check('reader close releases the selected MCI endpoint',
                  not players[-1]._open and players[-1].state == 'stopped')
            check('output controller is closed before reader destruction', views[-1]._closed)
            check('reader close unregisters its player', players[-1] not in progress._players)
            check('source PCM is byte-identical after all routes', sha256(source.read_bytes()).hexdigest() == source_hash)
            other.close()
            report.update(status='PASS', actual_native_keys=True, actual_selected_mci_output=True,
                          source_hashes=hashes, fixture_pcm_sha256=source_hash)
    finally:
        speech_dialog.WavePlayer, speech_dialog.ReaderOutputControls = original_player, original_controls
        for timer in timers:
            timer.Stop()
        for value in players + ([active['other_player']] if 'other_player' in active else []):
            value.close()
        if progress is not None:
            progress.close()
        frame.Destroy()
        app.Yield()
        if native is not None:
            native.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device-index', type=int, required=True)
    parser.add_argument('--expected-device-identity',
                        help='Stable endpoint identity from the reviewed output inventory.')
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = {'status': 'FAILED', 'checks': [], 'actual_native_keys': False,
              'actual_selected_mci_output': False, 'human_listening': 'NOT_RUN', 'nvda': 'NOT_RUN',
              'multiple_physical_output_devices': 'NOT_RUN', 'independent_deadline_seconds': 110}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    faulthandler.dump_traceback_later(110, exit=True)
    try:
        run(args, report)
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    main()
