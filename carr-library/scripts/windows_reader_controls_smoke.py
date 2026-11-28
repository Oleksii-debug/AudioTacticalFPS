"""Installed reader controls with real Windows MCI and native keyboard events.

The PCM fixture is synthetic and independently generated here. This checks
transport, focus and lifecycle behavior; it does not qualify a voice model,
human listening, an output-device matrix or NVDA. Run with python -I -B.
"""
import argparse
from array import array
import faulthandler
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import traceback
import wave


def _wave_output_diagnostics():
    """Query actual WinMM endpoints and the unchanged fixture format; no play."""
    import ctypes
    from ctypes import wintypes

    class WaveFormat(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('format_tag', ctypes.c_uint16), ('channels', ctypes.c_uint16),
                    ('samples_per_second', ctypes.c_uint32), ('bytes_per_second', ctypes.c_uint32),
                    ('block_align', ctypes.c_uint16), ('bits_per_sample', ctypes.c_uint16),
                    ('extra_size', ctypes.c_uint16)]

    class WaveOutputCaps(ctypes.Structure):
        _fields_ = [('manufacturer', ctypes.c_uint16), ('product', ctypes.c_uint16),
                    ('driver_version', ctypes.c_uint32), ('name', wintypes.WCHAR * 32),
                    ('formats', ctypes.c_uint32), ('channels', ctypes.c_uint16),
                    ('reserved', ctypes.c_uint16), ('support', ctypes.c_uint32)]

    native = ctypes.WinDLL('winmm', use_last_error=True)
    native.waveOutGetNumDevs.argtypes = ()
    native.waveOutGetNumDevs.restype = ctypes.c_uint
    native.waveOutGetDevCapsW.argtypes = (ctypes.c_size_t, ctypes.POINTER(WaveOutputCaps), ctypes.c_uint)
    native.waveOutGetDevCapsW.restype = ctypes.c_uint
    native.waveOutOpen.argtypes = (ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                                  ctypes.POINTER(WaveFormat), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint)
    native.waveOutOpen.restype = ctypes.c_uint
    native.waveOutGetErrorTextW.argtypes = (ctypes.c_uint, wintypes.LPWSTR, ctypes.c_uint)
    native.waveOutGetErrorTextW.restype = ctypes.c_uint
    native.mciGetErrorStringW.argtypes = (ctypes.c_uint, wintypes.LPWSTR, ctypes.c_uint)
    native.mciGetErrorStringW.restype = wintypes.BOOL
    count = native.waveOutGetNumDevs()
    devices = []
    for index in range(min(count, 64)):
        caps = WaveOutputCaps()
        result = native.waveOutGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
        devices.append({'index': index, 'caps_result': result, 'name': caps.name,
                        'channels': caps.channels, 'format_mask': caps.formats})
    # WAVE_FORMAT_PCM, 16 kHz, mono, 16 bits: exactly the fixture below.
    waveform = WaveFormat(1, 1, 16000, 32000, 2, 16, 0)
    result = native.waveOutOpen(None, 0xFFFFFFFF, ctypes.byref(waveform), 0, 0, 1)
    # WAVE_MAPPER + WAVE_FORMAT_QUERY does not open or play a device.
    message = ctypes.create_unicode_buffer(256)
    native.waveOutGetErrorTextW(result, message, len(message))
    mci_message = ctypes.create_unicode_buffer(256)
    native.mciGetErrorStringW(326, mci_message, len(mci_message))
    return {'device_count': count, 'devices': devices, 'inventory_truncated': count > len(devices),
            'fixture': {'sample_rate': 16000, 'channels': 1, 'bits_per_sample': 16},
            'format_query_result': result, 'format_query_message': message.value,
            'mci_326_message': mci_message.value,
            'scope': 'Read-only endpoint and format query; actual MCI playback remains required.'}


