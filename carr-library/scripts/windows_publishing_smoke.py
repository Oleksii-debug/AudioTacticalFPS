"""Installed wx/FFmpeg publication acceptance, with optional real local SAPI.

Synthetic fixtures and enumerated local voice output do not qualify speech
quality, EPUBCheck, reader interoperability or NVDA.
"""
import argparse
import importlib.util
from pathlib import Path
from hashlib import file_digest
import json
import math
import os
import re
import struct
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from types import SimpleNamespace
import traceback
import wave


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


def main(argv=None):
    if os.name != 'nt':
        print(json.dumps({'result':'NOT_RUN','reason':'Actual Windows is required.','nvda':'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', required=True, type=Path,
                        help='Explicit path/hash binding for the external test-only FFmpeg runtime')
    parser.add_argument('--daisy-schemas', required=True, type=Path,
                        help='Explicit directory containing the pinned offline validation DTDs')
    parser.add_argument('--require-sapi', action='store_true',
                        help='Also render and publish a short job with an actually enumerated local SAPI voice')
    args = parser.parse_args(argv)
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Use the installed Python interpreter with -I -B for native publication acceptance.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run(args)
    finally:
        if native_handle is not None:
            native_handle.close()


def _run(args):
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.studio import daisy, publishing, render
    from carr_library.studio.render import ProductionService
    from carr_library.speech import runtime as speech_runtime
    from carr_library.studio.publishing import PublicationService, inspect_epub
    from carr_library.ui import publishing_dialog as ui
    from carr_library.ui.desktop_app import DesktopFrame
    errors, checks, audio_proof = [], [], {}
    sapi_proof = {'status': 'NOT_RUN'}
    daisy_proof = {}
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
    def exercise_native_lifetime():
        """Use real native deletion, replaying captured UI callbacks afterward."""
        for termination in ('direct', 'native-parent'):
            native_parent = None
            window = None
            host = frame
            previous_cancel = frame.cancel_event.is_set()
            previous_run_job = None
            original_destroy = None
            captured, completed, delivered, destroyed = {}, [], [], []
            try:
                check('no unrelated host work during native ' + termination + ' lifetime probe', not frame.working)
                if termination == 'native-parent':
                    native_parent = wx.Dialog(frame, title='Native publication parent deletion')
                    native_parent.library = frame.library
                    native_parent.cancel_event = frame.cancel_event
                    native_parent.working = native_parent._closing = False
                    native_parent.run_job = frame.run_job
                    host = native_parent
                previous_run_job = host.run_job

                def pending(title, operation, *, done, failed):
                    captured.update(done=done, failed=failed)
                    return True

                host.run_job = pending
                window = ui.create_publishing_dialog(host)
                native_id = window.GetId()
                original_destroy = window.Destroy

                def observed(event):
                    if event.GetId() == native_id:
                        destroyed.append(True)
                    event.Skip()

                window.Bind(wx.EVT_WINDOW_DESTROY, observed)
                wx.Panel(window).Destroy()
                gui.Yield()
                check('child destruction preserves the live publication view: ' + termination,
                      window.alive and not window.cancel.is_set())
                check('actual publication callback is pending: ' + termination,
                      window.begin('Native publication lifetime fixture', lambda: None, completed.append)
                      and window.busy and set(captured) == {'done', 'failed'})

                if termination == 'native-parent':
                    def forbidden_python_destroy():
                        raise AssertionError('Native parent deletion must bypass the Python Destroy override.')
                    window.Destroy = forbidden_python_destroy
                    check('native parent destroys its actual publication child', native_parent.DestroyChildren())
                else:
                    check('native publication Destroy request is accepted', window.Destroy())
                check('publication destruction detaches and cancels before late callbacks: ' + termination,
                      not window.alive and window.cancel.is_set())
                until(lambda: destroyed == [True] and not window, timeout=5)
                check('one real destruction event and deleted native peer: ' + termination,
                      destroyed == [True] and not window)

                def replay_callbacks():
                    try:
                        window.report('Queued report after actual native deletion.')
                        captured['done']({'fixture': 'late completion'})
                        captured['failed'](RuntimeError('late failure'))
                        window.controls()
                        delivered.append(True)
                    except BaseException:
                        errors.append(traceback.format_exc())

                wx.CallAfter(replay_callbacks)
                until(lambda: delivered == [True], timeout=5)
                check('queued report and completion cannot touch deleted controls: ' + termination,
                      not completed and not window.alive and not window.busy)
                if termination == 'direct':
                    check('repeated Destroy is harmless after native deletion', window.Destroy() is False)
            finally:
                if previous_run_job is not None:
                    host.run_job = previous_run_job
                if window:
                    if original_destroy is not None:
                        window.Destroy = original_destroy
                    window.Destroy()
                if native_parent:
                    native_parent.Destroy()
                gui.Yield()
                if not previous_cancel:
                    frame.cancel_event.clear()

    def verify_audio(runtime):
        bound = verify_audio_evidence(runtime, args.audio_evidence)
        if audio_proof and bound != audio_proof:
            raise RuntimeError('External audio identity changed during publication acceptance.')
        audio_proof.update(bound)
        for name in ('ffmpeg', 'ffprobe'):
            check('actual external executable matches its explicit path/hash evidence: ' + name,
                  bound[name]['sha256'] and bound['bundled'] is False)
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
            source_bindings = verify_installed_sources(ui, publishing, daisy, render, speech_runtime)
            check('publication and native speech modules match the exact installed checkout sources',
                  len(source_bindings) == 5)
            check('real native Windows widgets', wx.Platform == '__WXMSW__')
            library = LibraryApp(root/'library')
            service = ProductionService(library.root/'state'/'studio', voices=SyntheticTones())
            verify_audio(service._runtime())
            script = '[Scene: Original]\nNarrator: Preserve <source> & exact text.\n[Pause: 0.2]\nReader: The second line.'
            project = service.create(script, title='Native EPUB fixture', voice_ids=('synthetic-tone',))
            check('synthetic publication uses the supported project schema1', project.schema == 1)
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
                    daisy_target = root / 'publication-daisy.zip'
                    dialog.format_choice.SetSelection(1)
                    dialog.publisher.ChangeValue('Explicit synthetic DAISY publisher')
                    dialog.output.ChangeValue(str(daisy_target))
                    press(dialog.buttons['export']); until(lambda: not dialog.busy)
                    check('native DAISY export uses explicit publisher and shared receipt authority',
                          daisy_target.exists() and len(dialog.receipts) == 2
                          and dialog.receipts[-1]['format'] == 'daisy-2.02'
                          and 'DAISY saved and its bytes verified' in dialog.status.GetValue())
                    daisy_source = daisy.inspect_daisy(daisy_target)
                    check('DAISY retains the exact original script and measured narration',
                          daisy_source['original_script'] == script
                          and daisy_source['publisher'] == 'Explicit synthetic DAISY publisher'
                          and sum(row['kind'] == 'line' for row in daisy_source['intervals']) == 2)
                    dialog.format_choice.SetSelection(0)
                    press(dialog.buttons['verify']); until(lambda: not dialog.busy)
                    check('native verification follows saved DAISY format despite changed export preference',
                          'Verified exact saved DAISY bytes' in dialog.status.GetValue())
                    validation_path = Path(__file__).with_name('daisy_schema_acceptance.py')
                    specification = importlib.util.spec_from_file_location('daisy_validation_acceptance', validation_path)
                    validator = importlib.util.module_from_spec(specification)
                    specification.loader.exec_module(validator)
                    daisy_proof.update(validator.validate(daisy_target, args.daisy_schemas))
                    check('actual offline DAISY DTD validation includes malformed XHTML negative control',
                          daisy_proof['status'] == 'PASS' and daisy_proof['invalid_xhtml_rejected']
                          and len(daisy_proof['documents']) == 4)
                    check('EPUB bytes remain unchanged beside the separate DAISY publication',
                          target.read_bytes() == original)
                    press(dialog.buttons['close'])
                    check('native keyboard close completes', not dialog.alive)
                    exercise_native_lifetime()
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()
            wx.CallAfter(scenario)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            if args.require_sapi:
                registry = speech_runtime.VoiceRegistry((speech_runtime.NativeSapiProvider(),))
                sapi_service = ProductionService(root / 'sapi-library' / 'state' / 'studio', voices=registry)
                voices = [voice for voice in sapi_service.voices.voices()
                          if voice.id.startswith('sapi5-') and voice.engine.startswith('SAPI 5')]
                check('an actual local SAPI voice was enumerated', bool(voices))
                voices.sort(key=lambda voice: (not voice.language.lower().startswith('en'), voice.id))
                voice = voices[0]
                verify_audio(sapi_service._runtime())
                spoken_script = 'Narrator: A local Windows voice publishes this short source.'
                spoken = sapi_service.create(spoken_script, title='Actual local SAPI publication',
                                             voice_ids=(voice.id,))
                check('actual native SAPI publication uses project schema1', spoken.schema == 1)
                destination = root / 'sapi-production'; destination.mkdir()
                spoken_id = sapi_service.submit(spoken, destination, stems=False)
                spoken_job = sapi_service.run(spoken_id)
                check('actual SAPI and FFmpeg completed the current Studio job',
                      spoken_job['status'] == 'complete' and bool(spoken_job['result']['lines']))
                spoken_epub = root / 'actual-sapi.epub'
                spoken_receipt = PublicationService(sapi_service).export_epub(
                    spoken_id, spoken_epub, language='en')
                spoken_provenance = inspect_epub(spoken_epub)
                check('actual SAPI publication retains exact source and measured audio identity',
                      spoken_receipt['status'] == 'complete'
                      and spoken_provenance['original_script'] == spoken_script
                      and spoken_provenance['audio']['codec'] == 'mp3'
                      and spoken_provenance['audio']['identity']['size'] > 0)
                verify_audio(sapi_service._runtime())
                sapi_proof = {'status': 'PASS', 'voice_id': voice.id, 'voice_engine': voice.engine,
                              'voice_language': voice.language, 'publication': spoken_receipt['artifact'],
                              'speech_quality': 'NOT_ASSESSED', 'epubcheck': 'NOT_RUN'}
                if hasattr(sapi_service, 'close'):
                    sapi_service.close()
            verify_audio(service._runtime())
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
        if errors: raise RuntimeError('\n'.join(errors))
    print(json.dumps({'result':'PASS','checks':checks,'audio':'Synthetic PCM tones through actual FFmpeg',
        'audio_runtime':audio_proof,'sapi_publication':sapi_proof,'daisy_structural_validation':daisy_proof,
        'installed_sources':source_bindings,
        'native_keyboard':'PASS','native_direct_and_parent_destruction':'PASS','epubcheck':'NOT_RUN',
        'reader_interoperability':'NOT_RUN','manual_keyboard':'NOT_RUN','nvda':'NOT_RUN'},indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
