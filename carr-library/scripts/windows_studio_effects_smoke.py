"""Installed native persisted Studio effect scopes and measured synthetic PCM.

Only local source/output chooser results and a competing saved revision are
supplied by the fixture. Effect dialogs, keyboard actions, shared background
work, ProjectStore persistence and FFmpeg rendering are real. No playback,
physical-device, speech-quality or NVDA acceptance is implied.
"""
from array import array
import argparse
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import threading
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows required'}))
        return 77
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio-directory', required=True, type=Path)
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
    from carr_library.audio import Effect
    from carr_library.audio import dsp, effect_presets, envelopes, process
    from carr_library import durable_state, runtime_settings
    from carr_library.operations import broker
    from carr_library.speech import VoiceRegistry
    from carr_library.studio import project as project_module, clips, managed_inputs, render
    from carr_library.studio import clip_operations, project_history
    from carr_library.studio.project import Role
    from dataclasses import replace
    from carr_library.runtime_settings import audio_runtime
    from carr_library.ui import effects_dialog, studio_clips
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, proof, module_origins, module_hashes = [], [], {}, {}, {}
    modal_loops = []
    unwind_pending = False
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            unwind()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes, user32.IsWindow.restype = (wintypes.HWND,), wintypes.BOOL

    def own_modal(window):
        loop = wx.EventLoopBase.GetActive()
        if (not window.IsModal() or loop is None or not loop.IsRunning() or loop.IsMain()):
            raise AssertionError('The selected Studio modal has no active owned loop.')
        modal_loops.append((window, window.GetHandle(), loop))

    def unwind():
        nonlocal unwind_pending
        if unwind_pending:
            return
        unwind_pending = True
        def close_active():
            nonlocal unwind_pending
            unwind_pending = False
            try:
                for window, handle, loop in reversed(modal_loops):
                    if window and user32.IsWindow(handle) and window.IsModal() and loop.IsRunning():
                        window.EndModal(wx.ID_CANCEL)
                        return
                active = wx.EventLoopBase.GetActive()
                if errors and active is not None and active.IsMain():
                    active.ScheduleExit(1)
            except Exception:
                errors.append(traceback.format_exc())
        wx.CallAfter(close_active)

    def pump(predicate, label, timeout=45, *, cleanup=False):
        deadline = monotonic() + timeout
        while not predicate():
            if errors and not cleanup:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= deadline:
                raise TimeoutError(label)
            gui.Yield()
            sleep(.01)

    def find(parent, name):
        control = wx.Window.FindWindowByName(name, parent)
        check('named native control: ' + name, control is not None)
        return control

    def press(control):
        name = control.GetName()
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle(), 'Keyboard focus: ' + name, 10)
        check('native Space down: ' + name, simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            sleep(.03)
        finally:
            check('native Space up: ' + name, simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()

    def later(title, callback):
        deadline = monotonic() + 45
        def invoke():
            try:
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if window.IsShown() and window.GetTitle() == title]
                if not dialogs:
                    if monotonic() >= deadline:
                        raise TimeoutError('Missing native dialog: ' + title)
                    wx.CallLater(20, invoke)
                    return
                popup, = dialogs
                own_modal(popup)
                callback(popup)
            except Exception:
                errors.append(traceback.format_exc())
                # A suspended outer modal cannot be ended while its child loop runs.
                unwind()
        wx.CallLater(20, invoke)

    def set_gain(popup, value):
        control = find(popup, 'gain db (dB); -120 to 24')
        check('native numeric effect input', isinstance(control, wx.TextCtrl))
        control.ChangeValue(str(value))
        press(find(popup, 'Apply effect'))

    def choose(popup, selection):
        check('native preset choice dialog', isinstance(popup, wx.SingleChoiceDialog))
        popup.SetSelection(selection)
        press(wx.Window.FindWindowById(wx.ID_OK, popup))

    def verify_audio(runtime):
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            check('explicit selected audio executable: ' + name,
                  binary.samefile(selected_audio / 'bin' / (name + '.exe')))
            check('pinned audio executable bytes: ' + name,
                  binary.stat().st_size == expected['size'] and digest == expected['sha256'])
            proof[name] = {'path': str(binary), 'sha256': digest}

    check('actual wx Windows controls', wx.Platform == '__WXMSW__')
    checkout = Path(__file__).resolve().parents[1]
    for module in (project_module, clips, managed_inputs, render, studio_clips, effects_dialog,
                   effect_presets, envelopes, clip_operations, project_history,
                   dsp, process, broker, durable_state, runtime_settings):
        actual = Path(module.__file__).resolve()
        module_origins[module.__name__] = str(actual)
        module_hashes[module.__name__] = sha256(actual.read_bytes()).hexdigest()
        original = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('exact installed source: ' + module.__name__,
              actual.is_relative_to(Path(sys.prefix).resolve()) and not actual.is_relative_to(checkout)
              and sha256(actual.read_bytes()).digest() == sha256(original.read_bytes()).digest())


    def verify_nested_cleanup():
        outer = wx.Dialog(None, title='Studio cleanup outer')
        inner = wx.Dialog(outer, title='Studio cleanup inner')
        observed = []
        def inside():
            try:
                own_modal(outer)
                def close_inner():
                    own_modal(inner)
                    unwind()
                wx.CallAfter(close_inner)
                observed.append(inner.ShowModal() == wx.ID_CANCEL)
                observed.append(outer.IsModal() and wx.EventLoopBase.GetActive().IsRunning())
                wx.CallAfter(unwind)
            except Exception:
                errors.append(traceback.format_exc())
                unwind()
        try:
            wx.CallAfter(inside)
            observed.append(outer.ShowModal() == wx.ID_CANCEL)
        finally:
            inner.Destroy()
            outer.Destroy()
            gui.Yield()
        check('native Studio failure cleanup closes only the running owned modal',
              observed == [True, True, True] and not errors)

    def close_host():
        if host_executor is None:
            return
        try:
            if frame and user32.IsWindow(host_handle):
                frame.cancel_event.set()
                frame.Close(force=True)
                pump(lambda: not user32.IsWindow(host_handle),
                     'Actual Studio host Close did not finish.', cleanup=True)
        finally:
            host_executor.shutdown(wait=True, cancel_futures=True)
        check('Studio effect worker cleanup is complete',
              not user32.IsWindow(host_handle) and not host_executor.cleanup_failures)

    original_picker, original_directory_picker = wx.FileDialog, wx.DirDialog
    frame = dialog = service = voices = None
    host_handle = host_executor = None

    def pcm(path):
        with wave.open(str(path), 'rb') as stream:
            shape = stream.getframerate(), stream.getnchannels(), stream.getnframes()
            raw = stream.readframes(stream.getnframes())
        values = array('h')
        values.frombytes(raw)
        if sys.byteorder != 'little':
            values.byteswap()
        return raw, shape, math.sqrt(math.fsum(value * value for value in values) / len(values))

    try:
        verify_nested_cleanup()
        with TemporaryDirectory(prefix='resonance-native-studio-effects-') as temporary:
            root = Path(temporary)
            source = root / 'Synthetic original — Україна.wav'
            data = array('h', (round(7000 * math.sin(2 * math.pi * 440 * index / 24000))
                               for index in range(24000)))
            if sys.byteorder != 'little':
                data.byteswap()
            with wave.open(str(source), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(data.tobytes())
            original = sha256(source.read_bytes()).hexdigest()
            library = LibraryApp(root / 'private library')
            settings = runtime_settings.load_settings(library.root / 'state')
            settings.update(ffmpeg=str(selected_audio / 'bin' / 'ffmpeg.exe'),
                            ffprobe=str(selected_audio / 'bin' / 'ffprobe.exe'))
            runtime_settings.save_settings(library.root / 'state', settings)
            frame = DesktopFrame(library)
            host_handle, host_executor = frame.GetHandle(), frame.executor
            frame.Show()
            gui.SetTopWindow(frame)
            voices = VoiceRegistry([])
            service = render.ProductionService(library.root / 'state/studio', voices=voices)
            editor = clips.ClipEditor(library.root, service=service)
            verify_audio(service._runtime())
            project = editor.create('Native persisted effects')
            # A deliberately unused speaking role exercises its real editor
            # without misrepresenting a synthetic tone as speech synthesis.
            project = service.projects.save(replace(project, revision=2,
                roles=(Role('unused-role', 'Reviewed speaking role'),)),
                expected_revision=project.revision, expected_fingerprint=project.fingerprint)
            outputs = root / 'reviewed output parent'
            outputs.mkdir()

            class SelectedPath:
                def __init__(self, *args, **kwargs):
                    check('explicit original picker requires existing file',
                          kwargs.get('style') == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(source)
                def Destroy(self): pass
            class SelectedOutput:
                def __init__(self, *args, **kwargs): pass
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(outputs)
                def Destroy(self): pass
            wx.FileDialog, wx.DirDialog = SelectedPath, SelectedOutput
            dialog = studio_clips.ClipDialog(frame, editor=editor)
            dialog.Show()

            def add_gain(popup, gain, *, bypass=False):
                kinds = find(popup, 'Effect type to add')
                check('Studio exposes only duration-preserving effect choices',
                      kinds.FindString('tempo') == wx.NOT_FOUND)
                index = kinds.FindString('gain')
                check('installed runtime offers gain', index != wx.NOT_FOUND)
                kinds.SetSelection(index)
                press(find(popup, 'Add effect'))
                later('Edit gain effect', lambda edit: set_gain(edit, gain))
                press(find(popup, 'Edit selected effect'))
                if bypass:
                    press(find(popup, 'Toggle effect bypass'))

            def review_scope(scope):
                def chain(popup):
                    ordered = find(popup, 'Ordered audio effects')
                    check('new scope starts without processing', ordered.GetCount() == 0)
                    add_gain(popup, -9 if scope == 'role' else -6)
                    if scope == 'clip':
                        add_gain(popup, -3, bypass=True)
                        press(find(popup, 'Move effect up'))
                        check('clip order changes in native editor', 'bypassed' in ordered.GetString(0))
                        press(find(popup, 'Move effect down'))
                        check('clip bypass remains after reorder', 'bypassed' in ordered.GetString(1))
                    press(find(popup, 'Apply effect chain'))
                later('Ordered audio effects', chain)
                if scope == 'role':
                    later('Speaking role effects', lambda picker: choose(picker, 0))
                press(dialog.buttons[scope + '_effects'])
                pump(lambda: not frame.working and not dialog.pending, 'Saving ' + scope + ' processing')
                value = service.projects.load(project.id)
                target = {'master': value.effects, 'role': value.roles[0].effects,
                          'scene': value.scenes[0].effects,
                          'clip': value.scenes[0].cues[0].effects}[scope]
                check('native saved exact ' + scope + ' processing',
                      value.schema == 3 and target[0] == Effect('gain', gain_db=-9 if scope == 'role' else -6))
                if scope == 'clip':
                    check('native persisted ordered bypass chain',
                          len(target) == 2 and target[1] == Effect('gain', gain_db=-3, enabled=False))
                return value

            def scenario():
                try:
                    check('actual Studio effect main loop', gui.GetMainLoop() is not None)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Load current project')
                    check('native project selection matches saved identity', dialog.project.id == project.id)
                    press(dialog.buttons['add'])
                    pump(lambda: not frame.working and not dialog.pending, 'Retain exact original')
                    check('native original is managed by content identity',
                          dialog.project.scenes[0].cues[0].path == 'inputs/' + original + '.audio')
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Baseline render')
                    baseline_rows = [row for row in service.jobs() if row['status'] == 'complete']
                    check('baseline render is an actual completed job', len(baseline_rows) == 1)
                    baseline = Path(baseline_rows[0]['destination']) / 'master.wav'
                    for scope in ('clip', 'role', 'scene', 'master'):
                        edited = review_scope(scope)
                    before_cancel = edited.fingerprint
                    later('Ordered audio effects',
                          lambda popup: press(wx.Window.FindWindowById(wx.ID_CANCEL, popup)))
                    press(dialog.buttons['master_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Cancel effect review')
                    check('native Cancel does not create a saved revision',
                          service.projects.load(project.id).fingerprint == before_cancel)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Reload saved processing')
                    check('native reload preserves full typed chains', dialog.project == edited)
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Processed render')
                    processed, = [row for row in service.jobs()
                                  if row['fingerprint'] == edited.fingerprint and row['status'] == 'complete']
                    check('native render completion has readable output and focus',
                          processed['destination'] in dialog.status.GetValue()
                          and wx.Window.FindFocus() is dialog.status)
                    rendered = Path(processed['destination'])
                    master = rendered / 'master.wav'
                    chapter, = (rendered / 'chapters').glob('*.wav')
                    stem = rendered / 'stems/clips/sfx.wav'
                    check('actual clip scene master stages attenuate PCM',
                          .122 < pcm(master)[2] / pcm(baseline)[2] < .130)
                    check('track stem includes clip effects but excludes bus effects',
                          .497 < pcm(stem)[2] / pcm(baseline)[2] < .505)
                    check('mastered chapter equals final master PCM', pcm(chapter)[0] == pcm(master)[0])
                    check('processing retains measured sample grid', pcm(master)[1] == pcm(baseline)[1])
                    check('saved timeline binds ordered project effects',
                          processed['result']['effects'] == project_module.project_effects(edited))

                    def edit_envelope(popup):
                        for name, value in (
                                ('point count (integer points); 2 to 8', 3),
                                ('interpolation (0 linear, 1 hold); 0 to 1', 1),
                                ('time 2 (seconds); 0 to 86400', .25),
                                ('value 2 (dB); -120 to 24', -12),
                                ('time 3 (seconds); 0 to 86400', .5),
                                ('value 3 (dB); -120 to 24', -6)):
                            control = find(popup, name)
                            check('native envelope numeric field: ' + name, isinstance(control, wx.TextCtrl))
                            control.ChangeValue(str(value))
                        press(find(popup, 'Apply effect'))

                    def review_envelope(popup):
                        kinds = find(popup, 'Effect type to add')
                        check('native bus exposes width and refuses mono pan envelopes',
                              kinds.FindString('width_envelope') != wx.NOT_FOUND
                              and kinds.FindString('pan_envelope') == wx.NOT_FOUND)
                        index = kinds.FindString('gain_envelope')
                        check('installed native effect picker offers gain envelope', index != wx.NOT_FOUND)
                        kinds.SetSelection(index)
                        press(find(popup, 'Add effect'))
                        later('Edit gain_envelope effect', edit_envelope)
                        press(find(popup, 'Edit selected effect'))
                        ordered = find(popup, 'Ordered audio effects')
                        reviewed_label = ordered.GetString(ordered.GetSelection())
                        check('native envelope review shows explicit hold points',
                              'hold' in reviewed_label and '0.25s: -12' in reviewed_label)
                        press(find(popup, 'Undo chain change'))
                        check('native envelope edit is reversible before applying',
                              ordered.GetString(ordered.GetSelection()) != reviewed_label)
                        press(find(popup, 'Redo chain change'))
                        check('native envelope redo restores exact reviewed points',
                              ordered.GetString(ordered.GetSelection()) == reviewed_label)
                        press(find(popup, 'Apply effect chain'))

                    later('Ordered audio effects', review_envelope)
                    press(dialog.buttons['master_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Save reviewed sample envelope')
                    edited = service.projects.load(project.id)
                    curve = edited.effects[-1]
                    check('native envelope points persist in existing schema3 history',
                          edited.schema == 3 and curve.kind == 'gain_envelope'
                          and envelopes.read_envelope(curve, 24000).frames == (0, 6000, 12000)
                          and envelopes.read_envelope(curve, 24000).values == (0, -12, -6))
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Render actual sample envelope')
                    curve_job, = [row for row in service.jobs()
                                  if row['fingerprint'] == edited.fingerprint and row['status'] == 'complete']
                    curve_master = Path(curve_job['destination']) / 'master.wav'
                    old_pcm, new_pcm = array('h'), array('h')
                    old_pcm.frombytes(pcm(master)[0])
                    new_pcm.frombytes(pcm(curve_master)[0])
                    if sys.byteorder != 'little':
                        old_pcm.byteswap()
                        new_pcm.byteswap()
                    measured = []
                    for sample, gain in ((3613, 0), (9013, -12), (18013, -6)):
                        factor = 10 ** (gain / 20)
                        measured.extend(abs(new_pcm[sample * 2 + channel]
                                            - old_pcm[sample * 2 + channel] * factor) <= 3
                                        for channel in (0, 1))
                    check('native saved envelope changes actual PCM at reviewed sample intervals', all(measured))
                    check('native envelope retains exact sample geometry',
                          pcm(curve_master)[1] == pcm(master)[1] == (24000, 2, 24000))
                    check('native envelope receipt binds every saved numeric point',
                          curve_job['result']['effects'] == project_module.project_effects(edited))
                    check('native envelope reuses completed unchanged scene audio',
                          all(node['reused'] for node in curve_job['nodes'].values()
                              if node['kind'] == 'scene mix')
                          and any(node['kind'] == 'scene mix' for node in curve_job['nodes'].values()))
                    before_timeline = edited
                    check('native timeline menu is available', 'timeline' in dialog.buttons)

                    def enter_time(popup, value):
                        check('native clip time entry dialog', isinstance(popup, wx.TextEntryDialog))
                        popup.SetValue(value)
                        press(wx.Window.FindWindowById(wx.ID_OK, popup))

                    def timeline_action(index, entry_title=None, entry_value=None):
                        if entry_title is not None:
                            later(entry_title, lambda popup: enter_time(popup, entry_value))
                        later('Timeline editing and project history', lambda popup: choose(popup, index))
                        press(dialog.buttons['timeline'])
                        pump(lambda: not frame.working and not dialog.pending, 'Complete native timeline action')

                    timeline_action(0, 'Split saved clip', '.5')
                    split_project = service.projects.load(project.id)
                    first, second = split_project.scenes[0].cues
                    check('native split retains two exact original selections',
                          first.path == second.path == before_timeline.scenes[0].cues[0].path
                          and (first.start, first.trim_start, first.duration) == (0, 0, .5)
                          and (second.start, second.trim_start, second.duration) == (.5, .5, .5)
                          and first.effects == second.effects == before_timeline.scenes[0].cues[0].effects)
                    timeline_action(3)
                    undone = service.projects.load(project.id)
                    check('native project undo restores prior complete content',
                          replace(undone, revision=before_timeline.revision) == before_timeline)
                    timeline_action(4)
                    redone = service.projects.load(project.id)
                    check('native project redo returns reviewed split',
                          replace(redone, revision=split_project.revision) == split_project)
                    dialog.clip_choice.SetSelection(0)
                    dialog.select_clip()
                    later('Rejoin saved clips', lambda popup: choose(popup, 0))
                    timeline_action(2)
                    joined = service.projects.load(project.id)
                    check('native rejoin recovers original timing and effect chains',
                          joined.scenes[0].cues[0].id == before_timeline.scenes[0].cues[0].id
                          and len(joined.scenes[0].cues) == 1
                          and joined.scenes[0].cues[0].duration == 1
                          and joined.effects == before_timeline.effects
                          and joined.scenes[0].cues[0].effects == before_timeline.scenes[0].cues[0].effects)
                    timeline_action(1, 'Cut saved clip interval', '.25 .5')
                    cut_project = service.projects.load(project.id)
                    cut_first, cut_second = cut_project.scenes[0].cues
                    check('native cut preserves scene clock and removes selected interval',
                          cut_first.duration == .25 and cut_second.start == .5
                          and cut_second.trim_start == .5 and cut_second.duration == .5
                          and cut_project.scenes[0].minimum_duration == 1)
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Render native cut interval')
                    cut_job, = [row for row in service.jobs()
                                if row['fingerprint'] == cut_project.fingerprint and row['status'] == 'complete']
                    cut_master = Path(cut_job['destination']) / 'master.wav'
                    cut_pcm = array('h')
                    cut_pcm.frombytes(pcm(cut_master)[0])
                    if sys.byteorder != 'little':
                        cut_pcm.byteswap()
                    left_edge, right_edge = 6000 * 2, 12000 * 2
                    check('native cut renders silence only inside selected interval',
                          pcm(cut_master)[1] == pcm(curve_master)[1]
                          and max(abs(value) for value in cut_pcm[left_edge:right_edge]) == 0
                          and max(abs(a - b) for a, b in zip(cut_pcm[:left_edge], new_pcm[:left_edge])) <= 3
                          and max(abs(a - b) for a, b in zip(cut_pcm[right_edge:], new_pcm[right_edge:])) <= 3)

                    def choose_original_history(popup):
                        popup.SetSelection(4)
                        check('native history picker targets exact reviewed revision',
                              popup.GetStringSelection() ==
                              f'Revision {before_timeline.revision}; identity {before_timeline.fingerprint[:12]}')
                        press(wx.Window.FindWindowById(wx.ID_OK, popup))
                    later('Saved project history', choose_original_history)
                    timeline_action(5)
                    edited = service.projects.load(project.id)
                    check('native saved project history restores exact prior content',
                          replace(edited, revision=before_timeline.revision) == before_timeline)
                    check('native history restore creates new revision without replacing outputs',
                          edited.revision == cut_project.revision + 1
                          and pcm(curve_master)[0] == new_pcm.tobytes()
                          and pcm(cut_master)[1] == (24000, 2, 24000))


                    clipboard_origin = edited
                    original_clip, = edited.scenes[0].cues
                    timeline_action(6)
                    copied_reference = dialog._copied_clip
                    check('native Copy retains an exact saved selection without mutation',
                          service.projects.load(project.id) == edited
                          and copied_reference.source.revision == edited.revision
                          and copied_reference.source.fingerprint == edited.fingerprint
                          and copied_reference.clip_id == original_clip.id)
                    timeline_action(8, 'Paste saved clip', '1')
                    pasted = service.projects.load(project.id)
                    initial_clip, pasted_clip = pasted.scenes[0].cues
                    check('native Paste preserves original selection and effects at a new scene position',
                          initial_clip == original_clip
                          and pasted_clip.id != original_clip.id and pasted_clip.start == 1
                          and replace(pasted_clip, id=original_clip.id, start=original_clip.start) == original_clip)
                    timeline_action(7)
                    cut_for_paste = service.projects.load(project.id)
                    check('native Cut for Paste retains the removed clip in immutable history',
                          cut_for_paste.scenes[0].cues == (original_clip,)
                          and dialog._copied_clip.clip_id == pasted_clip.id
                          and dialog._copied_clip.source.fingerprint == pasted.fingerprint)
                    timeline_action(8, 'Paste saved clip', '2')
                    repasted = service.projects.load(project.id)
                    recovered_clip = repasted.scenes[0].cues[1]
                    check('native Paste reuses the cut selection with a fresh stable identity',
                          recovered_clip.id not in (original_clip.id, pasted_clip.id)
                          and recovered_clip.start == 2
                          and replace(recovered_clip, id=original_clip.id, start=original_clip.start) == original_clip)
                    timeline_action(3)
                    edited = service.projects.load(project.id)
                    check('native Undo restores content after clipboard edits',
                          replace(edited, revision=clipboard_origin.revision) == clipboard_origin)

                    newer = None
                    def concurrent_review(popup):
                        nonlocal newer
                        newer = editor.scene_duration(edited, edited.scenes[0].id, 2)
                        press(find(popup, 'Apply effect chain'))
                    later('Ordered audio effects', concurrent_review)
                    press(dialog.buttons['master_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Refuse stale reviewed effect save')
                    check('concurrent saved revision cannot be overwritten by native review',
                          newer is not None and service.projects.load(project.id) == newer
                          and 'changed since' in dialog.status.GetValue())
                    check('Studio effects do not start playback', dialog.player is None and not dialog.previews)
                    check('original and retained media remain unchanged',
                          sha256(source.read_bytes()).hexdigest() == original
                          and sha256((service.inputs / (original + '.audio')).read_bytes()).hexdigest() == original)
                    check('processing releases current shared admission', service.broker.snapshot()['leases'] == [])
                    press(dialog.buttons['close'])
                    check('native effect editor closes cleanly', dialog.closed)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()

            wx.CallAfter(scenario)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            dialog.Destroy()
            dialog = None
            service.close()
            service = None
            voices.close()
            voices = None
            close_host()
            frame = None
            gui.Yield()
    finally:
        wx.FileDialog, wx.DirDialog = original_picker, original_directory_picker
        if frame is not None:
            frame.cancel_event.set()
        if dialog is not None:
            dialog.stop()
            dialog.Destroy()
        if frame is not None:
            close_host()
            gui.Yield()
        if service is not None:
            service.close()
        if voices is not None:
            voices.close()
        if native_handle is not None:
            native_handle.close()
    for name, location in module_origins.items():
        check('unchanged installed module after envelope work: ' + name,
              sha256(Path(location).read_bytes()).hexdigest() == module_hashes[name])
    print(json.dumps({'result': 'PASS', 'checks': checks, 'module_origins': module_origins,
        'module_sha256': module_hashes, 'actual_gain_envelope': 'PASS',
        'actual_clip_editing': 'PASS', 'project_history_restore': 'PASS',
        'native_clipboard': 'PASS', 'native_failure_cleanup': 'PASS',
        'audio_runtime': proof, 'native_keyboard': 'PASS', 'actual_ffmpeg': 'PASS',
        'fixture': 'actual synthetic PCM and a dormant role; no speech inference',
        'project_schema': 3, 'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'audible_listening': 'NOT_RUN', 'physical_device': 'NOT_RUN'}, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    def watchdog_failure():
        print(json.dumps({'result': 'FAIL', 'reason': 'Native Studio effects deadline exceeded',
                          'cleanup': 'NOT_CONFIRMED', 'timeout_seconds': 180}), flush=True)
        os._exit(1)
    watchdog = threading.Timer(180, watchdog_failure)
    watchdog.daemon = True
    watchdog.start()
    result = main()
    watchdog.cancel()
    raise SystemExit(result)