def _run_probe(args, report):
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.speech import player as player_module
    from carr_library.speech.progress_speech import play_when_quiet
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.ui import reader_playback, speech_dialog
    from carr_library.ui.operation_progress import OperationProgress

    checks = report['checks']
    failures, players, views, timers, hashes = [], [], [], [], {}
    deadline = monotonic() + report['independent_deadline_seconds']
    active = {}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def abort():
        # End an entry chooser before the reader, without accepting its input.
        for window in reversed(wx.GetTopLevelWindows()):
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            abort()
            return False

    gui = ProbeApp(False)
    event_loop = wx.GUIEventLoop()
    event_loop_activation = wx.EventLoopActivator(event_loop)
    simulator = wx.UIActionSimulator()
    frame = wx.Frame(None, title='Synthetic reader controls acceptance')
    box = wx.BoxSizer(wx.VERTICAL)
    frame.status = wx.TextCtrl(frame, style=wx.TE_READONLY)
    gauge, label = wx.Gauge(frame, range=100), wx.StaticText(frame, label='')
    box.Add(frame.status, 0, wx.EXPAND)
    box.Add(gauge, 0, wx.EXPAND)
    box.Add(label, 0, wx.EXPAND)
    frame.SetSizer(box)
    frame.report = lambda message: failures.append(str(message))
    frame._closing = False
    actual_player = speech_dialog.WavePlayer
    actual_controls = speech_dialog.ReaderPlaybackControls

    def observe_player():
        # Instrument construction only: every returned transport is the actual
        # installed WavePlayer using WinMM, not a model or replacement backend.
        value = actual_player()
        players.append(value)
        return value

    def observe_controls(*args, **kwargs):
        value = actual_controls(*args, **kwargs)
        views.append(value)
        return value

    def guarded(callback):
        if failures:
            abort()
            return
        try:
            if monotonic() > deadline:
                raise TimeoutError('Native reader controls exceeded the probe deadline.')
            callback()
        except BaseException:
            failures.append(traceback.format_exc())
            abort()

    def later(callback, milliseconds=25):
        timer = wx.CallLater(milliseconds, lambda: guarded(callback))
        timers.append(timer)
        return timer

    def wait(predicate, done, name, seconds=8):
        until = min(deadline, monotonic() + seconds)
        def tick():
            if predicate():
                done()
            elif monotonic() >= until:
                raise TimeoutError(name)
            else:
                later(tick)
        later(tick)

    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)

    def named(name):
        return next(child for child in children(active['dialog'])
                    if child.GetName() == name)

    def key(control, code, done=lambda: None):
        control.SetFocus()
        def send():
            check('OS key dispatch ' + str(code), simulator.Char(code))
            later(done)
        wait(lambda: wx.Window.FindFocus() is control, send, 'Native focus did not reach the control.')

    def entry(title, value, done):
        def chooser():
            return next((window for window in wx.GetTopLevelWindows()
                         if isinstance(window, wx.TextEntryDialog)
                         and window.IsModal() and window.GetTitle() == title), None)
        def fill():
            field = next(child for child in children(chooser()) if isinstance(child, wx.TextCtrl))
            field.ChangeValue(value)
            key(field, wx.WXK_RETURN, done)
        wait(chooser, fill, 'Native entry chooser did not open.')

    def set_points(done):
        later(lambda: entry('Set A/B playback times', '0.15, 0.85', lambda: None))
        key(named('Set A/B times...'), wx.WXK_SPACE,
            lambda: wait(lambda: active['player'].ab_loop == (.15, .85), done,
                         'A/B points were not accepted.'))

    def enable_repeat(done):
        check('A/B points alone do not enable repeat', not active['player'].repeat_enabled)
        key(named('Enable A/B repeat'), wx.WXK_SPACE,
            lambda: wait(lambda: active['player'].repeat_enabled, done, 'Explicit repeat was not enabled.'))

    def close_reader():
        check('playback leaves the reading copy unchanged',
              active['editor'].GetValue() == active['original_text'])
        # Fresh input is an unsaved reading copy. Use the real Save workflow
        # before Close, instead of leaving its native unsaved-copy prompt open.
        def saved():
            check('native Save marks the reading copy saved',
                  '; saved.' in named('Original and reading-copy status').GetValue())
            recovered = ReaderDraftStore(active['original_library'].root,
                                         slot='native-controls').load()
            check('native Save persists the unchanged reading copy',
                  recovered is not None and recovered.document.text == active['original_text']
                  and recovered.document.source_text == active['original_text'])
            button = next(child for child in children(active['dialog'])
                          if isinstance(child, wx.Button) and child.GetId() == wx.ID_CANCEL)
            key(button, wx.WXK_SPACE)
        button = next(child for child in children(active['dialog'])
                      if isinstance(child, wx.Button)
                      and child.GetLabel().replace('&', '') == 'Save reading copy')
        key(button, wx.WXK_SPACE,
            lambda: wait(lambda: 'saved for recovery' in named('Speech and save status').GetValue(),
                         saved, 'Native Save did not persist the reading copy.'))

    def arm_timer(minutes, done):
        later(lambda: entry('Reader sleep timer', str(minutes), lambda: None))
        key(named('Sleep timer...'), wx.WXK_SPACE,
            lambda: wait(lambda: active['player'].sleep_remaining_seconds is not None, done,
                         'Explicit sleep timer did not start.'))

    def after_repeats():
        player = active['player']
        check('native B endpoint bounds repeated position', .149 <= player.position_seconds <= .852)
        check('automatic repeat preserves editor focus', wx.Window.FindFocus() is active['editor'])
        check('one player stays registered with shared progress', player in frame.operation_progress._players)
        if active['mode'] == 'close':
            arm_timer(1, close_reader)
        elif active['mode'] == 'scope':
            previous = player.session_generation
            play_when_quiet((frame.operation_progress,), lambda: player.play(active['source']),
                schedule=wx.CallLater, alive=lambda: bool(active['dialog']),
                on_error=lambda error: (_ for _ in ()).throw(error))
            def new_audio_started():
                check('new explicit audio clears old controls', not player.repeat_enabled
                      and player.ab_loop is None and player.sleep_remaining_seconds is None)
                later(lambda: set_points(lambda: enable_repeat(lambda: arm_timer(1, change_scope))), 150)
            wait(lambda: player.session_generation > previous and player.state == 'playing',
                 new_audio_started, 'Explicit new audio did not start.')
        else:
            # Deliberately defer only the control poll. Native MCI must still
            # stop at B, and the real F7 handler must honor Pause at that edge.
            views[-1].timer.Stop()
            wait(lambda: player.state == 'stopped',
                 lambda: key(active['editor'], wx.WXK_F7,
                     lambda: wait(lambda: player.state == 'paused', paused,
                                  'F7 did not pause the completed native A/B interval.')),
                 'The native B endpoint did not stop without a GUI poll.')

    def paused():
        player = active['player']
        check('native pause at completed B keeps repeat armed without playback',
              player.repeat_enabled and player.state == 'paused'
              and abs(player.position_seconds - .15) <= .002)
        views[-1].timer.Start(reader_playback.ReaderPlaybackControls.POLL_MS)
        arm_timer(.02, timer_started)

    def timer_started():
        active['timer_started'] = monotonic()
        active['paused_position'] = active['player'].position_seconds
        wait(lambda: active['player'].sleep_remaining_seconds is None, timer_expired,
             'Pause-inclusive sleep timer did not expire.', seconds=6)

    def timer_expired():
        player = active['player']
        check('sleep expired while paused without rearming', player.state == 'paused'
              and not player.repeat_enabled and player.ab_loop is None
              and monotonic() - active['timer_started'] >= .8)
        report['observed_paused_sleep_seconds'] = monotonic() - active['timer_started']
        check('sleep preserves paused playback position',
              abs(player.position_seconds - active['paused_position']) <= .002)
        check('automatic sleep status preserves editor focus',
              wx.Window.FindFocus() is not named('A/B repeat and sleep timer status'))
        key(active['editor'], wx.WXK_F7,
            lambda: wait(lambda: player.state == 'playing' and player.position_seconds > 1.0,
                         resumed, 'F7 did not resume beyond the former B endpoint.'))

    def resumed():
        player = active['player']
        check('explicit resume does not rearm repeat or sleep',
              not player.repeat_enabled and player.sleep_remaining_seconds is None)
        key(active['editor'], wx.WXK_F8,
            lambda: wait(lambda: player.state == 'stopped', stopped, 'F8 did not stop actual audio.'))

    def stopped():
        player = active['player']
        check('F8 clears repeat and timer', player.ab_loop is None and not player.repeat_enabled
              and player.sleep_remaining_seconds is None)
        generation = player.session_generation
        def no_restart():
            check('cancelled playback cannot restart from a late poll',
                  player.state == 'stopped' and player.session_generation == generation)
            close_reader()
        later(no_restart, 700)

    def change_scope():
        frame.library = SimpleNamespace(root=active['other_root'])
        wait(lambda: active['player'].state == 'stopped', scope_stopped,
             'Changed library did not stop the old recording.')

    def scope_stopped():
        player = active['player']
        check('library change disarms both controls', not player.repeat_enabled
              and player.ab_loop is None and player.sleep_remaining_seconds is None)
        check('scope failure is visible without focus theft',
              'open this reader again' in named('Speech and save status').GetValue().lower())
        frame.library = active['original_library']
        close_reader()

    def begin_playback():
        player = active['player']
        check('actual installed Windows MCI transport', isinstance(player, player_module.WavePlayer)
              and player.backend == 'Windows MCI' and not player.position_is_approximate)
        play_when_quiet((frame.operation_progress,), lambda: player.play(active['source']),
            schedule=wx.CallLater, alive=lambda: bool(active['dialog']),
            on_error=lambda error: (_ for _ in ()).throw(error))
        wait(lambda: player.state == 'playing' and named('Set A/B times...').IsEnabled(),
             controls_ready, 'Synthetic PCM did not reach the actual native device.')

    def controls_ready():
        def traversed():
            check('native Tab reaches next named control',
                  wx.Window.FindFocus() is named('Mark B at current position'))
            set_points(lambda: enable_repeat(looping))
        key(named('Mark A at current position'), wx.WXK_TAB, traversed)

    def looping():
        active['editor'].SetFocus()
        def bounded():
            player = active['player']
            if player.repeat_enabled:
                check_position = player.position_seconds
                if not .149 <= check_position <= .852:
                    raise AssertionError('Actual MCI position escaped the A/B interval.')
            return player.repeat_count >= 2
        wait(bounded, after_repeats, 'Actual native playback did not repeat twice.')

    def ready(before):
        def window():
            return next((item for item in wx.GetTopLevelWindows()
                         if isinstance(item, wx.Dialog) and item.IsModal()
                         and item.GetTitle() == 'Voice reader and reading copy'), None)
        def found():
            active['dialog'], active['player'] = window(), players[-1]
            active['editor'] = named('Reading copy (editable; Ctrl+S saves)')
            for name in ('Mark A at current position', 'Mark B at current position', 'Set A/B times...',
                         'Enable A/B repeat', 'Clear A/B', 'Sleep timer...',
                         'Repeat and sleep status', 'A/B repeat and sleep timer status'):
                check('native named reader control: ' + name, bool(named(name)))
            reload_button = next(child for child in children(active['dialog'])
                                 if isinstance(child, wx.Button)
                                 and child.GetLabel().replace('&', '') == 'Reload voices')
            wait(reload_button.IsEnabled, begin_playback, 'Initial local discovery did not finish.', seconds=45)
        wait(lambda: len(players) > before and window(), found, 'Installed reader did not open.')

    progress = None
    try:
        checkout = Path(__file__).resolve().parents[1]
        for module in (player_module, reader_playback, speech_dialog):
            installed = Path(module.__file__).resolve()
            expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
            check('installed exact source: ' + module.__name__, not installed.is_relative_to(checkout)
                  and sha256(installed.read_bytes()).digest() == sha256(expected.read_bytes()).digest())
            hashes[module.__name__] = sha256(installed.read_bytes()).hexdigest()
        check('real Windows native widgets', wx.Platform == '__WXMSW__')
        report['wave_output'] = _wave_output_diagnostics()
        print('Actual WinMM capability: ' + json.dumps(report['wave_output'], ensure_ascii=False), flush=True)
        with TemporaryDirectory(prefix='resonance-reader-controls-') as temporary:
            root = Path(temporary)
            library_root, other_root = root / 'library', root / 'other'
            library_root.mkdir()
            other_root.mkdir()
            frame.library = SimpleNamespace(root=library_root)
            original_library = frame.library
            progress = OperationProgress(frame, gauge, label)
            frame.operation_progress = progress
            source = root / 'synthetic tone.wav'
            samples = array('h', (round(800 * math.sin(2 * math.pi * 220 * index / 16000))
                                  for index in range(64000)))
            with wave.open(str(source), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                audio.writeframes(samples.tobytes())
            source_hash = sha256(source.read_bytes()).hexdigest()
            original_text = 'Synthetic reading copy. Український текст. No inference is requested.'
            gui.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            speech_dialog.WavePlayer = observe_player
            speech_dialog.ReaderPlaybackControls = observe_controls
            for mode in ('sleep', 'scope', 'close'):
                active.clear()
                active.update(mode=mode, source=source, other_root=other_root,
                              original_library=original_library, original_text=original_text)
                before = len(players)
                later(lambda before=before: ready(before))
                speech_dialog.show_speech_reader(frame, text=original_text, recovery_slot='native-controls')
                if failures:
                    raise AssertionError('\n'.join(failures))
                player = players[-1]
                check(mode + ': close releases the actual device', not player._open
                      and player.state == 'stopped' and not player.repeat_enabled
                      and player.sleep_remaining_seconds is None)
                check(mode + ': native polling timer is closed', views[-1]._closed and not views[-1].timer.IsRunning())
                check(mode + ': player is removed from shared progress', player not in progress._players)
                check(mode + ': source PCM remains byte-identical', sha256(source.read_bytes()).hexdigest() == source_hash)
            report.update(status='PASS', actual_winmm=True, actual_native_keys=True,
                          source_hashes=hashes, fixture_pcm_sha256=source_hash)
    except BaseException:
        report['failure'] = traceback.format_exc()
    finally:
        speech_dialog.WavePlayer = actual_player
        speech_dialog.ReaderPlaybackControls = actual_controls
        for timer in timers:
            timer.Stop()
        for player in players:
            try:
                player.close()
            except Exception as error:
                report['status'] = 'FAILED'
                report['cleanup_failure'] = str(error)
        if progress is not None:
            progress.close()
        frame.Destroy()
        gui.Yield()
        if native_handle is not None:
            native_handle.close()
    report['count'] = len(checks)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report is not None:
        args.report.write_text(payload + '\n', encoding='utf-8')
    print(payload)
    return 0 if report['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    report = {'status': 'FAILED', 'checks': [], 'fixture': 'synthetic PCM16 tone',
              'actual_winmm': False, 'actual_native_keys': False,
              'nvda': 'NOT_RUN', 'human_listening': 'NOT_RUN', 'voice_inference': 'NOT_RUN',
              'independent_deadline_seconds': 180}
    # Leave a failed receipt if native code never returns to final reporting.
    # faulthandler's own watchdog is independent of wx callbacks and emits the
    # blocked stacks before terminating, even inside a native modal or cleanup.
    initial = {**report, 'count': 0, 'failure': 'The native reader probe did not reach final reporting. '
               'An independent 180-second watchdog records timeout stacks in stderr and exits nonzero.'}
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(initial, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Independent native reader deadline: 180 seconds; incomplete execution remains FAILED.', flush=True)
    faulthandler.dump_traceback_later(180, exit=True)
    try:
        return _run_probe(args, report)
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
