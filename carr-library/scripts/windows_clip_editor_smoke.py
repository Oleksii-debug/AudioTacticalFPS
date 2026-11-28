"""Installed native clip controls plus real FFmpeg rendering of synthetic PCM.

The probe supplies an explicit file-picker result and authored text/choice
values, then real Windows keyboard events activate the native buttons. Actual
FFmpeg renders every preview; an explicitly synthetic clock transport exercises
the existing loop controller without claiming audio-device or listening output.
Physical recording devices, native file pickers and NVDA are separate gates.
"""
import argparse
import faulthandler
from hashlib import sha256
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


def _acceptance_bindings():
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_media_acceptance_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    sys.stdout.flush()
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
    sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, required=True)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows session required', 'nvda': 'NOT_RUN'}))
        return 77
    if not arguments.native_child:
        return _run_owned_native(arguments)
    faulthandler.dump_traceback_later(330, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run_native(arguments)
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native(arguments):
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.studio import clips, project as project_module, render as render_module
    from carr_library.audio import dsp as dsp_module
    from carr_library.speech import player as player_module
    from carr_library.ui import playback_navigation as navigation_module, effects_dialog
    from carr_library.ui import studio_clips as ui
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, audio_proof, observed = [], [], {}, {}
    frame = dialog = None
    failure_handler = None
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    binding = _acceptance_bindings()
    def verify_audio(runtime):
        checked = binding.verify_audio_evidence(runtime, arguments.audio_evidence)
        if audio_proof and audio_proof != checked:
            raise AssertionError('External audio executable identity changed during native acceptance.')
        audio_proof.update(checked)

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
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    def pump(predicate, description, timeout=45):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() > deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)
    def press(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(),
             'Clip control did not receive foreground keyboard focus', timeout=10)
        check('keyboard focus: ' + control.GetName(), wx.Window.FindFocus() is control)
        check('Space key down: ' + control.GetName(), simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            sleep(.04)
        finally:
            check('Space key up: ' + control.GetName(), simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()

    def report_failure(detail):
        errors.append(detail)
        print('FAIL: native clip controls\n' + detail, file=sys.stderr, flush=True)

    def current_owners():
        owners = []
        if frame is None or not frame:
            return ()
        frame_id = frame.GetId()
        for window in wx.GetTopLevelWindows():
            if not window or not isinstance(window, wx.Dialog):
                continue
            parent, depth = window, 0
            while parent is not None and depth < 32:
                if parent.GetId() == frame_id:
                    # The helper keeps a hidden modal as a return barrier until
                    # its actual ShowModal loop has finished unwinding.
                    owners.append((depth, SimpleNamespace(dialog=window,
                        close=lambda peer=window: peer.EndModal(wx.ID_CANCEL))))
                    break
                parent, depth = parent.GetParent(), depth + 1
        return tuple(owner for _, owner in sorted(owners, key=lambda item: item[0], reverse=True))

    abort = _OwnedModalAbort(wx, gui, current_owners, report_failure)

    def abort_modals():
        abort.request()

    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value

    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)

    def answer_native_dialog(title, value):
        """Enter test data into the real modal dialog, then use its native OK."""
        deadline = monotonic() + 8
        def answer():
            try:
                matching = [window for window in wx.GetTopLevelWindows()
                            if window.IsShown() and window.GetTitle() == title]
                if not matching:
                    if monotonic() >= deadline:
                        raise TimeoutError('The expected native clip dialog did not open: ' + title)
                    wx.CallLater(20, answer)
                    return
                popup, = matching
                if isinstance(popup, wx.TextEntryDialog):
                    popup.SetValue(str(value))
                elif isinstance(popup, wx.SingleChoiceDialog):
                    popup.SetSelection(value)
                else:
                    raise AssertionError('Unexpected native clip dialog type')
                button = wx.Window.FindWindowById(wx.ID_OK, popup)
                check('actual native clip review button: ' + title, isinstance(button, wx.Button))
                press(button)
            except Exception:
                errors.append(traceback.format_exc())
                abort_modals()
        wx.CallLater(20, answer)


    def answer_effect_chain(gain):
        deadline = monotonic() + 20
        def failed():
            errors.append(traceback.format_exc())
            abort_modals()

        def edit_numeric():
            try:
                matching = [window for window in wx.GetTopLevelWindows()
                            if window.IsShown() and window.GetTitle() == 'Edit gain effect']
                if not matching:
                    if monotonic() >= deadline:
                        raise TimeoutError('Native gain editor did not open.')
                    wx.CallLater(20, edit_numeric)
                    return
                popup, = matching
                control = wx.Window.FindWindowByName('gain db (dB); -120 to 24', popup)
                check('actual numeric clip effect control', isinstance(control, wx.TextCtrl))
                control.ChangeValue(str(gain))
                press(wx.Window.FindWindowByName('Apply effect', popup))
            except Exception:
                failed()

        def answer():
            try:
                matching = [window for window in wx.GetTopLevelWindows()
                            if window.IsShown() and window.GetTitle() == 'Ordered audio effects']
                if not matching:
                    if monotonic() >= deadline:
                        raise TimeoutError('Native ordered effects did not open.')
                    wx.CallLater(20, answer)
                    return
                popup, = matching
                kind = wx.Window.FindWindowByName('Effect type to add', popup)
                check('actual supported effect choice', isinstance(kind, wx.Choice)
                      and kind.SetStringSelection('gain'))
                press(wx.Window.FindWindowByName('Add effect', popup))
                wx.CallLater(20, edit_numeric)
                press(wx.Window.FindWindowByName('Edit selected effect', popup))
                press(wx.Window.FindWindowByName('Apply effect chain', popup))
            except Exception:
                failed()
        wx.CallLater(20, answer)


    class ClockedTransport:
        """Explicit test-only player protocol, never an audio-device fallback."""
        position_is_approximate = True
        backend = 'Synthetic clock fixture (no audio output)'
        def __init__(self):
            self.path, self.duration_seconds, self._state = None, 0.0, 'stopped'
            self._offset, self._started, self.closed = 0.0, None, False
            self.seeks = []; observed['player'] = self
        @property
        def state(self):
            if self._state == 'playing' and self.position_seconds >= self.duration_seconds:
                self._state, self._offset, self._started = 'stopped', self.duration_seconds, None
            return self._state
        @property
        def position_seconds(self):
            elapsed = monotonic() - self._started if self._state == 'playing' and self._started is not None else 0
            return min(self.duration_seconds, self._offset + elapsed)
        def play(self, path, *, start_seconds=0):
            with wave.open(str(path), 'rb') as stream:
                check('fixture receives actual published nonempty PCM', stream.getcomptype() == 'NONE'
                      and stream.getnframes() > 0)
                self.duration_seconds = stream.getnframes() / stream.getframerate()
            self.path, self._offset, self._started, self._state = Path(path), start_seconds, monotonic(), 'playing'
        def pause(self):
            if self.state != 'playing': return False
            self._offset, self._state = self.position_seconds, 'paused'; return True
        def resume(self):
            if self.state != 'paused': return False
            self._started, self._state = monotonic(), 'playing'; return True
        def seek(self, seconds):
            paused = self.state == 'paused'
            self.seeks.append(seconds); self.play(self.path, start_seconds=seconds)
            if paused: self.pause()
        def stop(self): self._state, self._offset, self._started = 'stopped', 0.0, None
        def close(self): self.stop(); self.closed = True


    check('real wx Windows controls', wx.Platform == '__WXMSW__')
    source_modules = (ui, clips, project_module, render_module, dsp_module, player_module,
                      navigation_module, effects_dialog)
    source_identity = binding.verify_installed_sources(*source_modules)
    check('exact current installed clip source', bool(source_identity))
    actual_picker = wx.FileDialog
    original_player = player_module.WavePlayer
    frame = dialog = None
    with TemporaryDirectory(prefix='carr-native-clips-') as temporary:
        root = Path(temporary)
        source = root / 'Synthetic original — Україна.wav'
        values = [round(10000 * math.sin(2 * math.pi * 400 * index / 24000))
                  if index >= 12000 else 0 for index in range(48000)]
        with wave.open(str(source), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(struct.pack('<' + 'h' * len(values), *values))
        original = sha256(source.read_bytes()).hexdigest()
        class SelectedPath:
            def __init__(self, *args, **kwargs):
                check('original selection requires an existing file',
                      kwargs.get('style') == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(source)
            def Destroy(self): pass
        wx.FileDialog = SelectedPath
        try:
            player_module.WavePlayer = ClockedTransport
            frame = DesktopFrame(LibraryApp(root / 'private library'))
            frame.Show()
            gui.SetTopWindow(frame)
            editor = clips.ClipEditor(frame.library.root)
            proof_runtime = editor.service._runtime()
            verify_audio(proof_runtime)
            project = editor.create('Native scene clips')
            dialog = ui.ClipDialog(frame, editor=editor)
            dialog.Show()
            def lifecycle_checks():
                class NativeParent(wx.Frame):
                    @property
                    def working(self):
                        return frame.working
                for destruction in ('direct', 'native-parent'):
                    parent = NativeParent(frame, title='Owned clip lifetime fixture')
                    parent.library = frame.library
                    parent.cancel_event = frame.cancel_event
                    parent.operation_progress = frame.operation_progress
                    parent.run_job, parent.report = frame.run_job, frame.report
                    parent.Show()
                    selected_editor = clips.ClipEditor(frame.library.root)
                    project = selected_editor.create('Owned preview lifetime')
                    project = selected_editor.add(project, project.scenes[0].id, source,
                                                   trim_start=.5, duration=.25)
                    selected = ui.ClipDialog(parent, editor=selected_editor)
                    selected.Show()
                    selected._accept(project)
                    native_id, events = selected._native_id, []
                    def native_destroy(event, events=events):
                        events.append(event.GetId())
                        event.Skip()
                    selected.Bind(wx.EVT_WINDOW_DESTROY, native_destroy)
                    ready, release, completed_paths = threading.Event(), threading.Event(), []
                    original_preview = selected_editor.preview
                    def held_preview(*args, **kwargs):
                        # Actual installed ClipEditor/FFmpeg creates and verifies
                        # its preview before this test-only completion barrier.
                        path = original_preview(*args, **kwargs)
                        completed_paths.append(path)
                        ready.set()
                        if not release.wait(20):
                            raise TimeoutError('Owned preview completion barrier was not released')
                        return path
                    selected_editor.preview = held_preview
                    try:
                        press(selected.buttons['preview'])
                        pump(ready.is_set, 'Actual preview did not reach native lifetime barrier')
                        check('actual verified preview is pending before ' + destruction + ' destruction',
                              selected.pending and frame.working and completed_paths[0].is_file())
                        if destruction == 'direct':
                            selected.Destroy()
                        else:
                            parent.DestroyChildren()
                        pump(lambda: not bool(selected), 'Native clip wrapper was not deleted', timeout=10)
                        check('real cached-ID clip destruction latches cancellation: ' + destruction,
                              events.count(native_id) == 1 and selected.closed and selected._cancelled()
                              and not parent.cancel_event.is_set())
                        selected.message('Late progress after native deletion')
                        release.set()
                        pump(lambda: not frame.working and not selected.pending,
                             'Actual preview worker completion did not release')
                        gui.Yield()
                        selected.message('Late report after actual completion')
                        check('owned late preview is cleaned without native access: ' + destruction,
                              not completed_paths[0].exists() and not selected.previews)
                        check('preview teardown preserves exact input: ' + destruction,
                              sha256(source.read_bytes()).hexdigest() == original
                              and Path(project.scenes[0].cues[0].path).read_bytes() == source.read_bytes())
                    finally:
                        release.set()
                        parent.cancel_event.set()
                        if frame.working:
                            pump(lambda: not frame.working, 'Owned fixture worker cleanup', timeout=25)
                        if selected:
                            selected.Destroy()
                        if parent:
                            parent.DestroyChildren()
                            parent.Destroy()
                        gui.Yield()


                for destruction in ('direct-loop', 'native-parent-loop'):
                    parent = NativeParent(frame, title='Owned clip repeat lifetime fixture')
                    parent.library, parent.cancel_event = frame.library, frame.cancel_event
                    parent.operation_progress = frame.operation_progress
                    parent.run_job, parent.report = frame.run_job, frame.report
                    parent.Show()
                    selected_editor = clips.ClipEditor(frame.library.root)
                    project = selected_editor.create('Owned repeat lifetime')
                    project = selected_editor.add(project, project.scenes[0].id, source,
                                                   trim_start=.5, duration=.5)
                    selected = ui.ClipDialog(parent, editor=selected_editor)
                    selected.Show()
                    selected._accept(project)
                    native_id, events, held, delivered, forbidden = selected._native_id, [], [], [], []
                    def destroyed(event, events=events):
                        events.append(event.GetId())
                        event.Skip()
                    selected.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
                    original_message = selected.message
                    def message(value):
                        if selected.closed:
                            forbidden.append('late repeat feedback')
                        else:
                            original_message(value)
                    selected.message = message
                    try:
                        press(selected.buttons['loop'])
                        pump(lambda: not frame.working and not selected.pending
                             and selected.navigation is not None and selected.navigation.controller.repeat,
                             'Native repeat did not reach active ownership')
                        player = selected.player
                        navigation = selected.navigation
                        dispatch = navigation.controller.dispatch
                        def hold(action, *, alive, on_error):
                            held.append((action, alive, on_error))
                        navigation.controller.dispatch = hold
                        pump(lambda: bool(held), 'Actual repeat callback was not captured', timeout=10)
                        before = len(player.seeks)
                        if destruction == 'direct-loop':
                            selected.Destroy()
                        else:
                            parent.DestroyChildren()
                        pump(lambda: not bool(selected) and events.count(native_id) == 1,
                             'Repeating clip native peer did not delete', timeout=10)
                        action, alive, failed = held.pop(0)
                        def replay():
                            dispatch(action, alive=alive, on_error=failed)
                            delivered.append(True)
                        wx.CallAfter(replay)
                        pump(lambda: bool(delivered), 'Actual late repeat callback was not delivered')
                        check('deleted native repeat owner rejects held callback: ' + destruction,
                              navigation.closed and selected.closed and len(player.seeks) == before
                              and player.state == 'stopped' and not forbidden)
                        check('repeat deletion preserves selected original bytes: ' + destruction,
                              sha256(source.read_bytes()).hexdigest() == original)
                    finally:
                        if frame.working:
                            parent.cancel_event.set()
                            pump(lambda: not frame.working, 'Repeat worker cleanup', timeout=25)
                        if selected:
                            selected.Destroy()
                        if parent:
                            parent.DestroyChildren()
                            parent.Destroy()
                        gui.Yield()

            def scenario():
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
                    retained = Path(dialog.project.scenes[0].cues[0].path)
                    for name, value in {'trim_start': '.5', 'duration': '.75', 'start': '.25',
                                        'pan': '-1', 'fade_in': '.1', 'fade_out': '.1'}.items():
                        dialog.fields[name].ChangeValue(value)
                    press(dialog.buttons['save'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving clip settings')
                    edited = editor.service.projects.load(project.id)
                    cue = edited.scenes[0].cues[0]
                    check('native Save persists reviewed numeric controls',
                          (edited.revision, cue.trim_start, cue.duration, cue.start, cue.pan) == (3, .5, .75, .25, -1))
                    outputs = root / 'rendered'; outputs.mkdir()
                    results = []
                    identity = editor.service.submit(edited, outputs)
                    frame.run_job('Native edited clip render', lambda: editor.service.run(identity), results.append,
                                  lambda error: errors.append(str(error)))
                    pump(lambda: not frame.working, 'Rendering native edited clip')
                    check('real edited project rendered', results and results[0]['status'] == 'complete')
                    with wave.open(str(Path(results[0]['destination']) / 'master.wav'), 'rb') as stream:
                        count, channels = stream.getnframes(), stream.getnchannels()
                        samples = struct.unpack('<' + 'h' * count * channels, stream.readframes(count))
                        check('actual left-only timeline and one-second duration', count == 24000
                              and channels == 2 and max(map(abs, samples[1::2])) == 0
                              and max(map(abs, samples[::2])) > 1000)
                    press(dialog.buttons['copy'])
                    pump(lambda: not frame.working and not dialog.pending, 'Copying verified clip')
                    check('native copy preserves the saved project', dialog.project == edited)
                    answer_native_dialog('Split selected clip', '.375')
                    press(dialog.buttons['split'])
                    pump(lambda: not frame.working and not dialog.pending, 'Splitting native clip')
                    check('native split preserves retained source and measured cuts',
                          len(dialog.project.scenes[0].cues) == 2
                          and {item.path for item in dialog.project.scenes[0].cues} == {cue.path}
                          and [item.duration for item in dialog.project.scenes[0].cues] == [.375, .375])
                    answer_native_dialog('Join adjacent cuts of the same original', 0)
                    press(dialog.buttons['join'])
                    pump(lambda: not frame.working and not dialog.pending, 'Joining native cuts')
                    check('native join restores the original instructions', dialog.project.scenes[0].cues == (cue,))
                    press(dialog.buttons['undo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Undoing native join')
                    check('native undo restores both cuts', len(dialog.project.scenes[0].cues) == 2)
                    press(dialog.buttons['redo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Redoing native join')
                    check('native redo restores the joined clip', dialog.project.scenes[0].cues == (cue,))
                    answer_native_dialog('Paste copied clip', '1.25')
                    press(dialog.buttons['paste'])
                    pump(lambda: not frame.working and not dialog.pending, 'Pasting native clip')
                    check('native paste creates a distinct clip at the reviewed position',
                          len(dialog.project.scenes[0].cues) == 2
                          and dialog.project.scenes[0].cues[1].start == 1.25
                          and dialog.project.scenes[0].cues[1].id != cue.id)
                    press(dialog.buttons['cut'])
                    pump(lambda: not frame.working and not dialog.pending, 'Cutting pasted clip')
                    check('native cut keeps original bytes and the remaining clip',
                          dialog.project.scenes[0].cues == (cue,)
                          and sha256(retained.read_bytes()).hexdigest() == original)
                    dialog.range_start.ChangeValue('.125')
                    dialog.range_end.ChangeValue('.625')
                    press(dialog.buttons['copy_range'])
                    pump(lambda: not frame.working and not dialog.pending, 'Copying exact native range')
                    check('native exact range clipboard is sample aligned',
                          editor._clipboard.trim_start == .625 and editor._clipboard.duration == .5)
                    press(dialog.buttons['trim_range'])
                    pump(lambda: not frame.working and not dialog.pending, 'Trimming native range')
                    check('native trim preserves original reference and selected sample interval',
                          dialog.project.scenes[0].cues[0].trim_start == .625
                          and dialog.project.scenes[0].cues[0].duration == .5)
                    press(dialog.buttons['undo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Undoing native trim')
                    dialog.range_start.ChangeValue('.125')
                    dialog.range_end.ChangeValue('.625')
                    press(dialog.buttons['silence_range'])
                    pump(lambda: not frame.working and not dialog.pending, 'Silencing native range')
                    check('native silence leaves two exact outer source portions',
                          len(dialog.project.scenes[0].cues) == 2
                          and all(item.duration == .125 for item in dialog.project.scenes[0].cues))
                    press(dialog.buttons['undo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Undoing native silence')
                    answer_native_dialog('Scene marker position', '00:00:00.5')
                    answer_native_dialog('Scene marker label', 'Authored native marker — Україна')
                    press(dialog.buttons['marker_add'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native marker')
                    check('native marker is an authored scene annotation',
                          len(dialog.project.scenes[0].markers) == 1
                          and dialog.project.scenes[0].markers[0].position == .5
                          and dialog.project.original_script == edited.original_script)
                    press(dialog.buttons['copy'])
                    pump(lambda: not frame.working and not dialog.pending, 'Copying crossfade source')
                    answer_native_dialog('Paste copied clip', '1.25')
                    press(dialog.buttons['paste'])
                    pump(lambda: not frame.working and not dialog.pending, 'Preparing native crossfade')
                    dialog.clip_choice.SetSelection(0)
                    dialog.select_clip()
                    answer_native_dialog('Choose the clip to move into this crossfade', 0)
                    answer_native_dialog('Crossfade overlap', '.125')
                    press(dialog.buttons['crossfade'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native crossfade')
                    check('native crossfade moves only selected second clip to a measured overlap',
                          dialog.project.scenes[0].cues[0].fade_out == .125
                          and dialog.project.scenes[0].cues[1].fade_in == .125
                          and dialog.project.scenes[0].cues[1].start == .875)
                    press(dialog.buttons['undo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Undoing native crossfade')
                    press(dialog.buttons['undo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Removing copied crossfade placement')
                    answer_effect_chain(-6)
                    press(dialog.buttons['clip_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native clip effects')
                    clip = dialog.project.scenes[0].cues[0]
                    check('native clip effects persist in schema three',
                          dialog.project.schema == 3 and len(clip.effects) == 1
                          and clip.effects[0].parameters['gain_db'] == -6)
                    answer_effect_chain(-6)
                    press(dialog.buttons['master_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native master effects')
                    check('native master effects persist independently',
                          len(dialog.project.effects) == 1
                          and dialog.project.effects[0].parameters['gain_db'] == -6)
                    affected = dialog.project
                    effect_job = editor.service.submit(affected, outputs)
                    effect_results = []
                    frame.run_job('Native saved effect render', lambda: editor.service.run(effect_job),
                                  effect_results.append, lambda error: errors.append(str(error)))
                    pump(lambda: not frame.working, 'Rendering saved native effect chains')
                    check('native effect project completes actual rendering',
                          bool(effect_results) and effect_results[0]['status'] == 'complete')
                    def pcm(path):
                        with wave.open(str(path), 'rb') as stream:
                            data = stream.readframes(stream.getnframes())
                        return data, struct.unpack('<' + 'h' * (len(data) // 2), data)
                    baseline = pcm(Path(results[0]['destination']) / 'master.wav')[1]
                    master = Path(effect_results[0]['destination']) / 'master.wav'
                    actual = pcm(master)[1]
                    rms_before = math.sqrt(sum(value * value for value in baseline) / len(baseline))
                    rms_after = math.sqrt(sum(value * value for value in actual) / len(actual))
                    check('actual native clip and master effects both apply',
                          len(actual) == len(baseline) and .245 < rms_after / rms_before < .257)
                    chapter, = (Path(effect_results[0]['destination']) / 'chapters').glob('*.wav')
                    check('delivered chapter matches the mastered audio', pcm(chapter)[0] == pcm(master)[0])
                    dialog.range_start.ChangeValue('.125')
                    dialog.range_end.ChangeValue('.625')
                    press(dialog.buttons['dry_preview'])
                    pump(lambda: not frame.working and not dialog.pending and dialog.player is not None
                         and dialog.player.state == 'playing', 'Starting explicit dry native preview')
                    dry_values = pcm(dialog.player.path)[1]
                    press(dialog.buttons['stop'])
                    press(dialog.buttons['range_preview'])
                    pump(lambda: not frame.working and not dialog.pending and dialog.player is not None
                         and dialog.player.state == 'playing', 'Starting explicit wet native preview')
                    wet_values = pcm(dialog.player.path)[1]
                    check('native dry/wet preview uses actual separate processed PCM',
                          len(dry_values) == len(wet_values) and .497 < max(wet_values) / max(dry_values) < .505)
                    press(dialog.buttons['stop'])
                    press(dialog.buttons['loop'])
                    pump(lambda: not frame.working and not dialog.pending and dialog.navigation is not None
                         and dialog.navigation.controller.repeat and bool(dialog.player.seeks),
                         'Actual native timer did not repeat the selected range', timeout=15)
                    check('native loop uses existing navigation timer over actual rendered PCM',
                          dialog.navigation.controller.a == 0 and dialog.navigation.controller.b == .5
                          and dialog.player.seeks[-1] == 0)
                    press(dialog.buttons['stop'])
                    check('native Stop clears pending repeat', not dialog.navigation.controller.repeat
                          and dialog.player.state == 'stopped')
                    press(dialog.buttons['bypass'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native bypass')
                    check('native bypass retains the complete effect chain',
                          dialog.project.scenes[0].cues[0].effects_bypassed
                          and dialog.project.scenes[0].cues[0].effects == affected.scenes[0].cues[0].effects)
                    press(dialog.buttons['undo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Undoing native bypass')
                    dialog.master_rate.ChangeValue('48000')
                    press(dialog.buttons['sample_rate'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native master sample rate')
                    check('native master sample rate preserves effect chains',
                          dialog.project.sample_rate == 48000 and dialog.project.effects == affected.effects)
                    edited = dialog.project
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
                    press(dialog.buttons['close'])
                    check('native keyboard close finishes cleanly', dialog.closed)
                    lifecycle_checks()
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()
            wx.CallAfter(scenario)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
        finally:
            abort.stop()
            wx.FileDialog = actual_picker
            player_module.WavePlayer = original_player
            if frame is not None:
                frame.cancel_event.set()
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
            if dialog is not None and dialog:
                dialog.stop()
                dialog.Destroy()
            if frame is not None:
                frame.Destroy()
            gui.Yield()
    verify_audio(proof_runtime)
    check('installed clip source bytes remain exact after all native operations',
          binding.verify_installed_sources(*source_modules) == source_identity)
    print(json.dumps({'result': 'PASS', 'checks': checks, 'native_keyboard': 'PASS',
        'source_identity': source_identity,
        'fixture': 'synthetic PCM rendered by actual FFmpeg', 'audio_runtime': audio_proof, 'nvda': 'NOT_RUN',
        'native_file_pickers': 'NOT_RUN', 'physical_microphone': 'NOT_RUN',
        'preview_transport': 'explicit synthetic clock over actual FFmpeg PCM', 'mci': 'NOT_RUN', 'audio_device': 'NOT_RUN',
        'direct_parent_destruction': 'PASS', 'late_preview_completion': 'PASS'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
