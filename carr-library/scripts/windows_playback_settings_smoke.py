"""Installed native independent playback controls with actual FFmpeg.

Authored PCM and field values are explicit fixtures. The current WavePlayer,
LiveReader, Desktop worker, source model, wx controls and FFmpeg remain real.
Only the MCI/WinMM transport calls are controlled so hosted machines without
audio can qualify UI ownership without claiming physical playback or NVDA.
A separate real WinMM inventory/format query and MCI error text are reported.
"""
import argparse
import ctypes
import faulthandler
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import sys
import threading
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from types import SimpleNamespace
import traceback
import wave


def _sibling(name, identity):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(identity, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hardware_preflight():
    """Query real devices without opening a stream or changing device volume."""
    from carr_library.audio.recording_monitor import _api, _WaveFormat
    from carr_library.audio.output_devices import _WaveOutCaps, MAX_OUTPUT_DEVICES
    library = _api()
    count = library.waveOutGetNumDevs
    count.argtypes, count.restype = (), ctypes.c_uint32
    total = int(count())
    if not 0 <= total <= MAX_OUTPUT_DEVICES:
        raise ValueError('The real Windows output inventory exceeds its declared bound.')
    caps = library.waveOutGetDevCapsW
    caps.argtypes = (ctypes.c_size_t, ctypes.POINTER(_WaveOutCaps), ctypes.c_uint32)
    caps.restype = ctypes.c_uint32
    error = library.mciGetErrorStringW
    error.argtypes = (ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_uint32)
    error.restype = ctypes.c_int
    message = ctypes.create_unicode_buffer(1024)
    message_ok = bool(error(326, message, len(message)))
    rows, queries = [], []
    for index in range(total):
        value = _WaveOutCaps()
        code = int(caps(index, ctypes.byref(value), ctypes.sizeof(value)))
        rows.append({'index': index, 'caps_code': code,
            'name': bytes(value.name).decode('utf-16-le').split('\0', 1)[0] if not code else None,
            'channels': int(value.channels) if not code else None,
            'support': int(value.support) if not code else None})
    for index in (0xFFFFFFFF, *range(total)):
        for rate, channels in ((8000, 1), (24000, 2), (48000, 2)):
            align = channels * 2
            fmt = _WaveFormat(1, channels, rate, rate * align, align, 16, 0)
            code = int(library.waveOutOpen(None, index, ctypes.byref(fmt), 0, 0, 1))
            queries.append({'index': 'WAVE_MAPPER' if index == 0xFFFFFFFF else index,
                            'rate': rate, 'channels': channels, 'bits': 16, 'code': code})
    return {'device_count': total, 'devices': rows, 'format_queries': queries,
            'compatible_query': any(row['code'] == 0 for row in queries),
            'mci_error_326': {'translated': message_ok, 'message': message.value},
            'physical_playback': 'NOT_RUN', 'device_volume_changed': False}


class _MCI:
    """Explicit controlled command boundary; no native audio fallback."""
    def __init__(self):
        self.commands, self.paths, self.threads = [], [], []
        self.mode, self.position, self.end = 'stopped', 0, 0

    def __call__(self, command):
        self.commands.append(command)
        self.threads.append(threading.get_ident())
        if command.startswith('open '):
            path = Path(command.split('"')[1])
            with wave.open(str(path), 'rb') as stream:
                frames, rate = stream.getnframes(), stream.getframerate()
                self.end = round(frames / rate * 1000)
            self.paths.append({'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest(),
                               'frames': frames, 'rate': rate})
        elif command.startswith('play '):
            self.mode = 'playing'
            self.position = int(command.split(' from ')[1].split()[0])
        elif command.startswith('pause '):
            self.mode = 'paused'
        elif command.startswith('resume '):
            self.mode = 'playing'
        elif command.startswith('seek '):
            self.position = int(command.split(' to ')[1].split()[0])
        elif command.startswith(('stop ', 'close ')):
            self.mode = 'stopped'
        elif command.endswith(' mode'):
            return self.mode
        elif command.endswith(' position'):
            return str(self.position)
        return ''


class _WaveDriver:
    """Control only the WinMM calls while retaining actual queued PCM headers."""
    def __init__(self):
        self.calls, self.headers, self.queued, self.threads = [], {}, set(), []
        self.volume = 0xFFFFFFFF

    def call(self, name, handle=None):
        if handle is not None and handle.value != 0x10000:
            raise AssertionError('Playback attempted a foreign native handle.')
        self.calls.append(name)
        self.threads.append(threading.get_ident())
        return 0

    def waveOutOpen(self, result, index, fmt, callback, instance, flags):
        if result is not None:
            result._obj.value = 0x10000
        return self.call('open' if result is not None else 'query')

    def waveOutGetID(self, handle, result):
        result._obj.value = 0
        return self.call('id', handle)

    def waveOutGetDevCapsW(self, handle, result, size):
        if handle != 0x10000:
            raise AssertionError('Capability query used a foreign handle.')
        result._obj.support = 12
        return self.call('caps')

    def waveOutPause(self, handle):
        return self.call('pause', handle)

    def waveOutRestart(self, handle):
        return self.call('restart', handle)

    def waveOutSetVolume(self, handle, value):
        raise AssertionError('Playback changed shared native volume state.')

    def waveOutGetVolume(self, handle, result):
        raise AssertionError('Playback queried shared native volume state.')

    def pcm(self):
        return b''.join(ctypes.string_at(header.data, header.length)
                       for identity, header in self.headers.items() if identity in self.queued)

    def waveOutGetPosition(self, handle, result, size):
        result._obj.kind, result._obj.data.value = 2, 0
        return self.call('position', handle)

    def waveOutPrepareHeader(self, handle, header, size):
        token = header._obj
        self.headers[id(token)] = token
        return self.call('prepare', handle)

    def waveOutWrite(self, handle, header, size):
        token = header._obj
        if self.headers.get(id(token)) is not token:
            raise AssertionError('PCM header was not retained before Write.')
        self.queued.add(id(token))
        return self.call('write', handle)

    def waveOutUnprepareHeader(self, handle, header, size):
        token = header._obj
        if id(token) in self.queued and not token.flags & 1:
            raise AssertionError('PCM released before the controlled device acknowledged it.')
        self.queued.discard(id(token))
        self.headers.pop(id(token), None)
        return self.call('unprepare', handle)

    def waveOutReset(self, handle):
        for token in self.headers.values():
            token.flags |= 1
        self.queued.clear()
        return self.call('reset', handle)

    def waveOutClose(self, handle):
        if self.headers or self.queued:
            raise AssertionError('The transport closed before releasing its PCM headers.')
        return self.call('close', handle)


def _run_owned_native(arguments):
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    result = run_process([sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
        '--audio-evidence', str(arguments.audio_evidence.resolve()), '--native-child'],
        budget=AudioBudget(timeout=360, max_log_bytes=2 * 1024 * 1024),
        owned_tree=True, allow_failure=True)
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace'))
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, required=True)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    if not arguments.native_child:
        return _run_owned_native(arguments)
    faulthandler.dump_traceback_later(330, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    native = activate_bundled_runtime()
    try:
        return _run_native(arguments)
    finally:
        try:
            if native is not None:
                native.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native(arguments):
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import AudioBudget
    from carr_library.audio import process as process_module, dsp as dsp_module
    from carr_library.durable_state import read_json
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader import live_reading as core_module, live_player as facade_module
    from carr_library.speech import player as player_module, listening, _wave_output
    from carr_library.speech.runtime import Voice, SpeechResult
    from carr_library.ui import live_reading as controls_module, playback_settings as settings_module
    from carr_library.ui import playback_navigation as navigation_module, builtin_keyboard_commands
    from carr_library.ui import studio_dialog, studio_clips, desktop_app
    from carr_library.assistant import player_tools
    from carr_library import runtime_settings

    checks, errors, drivers, backends, readers, hosts = [], [], [], [], [], []
    frame, failure_handler = None, None
    binding = _sibling('windows_publishing_smoke.py', '_playback_current_bindings')
    modal = _sibling('windows_clip_editor_smoke.py', '_playback_owned_modal_acceptance')
    modules = (player_module, listening, _wave_output, core_module, facade_module,
               controls_module, settings_module, navigation_module, builtin_keyboard_commands,
               studio_dialog, studio_clips, desktop_app, player_tools, process_module, dsp_module)
    identities = binding.verify_installed_sources(*modules)
    hardware = _hardware_preflight()
    print('REAL_WINDOWS_OUTPUT_PREFLIGHT ' + json.dumps(hardware, ensure_ascii=False), flush=True)
    evidence = read_json(arguments.audio_evidence)
    selected = SimpleNamespace(ffmpeg=evidence['ffmpeg']['path'], ffprobe=evidence['ffprobe']['path'])
    audio_proof = binding.verify_audio_evidence(selected, arguments.audio_evidence)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    def report_failure(detail):
        errors.append(detail)
        print('FAIL: independent native playback\n' + detail, file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                report_failure(detail)
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], wintypes.HWND

    def pump(predicate, label, timeout=45):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= deadline:
                raise TimeoutError(label)
            gui.Yield()
            sleep(.01)

    def press(control):
        top = wx.GetTopLevelParent(control)
        parent = control.GetParent()
        # wx.ScrolledWindow reveals its focused child automatically. The
        # ScrollChildIntoView helper belongs to wx.lib.scrolledpanel instead.
        def visible():
            return (not isinstance(parent, wx.ScrolledWindow)
                    or parent.GetClientRect().Contains(control.GetRect()))
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle() and visible(),
             'Playback control did not receive visible foreground focus.', timeout=10)
        check('native focus: ' + control.GetName(), wx.Window.FindFocus() is control)
        check('focused playback control is visible: ' + control.GetName(), visible())
        check('native Space down: ' + control.GetName(), simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            sleep(.04)
        finally:
            check('native Space up: ' + control.GetName(), simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()

    def owners():
        if frame is None or not frame:
            return ()
        root_id = frame.GetId()
        found = []
        for dialog in wx.GetTopLevelWindows():
            if not dialog or not isinstance(dialog, wx.Dialog):
                continue
            parent, depth = dialog, 0
            while parent is not None and depth < 32:
                if parent.GetId() == root_id:
                    found.append((depth, SimpleNamespace(dialog=dialog,
                        close=lambda peer=dialog: peer.EndModal(wx.ID_CANCEL))))
                    break
                parent, depth = parent.GetParent(), depth + 1
        return tuple(record for _, record in sorted(found, key=lambda pair: pair[0], reverse=True))

    abort = modal._OwnedModalAbort(wx, gui, owners, report_failure)

    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value

    modal._nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)

    def open_settings(button, callback):
        deadline = monotonic() + 15
        def act():
            try:
                found = [window for window in wx.GetTopLevelWindows()
                         if window and isinstance(window, wx.Dialog)
                         and window.GetTitle() == 'Independent playback settings' and window.IsShown()]
                if not found:
                    if monotonic() >= deadline:
                        raise TimeoutError('The real playback settings modal did not open.')
                    wx.CallLater(20, act)
                    return
                popup, = found
                callback(popup)
                if popup:
                    close = wx.Window.FindWindowByName('Close playback settings', popup)
                    press(close)
            except (Exception, KeyboardInterrupt):
                report_failure(traceback.format_exc())
                abort.request()
        wx.CallLater(20, act)
        press(button)
        if errors:
            raise RuntimeError('\n'.join(errors))

    def controlled_player():
        value, mci = player_module.WavePlayer(), _MCI()
        value._mci = mci
        backends.append(value)
        return value, mci

    original_wave_library = _wave_output._library
    def controlled_wave_library():
        value = _WaveDriver()
        drivers.append(value)
        return value

    observed = {}
    workspace = TemporaryDirectory(prefix='carr-native-listening-')
    root = Path(workspace.name)
    try:
        library = LibraryApp(root / 'library')
        scope = library.create_workspace('Synthetic independent playback acceptance')
        raw = 'Оригінальне читання 🚂. Playback stays separate.'
        book = library.add_text(scope, 'Immutable synthetic source', raw, language='uk')
        desk = ReadingDesk(library, scope, book['edition'])
        source_before = desk.page(1)
        stored_settings = runtime_settings.load_settings(library.root / 'state')
        stored_settings.update(ffmpeg=audio_proof['ffmpeg']['path'], ffprobe=audio_proof['ffprobe']['path'])
        runtime_settings.save_settings(library.root / 'state', stored_settings)
        frame = desktop_app.DesktopFrame(library)
        frame.Show()
        gui.SetTopWindow(frame)
        _wave_output._library = controlled_wave_library
        voice = Voice('synthetic:independent-playback', 'Explicit authored PCM fixture',
                      'synthetic', 'uk', ('rate', 'volume'))
        requests = []
        def render(request, target, **options):
            if options['cancelled']():
                raise InterruptedError('The actual live reader cancelled the authored PCM fixture.')
            requests.append((request, threading.get_ident()))
            values = [round(5000 * math.sin(2 * math.pi * 440 * index / 24000))
                      for index in range(48000)]
            with wave.open(str(target), 'wb') as stream:
                stream.setparams((2, 2, 24000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(b''.join(struct.pack('<hh', value, value) for value in values))
            return SpeechResult(Path(target), sha256(Path(target).read_bytes()).hexdigest(),
                2, 24000, 2, 48000, voice.id, sha256(request.text.encode('utf-8')).hexdigest(), 1)

        live_window = wx.Dialog(frame, title='Current live reader controls acceptance',
                                size=(900, 760), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        live_layout = wx.BoxSizer(wx.VERTICAL)
        text = wx.TextCtrl(live_window, value=raw, style=wx.TE_MULTILINE | wx.TE_READONLY)
        text.SetName('Exact current source for independent playback')
        live_layout.Add(text, 1, wx.EXPAND | wx.ALL, 6)
        backend, live_mci = controlled_player()
        facade = facade_module.CanonicalPlayer(backend)
        def core_factory(source, **options):
            core = core_module.LiveReader(source, discover=lambda **kwargs: (voice,), render=render,
                player_factory=lambda: facade,
                budget=core_module.LiveBudget(chunk_characters=128, minimum_free_bytes=1),
                **options)
            readers.append(core)
            return core
        buttons = {}
        live = controls_module.LiveReadingControls(live_window, live_layout, text,
            core_module.SourceSequence(desk), current=lambda: (1, raw),
            show_section=lambda section: check('live UI receives exact source section', section.text == raw),
            command_buttons=buttons, host=frame, core_factory=core_factory)
        live_window.SetSizer(live_layout)
        native_live_id = live_window.GetId()
        def live_destroyed(event):
            if event.GetId() == native_live_id:
                live.close()
            event.Skip()
        live_window.Bind(wx.EVT_WINDOW_DESTROY, live_destroyed)
        live_window.Show()

        (root / 'private-listening').mkdir()
        source = root / 'Synthetic original stereo.wav'
        values = [round(6000 * math.sin(2 * math.pi * 330 * index / 24000))
                  for index in range(48000)]
        with wave.open(str(source), 'wb') as stream:
            stream.setparams((2, 2, 24000, 0, 'NONE', 'uncompressed'))
            stream.writeframes(b''.join(struct.pack('<hh', value, -value) for value in values))
        original_audio = sha256(source.read_bytes()).hexdigest()

        def saved_host(parent, *, held=None):
            player, mci = controlled_player()
            player.open(source)
            generation = [0]
            holder = {}
            def stop():
                generation[0] += 1
                player.stop()
                if 'host' in holder:
                    holder['host'].invalidate()
            def prepare(request, completed, failed, *, cancelled):
                if frame.working:
                    return False
                def operation():
                    result = player.prepare_listening(request, root / 'private-listening',
                        state_root=library.root / 'state', cancelled=cancelled,
                        budget=AudioBudget(timeout=45, max_output_bytes=32 * 1024 * 1024))
                    if held is not None:
                        held['result'] = result
                        held['entered'].set()
                        if not held['release'].wait(45):
                            result.close()
                            raise TimeoutError('The held actual listening result was not released.')
                    return result
                def done(result):
                    if held is not None:
                        held['delivered'].append(result)
                    completed(result)
                frame.run_job('Prepare explicit listening copy', operation, done=done, failed=failed)
                return frame.working
            host = navigation_module.PlaybackNavigationHost(parent, player,
                generation=lambda: generation[0], alive=lambda: bool(parent),
                stop_current=stop, report=lambda message: observed.setdefault('saved_notices', []).append(str(message)),
                wx_module=wx, prepare=prepare)
            holder['host'] = host
            hosts.append(host)
            return host, player, mci

        def saved_lifetime():
            for termination in ('direct', 'native-parent'):
                parent = wx.Dialog(frame, title='Owned listening settings lifetime', size=(300, 150))
                parent.Show()
                held = {'entered': threading.Event(), 'release': threading.Event(), 'delivered': []}
                host, player, mci = saved_host(parent, held=held)
                view = settings_module.create_playback_settings_dialog(parent,
                    settings=player.listening_settings, levels=player.levels,
                    prepare=host._prepare_settings,
                    apply_levels=lambda value: player.set_levels(value.volume, muted=value.muted, balance=value.balance),
                    mix_available=True, alive=lambda: not host.closed and bool(parent),
                    play=host._play_current, stop=host.controller.stop_current, wx_module=wx)
                view.Show()
                view.fields['speed'].ChangeValue('1.25')
                view.fields['pitch'].ChangeValue('-2')
                cached_id, destroyed, forbidden, replayed = view.GetId(), [], [], []
                def destruction(event):
                    if event.GetId() == cached_id:
                        destroyed.append(event.GetId())
                    event.Skip()
                view.Bind(wx.EVT_WINDOW_DESTROY, destruction)
                try:
                    press(view.buttons['prepare'])
                    pump(held['entered'].is_set, 'The real private listening result did not finish.')
                    check('actual Desktop worker still owns held listening result', frame.working
                          and held['result'].path.is_file() and not held['delivered'])
                    callbacks = (view.prepare, view.levels, view.reset, view.cancel, view.play, view.stop)
                    if termination == 'native-parent':
                        def forbidden_destroy():
                            raise AssertionError('Parent deletion must bypass the Python Destroy override.')
                        view.Destroy = forbidden_destroy
                        parent.DestroyChildren()
                    else:
                        view.Destroy()
                    pump(lambda: not view and destroyed == [cached_id],
                         'Native settings deletion was not observed before the actual result.')
                    check('native playback settings disposal sets pure cancellation first', not view.alive)
                    def poisoned(*args, **kwargs):
                        forbidden.append(True)
                        raise AssertionError('A late listening callback accessed deleted native controls.')
                    for control in (view.status, *view.fields.values(), view.muted):
                        for method in ('GetValue', 'ChangeValue', 'SetFocus', 'Enable'):
                            if hasattr(control, method):
                                setattr(control, method, poisoned)
                    def replay():
                        for callback in callbacks:
                            callback()
                        replayed.append(True)
                    wx.CallAfter(replay)
                    held['release'].set()
                    pump(lambda: held['delivered'] and not frame.working and replayed,
                         'Actual Desktop completion and late callbacks were not delivered.')
                    check('late listening result is closed without native adoption: ' + termination,
                        held['result'].closed and player._listening is None
                        and not forbidden and destroyed == [cached_id] and not mci.paths)
                    check('late listening controls do not change original bytes or other work',
                          sha256(source.read_bytes()).hexdigest() == original_audio
                          and not frame.cancel_event.is_set())
                finally:
                    held['release'].set()
                    pump(lambda: not frame.working, 'Held native owner did not finish cleanup.')
                    host.close()
                    player.close()
                    if view:
                        type(view).Destroy(view)
                    if parent:
                        parent.Destroy()

        def exercise():
            try:
                check('actual Windows wx controls', wx.Platform == '__WXMSW__')
                check('real output preflight retained exact MCI326 explanation',
                      hardware['mci_error_326']['translated'] and bool(hardware['mci_error_326']['message']))
                check('opening controls starts neither synthesis nor sound', not requests and not live_mci.paths)
                press(live.discover_button)
                pump(lambda: len(live._voices) == 1 and not live.core.busy,
                     'Actual live voice discovery did not finish.')
                live.voice.SetSelection(0)
                live.rate.SetValue(-2)
                live.volume.SetValue(70)
                playback_button = buttons['reader.playback_settings'][0]
                def select_live(popup):
                    popup.fields['speed'].ChangeValue('2')
                    popup.fields['pitch'].ChangeValue('5')
                    press(popup.buttons['prepare'])
                    check('explicit live settings are separate from synthesis fields',
                        live.core.listening_settings == listening.ListeningSettings(2, 5)
                        and live.rate.GetValue() == -2 and live.volume.GetValue() == 70
                        and not requests and not live_mci.paths)
                open_settings(playback_button, select_live)
                press(live.read_buttons[0])
                pump(lambda: live.core.status().state == 'playing',
                     'Actual live FFmpeg listening conversion did not reach playback.', timeout=60)
                check('real source worker rendered unchanged synthesis request',
                    len(requests) == 1 and requests[0][0].text == raw
                    and abs(requests[0][0].rate - 2 ** (-2 / 10)) < 1e-12
                    and requests[0][0].volume == .7)
                check('real FFmpeg rendered independent speed and pitch on the source worker',
                    len(live_mci.paths) == 1 and Path(live_mci.paths[0]['path']).name == 'listening.wav'
                    and abs(live_mci.paths[0]['frames'] - 24000) <= 1
                    and live.core.status().duration_seconds == 2
                    and len(set(live_mci.threads)) == 1
                    and live_mci.threads[0] == requests[0][1] != threading.get_ident())
                press(live.pause_button)
                pump(lambda: live.core.status().state == 'paused', 'Live playback did not pause.')
                def live_levels(popup):
                    popup.fields['volume'].ChangeValue('35')
                    popup.fields['balance'].ChangeValue('50')
                    popup.muted.SetValue(True)
                    press(popup.buttons['levels'])
                    pump(lambda: live.core.status().playback_muted and drivers,
                         'The existing source worker did not apply muted instance levels.')
                    driver = drivers[-1]
                    check('mute changes only the existing source worker PCM buffers',
                          driver.pcm() and not any(driver.pcm())
                          and driver.volume == 0xFFFFFFFF and 'restart' not in driver.calls
                          and live.core.status().state == 'paused'
                          and len(set(driver.threads)) == 1
                          and driver.threads[0] == requests[0][1])
                    popup.muted.SetValue(False)
                    press(popup.buttons['levels'])
                    pump(lambda: not live.core.status().playback_muted and any(driver.pcm()),
                         'Unmute did not retain the requested player-only PCM volume and balance.')
                    queued = driver.pcm()
                    with wave.open(str(backend.listening_path), 'rb') as original:
                        original.setpos(backend._wave.first_frame)
                        unscaled = original.readframes(len(queued) // 4)
                    actual_pairs = list(struct.iter_unpack('<hh', queued))
                    source_pairs = list(struct.iter_unpack('<hh', unscaled))
                    check('actual queued stereo PCM applies independent gain and balance',
                          len(actual_pairs) == len(source_pairs) > 0
                          and all(abs(left - round(a * .175)) <= 1
                                  and abs(right - round(b * .35)) <= 1
                                  for (left, right), (a, b) in zip(actual_pairs, source_pairs))
                          and driver.volume == 0xFFFFFFFF)
                    check('paused unmute never restarts audio', 'restart' not in driver.calls
                          and live.core.status().state == 'paused')
                open_settings(playback_button, live_levels)
                press(live.stop_button)
                pump(lambda: not live.core.busy and live.core.quiescent,
                     'Live Stop did not confirm the existing playback owner.')
                check('live Stop releases original chunk and listening derivative',
                      live.core._retained_resources is None and backend._listening is None
                      and not list(library.root.glob('.live-reading-*')))
                live.close()
                live_window.Destroy()

                parent = wx.Dialog(frame, title='Saved playback controls acceptance', size=(500, 200))
                host, player, mci = saved_host(parent)
                layout = wx.BoxSizer(wx.VERTICAL)
                button = wx.Button(parent, label='Independent playback settings...')
                button.SetName('Independent playback settings...')
                button.Bind(wx.EVT_BUTTON, lambda event: host.show_settings())
                layout.Add(button, 0, wx.ALL, 10)
                parent.SetSizer(layout)
                parent.Show()
                try:
                    def saved_settings(popup):
                        popup.fields['speed'].ChangeValue('.5')
                        popup.fields['pitch'].ChangeValue('-3')
                        press(popup.buttons['prepare'])
                        pump(lambda: not popup.pending and not frame.working,
                             'Actual saved listening preparation did not finish.', timeout=60)
                        check('saved preparation stays silent and retains original timeline',
                            player.listening_settings == listening.ListeningSettings(.5, -3)
                            and abs(player.listening_duration_seconds - 4) <= 1 / 24000
                            and player.duration_seconds == 2 and player.state == 'stopped' and not mci.paths)
                        press(popup.buttons['play'])
                        pump(lambda: player.state == 'playing', 'Explicit saved Play did not start.')
                        check('saved explicit Play uses real converted PCM', len(mci.paths) == 1
                              and abs(mci.paths[0]['frames'] - 96000) <= 1)
                        press(popup.buttons['stop'])
                        check('saved Stop revokes navigation without touching original audio',
                            player.state == 'stopped' and host.controller.audio is None
                            and sha256(source.read_bytes()).hexdigest() == original_audio)
                    open_settings(button, saved_settings)
                finally:
                    host.close()
                    player.close()
                    if parent:
                        parent.Destroy()
                saved_lifetime()
                after_source = ReadingDesk(library, scope, book['edition']).page(1)
                check('native playback leaves source bytes and source identity unchanged',
                      after_source.raw_text == source_before.raw_text and after_source.location == source_before.location)
                check('all controlled WinMM buffers are released by their original owners',
                      all(not driver.headers and not driver.queued for driver in drivers)
                      and not _wave_output._UNRELEASED_OUTPUTS)
                check('selected external runtime remains byte-identical',
                      binding.verify_audio_evidence(selected, arguments.audio_evidence) == audio_proof)
                check('installed playback implementation remains exact', binding.verify_installed_sources(*modules) == identities)
                observed['native_ui'] = 'PASS'
                gui.ExitMainLoop()
            except (Exception, KeyboardInterrupt):
                report_failure(traceback.format_exc())
                abort.request()

        wx.CallAfter(exercise)
        gui.MainLoop()
        check('complete native playback sequence executed', observed.get('native_ui') == 'PASS')
        if errors:
            raise RuntimeError('\n'.join(errors))
    finally:
        abort.stop()
        for core in readers:
            future = core.close()
            if future is not None:
                future.result(30)
        for host in hosts:
            host.close()
        for backend in backends:
            if backend.close() is False:
                raise RuntimeError('Playback native acceptance retained an unconfirmed owner.')
        _wave_output._library = original_wave_library
        if frame is not None:
            if frame.working:
                raise RuntimeError('The native Desktop worker is still active; preserve its workspace.')
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            if getattr(frame, 'progress_panel', None) is not None:
                frame.progress_panel.close()
            if frame:
                frame.Destroy()
        workspace.cleanup()
    print(json.dumps({'result': 'PASS', 'native_ui': observed['native_ui'],
        'actual_ffmpeg': 'PASS', 'real_windows_output_query': hardware,
        'physical_output': 'NOT_RUN', 'voice_quality': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'transport': 'Explicit controlled MCI/WinMM calls; real WavePlayer and buffers',
        'checks': checks, 'source_identity': identities, 'audio_runtime': audio_proof}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
