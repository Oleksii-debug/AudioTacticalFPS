"""Installed W15 native review/save, with actual pinned FFmpeg and original fixtures.

Only three explicit file-picker paths are substituted. Native keys operate the
real named controls, and the existing Desktop worker performs all metadata work.
Two lifecycle checks hold a real completed inspection result until the native
owner is deleted; they do not substitute operation results or native events.
No device, listening, screen-reader or third-party player claim is made.
"""
import argparse
import faulthandler
from hashlib import file_digest, sha256
import json
import os
import re
from pathlib import Path
import struct
import sys
import sysconfig
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
import traceback
import wave
import zlib


def verify_installed_sources(*modules):
    """Require the current interpreter's installed bytes, never source overrides."""
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Installed acceptance requires Python -I -B.')
    source_root = Path(__file__).resolve().parents[1] / 'src'
    installed_roots = {Path(sysconfig.get_path(key)).resolve(strict=True)
                       for key in ('purelib', 'platlib')}
    result = {}
    for module in modules:
        actual = Path(module.__file__).resolve(strict=True)
        if (actual.is_relative_to(source_root.resolve(strict=True))
                or not any(actual.is_relative_to(root) for root in installed_roots)):
            raise RuntimeError('Acceptance imported a module outside the installed interpreter: ' + module.__name__)
        reviewed = source_root.joinpath(*module.__name__.split('.')).with_suffix('.py')
        if not reviewed.is_file() or actual.read_bytes() != reviewed.read_bytes():
            raise RuntimeError('Installed source differs from the reviewed checkout: ' + module.__name__)
        with actual.open('rb') as stream:
            digest = file_digest(stream, 'sha256').hexdigest()
        result[module.__name__] = {'path': str(actual), 'sha256': digest}
    return result



