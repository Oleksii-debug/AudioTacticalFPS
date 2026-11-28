"""Native retained playback queue on the existing Studio owner.

The Desktop, Studio executor, WavePlayer, retained copies, saved CAS document
and wx controls are real. Authored file-picker answers and only the MCI command
boundary are controlled; the latter reads actual retained PCM metadata. No
physical listening, native file-picker or NVDA qualification is claimed.
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



class _OwnedModalAbort:
    """Unwind one owned modal loop per GUI turn, never its suspended parent."""

    def __init__(self, wx, app, owners, report):
        self.wx, self.app, self.owners, self.report = wx, app, owners, report
        self.active = False
        self.timer = None
        self.reported = set()

    def request(self):
        if not self.active:
            self.active = True
            self.wx.CallAfter(self._advance)

    def stop(self):
        self.active = False
        if self.timer is not None:
            self.timer.Stop()
            self.timer = None

    def _advance(self):
        self.timer = None
        if not self.active:
            return
        try:
            # Owners are innermost first. EndModal() requests a return; its
            # IsModal() remains true until that ShowModal() actually unwinds.
            for owner in self.owners():
                if owner is None:
                    continue
                dialog = owner.dialog
                if dialog and dialog.IsModal():
                    if dialog.IsShown():
                        owner.close()
                    self.timer = self.wx.CallLater(10, self._advance)
                    return
            self.active = False
            self.app.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            detail = traceback.format_exc()
            if detail not in self.reported:
                self.reported.add(detail)
                self.report(detail)
            # Cleanup refusal remains a failure. Do not force-delete a native
            # owner or report silence; the outer owned process bound applies.
            self.timer = self.wx.CallLater(25, self._advance)


def _nested_modal_failure_acceptance(wx, app, check, select_failure_handler):
    """Real Windows negative control for the probe's former nested-loop hang."""
    events, returned, failures = [], [], []
    owners = []
    abort = _OwnedModalAbort(wx, app, lambda: tuple(reversed(owners)), failures.append)

    def fail(detail):
        failures.append(detail)
        abort.request()

    def owner(label, parent):
        dialog = wx.Dialog(parent, title='Owned clip failure control — ' + label)
        native_id = dialog.GetId()
        record = SimpleNamespace(dialog=dialog, loop=None, native_id=native_id)

        def destroyed(event):
            if event.GetId() == native_id:
                events.append(label)
            event.Skip()

        def close():
            if wx.EventLoopBase.GetActive() != record.loop:
                raise AssertionError('Attempted to close a suspended ' + label + ' loop.')
            dialog.EndModal(wx.ID_CANCEL)

        record.close = close
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        owners.append(record)
        return record

    def provoke(inner):
        inner.loop = wx.EventLoopBase.GetActive()
        try:
            raise AssertionError('deliberate owned nested-modal failure')
        except AssertionError:
            fail(traceback.format_exc())

    def open_inner(outer):
        outer.loop = wx.EventLoopBase.GetActive()
        inner = owner('inner', outer.dialog)
        wx.CallAfter(provoke, inner)
        try:
            inner.dialog.ShowModal()
            returned.append('inner')
        finally:
            if inner.dialog:
                inner.dialog.Destroy()

    # wxPython MainLoop returns immediately with no top-level windows. Own the
    # actual outer dialog before starting the loop, then show it on that loop.
    outer = owner('outer', None)

    def open_outer():
        wx.CallAfter(open_inner, outer)
        try:
            outer.dialog.ShowModal()
            returned.append('outer')
        finally:
            if outer.dialog:
                outer.dialog.Destroy()
        app.ExitMainLoop()

    select_failure_handler(fail)
    try:
        wx.CallAfter(open_outer)
        app.MainLoop()
    finally:
        abort.stop()
        select_failure_handler(None)
    check('actual nested-modal failure returns child before parent',
          returned == ['inner', 'outer']
          and len(failures) == 1
          and 'deliberate owned nested-modal failure' in failures[0])
    check('actual failed modal owners receive one native destruction each',
          events.count('inner') == 1 and events.count('outer') == 1
          and all(not item.dialog for item in owners))



