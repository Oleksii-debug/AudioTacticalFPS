"""Installed native clip controls plus real FFmpeg rendering of synthetic PCM.

File/directory picker results and one explicitly passive preview admission are
supplied by the probe. Real Windows keyboard events activate the clip, crossfade,
marker and repeat-cancellation controls; the real wx loop handles worker results.
Actual repeat playback, physical devices, native file pickers and NVDA remain
separate gates. The passive preview never opens an audio output.
"""
import argparse
from dataclasses import replace
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import struct
import sys
import threading
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
import wave


_cleanup_complete = threading.Event()


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows session required', 'nvda': 'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-directory', required=True, type=Path,
                        help='Explicit pinned external FFmpeg test directory')
    parser.add_argument('--audio-lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    arguments = parser.parse_args()
    selected_audio = arguments.audio_directory.resolve(strict=True)
    audio_pin = json.loads(arguments.audio_lock.read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import AudioRuntime
    from carr_library.studio import clips
    from carr_library.studio.managed_inputs import resolve_managed_input
    from carr_library.studio.render import ProductionService
    from carr_library.studio.project import Scene
    from carr_library.speech.player import WavePlayer
    from carr_library.ui import studio_clips as ui
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, audio_proof, module_origins = [], [], {}, {}
    modules, source_digests = [], {}
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    checkout = Path(__file__).resolve().parents[1]
    for name in ('carr_library.studio.clips', 'carr_library.studio.managed_inputs', 'carr_library.studio.render',
                 'carr_library.studio.project', 'carr_library.audio.process',
                 'carr_library.audio.windows_job', 'carr_library.ui.studio_clips',
                 'carr_library.studio.clip_operations', 'carr_library.application',
                 'carr_library.ui.desktop_app', 'carr_library.speech.player',
                 'carr_library.speech.playlist', 'carr_library.speech.progress_speech',
                 'carr_library.durable_state', 'carr_library.operations.broker'):
        module = __import__(name, fromlist=['__file__'])
        location = Path(module.__file__).resolve(strict=True)
        module_origins[name] = str(location)
        modules.append(module)
        source_digests[name] = sha256(location.read_bytes()).hexdigest()
        check('installed module origin: ' + name,
              location.is_relative_to(Path(sys.prefix).resolve()) and 'site-packages' in location.parts)
        expected = checkout / 'src' / Path(*name.split('.')).with_suffix('.py')
        check('exact installed source bytes: ' + name,
              not location.is_relative_to(checkout)
              and sha256(location.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    def verify_audio(runtime):
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            audio_proof[name] = {'path': str(binary), 'sha256': digest}
            selected = selected_audio / 'bin' / (name + '.exe')
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            check('explicit selected external executable: ' + name, binary.samefile(selected))
            check('pinned external executable bytes: ' + name,
                  binary.stat().st_size == expected['size'] and digest == expected['sha256'])

    # Every picker/observer below remains alive until the original host and its
    # shared worker have actually settled. No playback transport is substituted.
    pressed, modal_loops, destroyed = set(), [], set()
    closing = {'scheduled': False, 'deadline': None, 'dialog_destroy': False,
               'host_close': False, 'complete': False, 'stalled': False}
    frame = dialog = service = host_executor = destroy_observer = None
    host_handle = dialog_handle = host_id = dialog_id = None
    passive_player = passive_path = None

    def remember_failure():
        detail = traceback.format_exc()
        errors.append(detail)
        print('FAIL: native clip editor\n' + detail, file=sys.stderr, flush=True)
        begin_shutdown()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            remember_failure()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def pump(predicate, description, timeout=45, *, cleanup=False):
        deadline = monotonic() + timeout
        while not predicate():
            if errors and not cleanup:
                raise RuntimeError(errors[0])
            if monotonic() > deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)

    def guarded(callback):
        def invoke():
            if errors:
                return
            try:
                callback()
            except BaseException:
                remember_failure()
        return invoke

    def native_control_current(control):
        top = wx.GetTopLevelParent(control)
        handle = top.GetHandle()
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner_pid))
        return (bool(user32.IsWindow(handle)) and owner_pid.value == os.getpid()
                and control.IsEnabled() and control.IsShownOnScreen())

    def focus(control):
        # A returned modal can precede the previous key-up callback. Never let
        # Escape/Space leak into the next dialog or bypass disabled controls.
        pump(lambda: not pressed and native_control_current(control),
             'Clip control is not current, enabled and free of held keys', timeout=10)
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
                     and user32.GetForegroundWindow() == top.GetHandle(),
             'Clip control did not receive foreground keyboard focus', timeout=10)
        check('keyboard focus: ' + control.GetName(), wx.Window.FindFocus() is control)

    def release(key, modifiers, name):
        try:
            if (key, modifiers) in pressed:
                if key == wx.WXK_SPACE:
                    check('Space key up: ' + name, simulator.KeyUp(key, modifiers))
                else:
                    check('native key up: ' + name, simulator.KeyUp(key, modifiers))
        except BaseException:
            remember_failure()
        finally:
            pressed.discard((key, modifiers))

    def press(control, key=wx.WXK_SPACE, modifiers=wx.MOD_NONE):
        focus(control)
        name = control.GetName()
        if key == wx.WXK_SPACE:
            check('Space key down: ' + name, simulator.KeyDown(key, modifiers))
        else:
            check('native key down: ' + name, simulator.KeyDown(key, modifiers))
        pressed.add((key, modifiers))
        # Schedule release before dispatch can enter a nested ShowModal loop.
        wx.CallLater(40, lambda: release(key, modifiers, name))
        gui.Yield()
        pump(lambda: (key, modifiers) not in pressed, 'Native clip key was not released', timeout=10)
        gui.Yield()

    def descendants(window):
        pending, result = list(window.GetChildren()), []
        while pending:
            if len(result) >= 128:
                raise AssertionError('Clip modal descendants exceed their fixed bound.')
            current = pending.pop(0)
            result.append(current)
            pending.extend(current.GetChildren())
        return result

    def current_modal(title):
        matches = []
        for window in wx.GetTopLevelWindows():
            if (isinstance(window, wx.Dialog) and window.GetTitle() == title
                    and window.GetParent() is dialog and window.IsModal()
                    and user32.IsWindow(window.GetHandle())):
                owner_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(window.GetHandle(), ctypes.byref(owner_pid))
                if owner_pid.value == os.getpid():
                    matches.append(window)
        if len(matches) > 1:
            raise AssertionError('Ambiguous current clip modal: ' + title)
        if not matches:
            return None
        window = matches[0]
        loop = wx.EventLoopBase.GetActive()
        if loop is None or not loop.IsRunning() or loop.IsMain():
            raise AssertionError('Clip modal has no actual running child loop: ' + title)
        if not any(item[0] is window for item in modal_loops):
            modal_loops.append((window, window.GetHandle(), loop))
        return window

    def choose(control, label):
        focus(control)
        # UIActionSimulator.Select itself sends real keyboard events and
        # processes their native notifications; cached modal results are not read.
        check('native choice keyboard input: ' + control.GetName(), simulator.Select(label))
        pump(lambda: control.GetStringSelection() == label,
             'Native clip choice did not select the exact live label', timeout=10)

    def enter(control, value):
        press(control, ord('A'), wx.MOD_CONTROL)
        check('native text keyboard input: ' + control.GetName(),
              simulator.Text(value.encode('ascii')))
        pump(lambda: control.GetValue() == value, 'Native clip text did not match typed input', timeout=10)

    def modal_sequence(trigger, steps):
        state = {'index': 0, 'finished': False}
        deadline = monotonic() + 45
        def visit():
            if state['finished']:
                return
            if monotonic() >= deadline:
                raise TimeoutError('Native clip modal sequence did not finish.')
            title, kind, value = steps[state['index']]
            window = current_modal(title)
            if window is None or pressed:
                wx.CallLater(25, guarded(visit))
                return
            if kind == 'choice':
                controls = [item for item in descendants(window) if isinstance(item, wx.ListBox)]
                if len(controls) != 1:
                    raise AssertionError('Expected one actual clip choice list: ' + title)
                selected = value(controls[0]) if callable(value) else value
                label = controls[0].GetString(selected) if type(selected) is int else selected
                choose(controls[0], label)
            elif kind == 'text':
                controls = [item for item in descendants(window) if isinstance(item, wx.TextCtrl)]
                if len(controls) != 1:
                    raise AssertionError('Expected one actual clip text entry: ' + title)
                enter(controls[0], value)
            elif kind != 'cancel':
                raise AssertionError('Unknown fixed clip modal operation.')
            button = wx.Window.FindWindowById(wx.ID_CANCEL if kind == 'cancel' else wx.ID_OK, window)
            if button is None:
                raise AssertionError('Expected the actual modal confirmation button.')
            state['index'] += 1
            if state['index'] == len(steps):
                state['finished'] = True
            else:
                # Admit the next child only after the current confirmation yields.
                wx.CallLater(25, guarded(visit))
            press(button)
        wx.CallLater(25, guarded(visit))
        trigger()
        pump(lambda: state['finished'] and not pressed and not any(
                 window.IsModal() for window, handle, loop in modal_loops
                 if user32.IsWindow(handle)),
             'Native clip modal sequence remains active', timeout=45)
        pump(lambda: not frame.working and not dialog.pending,
             'Native clip modal operation did not settle', timeout=45)

    def observe_destroy(event):
        # Match the actual external event/window IDs. wx may expose a base
        # wrapper after a top-level Python handler has already disappeared.
        if event.GetEventType() == wx.EVT_WINDOW_DESTROY.typeId:
            window = event.GetWindow()
            for identifier, handle, label in ((host_id, host_handle, 'host'),
                                               (dialog_id, dialog_handle, 'clip')):
                if (identifier is not None and event.GetId() == identifier
                        and window is not None and window.GetId() == identifier):
                    destroyed.add((label, handle))
        return wx.EventFilter.Event_Skip

    class ClipDestroyObserver(wx.EventFilter):
        def FilterEvent(self, event):
            return observe_destroy(event)

    def passive_output(player):
        return (player.state == 'stopped' and not player._open
                and player._wave is None and player._process is None
                and not player.queue_active)

    def begin_shutdown():
        if closing['scheduled'] or closing['complete'] or closing['stalled']:
            return
        closing['scheduled'] = True
        if closing['deadline'] is None:
            closing['deadline'] = monotonic() + 45
        wx.CallLater(25, shutdown_tick)

    def shutdown_tick():
        closing['scheduled'] = False
        try:
            if monotonic() >= closing['deadline']:
                raise TimeoutError('Actual clip modal, host or shared worker did not confirm Close.')
            if frame is not None:
                frame.cancel_event.set()
            if dialog is not None:
                dialog.cancelled.set()
            for key, modifiers in tuple(pressed):
                release(key, modifiers, 'owned clip cleanup')
            # Failure cleanup closes only a captured, currently running owned
            # modal. A later timer turn resumes its parent before another close.
            for window, handle, loop in reversed(modal_loops):
                if (user32.IsWindow(handle) and window.IsModal() and loop.IsRunning()
                        and wx.EventLoopBase.GetActive() is loop):
                    window.EndModal(wx.ID_CANCEL)
                    begin_shutdown()
                    return
            if ((frame is not None and frame.working)
                    or (dialog is not None and dialog.pending)):
                begin_shutdown()
                return
            if dialog is not None and user32.IsWindow(dialog_handle):
                if not dialog.closed:
                    dialog.close()
                if not dialog.closed:
                    begin_shutdown()
                    return
                if not closing['dialog_destroy']:
                    closing['dialog_destroy'] = True
                    dialog.Destroy()
                begin_shutdown()
                return
            if frame is not None and user32.IsWindow(host_handle):
                if not closing['host_close']:
                    closing['host_close'] = True
                    frame.Close()
                begin_shutdown()
                return
            check('actual clip and host Destroy events precede original loop exit',
                  ('clip', dialog_handle) in destroyed and ('host', host_handle) in destroyed)
            check('actual clip and host native windows are closed',
                  not user32.IsWindow(dialog_handle) and not user32.IsWindow(host_handle))
            closing['complete'] = True
            gui.ExitMainLoop()
        except BaseException:
            detail = traceback.format_exc()
            errors.append(detail)
            print('FAIL: native clip cleanup\n' + detail, file=sys.stderr, flush=True)
            closing['stalled'] = True
            # Retain the original event loop, fixtures, temporary directory,
            # runtime DLL handle and worker owners until the bounded watchdog.

    check('real wx Windows controls', wx.Platform == '__WXMSW__')
    for module in (ui, clips):
        check('installed module: ' + module.__name__,
              'site-packages' in [part.casefold() for part in Path(module.__file__).resolve().parts])
    actual_picker = wx.FileDialog
    actual_directory_picker = wx.DirDialog
    with TemporaryDirectory(prefix='carr-native-clips-') as temporary:
        root = Path(temporary).resolve(strict=True)
        source = root / 'Synthetic original — Україна.wav'
        values = [round(10000 * math.sin(2 * math.pi * 400 * index / 24000))
                  if index >= 12000 else 0 for index in range(48000)]
        with wave.open(str(source), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(struct.pack('<' + 'h' * len(values), *values))
        original = sha256(source.read_bytes()).hexdigest()
        outputs = root / 'rendered'
        outputs.mkdir()
        class SelectedOutput:
            def __init__(self, *args, **kwargs):
                pass
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(outputs)
            def Destroy(self): pass
        class SelectedPath:
            def __init__(self, *args, **kwargs):
                check('original selection requires an existing file',
                      kwargs.get('style') == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(source)
            def Destroy(self): pass
        wx.FileDialog = SelectedPath
        wx.DirDialog = SelectedOutput
        try:
            destroy_observer = ClipDestroyObserver()
            wx.EvtHandler.AddFilter(destroy_observer)
            frame = DesktopFrame(LibraryApp(root / 'private library'))
            frame.Show()
            gui.SetTopWindow(frame)
            host_executor = frame.executor
            host_handle, host_id = frame.GetHandle(), frame.GetId()
            def external_runtime(directory, *, input_roots=()):
                return AudioRuntime(directory, input_roots=input_roots,
                    ffmpeg=str(selected_audio / 'bin' / 'ffmpeg.exe'),
                    ffprobe=str(selected_audio / 'bin' / 'ffprobe.exe'))
            service = ProductionService(frame.library.root / 'state' / 'studio',
                                        runtime_factory=external_runtime)
            editor = clips.ClipEditor(frame.library.root, service=service)
            verify_audio(editor.service._runtime())
            project = editor.create('Native scene clips')
            dialog = ui.ClipDialog(frame, editor=editor)
            dialog.Show()
            dialog_handle, dialog_id = dialog.GetHandle(), dialog.GetId()
            check('clip and host have distinct captured native window IDs',
                  host_id != dialog_id and host_handle != dialog_handle)
            def scenario():
                nonlocal passive_player, passive_path
                try:
                    check('real native main loop is active', gui.GetMainLoop() is not None)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Loading project')
                    check('native project choice binds saved identity', dialog.project.id == project.id)
                    check('all timing controls have readable names', all(
                        isinstance(control, wx.TextCtrl) and control.GetName() for control in dialog.fields.values()))
                    press(dialog.buttons['add'])
                    pump(lambda: not frame.working and not dialog.pending, 'Retaining original')
                    check('native Add retains an actual clip', len(dialog.project.scenes[0].cues) == 1)
                    declaration = dialog.project.scenes[0].cues[0].path
                    check('native Add retains a portable content identity',
                          dialog.project.schema == 2 and declaration == 'inputs/' + original + '.audio')
                    retained = resolve_managed_input(editor.service.root, declaration)
                    for name, value in {'trim_start': '.5', 'duration': '.75', 'start': '.25',
                                        'pan': '-1', 'fade_in': '.1', 'fade_out': '.1'}.items():
                        dialog.fields[name].ChangeValue(value)
                    press(dialog.buttons['save'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving clip settings')
                    edited = editor.service.projects.load(project.id)
                    cue = edited.scenes[0].cues[0]
                    check('native Save persists reviewed numeric controls',
                          (edited.revision, cue.trim_start, cue.duration, cue.start, cue.pan) == (3, .5, .75, .25, -1))
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Rendering from the native clip editor')
                    results = editor.service.jobs()
                    check('native Render creates exactly one completed saved project job',
                          len(results) == 1 and results[0]['status'] == 'complete'
                          and results[0]['fingerprint'] == edited.fingerprint)
                    check('native render completion exposes its destination in readable status',
                          results[0]['destination'] in dialog.status.GetValue()
                          and wx.Window.FindFocus() is dialog.status)
                    with wave.open(str(Path(results[0]['destination']) / 'master.wav'), 'rb') as stream:
                        count, channels = stream.getnframes(), stream.getnchannels()
                        samples = struct.unpack('<' + 'h' * count * channels, stream.readframes(count))
                        check('actual left-only timeline and one-second duration', count == 24000
                              and channels == 2 and max(map(abs, samples[1::2])) == 0
                              and max(map(abs, samples[::2])) > 1000)
                    newer = editor.scene_duration(edited, edited.scenes[0].id, 2)
                    dialog.fields['pan'].ChangeValue('1')
                    press(dialog.buttons['save'])
                    pump(lambda: not frame.working and not dialog.pending, 'Rejecting stale clip edit')
                    check('stale native edit cannot replace a newer revision',
                          'changed since' in dialog.status.GetValue() and editor.service.projects.load(project.id) == newer)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Reloading current clip')
                    press(dialog.buttons['remove'])
                    pump(lambda: not frame.working and not dialog.pending, 'Removing clip')
                    check('remove changes project but retains original bytes', not dialog.project.scenes[0].cues
                          and sha256(retained.read_bytes()).hexdigest() == original
                          and sha256(source.read_bytes()).hexdigest() == original)
                    # Two actual native Add operations prepare an adjacent pair.
                    # Numeric fixture setup is independent of the native modals.
                    for trim, start in (('0', '0'), ('1', '1')):
                        for name, value in {'trim_start': trim, 'duration': '1',
                                            'start': start, 'gain_db': '0', 'pan': '-1',
                                            'fade_in': '0', 'fade_out': '0'}.items():
                            dialog.fields[name].ChangeValue(value)
                        press(dialog.buttons['add'])
                        pump(lambda: not frame.working and not dialog.pending,
                             'Retaining the native crossfade pair')
                    pair = dialog.project
                    check('native pair setup retains two exact original selections',
                          len(pair.scenes[0].cues) == 2
                          and tuple((item.path, item.trim_start, item.duration, item.start)
                                    for item in pair.scenes[0].cues)
                          == ((declaration, 0, 1, 0), (declaration, 1, 1, 1)))
                    # A typed silent later scene makes unintended ripple observable.
                    later = Scene('later-native-scene', 'Later fixed scene', (), minimum_duration=.5)
                    paired = replace(pair, revision=pair.revision + 1, scenes=(*pair.scenes, later))
                    editor.service.projects.save(paired, expected_revision=pair.revision,
                                                 expected_fingerprint=pair.fingerprint)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Reloading native crossfade pair')
                    choose(dialog.clip_choice, dialog.clip_choice.GetString(0))
                    first, second = paired.scenes[0].cues
                    modal_sequence(lambda: press(dialog.buttons['timeline']), (
                        ('Timeline editing and project history', 'choice', 9),
                        ('Crossfade saved clips', 'choice', 0),
                        ('Crossfade overlap', 'text', '.25'),
                    ))
                    faded = editor.service.projects.load(project.id)
                    changed_first, changed_second = faded.scenes[0].cues
                    check('native crossfade saves exactly one paired project revision',
                          dialog.project == faded and faded.revision == paired.revision + 1
                          and changed_first == replace(first, fade_out=.25)
                          and changed_second == replace(second, start=.75, fade_in=.25))
                    check('native crossfade preserves retained originals and later scene placement',
                          faded.scenes[0].minimum_duration == 2 and faded.scenes[1] == later
                          and sha256(retained.read_bytes()).hexdigest() == original
                          and sha256(source.read_bytes()).hexdigest() == original)
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Rendering actual native crossfade')
                    matching = [row for row in editor.service.jobs()
                                if row['fingerprint'] == faded.fingerprint and row['status'] == 'complete']
                    check('native crossfade renders the exact saved project', len(matching) == 1)
                    crossfade_output = Path(matching[0]['destination']) / 'master.wav'
                    with wave.open(str(crossfade_output), 'rb') as stream:
                        crossfade_shape = (stream.getframerate(), stream.getnchannels(),
                                           stream.getsampwidth(), stream.getnframes())
                        crossfade_pcm = stream.readframes(stream.getnframes())
                    crossfade_samples = struct.unpack('<' + 'h' * (len(crossfade_pcm) // 2), crossfade_pcm)
                    check('actual crossfade PCM keeps old scene extent and later silence',
                          crossfade_shape == (24000, 2, 2, 60000)
                          and max(map(abs, crossfade_samples[1::2])) == 0
                          and max(map(abs, crossfade_samples[18000*2:24000*2:2])) > 1000
                          and not any(crossfade_samples[42000*2:]))

                    modal_sequence(lambda: press(dialog.buttons['timeline']), (
                        ('Timeline editing and project history', 'choice', 10),
                        ('Named scene markers', 'choice', 0),
                        ('Scene marker position', 'text', '.5'),
                        ('Scene marker name', 'text', 'Native cue A'),
                    ))
                    marked = editor.service.projects.load(project.id)
                    check('native marker Add saves exact scene-relative name and time',
                          dialog.project == marked and marked.schema == 4
                          and marked.revision == faded.revision + 1 and len(marked.markers) == 1
                          and (marked.markers[0].scene_id, marked.markers[0].position,
                               marked.markers[0].label) == (marked.scenes[0].id, .5, 'Native cue A')
                          and marked.scenes == faded.scenes)
                    saved_marker = marked.markers[0]
                    modal_sequence(lambda: press(dialog.buttons['timeline']), (
                        ('Timeline editing and project history', 'choice', 10),
                        ('Named scene markers', 'choice', 'Native cue A; 0.5 seconds'),
                        ('Edit named scene marker', 'choice', 0),
                        ('Scene marker position', 'text', '.75'),
                        ('Scene marker name', 'text', 'Native cue revised'),
                    ))
                    revised = editor.service.projects.load(project.id)
                    check('native marker Edit retains exact selected identity without moving audio',
                          revised.revision == marked.revision + 1
                          and revised.markers == (replace(saved_marker, position=.75,
                                                        label='Native cue revised'),)
                          and revised.scenes == faded.scenes)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Reloading saved native markers')
                    check('native marker reload retains the complete typed project', dialog.project == revised)
                    history_label = f'Revision {marked.revision}; identity {marked.fingerprint[:12]}'
                    modal_sequence(lambda: press(dialog.buttons['timeline']), (
                        ('Timeline editing and project history', 'choice', 5),
                        ('Saved project history', 'choice', history_label),
                    ))
                    restored = editor.service.projects.load(project.id)
                    check('native project history restores markers as a fresh revision',
                          dialog.project == restored and restored.revision == revised.revision + 1
                          and restored.markers == marked.markers and restored.scenes == faded.scenes)
                    modal_sequence(lambda: press(dialog.buttons['timeline']), (
                        ('Timeline editing and project history', 'choice', 10),
                        ('Named scene markers', 'choice', 'Native cue A; 0.5 seconds'),
                        ('Edit named scene marker', 'choice', 1),
                    ))
                    removed_marker = editor.service.projects.load(project.id)
                    check('native marker Remove changes one revision and retains scene audio',
                          dialog.project == removed_marker and removed_marker.schema == 4
                          and removed_marker.revision == restored.revision + 1
                          and removed_marker.markers == () and removed_marker.scenes == faded.scenes)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Reloading removed native marker')
                    check('native removed marker stays removed after reload',
                          dialog.project == removed_marker and dialog.project.markers == ())
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending,
                         'Rendering after native marker history and removal')
                    matching = [row for row in editor.service.jobs()
                                if row['fingerprint'] == removed_marker.fingerprint and row['status'] == 'complete']
                    check('native marker render binds the current saved revision', len(matching) == 1)
                    with wave.open(str(Path(matching[0]['destination']) / 'master.wav'), 'rb') as stream:
                        marker_shape = (stream.getframerate(), stream.getnchannels(),
                                        stream.getsampwidth(), stream.getnframes())
                        marker_pcm = stream.readframes(stream.getnframes())
                    check('marker edits and history leave actual rendered PCM and original bytes unchanged',
                          marker_shape == crossfade_shape and marker_pcm == crossfade_pcm
                          and sha256(retained.read_bytes()).hexdigest() == original
                          and sha256(source.read_bytes()).hexdigest() == original)

                    # The actual shared worker renders this expressly passive
                    # preview. Public playlist validation admits it silently.
                    # Neither Play nor any output device is invoked here.
                    choose(dialog.clip_choice, dialog.clip_choice.GetString(0))
                    selected_project, selected_scene, selected_cue = (
                        dialog.project, dialog._scene(), dialog._clip())
                    passive = {}
                    def prepared(path):
                        player = WavePlayer()
                        player.set_playlist(WavePlayer.prepare_playlist([path]))
                        player.select_queue(0)
                        dialog.player = player
                        progress = getattr(frame, 'progress_panel', None)
                        if progress is not None:
                            progress.register_player(player)
                        dialog._preview_binding = (
                            player, player.path, selected_project, selected_scene.id, selected_cue.id)
                        dialog._enable()
                        passive.update(player=player, path=path)
                    check('passive loop fixture uses the original shared preview worker',
                          dialog._run('Preparing passive preview controls',
                              lambda: editor.preview(selected_project, selected_scene.id, selected_cue.id,
                                                     cancelled=dialog._cancelled),
                              prepared, preview=True))
                    pump(lambda: not frame.working and not dialog.pending,
                         'Rendering actual passive loop preview')
                    passive_player, passive_path = passive['player'], passive['path']
                    check('actual passive preview retains its exact path without opening output',
                          passive_path in dialog.previews and passive_path.exists()
                          and passive_player.path == passive_path and passive_output(passive_player)
                          and dialog._preview_binding[2] is selected_project)
                    modal_sequence(lambda: press(dialog.buttons['preview_repeat']), (
                        ('Clip preview repeat', 'cancel', None),
                    ))
                    check('native repeat chooser Cancel keeps passive audio and project unchanged',
                          passive_output(passive_player) and passive_player.ab_loop is None
                          and editor.service.projects.load(project.id) == selected_project)
                    modal_sequence(lambda: press(dialog.buttons['preview_repeat']), (
                        ('Clip preview repeat', 'choice', 0),
                        ('Preview repeat A', 'text', '.1'),
                        ('Preview repeat B', 'cancel', None),
                    ))
                    check('native repeat range Cancel never starts the selected preview',
                          passive_output(passive_player) and passive_player.ab_loop is None
                          and editor.service.projects.load(project.id) == selected_project)
                    passive_player.set_ab_loop(.1, .5)
                    dialog._start_preview_repeat_timer()
                    check('passive loop fixture uses actual silent A-B state and native timer',
                          passive_player.ab_loop == (.1, .5) and passive_output(passive_player)
                          and dialog._preview_repeat_timer.IsRunning())
                    modal_sequence(lambda: press(dialog.buttons['preview_repeat']), (
                        ('Clip preview repeat', 'choice', 1),
                    ))
                    pump(lambda: passive_player.ab_loop is None
                         and not dialog._preview_repeat_timer.IsRunning()
                         and dialog._preview_repeat_pending is None,
                         'Clearing the passive preview repeat')
                    check('native Clear repeat is silent and retains the owned preview',
                          passive_output(passive_player) and passive_path.exists()
                          and editor.service.projects.load(project.id) == selected_project)
                    passive_player.set_ab_loop(.1, .5)
                    dialog._start_preview_repeat_timer()
                    generation = dialog.playback_generation
                    press(dialog.buttons['stop'])
                    check('native Stop revokes repeat generation before passive owner release',
                          dialog.playback_generation > generation and dialog._preview_binding is None
                          and dialog._preview_repeat_pending is None
                          and not dialog._preview_repeat_timer.IsRunning()
                          and passive_player.ab_loop is None and passive_output(passive_player)
                          and passive_path.exists())
                    press(dialog.buttons['close'])
                    check('native keyboard close finishes cleanly', dialog.closed)
                    check('native Close discards preview only after confirmed passive release',
                          passive_output(passive_player) and not passive_path.exists()
                          and not dialog._preview_repeat_timer.IsRunning())
                except BaseException:
                    remember_failure()
                finally:
                    begin_shutdown()
            wx.CallAfter(scenario)
            gui.MainLoop()
        except BaseException:
            detail = traceback.format_exc()
            if detail not in errors:
                errors.append(detail)
                print('FAIL: native clip setup or loop\n' + detail, file=sys.stderr, flush=True)
        finally:
            # A setup failure also settles the same real host in an active loop.
            if frame is not None and not closing['complete']:
                begin_shutdown()
                gui.MainLoop()
            if host_executor is not None:
                try:
                    host_executor.shutdown(wait=True, cancel_futures=True)
                    check('original clip host executor drains without retained cleanup failure',
                          closing['complete'] and not host_executor.cleanup_failures)
                except BaseException:
                    errors.append(traceback.format_exc())
            if service is not None:
                try:
                    service.close()
                    from carr_library.operations import ResourceBroker
                    check('clip render and preview resource leases are released',
                          not ResourceBroker.for_app(root / 'private library').snapshot()['leases'])
                except BaseException:
                    errors.append(traceback.format_exc())
            if passive_player is not None:
                try:
                    check('passive loop owner remains silent after actual host settlement',
                          passive_output(passive_player)
                          and (passive_path is None or not passive_path.exists()))
                except BaseException:
                    errors.append(traceback.format_exc())
            # Retire fixture routes and DLLs only after the actual host/worker.
            # A stalled native owner retains the original loop until watchdog.
            wx.FileDialog = actual_picker
            wx.DirDialog = actual_directory_picker
            if destroy_observer is not None:
                wx.EvtHandler.RemoveFilter(destroy_observer)
            if native_handle is not None:
                native_handle.close()
        if errors:
            raise RuntimeError('\n'.join(errors))
    for module in modules:
        actual = Path(module.__file__).resolve(strict=True)
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('unchanged installed source after clip work: ' + module.__name__,
              actual.is_relative_to(Path(sys.prefix).resolve()) and not actual.is_relative_to(checkout)
              and sha256(actual.read_bytes()).hexdigest() == source_digests[module.__name__]
              and sha256(expected.read_bytes()).hexdigest() == source_digests[module.__name__])
    _cleanup_complete.set()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'native_keyboard': 'PASS',
        'fixture': 'synthetic PCM rendered by actual FFmpeg', 'audio_runtime': audio_proof,
        'module_origins': module_origins, 'native_crossfade': 'PASS',
        'native_scene_markers': 'PASS', 'native_loop_cancel_clear_stop_close': 'PASS',
        'native_owner_cleanup': 'PASS', 'loop_fixture': 'actual passive WavePlayer; no output opened',
        'repeat_playback': 'NOT_RUN', 'physical_audio_output': 'NOT_RUN',
        'listening_quality': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'native_file_pickers': 'NOT_RUN', 'physical_microphone': 'NOT_RUN'}), flush=True)
    return 0


if __name__ == '__main__':
    import faulthandler
    faulthandler.enable(all_threads=True)

    def watchdog():
        if not _cleanup_complete.wait(300):
            os.write(2, b'FAIL: native clip acceptance or owned cleanup exceeded 300 seconds.\n')
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            os._exit(86)

    threading.Thread(target=watchdog, daemon=True, name='clip-native-watchdog').start()
    raise SystemExit(main())