def verify_audio_evidence(runtime, evidence_path):
    """Bind the actual test runtime to explicit local path/hash evidence.

    This checks consistency with the reviewed CI preparation result. The report
    is not a publisher signature and neither executable is a bundled payload.
    """
    from carr_library.durable_state import local_path, read_json, regular
    evidence_path = local_path(evidence_path, exists=True)
    if regular(evidence_path).st_size > 64 * 1024:
        raise ValueError('External audio evidence exceeds its limit.')
    evidence = read_json(evidence_path)
    if (not isinstance(evidence, dict)
            or set(evidence) != {'schema', 'status', 'bundled', 'runtime_root', 'ffmpeg', 'ffprobe'}
            or type(evidence['schema']) is not int or evidence['schema'] != 1
            or evidence['status'] != 'PASS' or evidence['bundled'] is not False
            or not isinstance(evidence['runtime_root'], str)
            or not Path(evidence['runtime_root']).is_absolute()):
        raise ValueError('Expected explicit external CI audio preparation evidence.')
    root = local_path(evidence['runtime_root'], exists=True)
    regular(root, directory=True)
    program = os.environ.get('CARR_PROGRAM_DIR')
    if program and root.is_relative_to(local_path(program, exists=True)):
        raise ValueError('The test audio runtime must remain outside the installed application bundle.')
    result = {'runtime_root': str(root), 'bundled': False}
    for name in ('ffmpeg', 'ffprobe'):
        selected = evidence[name]
        if (not isinstance(selected, dict) or set(selected) != {'path', 'sha256'}
                or not isinstance(selected['path'], str)
                or not Path(selected['path']).is_absolute()
                or not isinstance(selected['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', selected['sha256'])):
            raise ValueError('Invalid external audio executable evidence: ' + name)
        expected = local_path(selected['path'], exists=True)
        actual = local_path(getattr(runtime, name), exists=True)
        if expected != root / 'bin' / (name + '.exe') or actual != expected:
            raise ValueError('The actual audio runtime differs from the explicit CI preparation: ' + name)
        before = regular(actual)
        with actual.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            measured = file_digest(stream, 'sha256').hexdigest()
            after_read = os.fstat(stream.fileno())
        after = regular(actual)
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (stamp(before) != stamp(opened) or stamp(before) != stamp(after_read)
                or stamp(before) != stamp(after) or measured != selected['sha256']):
            raise ValueError('External audio executable bytes changed: ' + name)
        result[name] = {'path': str(actual), 'sha256': measured}
    return result



def verify_current_audio(runtime):
    """Use the existing selected runtime and enforce sealed bundled identities."""
    from carr_library.durable_state import local_path, read_json, regular
    bundle = local_path(os.environ['CARR_PROGRAM_DIR'], exists=True) if os.environ.get('CARR_PROGRAM_DIR') else None
    manifest = read_json(bundle / 'bundle-manifest.json') if bundle else None
    result = {'bundled': bundle is not None}
    for name in ('ffmpeg', 'ffprobe'):
        binary = local_path(getattr(runtime, name), exists=True)
        before = regular(binary)
        with binary.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            checksum = file_digest(stream, 'sha256').hexdigest()
            finished = os.fstat(stream.fileno())
        after = regular(binary)
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stamp(before) != stamp(opened) or stamp(before) != stamp(finished) or stamp(before) != stamp(after):
            raise ValueError('Actual metadata runtime changed during verification: ' + name)
        if bundle:
            entry = 'audio-runtime/bin/' + name + '.exe'
            if binary != bundle / entry or checksum != manifest['files'][entry]:
                raise ValueError('Metadata probe did not use the exact sealed bundled executable: ' + name)
        result[name] = {'path': str(binary), 'sha256': checksum}
    return result


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'native_keyboard': 'NOT_RUN', 'actual_ffmpeg': 'NOT_RUN', 'nvda': 'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, help='Optional explicit external CI runtime evidence; otherwise use the current configured or sealed runtime.')
    arguments = parser.parse_args()
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
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
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    import carr_library.runtime_settings as settings_module
    import carr_library.audio.dsp as dsp_module
    import carr_library.audio.process as process_module
    import carr_library.audio.metadata as metadata
    import carr_library.ui.audio_metadata_dialog as ui
    import carr_library.ui.desktop_app as desktop_module
    import carr_library.audio.metadata_review as review_module
    import carr_library.audio.cleanup as cleanup_module
    import carr_library.audio.audiobook as audiobook_module

    checks, errors, identities, audio_proof, observed = [], [], {}, {}, {}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label); print('PASS: ' + label, flush=True)
    def stage(label): print('STAGE: ' + label, flush=True)

    origin = Path(carr_library.__file__).resolve()
    identities = verify_installed_sources(ui, metadata, settings_module, dsp_module,
        process_module, desktop_module, review_module, cleanup_module, audiobook_module)
    from carr_library.durable_state import read_json
    evidence = read_json(arguments.audio_evidence) if arguments.audio_evidence else None

    def verify_runtime(runtime):
        checked = (verify_audio_evidence(runtime, arguments.audio_evidence)
                   if arguments.audio_evidence else verify_current_audio(runtime))
        if audio_proof:
            check('actual metadata runtime retains the exact selected identity', checked == audio_proof)
        else:
            audio_proof.update(checked)

    frame, owner_loop = None, None
    timers, held = [], [None]
    metadata_parents = []
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc()); abort(); return False
    gui = ProbeApp(False)
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    check('real Windows wx controls', wx.Platform == '__WXMSW__')
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        if held[0] is not None:
            check('native key and modifiers released', simulator.KeyUp(*held[0]))
            held[0] = None

    def metadata_dialog():
        return next((window for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog)
                     and window.GetTitle() == 'Audio metadata and cover' and window.GetParent() is frame), None)

    def owned_metadata_dialogs():
        result = []
        for window in wx.GetTopLevelWindows():
            if not isinstance(window, wx.Dialog) or window.GetTitle() != 'Audio metadata and cover':
                continue
            parent = window.GetParent()
            if ((frame is not None and parent is frame)
                    or any(parent is owner for owner in metadata_parents)):
                result.append(window)
        return tuple(result)

    def abort():
        try:
            try:
                if frame is not None:
                    frame.cancel_event.set()
            except Exception:
                errors.append(traceback.format_exc())
            try:
                release()
            except Exception:
                errors.append(traceback.format_exc())
            for dialog in owned_metadata_dialogs():
                try:
                    try:
                        dialog.metadata_controller.close()
                    finally:
                        if dialog and dialog.IsModal():
                            dialog.EndModal(wx.ID_CANCEL)
                except Exception:
                    errors.append(traceback.format_exc())
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            if owner_loop is not None:
                owner_loop.ScheduleExit(1)

    def later(callback, delay=25, *, after_failure=False):
        def invoke():
            nonlocal owner_loop
            if owner_loop is None:
                owner_loop = gui.GetMainLoop()
            if errors and not after_failure:
                return
            try:
                callback()
            except Exception:
                errors.append(traceback.format_exc())
                print('NATIVE_METADATA_FAILURE: ' + errors[-1], file=sys.stderr, flush=True)
                abort()
        timer = wx.CallLater(delay, invoke)
        timers.append(timer)
        return timer

    def until(predicate, complete, *, timeout=15):
        deadline = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= deadline:
                raise TimeoutError('The native audio metadata stage did not finish.')
            else:
                later(poll)
        later(poll)

    def foreground(control):
        top, owner = wx.GetTopLevelParent(control), wintypes.DWORD()
        handle = user32.GetForegroundWindow()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        ready = (owner.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        return ready

    def key(control, code, complete, modifiers=wx.MOD_NONE):
        def down():
            check('target owns native foreground and focus: ' + control.GetName(), foreground(control))
            held[0] = (code, modifiers)
            try:
                check('native key down accepted', simulator.KeyDown(code, modifiers))
            except BaseException:
                release(); raise
            def up():
                release(); later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def named(label):
        value = wx.FindWindowByName(label, observed['dialog'])
        check('named metadata control exists: ' + label, value is not None)
        return value

    def press(label, complete):
        button = named(label)
        check('native metadata action enabled: ' + label, isinstance(button, wx.Button) and button.IsEnabled())
        key(button, wx.WXK_SPACE, complete)

    def numeric_title(text, complete):
        field, remaining = named('Title'), list(text)
        def next_digit():
            if remaining:
                key(field, ord(remaining.pop(0)), next_digit)
            else:
                check('native title edit committed', field.GetValue() == text)
                complete()
        key(field, ord('A'), next_digit, wx.MOD_CONTROL)

    with TemporaryDirectory(prefix='resonance-native-metadata-') as temporary:
        root = Path(temporary).resolve(strict=True)
        exports = root / 'exports'; exports.mkdir()
        pcm, cover = root / 'Original source — Україна.wav', root / 'Original synthetic cover.png'
        source, output = exports / 'Original measured book.m4b', exports / 'Reviewed book.m4b'
        with wave.open(str(pcm), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
            stream.writeframes(b''.join(struct.pack('<h', (index % 80 - 40) * 100) for index in range(24000)))
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
        cover.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 4, 4, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\x20\x60\x90' * 4) * 4)) + chunk(b'IEND', b''))
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic native metadata review')
        settings = settings_module.load_settings(library.root / 'state')
        if evidence is not None:
            settings.update(ffmpeg=evidence['ffmpeg']['path'], ffprobe=evidence['ffprobe']['path'])
        settings_module.save_settings(library.root / 'state', settings)
        stage('prepare original synthetic M4B and measured chapters with actual pinned FFmpeg')
        runtime = settings_module.audio_runtime(exports, state_root=library.root / 'state', input_roots=(root,))
        verify_runtime(runtime)
        runtime.audiobook(pcm, source, title='Original native title',
            chapters=[{'title': 'First α', 'start': 0, 'end': .375},
                      {'title': 'Second 😀', 'start': .375, 'end': 1}])
        baseline = metadata.inspect_metadata(runtime, source)
        packet_hash = metadata._stream_hash(runtime, source, lambda: False)
        source_hash, cover_hash, pcm_hash = baseline.source.sha256, sha256(cover.read_bytes()).hexdigest(), sha256(pcm.read_bytes()).hexdigest()
        original_picker, original_runtime = wx.FileDialog, settings_module.audio_runtime
        choices = [(source, wx.FD_OPEN | wx.FD_FILE_MUST_EXIST),
                   (cover, wx.FD_OPEN | wx.FD_FILE_MUST_EXIST), (output, wx.FD_SAVE)]
        class SelectedPath:
            def __init__(self, *args, **kwargs):
                self.path, style = choices.pop(0)
                check('explicit source/cover/output picker path', kwargs['style'] == style)
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(self.path)
            def Destroy(self): pass
        def selected_runtime(directory, *, state_root, input_roots=(), **kwargs):
            check('metadata retains the selected library and declared local roots',
                Path(state_root) == library.root / 'state' and Path(directory) == exports
                and all(Path(item) in (exports, root) for item in input_roots))
            actual = original_runtime(directory, state_root=state_root, input_roots=input_roots, **kwargs)
            verify_runtime(actual)
            return actual
        wx.FileDialog, settings_module.audio_runtime = SelectedPath, selected_runtime

        def worker_ready(attribute):
            if frame.working or observed['controller'].busy:
                return False
            if getattr(observed['controller'], attribute) is None:
                raise RuntimeError('Native metadata operation returned without its result: '
                    + named('Audio metadata status').GetValue())
            return True

        def opened():
            observed['dialog'] = metadata_dialog()
            observed['controller'] = observed['dialog'].metadata_controller
            check('opening metadata performs no automatic inspection or save',
                  observed['controller'].snapshot is None and not frame.working and not output.exists())
            press('Choose source audio', lambda: press('Inspect source tags and chapters',
                lambda: until(lambda: worker_ready('snapshot'), inspected)))
        def inspected():
            check('native inspection displays original source title', named('Title').GetValue() == 'Original native title')
            numeric_title('2026', choose_mode)
        def choose_mode():
            selection = named('Cover action')
            key(selection, wx.WXK_HOME, lambda: key(selection, wx.WXK_DOWN, selected_cover_mode))
        def selected_cover_mode():
            check('native cover replacement is explicit', named('Cover action').GetSelection() == 1)
            press('Choose replacement cover', lambda: press('Choose new output filename', review_first))
        def review_first():
            press('Review metadata and cover changes',
                lambda: until(lambda: worker_ready('plan'), reviewed_first))
        def reviewed_first():
            plan = observed['controller'].plan
            check('review binds source and selected cover before publication',
                  plan.snapshot.source.sha256 == source_hash and plan.replacement_cover.sha256 == cover_hash
                  and not output.exists() and 'Title: 2026' in named('Reviewed metadata and cover').GetValue())
            choices = review_module.load_choices(library.root)
            check('native review saves only passive choices with exact retained artwork',
                  choices['review_required'] is True and choices['source']['sha256'] == source_hash
                  and choices['cover']['sha256'] == cover_hash and choices['changes']['tags']['title'] == '2026'
                  and not output.exists())
            numeric_title('2027', changed_after_review)
        def changed_after_review():
            check('native editing invalidates the prior save approval',
                  observed['controller'].plan is None and not named('Save reviewed audio copy').IsEnabled())
            press('Review metadata and cover changes',
                lambda: until(lambda: worker_ready('plan'), save))
        def save():
            check('saving requires the newly reviewed title', 'Title: 2027' in named('Reviewed metadata and cover').GetValue())
            stage('save exact reviewed metadata through the existing background worker')
            press('Save reviewed audio copy',
                lambda: until(lambda: worker_ready('result'), saved, timeout=45))
        def saved():
            check('native completion reports the committed new audio', named('Audio metadata status').GetValue().startswith('Saved '))
            check('three explicit picker choices consumed', not choices)
            def verify():
                snapshot = metadata.inspect_metadata(runtime, output, cancelled=frame.cancel_event.is_set)
                result = observed['controller'].result
                return snapshot, json.loads(result.receipt_path.read_bytes()), \
                    sha256(source.read_bytes()).hexdigest(), sha256(cover.read_bytes()).hexdigest(), \
                    sha256(pcm.read_bytes()).hexdigest(), metadata._stream_hash(runtime, output, frame.cancel_event.is_set)
            def failed(exc):
                errors.append('Independent metadata readback failed: ' + str(exc)); abort()
            frame.run_job('Verify native audio metadata result', verify, verified, failed=failed)
        def verified(value):
            snapshot, receipt, original_audio, original_cover, original_pcm, actual_packets = value
            check('actual ffprobe confirms the reviewed title and exact original cover bytes',
                  dict(snapshot.tags)['title'] == '2027' and snapshot.cover.sha256 == cover_hash)
            check('actual encoded audio and measured chapter boundaries are unchanged',
                  actual_packets == packet_hash and snapshot.chapters == baseline.chapters)
            check('every original synthetic input remains unchanged',
                  (original_audio, original_cover, original_pcm) == (source_hash, cover_hash, pcm_hash))
            check('durable adjacent receipt matches actual new output and cover',
                  receipt['output']['sha256'] == snapshot.source.sha256 and receipt['cover']['sha256'] == cover_hash
                  and receipt['audio_packet_sha256'] == packet_hash and receipt['audio_reencoded'] is False)
            observed['output'] = {'sha256': snapshot.source.sha256, 'audio_packet_sha256': actual_packets,
                'cover_sha256': snapshot.cover.sha256, 'chapters': len(snapshot.chapters), 'title': dict(snapshot.tags)['title']}
            observed['complete'] = True
            press('Close', lambda: None)
        def exercise_native_lifetime():
            """Retain a real completed worker result until after native owner deletion."""
            check('metadata lifetime runs inside the real native MainLoop', gui.IsMainLoopRunning())
            outcomes = []
            for termination in ('direct', 'native-parent'):
                native_parent, window = None, None
                host = frame
                ready, resume = Event(), Event()
                delivered, destroyed, late_views = [], [], []
                previous_run_job = None

                def pump(predicate, timeout=10):
                    deadline = monotonic() + timeout
                    while not predicate():
                        if errors:
                            raise RuntimeError('\n'.join(errors))
                        if monotonic() >= deadline:
                            raise TimeoutError('Metadata native lifetime stage did not finish: ' + termination)
                        gui.Yield()
                        sleep(.01)

                try:
                    check('metadata lifetime starts without unrelated work', not frame.working)
                    if termination == 'native-parent':
                        native_parent = wx.Dialog(frame, title='Native metadata parent deletion')
                        metadata_parents.append(native_parent)
                        native_parent.library = frame.library
                        native_parent.cancel_event = frame.cancel_event
                        native_parent.working = native_parent._closing = False
                        native_parent.run_job = frame.run_job
                        native_parent.report = frame.report
                        host = native_parent
                    previous_run_job = host.run_job

                    def held_run_job(title, operation, done=None, failed=None, *, with_progress=False):
                        check('lifetime uses the real metadata worker contract', with_progress)
                        def held_operation(report):
                            value = operation(report)
                            ready.set()
                            if not resume.wait(15):
                                raise TimeoutError('Native metadata owner did not release the real worker result.')
                            return value
                        def observed_done(value):
                            check('real metadata completion is delivered only after native deletion',
                                  window is not None and not window and window.metadata_controller.closed)
                            delivered.append('done')
                            if done is not None:
                                done(value)
                        def observed_failed(error):
                            delivered.append('failed')
                            errors.append('Actual metadata lifetime worker failed: ' + repr(error))
                            if failed is not None:
                                failed(error)
                        return previous_run_job(title, held_operation, observed_done, observed_failed,
                                                with_progress=True)
                    host.run_job = held_run_job

                    def begin():
                        nonlocal window
                        try:
                            window = next(item for item in wx.GetTopLevelWindows()
                                if isinstance(item, wx.Dialog) and item.GetTitle() == 'Audio metadata and cover'
                                and item.GetParent() is host)
                            controller = window.metadata_controller
                            native_id = window.GetId()
                            notify = controller.notify
                            def traced_notify(kind, value):
                                if controller.closed or not window:
                                    late_views.append(kind)
                                return notify(kind, value)
                            controller.notify = traced_notify
                            def on_destroy(event):
                                if event.GetId() == native_id:
                                    destroyed.append(native_id)
                                event.Skip()
                            window.Bind(wx.EVT_WINDOW_DESTROY, on_destroy)
                            wx.Panel(window).Destroy()
                            gui.Yield()
                            check('child destruction preserves live metadata owner', not controller.closed)
                            controller.inspect(str(source))
                            pump(ready.is_set, timeout=30)
                            check('actual inspection result is held before UI completion',
                                  frame.working and controller.busy and not delivered)
                            if termination == 'native-parent':
                                def forbidden_python_destroy():
                                    raise AssertionError('Native parent deletion must bypass Python Destroy.')
                                window.Destroy = forbidden_python_destroy
                                check('native parent destroys metadata child', native_parent.DestroyChildren())
                            else:
                                check('direct metadata destruction accepted', window.Destroy())
                            pump(lambda: destroyed == [native_id] and not window, timeout=5)
                            check('actual metadata owner is detached before late worker delivery',
                                  controller.closed and controller.cancel_event.is_set())
                            resume.set()
                            pump(lambda: delivered and not frame.working, timeout=10)
                            check('one real completion cannot update deleted metadata controls',
                                  delivered == ['done'] and not late_views and controller.snapshot is None)
                            if termination == 'direct':
                                check('repeated metadata Destroy does not touch a deleted peer',
                                      window.Destroy() is False)
                            outcomes.append({'termination': termination,
                                'native_destroy_events': len(destroyed),
                                'real_completion_after_deletion': True})
                        except BaseException:
                            errors.append(traceback.format_exc())
                        finally:
                            resume.set()
                            if window:
                                # Preserve native parent semantics while allowing bounded failure cleanup.
                                wx.Dialog.Destroy(window)
                    wx.CallAfter(begin)
                    ui.show_audio_metadata(host)
                    pump(lambda: not frame.working, timeout=10)
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    check('native lifetime performed no publication', not late_views
                          and sha256(source.read_bytes()).hexdigest() == source_hash)
                finally:
                    resume.set()
                    if previous_run_job is not None:
                        host.run_job = previous_run_job
                    if window:
                        wx.Dialog.Destroy(window)
                    if native_parent:
                        native_parent.Destroy()
                    gui.Yield()
            observed['lifetime'] = outcomes

        def launch():
            stage('open native metadata controls')
            until(lambda: metadata_dialog() is not None, opened)
            try:
                ui.show_audio_metadata(frame)
                release()
                if not errors:
                    exercise_native_lifetime()
            finally:
                release()
                if owner_loop is not None:
                    owner_loop.ScheduleExit(1 if errors else 0)
        def timeout():
            raise TimeoutError('Native metadata exceeded its 120-second budget; see faulthandler stacks.')
        try:
            frame = DesktopFrame(library)
            gui.SetTopWindow(frame); frame.Show()
            later(timeout, 120000, after_failure=True)
            until(lambda: not frame.working, launch)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            check('normal native MainLoop completes actual metadata and independent readback', observed.get('complete') is True)
            check('both actual metadata destruction paths complete', len(observed.get('lifetime', ())) == 2)
            verify_runtime(runtime)
        finally:
            try:
                release()
            finally:
                try:
                    for timer in timers:
                        timer.Stop()
                finally:
                    wx.FileDialog, settings_module.audio_runtime = original_picker, original_runtime
                    if frame is not None:
                        frame._closing = True; frame.cancel_event.set()
                        controller = observed.get('controller')
                        if controller is not None:
                            controller.close()
                        try:
                            frame.executor.shutdown(wait=True, cancel_futures=True)
                        finally:
                            try:
                                frame.operation_progress.close()
                            finally:
                                frame.Destroy(); gui.Yield()
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'package_origin': str(origin),
            'source_identity': identities, 'audio_runtime': audio_proof, 'source_sha256': source_hash,
            'output': observed['output'], 'native_lifetime': observed['lifetime'],
            'native_keyboard': 'PASS', 'actual_ffmpeg_metadata': 'PASS',
            'actual_ffprobe': 'PASS', 'native_file_picker': 'NOT_RUN', 'audio_device': 'NOT_RUN',
            'listening_quality': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc(); raise SystemExit(1)

