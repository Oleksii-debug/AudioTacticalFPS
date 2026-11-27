"""Installed W15 native review/save, with actual pinned FFmpeg and original fixtures.

Only three explicit file-picker paths are substituted. Native keys operate the
real named controls, and the existing Desktop worker performs all metadata work.
No device, listening, screen-reader or third-party player claim is made.
"""
import faulthandler
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
import wave
import zlib


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'native_keyboard': 'NOT_RUN', 'actual_ffmpeg': 'NOT_RUN', 'nvda': 'NOT_RUN'}))
        return 77
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        return _run_native()
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native():
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

    checks, errors, identities, audio_proof, observed = [], [], {}, {}, {}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label); print('PASS: ' + label, flush=True)
    def stage(label): print('STAGE: ' + label, flush=True)

    checkout = Path(__file__).resolve().parents[1]
    origin = Path(carr_library.__file__).resolve()
    bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
    check('installed package origin', origin.is_relative_to(bundle) if bundle else 'site-packages' in origin.parts)
    for module in (ui, metadata, settings_module, dsp_module, process_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__, not actual.is_relative_to(checkout)
              and digest == sha256(expected.read_bytes()).hexdigest())
        identities[module.__name__] = digest
    lock = json.loads((checkout / 'packaging/windows-audio.lock.json').read_text(encoding='utf-8'))
    declared_audio = bundle / 'audio' if bundle else Path(os.environ['CARR_CI_AUDIO']).resolve(strict=True)
    manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8')) if bundle else None

    def verify_runtime(runtime):
        for name in ('ffmpeg', 'ffprobe'):
            path = Path(getattr(runtime, name)).resolve(strict=True)
            relative = 'bin/' + name + '.exe'
            expected = lock['output_files'][relative]
            with path.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            check('selected installed executable: ' + name,
                  path == (declared_audio / relative).resolve(strict=True))
            check('exact pinned executable bytes: ' + name,
                  path.stat().st_size == expected['size'] and digest == expected['sha256'])
            if bundle:
                check('sealed bundle executable identity: ' + name, digest == manifest['files']['audio/' + relative])
            audio_proof[name] = {'sha256': digest, 'size': path.stat().st_size}

    frame, owner_loop = None, None
    timers, held = [], [None]
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

    def abort():
        try:
            if frame is not None:
                frame.cancel_event.set()
            try:
                release()
            except Exception:
                errors.append(traceback.format_exc())
            dialog = metadata_dialog()
            if dialog is not None:
                dialog.metadata_controller.close()
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            if owner_loop is not None:
                owner_loop.ScheduleExit(1)

    def later(callback, delay=25):
        def invoke():
            nonlocal owner_loop
            if owner_loop is None:
                owner_loop = gui.GetMainLoop()
            if errors:
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
        root = Path(temporary)
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
        def launch():
            stage('open native metadata controls')
            until(lambda: metadata_dialog() is not None, opened)
            try:
                ui.show_audio_metadata(frame)
            finally:
                release()
                if owner_loop is not None:
                    owner_loop.ScheduleExit(1 if errors else 0)
        def timeout():
            raise TimeoutError('Native metadata exceeded its 120-second budget; see faulthandler stacks.')
        try:
            frame = DesktopFrame(library)
            gui.SetTopWindow(frame); frame.Show()
            later(timeout, 120000)
            until(lambda: not frame.working, launch)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            check('normal native MainLoop completes actual metadata and independent readback', observed.get('complete') is True)
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
            'output': observed['output'], 'native_keyboard': 'PASS', 'actual_ffmpeg_metadata': 'PASS',
            'actual_ffprobe': 'PASS', 'native_file_picker': 'NOT_RUN', 'audio_device': 'NOT_RUN',
            'listening_quality': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc(); raise SystemExit(1)