def _run_owned_native(arguments):
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    result = run_process(
        [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
         '--audio-evidence', str(arguments.audio_evidence.resolve()), '--native-child'],
        budget=AudioBudget(timeout=360, max_log_bytes=2 * 1024 * 1024),
        owned_tree=True, allow_failure=True)
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace'))
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
    sys.stdout.flush()
    sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, required=True)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows session required'}))
        return 77
    if not arguments.native_child:
        return _run_owned_native(arguments)
    faulthandler.dump_traceback_later(330, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime_handle = activate_bundled_runtime()
    try:
        return _run_native(arguments)
    finally:
        try:
            if runtime_handle is not None:
                runtime_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native(arguments):
    from ctypes import wintypes
    import wx
    from carr_library import durable_state
    from carr_library.application import LibraryApp
    from carr_library.audio import cleanup, process
    from carr_library.speech import player as player_module, playlist as playlist_module, listening
    from carr_library.studio import clips, backup_inputs, render
    from carr_library.ui import playback_queue, playback_navigation, studio_dialog, desktop_app
    from carr_library.ui.progress_panel import NativeProgressPanel

    checks, errors, executors, futures, held = [], [], [], [], []
    frame, failure_handler = None, None
    armed = []
    delivered_callbacks = []
    native_peers = []
    binding = _sibling('windows_publishing_smoke.py', '_queue_installed_bindings')
    modules = (player_module, playlist_module, listening, clips, backup_inputs, render,
               playback_queue, playback_navigation, studio_dialog, desktop_app,
               durable_state, cleanup, process)
    identities = binding.verify_installed_sources(*modules)
    evidence = durable_state.read_json(arguments.audio_evidence)
    selected_audio = SimpleNamespace(ffmpeg=evidence['ffmpeg']['path'],
                                     ffprobe=evidence['ffprobe']['path'])
    audio_identity = binding.verify_audio_evidence(selected_audio, arguments.audio_evidence)
    hardware = _hardware_preflight()
    print('REAL_WINDOWS_OUTPUT_PREFLIGHT ' + json.dumps(hardware, ensure_ascii=False), flush=True)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def failure(detail):
        errors.append(detail)
        print('FAIL: native playback queue\n' + detail, file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failure(detail)
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND

    def pump(predicate, description, timeout=8):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)

    def key(control, code, *, modifiers=()):
        top = wx.GetTopLevelParent(control)
        parent = control.GetParent()
        def visible():
            return (not isinstance(parent, wx.ScrolledWindow)
                    or parent.GetClientRect().Contains(control.GetRect()))
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle() and visible(),
             'Queue control did not receive visible foreground focus.')
        check('visible native focus: ' + control.GetName(), visible())
        pressed = []
        try:
            for modifier in modifiers:
                check('native modifier down', simulator.KeyDown(modifier))
                pressed.append(modifier)
            check('native key down: ' + control.GetName(), simulator.KeyDown(code))
            pressed.append(code)
            sleep(.025)
        finally:
            for code in reversed(pressed):
                check('native key released', simulator.KeyUp(code))
        # Do not synchronously enter the button's nested modal here. The same
        # main-loop driver continues inside its actual ShowModal event loop.

    def press(control):
        key(control, wx.WXK_SPACE)

    def descendants(parent):
        pending, result = list(parent.GetChildren()), []
        while pending:
            if len(result) + len(pending) > 256:
                raise ValueError('Native queue control inventory exceeds its bound.')
            item = pending.pop()
            if not item:
                raise ValueError('Native control disappeared during selection.')
            result.append(item)
            pending.extend(item.GetChildren())
        return result

    def named(parent, name):
        rows = [item for item in descendants(parent) if item.GetName() == name]
        check('one actual named control: ' + name, len(rows) == 1)
        return rows[0]

    def button_label(parent, label):
        rows = [item for item in descendants(parent)
                if isinstance(item, wx.Button) and item.GetLabel() == label]
        check('one actual labelled button: ' + label, len(rows) == 1)
        return rows[0]

    def window(title):
        rows = [item for item in wx.GetTopLevelWindows()
                if item and isinstance(item, wx.Dialog) and item.GetTitle() == title]
        if len(rows) > 1:
            raise AssertionError('More than one native owner has the same queue title.')
        return rows[0] if rows else None

    def owners():
        if frame is None or not frame:
            return ()
        root_id, found = frame.GetId(), []
        windows = tuple(wx.GetTopLevelWindows())
        if len(windows) > 256:
            raise ValueError('Native owned-window inventory exceeds its bound.')
        for peer in windows:
            if not peer or not isinstance(peer, wx.Dialog):
                continue
            parent, depth = peer, 0
            while parent is not None and depth < 64:
                if parent.GetId() == root_id:
                    found.append((depth, SimpleNamespace(dialog=peer,
                        close=lambda value=peer: value.EndModal(wx.ID_CANCEL))))
                    break
                parent, depth = parent.GetParent(), depth + 1
        return tuple(record for _, record in sorted(found, key=lambda value: value[0], reverse=True))

    abort = _OwnedModalAbort(wx, gui, owners, failure)

    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value

    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)
    check('actual native Windows controls', wx.Platform == '__WXMSW__')
    actual_executor = studio_dialog.ThreadPoolExecutor
    actual_picker, actual_after = wx.FileDialog, wx.CallAfter

    def executor_factory(*args, **kwargs):
        check('the existing Studio owns one executor', kwargs.get('max_workers') == 1
              and kwargs.get('thread_name_prefix') == 'audio-studio')
        executor = actual_executor(*args, **kwargs)
        submit = executor.submit
        def submitted(operation, *arguments, **options):
            record = armed.pop(0) if armed and getattr(operation, '__name__', '') == 'perform' else None
            if record is None:
                future = submit(operation, *arguments, **options)
            else:
                def performed():
                    value = operation(*arguments, **options)
                    record.value = value
                    record.ready.set()
                    if not record.release.wait(20):
                        raise TimeoutError('The authored native queue result was not released.')
                    return value
                future = submit(performed)
                record.future = future
            futures.append(future)
            return future
        executor.submit = submitted
        executors.append(executor)
        return executor

    def call_after(callback, *args, **kwargs):
        if getattr(callback, '__qualname__', '') == 'show_studio.<locals>.run.<locals>.finish.<locals>.apply':
            delivered_callbacks.append((callback, args, kwargs))
        return actual_after(callback, *args, **kwargs)

    def hold_next(label):
        record = SimpleNamespace(label=label, ready=threading.Event(), release=threading.Event(),
                                 completed=threading.Event(), future=None, value=None)
        armed.append(record)
        held.append(record)
        return record

    def completed_marker(record):
        # The Studio has already attached its actual completion callback before
        # its held operation signals ready. This observer runs after that callback.
        record.future.add_done_callback(lambda future: record.completed.set())

    def watch_destroy(peer, label):
        native_id, events = peer.GetId(), []
        def destroyed(event):
            if event.GetId() == native_id:
                events.append(event.GetId())
            event.Skip()
        peer.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        native_peers.append(peer)
        return events

    with TemporaryDirectory(prefix='carr-native-playback-queue-') as temporary:
        root = Path(temporary)
        source_paths = []
        original_hashes = {}
        for number in range(2):
            source = root / ('Original — Україна ' + str(number+1) + '.wav')
            samples = [round((9000+number*2000) * math.sin(2*math.pi*(300+number*100)*index/24000))
                       for index in range(48000)]
            with wave.open(str(source), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(struct.pack('<' + 'h'*len(samples), *samples))
            source_paths.append(source)
            original_hashes[source] = sha256(source.read_bytes()).hexdigest()

        class SelectedPaths:
            def __init__(self, parent, title, **kwargs):
                check('authored picker uses the current native queue parent',
                      parent.GetId() == window('Playback queue').GetId())
                check('authored picker requires existing WAV files in order',
                      title == 'Choose PCM WAV files in playback order'
                      and kwargs.get('style') == wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST)
                self.alive = True
            def ShowModal(self): return wx.ID_OK
            def GetPaths(self):
                return [str(source_paths[0]), str(source_paths[1]), str(source_paths[0])]
            def IsBeingDeleted(self): return not self.alive
            def Destroy(self): self.alive = False
            def __bool__(self): return self.alive

        frame = desktop_app.DesktopFrame(LibraryApp(root / 'Private queue library'))
        frame.Show()
        gui.SetTopWindow(frame)
        check('actual Desktop frame exists before the main event loop', bool(frame) and frame.IsShown())
        store = playlist_module.PlaylistStore(frame.library.root)
        studio_dialog.ThreadPoolExecutor = executor_factory
        wx.FileDialog, wx.CallAfter = SelectedPaths, call_after
        state = SimpleNamespace(waiting=None, description='', deadline=0, entered=False,
                                finished=False, started=monotonic())
        timer = wx.Timer(frame)

        def waiting(predicate, label, seconds=30):
            return predicate, label, monotonic() + seconds

        def open_studio():
            key(frame.status, ord('A'), modifiers=(wx.WXK_CONTROL, wx.WXK_ALT))

        def open_queue(studio):
            press(named(studio, 'Playback queue'))

        def queue_state(studio):
            host = studio._playback_navigation
            return host, host._queue_session, studio._audio_player

        def scenario():
            open_studio()
            yield waiting(lambda: window('Audio Studio — script, voices and production') is not None,
                          'Open actual Studio through its current registered shortcut', 60)
            studio = window('Audio Studio — script, voices and production')
            yield waiting(lambda: named(studio, 'Playback queue').IsEnabled(),
                          'Wait for actual Studio voice discovery', 90)
            open_queue(studio)
            yield waiting(lambda: window('Playback queue') is not None, 'Open actual nested queue')
            queue = window('Playback queue')
            host, session, player = queue_state(studio)
            mci = _MCI()
            player._mci = mci
            check('queue borrows the actual Studio player and timer',
                  session.host is host and host.controller.player is player
                  and isinstance(player, player_module.WavePlayer)
                  and host.timer.GetOwner() is studio and host.timer.IsRunning())
            check('opening a fresh queue is silent and creates no saved state',
                  not player.playlist and not mci.commands and store.load() == (None, None))
            check('same player is registered with actual shared progress',
                  player in studio.operation_progress.panel.media.players
                  and player in frame.operation_progress.panel.media.players)
            press(queue.buttons['add'])
            yield waiting(lambda: session.pending is None and not studio.working and len(player.playlist) == 3,
                          'Review and retain actual selected PCM copies', 60)
            check('actual retained queue keeps order and duplicates',
                  [item.sha256 for item in player.playlist] ==
                  [original_hashes[source_paths[0]], original_hashes[source_paths[1]], original_hashes[source_paths[0]]])
            check('retained copies have exact original bytes and remain silent',
                  all(item.path.parent == frame.library.root / 'state' / 'studio' / 'inputs'
                      and sha256(item.path.read_bytes()).hexdigest() == item.sha256 for item in player.playlist)
                  and not mci.commands and player.state == 'stopped')
            key(queue.items, wx.WXK_HOME)
            yield waiting(lambda: queue.items.GetSelection() == 0, 'Select first real native list row')
            key(queue.items, wx.WXK_DOWN)
            yield waiting(lambda: queue.items.GetSelection() == 1, 'Select second real native list row')
            press(queue.buttons['up'])
            expected = [original_hashes[source_paths[1]], original_hashes[source_paths[0]], original_hashes[source_paths[0]]]
            yield waiting(lambda: [item.sha256 for item in player.playlist] == expected,
                          'Move second retained item up through native controls')
            check('native reorder does not open an audio device', not mci.commands and player.state == 'stopped')
            press(queue.buttons['play'])
            yield waiting(lambda: player.state == 'playing', 'Start explicit native queue playback')
            check('explicit queue starts the first retained item on the same player',
                  player.playlist_index == 0 and len(mci.paths) == 1 and mci.paths[0]['sha256'] == expected[0])
            mci.position = 375
            press(queue.buttons['pause'])
            yield waiting(lambda: player.state == 'paused', 'Pause actual WavePlayer through native queue')
            check('paused source position is exact and disarms later automatic transitions',
                  player.position_seconds == .375 and not player.queue_active)
            press(queue.buttons['save'])
            yield waiting(lambda: session.pending is None and not studio.working and store.path.exists(),
                          'Save actual retained queue and source-frame position', 60)
            saved, saved_token = store.load()
            check('actual durable queue saves original frames and duplicate order',
                  saved.frame == 9000 and saved.index == 0
                  and [item.sha256 for item in saved.items] == expected)
            saved_bytes = store.path.read_bytes()
            press(queue.buttons['stop'])
            yield waiting(lambda: player.state == 'stopped' and not player.queue_active, 'Stop same native player')
            before_open = len(mci.paths)
            press(queue.buttons['next'])
            yield waiting(lambda: player.playlist_index == 1, 'Next selects silently')
            press(queue.buttons['previous'])
            yield waiting(lambda: player.playlist_index == 0, 'Previous selects silently')
            check('native next and previous never start playback',
                  len(mci.paths) == before_open and player.state == 'stopped')
            press(named(queue, 'Close playlist controls'))
            yield waiting(lambda: not queue, 'Return and destroy queue modal')
            press(button_label(studio, '&Close'))
            yield waiting(lambda: not studio, 'Return and destroy initial Studio modal')
            check('closing the Studio releases its actual queue timer and player',
                  host.closed and not host.timer.IsRunning() and host.timer.GetOwner() is None
                  and player.state == 'stopped')
            for source in source_paths:
                check('original external bytes survived every operation',
                      sha256(source.read_bytes()).hexdigest() == original_hashes[source])
                source.unlink()
            check('only authored external originals were removed before recovery',
                  not any(source.exists() for source in source_paths) and store.path.read_bytes() == saved_bytes)

            open_studio()
            yield waiting(lambda: window('Audio Studio — script, voices and production') is not None,
                          'Reopen actual Studio for passive recovery', 60)
            studio = window('Audio Studio — script, voices and production')
            yield waiting(lambda: named(studio, 'Playback queue').IsEnabled(),
                          'Wait for current Studio discovery', 90)
            open_queue(studio)
            yield waiting(lambda: window('Playback queue') is not None, 'Open saved queue as passive data')
            queue = window('Playback queue')
            host, session, player = queue_state(studio)
            mci = _MCI()
            player._mci = mci
            check('a reopened queue is passive until explicit retained-media review',
                  session.pending_saved == saved and not player.playlist and not mci.commands
                  and not queue.buttons['play'].IsEnabled())
            press(queue.buttons['review'])
            yield waiting(lambda: session.pending is None and not studio.working
                          and session.pending_saved is None and len(player.playlist) == 3,
                          'Review the exact saved retained copies without original external paths', 60)
            check('saved source-frame position is restored without opening the device',
                  player.playlist_index == 0 and player.position_seconds == .375
                  and player.state == 'stopped' and not mci.commands)
            press(queue.buttons['play'])
            yield waiting(lambda: player.state == 'playing', 'Start explicitly from reviewed saved position')
            check('restored queue starts at the original 375 ms position',
                  mci.position == 375 and mci.paths[-1]['sha256'] == expected[0])
            before_open = len(mci.paths)
            mci.position, mci.mode = mci.end, 'stopped'
            yield waiting(lambda: player.playlist_index == 1 and player.state == 'playing',
                          'Existing actual native timer admits natural EOF transition')
            check('natural ending advances once through the same player and host',
                  len(mci.paths) == before_open + 1 and mci.paths[-1]['sha256'] == expected[1]
                  and studio._audio_player is player and studio._playback_navigation is host)
            press(queue.buttons['pause'])
            yield waiting(lambda: player.state == 'paused', 'Native Pause withdraws queue continuation')
            check('paused queue cannot continue itself', not player.queue_active)
            press(queue.buttons['play'])
            yield waiting(lambda: player.state == 'playing', 'Explicit native Play resumes the same item')
            press(queue.buttons['stop'])
            yield waiting(lambda: player.state == 'stopped', 'Stop explicit queue session')
            check('Stop revokes all queued EOF intent', not player.queue_active and host._queue_ticket is None)

            press(queue.buttons['reload'])
            yield waiting(lambda: session.pending_saved is not None, 'Reload saved document for actual held review')
            before_rows, before_path = player.playlist, player.path
            review = hold_next('direct-view-review')
            press(queue.buttons['review'])
            yield waiting(review.ready.is_set, 'Hold genuine completed saved-review result', 60)
            completed_marker(review)
            check('held native review is the real retained tuple',
                  type(review.value) is tuple and len(review.value) == 3
                  and all(type(item) is playlist_module.PlaylistItem for item in review.value))
            events = watch_destroy(queue, 'direct')
            forbidden = []
            def no_refresh():
                forbidden.append('refresh')
                raise AssertionError('Late completion touched deleted native queue controls.')
            queue.refresh = no_refresh
            queue.Destroy()
            yield waiting(lambda: events and not queue, 'Observe actual direct native queue destruction')
            check('direct destruction cancels only its pending queue review',
                  len(events) == 1 and session.pending.is_set() and not host.closed)
            review.release.set()
            yield waiting(lambda: review.completed.is_set() and session.pending is None and not studio.working,
                          'Deliver real review completion after direct deletion')
            check('late reviewed tuple remains unapplied to the current player',
                  player.playlist is before_rows and player.path == before_path
                  and not forbidden and store.path.read_bytes() == saved_bytes)

            open_queue(studio)
            yield waiting(lambda: window('Playback queue') is not None, 'Reopen queue after direct destruction')
            queue = window('Playback queue')
            press(queue.buttons['review'])
            yield waiting(lambda: session.pending is None and session.pending_saved is None and not studio.working,
                          'Explicitly review the saved data again', 60)
            record = hold_next('parent-completed-save')
            press(queue.buttons['save'])
            yield waiting(record.ready.is_set, 'Hold actual durable save receipt before parent deletion', 60)
            completed_marker(record)
            check('completed save has actual durable receipt and exact bytes',
                  type(record.value) is dict and record.value['status'] == 'saved'
                  and record.value['value'] == saved and store.path.read_bytes() == saved_bytes)
            events = watch_destroy(queue, 'parent-child')
            parent_events = watch_destroy(studio, 'parent')
            queue.refresh = no_refresh
            studio.Destroy()
            yield waiting(lambda: parent_events and events and not studio and not queue,
                          'Observe actual native parent and child deletion')
            check('native parent disposal detaches the timer before worker release',
                  len(events) == 1 and len(parent_events) == 1 and host.closed
                  and not host.timer.IsRunning() and host.timer.GetOwner() is None)
            record.release.set()
            yield waiting(record.completed.is_set, 'Finish actual saved receipt after native parent deletion')
            check('saved receipt remains successful after owner death',
                  record.future.result()['status'] == 'saved'
                  and store.path.read_bytes() == saved_bytes and not forbidden)
            for callback, args, kwargs in tuple(delivered_callbacks):
                callback(*args, **kwargs)
            check('replayed actual delivered callbacks cannot touch deleted controls',
                  not forbidden and store.path.read_bytes() == saved_bytes)
            yield waiting(lambda: all(future.done() for future in futures),
                          'Confirm all existing Studio work and queued cleanup completed', 30)
            check('all genuine held operations completed on the original executors',
                  len(executors) == 2 and all(record.completed.is_set() for record in held)
                  and all(future.done() and future.exception() is None for future in futures))
            for item in saved.items:
                check('each retained source remains exact after all native operations',
                      sha256((frame.library.root / item.filename).read_bytes()).hexdigest() == item.sha256)
            check('all playback queue modal owners have actually disappeared', not owners())
            state.finished = True

        sequence = scenario()

        def drive(event=None):
            if state.entered or state.finished:
                return
            state.entered = True
            try:
                if monotonic() - state.started > 280:
                    raise TimeoutError('The native queue workflow exceeded its total bound.')
                if state.waiting is not None:
                    predicate, state.description, deadline = state.waiting
                    if not predicate():
                        if monotonic() > deadline:
                            raise TimeoutError(state.description)
                        return
                    state.waiting = None
                try:
                    state.waiting = next(sequence)
                except StopIteration:
                    if not state.finished:
                        raise AssertionError('Native queue scenario ended without its final checks.')
                    timer.Stop()
                    gui.ExitMainLoop()
            except (Exception, KeyboardInterrupt):
                failure(traceback.format_exc() + '\nWaiting: ' + state.description)
                timer.Stop()
                for record in held:
                    record.release.set()
                abort.request()
            finally:
                state.entered = False

        frame.Bind(wx.EVT_TIMER, drive, timer)
        timer.Start(30)
        try:
            gui.MainLoop()
            if errors or not state.finished:
                raise RuntimeError('\n'.join(errors) or 'Native queue workflow did not complete.')
        finally:
            timer.Stop()
            timer.SetOwner(None)
            for record in held:
                record.release.set()
            abort.stop()
            wx.FileDialog, wx.CallAfter = actual_picker, actual_after
            studio_dialog.ThreadPoolExecutor = actual_executor
            for executor in executors:
                executor.shutdown(wait=True, cancel_futures=False)
            if frame:
                frame.cancel_event.set()
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
            gui.Yield()
        check('selected external audio executables remain identical',
              binding.verify_audio_evidence(selected_audio, arguments.audio_evidence) == audio_identity)
        check('all selected installed modules remain exact',
              binding.verify_installed_sources(*modules) == identities)

    print(json.dumps({'result': 'PASS', 'checks': len(checks),
        'native_keyboard': 'PASS', 'actual_retained_files': 'PASS',
        'passive_reopen_and_explicit_review': 'PASS', 'same_studio_executor': 'PASS',
        'direct_parent_late_completion': 'PASS', 'source_identity': identities,
        'audio_runtime': audio_identity, 'real_output_preflight': hardware,
        'native_file_picker': 'NOT_RUN', 'physical_output': 'NOT_RUN', 'NVDA': 'NOT_RUN',
        'transport': 'Explicit controlled MCI command boundary over actual retained PCM'},
        ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
