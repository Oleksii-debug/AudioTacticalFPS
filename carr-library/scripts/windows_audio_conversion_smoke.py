"""Installed W14 native numeric conversion through real pinned FFmpeg/ffprobe.

Only the source/output file-picker results are substituted with owned synthetic
paths. Actual native keys edit the real form; the existing DesktopFrame worker
performs conversion. No playback, device, listening or NVDA claim is made.
"""
from dataclasses import replace
import faulthandler
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'native_keyboard': 'NOT_RUN', 'actual_ffmpeg': 'NOT_RUN', 'nvda': 'NOT_RUN'}))
        return 77
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(65, repeat=True, file=sys.stderr)
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
    from carr_library.audio.process import run_process
    from carr_library.ui.desktop_app import DesktopFrame
    import carr_library.runtime_settings as settings_module
    import carr_library.audio.dsp as dsp_module
    import carr_library.audio.process as process_module
    import carr_library.ui.studio_dialog as ui

    checks, errors, observed, audio_proof, identities = [], [], {}, {}, {}
    def check(label, condition):
        if not condition: raise AssertionError(label)
        checks.append(label); print('PASS: ' + label, flush=True)
    def stage(label): print('STAGE: ' + label, flush=True)

    checkout = Path(__file__).resolve().parents[1]
    origin = Path(carr_library.__file__).resolve()
    bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
    check('installed package origin', origin.is_relative_to(bundle) if bundle else 'site-packages' in origin.parts)
    for module in (ui, settings_module, dsp_module, process_module):
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
            with path.open('rb') as stream: digest = file_digest(stream, 'sha256').hexdigest()
            check('actual selected executable is the declared installed runtime: ' + name,
                  path == (declared_audio / relative).resolve(strict=True))
            check('exact pinned executable bytes: ' + name,
                  path.stat().st_size == expected['size'] and digest == expected['sha256'])
            if bundle: check('sealed bundle executable identity: ' + name, digest == manifest['files']['audio/' + relative])
            audio_proof[name] = {'path': str(path), 'sha256': digest, 'size': path.stat().st_size}

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc()); fail(); return False
    gui = ProbeApp(False)
    check('real Windows wx controls', wx.Platform == '__WXMSW__')
    simulator, held = wx.UIActionSimulator(), [None]
    frame, watchdog = None, None
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        key, held[0] = held[0], None
        if key is not None: check('native key and modifiers released', simulator.KeyUp(*key))
    def fail():
        if frame is not None: frame.cancel_event.set()
        try: release()
        except Exception: errors.append(traceback.format_exc())
        for window in tuple(wx.GetTopLevelWindows()):
            try:
                if isinstance(window, wx.Dialog) and window.IsModal(): window.EndModal(wx.ID_CANCEL)
            except RuntimeError: pass
        gui.ExitMainLoop()
    def later(callback, delay=25):
        def invoke():
            if errors: return
            try: callback()
            except Exception: errors.append(traceback.format_exc()); fail()
        return wx.CallLater(delay, invoke)
    def until(predicate, complete, *, timeout=15):
        deadline = monotonic() + timeout
        def poll():
            if predicate(): complete()
            elif monotonic() >= deadline: raise TimeoutError('Native audio-conversion stage did not finish.')
            else: later(poll)
        later(poll)
    def foreground(control):
        top, owner = wx.GetTopLevelParent(control), wintypes.DWORD()
        handle = user32.GetForegroundWindow()
        if handle: user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        ready = (owner.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        return ready
    def key(control, code, complete, modifiers=wx.MOD_NONE):
        def down():
            check('target owns native foreground and focus: ' + control.GetName(), foreground(control))
            held[0] = (code, modifiers)
            try: check('native key down accepted', simulator.KeyDown(code, modifiers))
            except BaseException: release(); raise
            def up(): release(); later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)
    def replace_text(control, text, complete):
        codes = [ord(char) for char in text] if text else [wx.WXK_BACK]
        def type_next():
            if codes: key(control, codes.pop(0), type_next)
            else:
                check('native numeric text committed: ' + control.GetName(), control.GetValue() == text)
                complete()
        key(control, ord('A'), type_next, wx.MOD_CONTROL)
    def conversion_dialog():
        return next((window for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog)
                     and window.GetTitle() == 'Convert audio and apply effects'), None)
    def named(name):
        result = wx.FindWindowByName(name, observed['dialog'])
        check('named numeric control exists: ' + name, isinstance(result, wx.TextCtrl))
        return result

    with TemporaryDirectory(prefix='resonance-native-conversion-') as temporary:
        root = Path(temporary)
        source, output = root / 'Synthetic source — Україна.wav', root / 'New 48k mono 24bit.wav'
        samples = bytearray()
        for index in range(48000):
            samples.extend(struct.pack('<hh', round(6000 * math.sin(2 * math.pi * 440 * index / 24000)),
                                       round(4000 * math.sin(2 * math.pi * 660 * index / 24000))))
        with wave.open(str(source), 'wb') as stream:
            stream.setparams((2, 2, 24000, 0, 'NONE', 'PCM')); stream.writeframes(samples)
        original = sha256(source.read_bytes()).hexdigest()
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic native audio conversion')
        original_picker, original_runtime = wx.FileDialog, settings_module.audio_runtime
        picks = [source, output]
        class SelectedPath:
            def __init__(self, *args, **kwargs):
                self.path = picks.pop(0)
                check('only explicit source/output picker paths are admitted',
                    kwargs['style'] == (wx.FD_OPEN | wx.FD_FILE_MUST_EXIST if self.path == source else wx.FD_SAVE))
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(self.path)
            def Destroy(self): pass
        def runtime(directory, *, state_root, input_roots=(), **kwargs):
            check('actual conversion retains the original library and selected input scope',
                Path(directory) == output.parent and Path(state_root) == library.root / 'state'
                and tuple(input_roots) == (source.parent,))
            value = original_runtime(directory, state_root=state_root, input_roots=input_roots, **kwargs)
            verify_runtime(value)
            observed['runtime'] = value
            return value
        wx.FileDialog, settings_module.audio_runtime = SelectedPath, runtime

        def configure():
            stage('enter exact conversion settings with native keys')
            observed['dialog'] = conversion_dialog()
            rate = named('Output sample rate in Hz, 8000 to 192000; Opus requires 48000')
            channels = named('Output channels: 1 for mono, 2 for stereo')
            depth = named('PCM bit depth: 16 or 24; 32 for WAV or AIFF only')
            loudness = named('Target loudness in LUFS; empty leaves levels unchanged')
            observed['fields'] = rate, channels, depth, loudness
            replace_text(rate, '48000', lambda: replace_text(channels, '1',
                lambda: replace_text(depth, '24', lambda: replace_text(loudness, '', submit))))
        def submit():
            check('no audio is written before explicit confirmation', not output.exists())
            button = wx.FindWindowById(wx.ID_OK, observed['dialog'])
            check('native conversion OK button is enabled', isinstance(button, wx.Button) and button.IsEnabled())
            stage('confirm conversion and wait for the existing background worker')
            key(button, wx.WXK_SPACE, lambda: None)
        def converted():
            print('CONVERSION_STATUS: ' + frame.status.GetValue(), flush=True)
            check('native completion reports actual committed settings', frame.status.GetValue().startswith('Saved ')
                  and '48000 Hz; 1 channel(s)' in frame.status.GetValue())
            check('both picker choices were consumed exactly once', not picks)
            check('real conversion publishes a new output and preserves original bytes', output.is_file()
                  and sha256(source.read_bytes()).hexdigest() == original)
            stage('independently inspect saved PCM with the same pinned ffprobe')
            def verify():
                runtime = observed['runtime']
                info = runtime.inspect(output, cancelled=frame.cancel_event.is_set)
                raw = run_process([runtime.ffprobe, '-v', 'error', '-protocol_whitelist', 'file',
                    '-format_whitelist', 'wav', '-select_streams', 'a:0', '-show_entries',
                    'stream=codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample,duration_ts,time_base:format=duration',
                    '-of', 'json', str(output)], budget=replace(runtime.budget, timeout=10),
                    cancelled=frame.cancel_event.is_set).stdout
                return info, json.loads(raw), sha256(output.read_bytes()).hexdigest()
            def failed(error):
                errors.append(f'Independent saved-output verification failed: {error}')
                fail()
            frame.run_job('Verify native conversion result', verify, verified, failed=failed)
        def verified(result):
            info, document, digest = result
            stream, = document['streams']
            check('actual ffprobe confirms 48000 Hz mono 24-bit PCM',
                stream['codec_name'] == 'pcm_s24le' and int(stream['sample_rate']) == 48000
                and stream['channels'] == 1 and int(stream['bits_per_sample']) == 24)
            check('actual runtime measurement agrees with the selected duration and frame count',
                info.codec == 'pcm_s24le' and info.sample_rate == 48000 and info.channels == 1
                and abs(info.duration - 2) < .001 and info.frames == 96000)
            check('source remains unchanged after independent inspection', sha256(source.read_bytes()).hexdigest() == original)
            observed['output'] = dict(sha256=digest, sample_rate=info.sample_rate, channels=info.channels,
                                      bit_depth=24, duration=info.duration, frames=info.frames, codec=info.codec)
            observed['complete'] = True
            later(gui.ExitMainLoop)
        def launch():
            stage('open actual conversion form')
            until(lambda: conversion_dialog() is not None, configure)
            ui.show_audio_conversion(frame)
            release()  # A native modal result can precede the scheduled release continuation.
            if not errors:
                until(lambda: not frame.working, converted, timeout=40)
        def timeout(): raise TimeoutError('Native conversion exceeded its 80-second overall budget; see faulthandler stacks.')
        try:
            frame = DesktopFrame(library)
            gui.SetTopWindow(frame); frame.Show()
            watchdog = later(timeout, 80000)
            until(lambda: not frame.working, launch)
            gui.MainLoop()
            if errors: raise RuntimeError('\n'.join(errors))
            check('normal native MainLoop completed actual conversion and inspection', observed.get('complete') is True)
        finally:
            try:
                release()
            finally:
                try:
                    if watchdog is not None: watchdog.Stop()
                finally:
                    wx.FileDialog, settings_module.audio_runtime = original_picker, original_runtime
                    if frame is not None:
                        frame._closing = True; frame.cancel_event.set()
                        try:
                            frame.executor.shutdown(wait=True, cancel_futures=True)
                        finally:
                            try: frame.operation_progress.close()
                            finally:
                                frame.Destroy(); gui.Yield()
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'package_origin': str(origin),
            'source_identity': identities, 'audio_runtime': audio_proof, 'source_sha256': original,
            'output': observed['output'], 'native_keyboard': 'PASS', 'actual_ffmpeg_conversion': 'PASS',
            'actual_ffprobe': 'PASS', 'native_file_picker': 'NOT_RUN', 'audio_device': 'NOT_RUN',
            'listening_quality': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception:
        traceback.print_exc(); raise SystemExit(1)
