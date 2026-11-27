"""Installed wx/FFmpeg EPUB publishing acceptance; synthetic tones, no reader/NVDA claim."""
import argparse
from pathlib import Path
from hashlib import file_digest
import json
import math
import os
import struct
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from types import SimpleNamespace
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result':'NOT_RUN','reason':'Actual Windows is required.','nvda':'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-directory', required=True, type=Path,
                        help='Explicit pinned test audio directory; it need not be in the application bundle')
    parser.add_argument('--audio-lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    args = parser.parse_args()
    selected_audio = args.audio_directory.resolve(strict=True)
    audio_pin = json.loads(args.audio_lock.read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.studio.render import ProductionService
    from carr_library.studio.publishing import inspect_epub
    from carr_library.ui import publishing_dialog as ui
    from carr_library.ui.desktop_app import DesktopFrame
    errors, checks, audio_proof = [], [], {}
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc()); return False
    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    def check(name, condition):
        if not condition: raise AssertionError(name)
        checks.append(name); print('PASS:', name, flush=True)
    def until(predicate, timeout=30):
        deadline = monotonic() + timeout
        while not predicate():
            if errors: raise RuntimeError('\n'.join(errors))
            if monotonic() > deadline: raise TimeoutError('Native EPUB operation did not finish.')
            gui.Yield(); sleep(.01)
    def press(button):
        top = wx.GetTopLevelParent(button)
        top.Raise(); user32.SetForegroundWindow(top.GetHandle()); button.SetFocus()
        until(lambda: wx.Window.FindFocus() is button and user32.GetForegroundWindow() == top.GetHandle())
        check('native Space down: ' + button.GetLabel(), simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield(); sleep(.04)
        finally:
            check('native Space up: ' + button.GetLabel(), simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()
    def verify_audio(runtime):
        bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
        manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8')) if bundle else None
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            audio_proof[name] = {'path': str(binary), 'sha256': digest}
            selected = selected_audio / 'bin' / (name + '.exe')
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            check('explicit selected audio executable: ' + name, binary.samefile(selected))
            check('pinned audio executable bytes: ' + name,
                  binary.stat().st_size == expected['size'] and digest == expected['sha256'])
            if bundle and manifest.get('bundled_audio'):
                entry = 'audio/bin/' + name + '.exe'
                check('exact bundled executable: ' + name, binary == (bundle / entry).resolve(strict=True))
                check('sealed executable bytes: ' + name, digest == manifest['files'][entry])
    class SyntheticTones:
        def voices(self):
            return (SimpleNamespace(id='synthetic-tone',name='Synthetic tone fixture',language='und',engine='test fixture'),)
        def synthesize(self, text, voice_id, destination, *, settings, cancelled):
            if cancelled(): raise InterruptedError('Synthetic production cancelled.')
            with wave.open(str(destination), 'wb') as stream:
                stream.setparams((1,2,24000,0,'NONE','synthetic'))
                stream.writeframes(b''.join(struct.pack('<h',int(3000*math.sin(2*math.pi*440*i/24000))) for i in range(4800)))
    frame = dialog = None
    with TemporaryDirectory(prefix='resonance-native-epub-') as temporary:
        root = Path(temporary)
        try:
            check('publication UI comes from installed package',
                  'site-packages' in {part.casefold() for part in Path(ui.__file__).resolve().parts})
            check('real native Windows widgets', wx.Platform == '__WXMSW__')
            library = LibraryApp(root/'library')
            service = ProductionService(library.root/'state'/'studio', voices=SyntheticTones())
            verify_audio(service._runtime())
            script = '[Scene: Original]\nNarrator: Preserve <source> & exact text.\n[Pause: 0.2]\nReader: The second line.'
            project = service.create(script, title='Native EPUB fixture', voice_ids=('synthetic-tone',))
            source_id = service.submit(project, root, stems=False)
            completed = service.run(source_id)
            check('actual FFmpeg produced a completed Studio fixture', completed['status'] == 'complete')
            frame = DesktopFrame(library); frame.Show(); gui.SetTopWindow(frame)
            dialog = ui.create_publishing_dialog(frame); dialog.Show()
            def scenario():
                try:
                    check('real native main loop is active', gui.GetMainLoop() is not None)
                    check('export controls have native accessible names', isinstance(dialog.job_choice,wx.Choice)
                          and 'Completed Studio job' in dialog.job_choice.GetName()
                          and isinstance(dialog.status,wx.TextCtrl) and 'Publication status' in dialog.status.GetName())
                    press(dialog.buttons['refresh']); until(lambda: not dialog.busy)
                    check('completed job is listed without implicit selection',len(dialog.jobs)==1 and dialog.job_choice.GetSelection()==wx.NOT_FOUND)
                    target = root/'publication.epub'
                    dialog.job_choice.SetSelection(0); dialog.language.ChangeValue('en'); dialog.output.ChangeValue(str(target))
                    press(dialog.buttons['export']); until(lambda: not dialog.busy)
                    check('native export completed with a durable receipt', target.exists() and len(dialog.receipts)==1
                          and 'saved and its bytes verified' in dialog.status.GetValue())
                    check('shared progress completes only after publication verification',frame.operation_progress.event.state=='succeeded')
                    check('native completion focuses readable publication status',wx.Window.FindFocus()==dialog.status)
                    provenance = inspect_epub(target)
                    check('exact script and measured line overlays are retained',provenance['original_script']==script
                          and sum(row['kind']=='line' for row in provenance['intervals'])==2)
                    check('embedded audio is MP3 with an actual byte identity',provenance['audio']['codec']=='mp3'
                          and provenance['audio']['identity']['size']>0)
                    press(dialog.buttons['verify']); until(lambda: not dialog.busy)
                    check('native explicit receipt verification succeeds','Verified exact saved EPUB bytes' in dialog.status.GetValue())
                    original = target.read_bytes(); press(dialog.buttons['export']); until(lambda: not dialog.busy)
                    check('native duplicate export cannot overwrite completed bytes',target.read_bytes()==original
                          and 'not be overwritten' in dialog.status.GetValue())
                    press(dialog.buttons['close'])
                    check('native keyboard close completes', not dialog.alive)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()
            wx.CallAfter(scenario)
            gui.MainLoop()
        finally:
            # Wait for the owned operation before deleting temporary sources;
            # otherwise a failed assertion can mask its cause with Windows
            # open-file cleanup errors and leave a worker running.
            if dialog is not None:
                dialog.on_close()
            if frame is not None:
                frame.cancel_event.set(); frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
            if dialog is not None:
                dialog.Destroy()
            if frame is not None:
                frame.Destroy()
            gui.Yield()
            if native_handle is not None:
                native_handle.close()
        if errors: raise RuntimeError('\n'.join(errors))
    print(json.dumps({'result':'PASS','checks':checks,'audio':'Synthetic PCM tones through actual FFmpeg',
        'audio_runtime':audio_proof,'native_keyboard':'PASS','epubcheck':'NOT_RUN',
        'reader_interoperability':'NOT_RUN','manual_keyboard':'NOT_RUN','nvda':'NOT_RUN'},indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
