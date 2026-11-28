"""Native DAISY authoring and imported EPUB recorded-audio controls.

Purpose-written PCM exercises the existing FFmpeg/WinMM path. This is not
DAISY hardware-reader, model, quotation, acoustic-quality or NVDA qualification.
"""
from argparse import ArgumentParser
import ctypes
import faulthandler
from hashlib import sha256
import importlib
import json
import math
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
from types import SimpleNamespace
import wave

def _installed_package_root(distributions, package_file, checkout):
    """Bind installed acceptance to the imported package's actual wheel record.

    A checkout egg-info entry may precede the wheel in metadata discovery.
    Its project-relative locate_file('carr_library') is not package evidence.
    Source acceptance never consults installed distribution metadata.
    """
    actual = Path(package_file).resolve(strict=True)
    checkout = Path(checkout).resolve(strict=True)
    if actual.is_relative_to(checkout):
        raise RuntimeError('Installed acceptance imported the checkout package.')
    matches = []
    for distribution in distributions:
        for member in distribution.files or ():
            if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                continue
            try:
                declared = Path(distribution.locate_file(member)).resolve(strict=True)
            except FileNotFoundError:
                continue  # An unrelated stale metadata entry is not this package.
            if declared == actual:
                matches.append(declared.parent)
    if len(matches) != 1:
        raise RuntimeError('Exactly one installed wheel record must own the imported package.')
    return matches[0]


def run_native(source, report, checkpoint):
    import wx
    from ctypes import wintypes
    from carr_library.application import LibraryApp
    from carr_library.audio.local_runtime import open_local_audio_runtime
    from carr_library.audio.runtime import discover_bundled_audio
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.studio.render import ProductionService
    from carr_library.studio.daisy import inspect_daisy
    from carr_library.studio.publishing import inspect_epub
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.publishing_dialog import create_publishing_dialog
    from carr_library.ui.reading_desk import ReadingDialog

    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    simulator = wx.UIActionSimulator()
    deadline, failures = monotonic() + 210, []
    checkout = Path(__file__).resolve().parents[1]
    names = ('carr_library.studio.publishing', 'carr_library.studio.publishing_source',
        'carr_library.studio.daisy', 'carr_library.reader.ebook_audio',
        'carr_library.ui.publishing_dialog', 'carr_library.ui.ebook_audio_dialog',
        'carr_library.ui.ebook_model',
        'carr_library.ui.reading_desk', 'carr_library.speech.player',
        'carr_library.speech._wave_output',
        'carr_library.runtime_settings', 'carr_library.audio.cleanup')
    modules = [importlib.import_module(name) for name in names]
    origins = {module.__name__: Path(module.__file__).resolve(strict=True) for module in modules}
    installed = None
    if source is None:
        import carr_library
        from importlib.metadata import distributions
        installed = _installed_package_root(distributions(name='universal-research-library'),
                                            carr_library.__file__, checkout)

    def check(label, value):
        if not value:
            raise AssertionError(label)
        report['checks'].append(label); checkpoint(label)

    for name, actual in origins.items():
        expected = checkout / 'src' / Path(*name.split('.')).with_suffix('.py')
        check('exact imported publication/reader module: ' + name,
            sha256(actual.read_bytes()).hexdigest() == sha256(expected.read_bytes()).hexdigest()
            and (actual.is_relative_to(source / 'src') if source is not None else actual.is_relative_to(installed)))
    check('actual Windows native widgets', wx.Platform == '__WXMSW__')

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]; user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []; user32.GetForegroundWindow.restype = wintypes.HWND

    def pump(predicate, timeout=30):
        end = min(deadline, monotonic() + timeout)
        while not predicate():
            if failures:
                raise RuntimeError(failures[0])
            if monotonic() > end:
                raise TimeoutError('Native publication/recorded-audio operation did not reach its required state.')
            app.Yield(); sleep(.01)

    def press(button):
        top = wx.GetTopLevelParent(button)
        top.Raise(); user32.SetForegroundWindow(top.GetHandle()); button.SetFocus()
        pump(lambda: wx.Window.FindFocus() is button and user32.GetForegroundWindow() == top.GetHandle(), 5)
        check('native Space down: ' + button.GetLabel(), simulator.KeyDown(wx.WXK_SPACE))
        try:
            app.Yield(); sleep(.02)
        finally:
            check('native Space up: ' + button.GetLabel(), simulator.KeyUp(wx.WXK_SPACE))
        app.Yield()

    def button(parent, label):
        found = []
        def visit(window):
            for child in window.GetChildren():
                if isinstance(child, wx.Button) and child.GetLabel().replace('&', '') == label:
                    found.append(child)
                visit(child)
        visit(parent)
        if len(found) != 1:
            raise AssertionError('Expected one actual native button: ' + label)
        return found[0]

    class SyntheticTones:
        def voices(self):
            return (SimpleNamespace(id='synthetic-tone', name='Explicit recorded-audio fixture',
                                    language='und', engine='synthetic protocol'),)
        def synthesize(self, text, voice_id, destination, *, settings, cancelled):
            if cancelled(): raise InterruptedError('Synthetic fixture cancelled.')
            assert voice_id == 'synthetic-tone'
            with wave.open(str(destination), 'wb') as out:
                out.setparams((1, 2, 24000, 0, 'NONE', 'synthetic PCM'))
                out.writeframes(b''.join(struct.pack('<h', int(2000 * math.sin(2 * math.pi * 440 * n / 24000)))
                                          for n in range(48000)))

    frame = publisher = reading = overlay = service = binding = None
    with TemporaryDirectory(prefix='carr-native-epub-daisy-') as temporary:
        root = Path(temporary)
        try:
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Recorded publication fixture')
            binding = open_local_audio_runtime(library.root, strict=True)
            service = ProductionService(library.root / 'state/studio', voices=SyntheticTones())
            runtime = service._runtime()
            bundle = discover_bundled_audio()
            audio_hashes = {}
            for name in ('ffmpeg', 'ffprobe'):
                path = Path(getattr(runtime, name)).resolve(strict=True)
                audio_hashes[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
                if bundle is not None:
                    bundle_root = Path(bundle).resolve(strict=True)
                    manifest = json.loads((bundle_root / 'bundle-manifest.json').read_text(encoding='utf-8'))
                    relative = path.relative_to(bundle_root).as_posix()
                    check('sealed configured executable: ' + name,
                        relative in {'audio/bin/' + name + '.exe', 'audio-runtime/bin/' + name + '.exe'}
                        and manifest['files'][relative] == audio_hashes[name]['sha256'])
            script = '[Scene: Exact retained section]\nNarrator: Recorded source words stay unchanged.\nReader: The second recorded source interval.'
            project = service.create(script, title='Native synchronized fixture', voice_ids=('synthetic-tone',))
            job = service.submit(project, root, stems=False)
            check('actual completed synthetic PCM production', service.run(job)['status'] == 'complete')
            frame = DesktopFrame(library, audio_runtime_binding=binding); frame.Show(); app.SetTopWindow(frame)
            pump(lambda: not frame.working)
            publisher = create_publishing_dialog(frame); publisher.Show()
            press(publisher.buttons['refresh']); pump(lambda: not publisher.busy)
            check('completed job requires explicit native selection',
                  len(publisher.jobs) == 1 and publisher.job_choice.GetSelection() == wx.NOT_FOUND)
            publisher.job_choice.SetSelection(0)
            publisher.language.ChangeValue('en')
            publisher.publisher.ChangeValue('Purpose-written native acceptance publisher')
            epub, daisy = root / 'recorded.epub', root / 'talking-book.zip'
            publisher.output.ChangeValue(str(epub))
            press(publisher.buttons['export']); pump(lambda: not publisher.busy)
            check('native EPUB export completed', epub.exists() and len(publisher.receipts) == 1)
            publisher.output.ChangeValue(str(daisy))
            press(publisher.buttons['daisy']); pump(lambda: not publisher.busy)
            check('native DAISY export completed', daisy.exists() and len(publisher.receipts) == 2)
            proof = inspect_daisy(daisy)
            check('DAISY preserves exact source and measured-only synchronization',
                  proof['original_script'] == script and proof['validation']['independent_daisy_validator'] == 'NOT_RUN')
            press(publisher.buttons['verify']); pump(lambda: not publisher.busy)
            check('same DAISY receipt verifies exact bytes', 'Verified exact saved DAISY bytes' in publisher.status.GetValue())
            daisy_bytes = daisy.read_bytes()
            press(publisher.buttons['daisy']); pump(lambda: not publisher.busy)
            check('native new-only publication retains existing bytes',
                  daisy.read_bytes() == daisy_bytes and 'not be overwritten' in publisher.status.GetValue())
            press(publisher.buttons['close'])
            publisher.Destroy(); publisher = None
            original = epub.read_bytes()
            source_proof = inspect_epub(epub)
            imported = library.import_file(workspace, epub)
            check('actual EPUB import completed', imported['state'] == 'complete')
            desk = ReadingDesk(library, workspace, imported['edition'])
            def guard():
                if frame.library is not library or frame._closing:
                    raise ValueError('Native fixture library scope changed.')
            reading = ReadingDialog(frame, desk, ReadingState(library.root), guard)
            reading.dialog.Show()
            reading._run('Opening the real imported source', reading.controller.start, reading._page)
            pump(lambda: not reading.busy and reading.controller.current is not None)
            original_page = reading.controller.current

            def scenario():
                nonlocal overlay
                try:
                    overlay = reading._audio_dialog
                    if overlay is None:
                        wx.CallLater(20, scenario); return
                    def initialization_state():
                        session = overlay.session
                        value = {'busy': overlay.busy, 'closed': overlay.closed,
                            'closing': overlay.closing, 'cancelled': overlay.cancelled.is_set(),
                            'parent_cancelled': bool(overlay.parent_cancelled()),
                            'has_session': session is not None,
                            'session_closed': None if session is None else session._closed.is_set(),
                            'has_transport': overlay.transport is not None,
                            'has_player': overlay.player is not None,
                            'status': overlay.status.GetValue()[:2048]}
                        if session is not None:
                            value.update(source_characters=len(session.page.raw_text),
                                source_logical_lines=session.page.raw_text.count('\n') + 1,
                                native_display_lines=overlay.text.GetNumberOfLines())
                        return value
                    try:
                        pump(lambda: not overlay.busy and overlay.transport is not None)
                    except BaseException as exc:
                        raise RuntimeError('Recorded EPUB initialization did not complete: ' +
                            json.dumps(initialization_state(), sort_keys=True)) from exc
                    report['recorded_initialization'] = initialization_state()
                    check('recorded reader retained a live session after native initialization',
                          overlay.session is not None and not overlay.session._closed.is_set()
                          and not overlay.stopped() and overlay.player is not None
                          and overlay.status.GetValue().startswith('Ready.'))
                    cue = overlay.session.chapter.cues[overlay.transport.index]
                    check('wrapped source view selects the exact authored recorded interval',
                          overlay.text.GetNumberOfLines() > overlay.session.page.raw_text.count('\n') + 1
                          and overlay.text.GetValue() == overlay.session.page.raw_text
                          and overlay.text.GetRange(*overlay.text.GetSelection())
                              == overlay.session.page.raw_text[cue.start:cue.end])
                    check('recorded reader borrows the existing source-reader worker',
                          overlay.executor is reading.executor)
                    check('open and selected fragment do not autoplay', overlay.player.state == 'stopped')
                    check('recorded cues retain the exact imported source interval count',
                          overlay.session.page == original_page
                          and len(overlay.session.chapter.cues) == len(source_proof['intervals']))
                    native_events = []
                    def native_state():
                        wave_output = overlay.player._wave
                        return {'backend': overlay.player.backend,
                            'mci_open': overlay.player._open,
                            'wave_open': bool(wave_output is not None and wave_output.handle.value),
                            'wave_stopping': None if wave_output is None else wave_output.stopping,
                            'wave_buffers': 0 if wave_output is None else len(wave_output.pending),
                            'wave_source_open': bool(wave_output is not None
                                and wave_output.stream is not None and not wave_output.stream.closed)}
                    actual_open = overlay.player._mci_open
                    def observed_open(path):
                        event = {'operation': 'open', 'api': 'mciSendCommandW',
                                 'filename_utf16_units': len(str(path).encode('utf-16-le')) // 2,
                                 'suffix': Path(path).suffix}
                        try:
                            result = actual_open(path)
                        except BaseException as error:
                            event['error'] = type(error).__name__ + ': ' + str(error)[:512]
                            if len(native_events) < 24:
                                native_events.append(event)
                            raise
                        event['ok'] = True
                        if len(native_events) < 24:
                            native_events.append(event)
                        return result
                    overlay.player._mci_open = observed_open
                    press(button(overlay.dialog, 'Play from selected segment'))
                    try:
                        pump(lambda: overlay.player.state == 'playing' and overlay.player.position_seconds > .03)
                    except BaseException as exc:
                        diagnostic = {'busy': overlay.busy, 'closed': overlay.closed,
                            'cancelled': overlay.stopped(), 'mci_open': overlay.player._open,
                            'status': overlay.status.GetValue()[:2048], 'native_events': native_events,
                            'initialization': initialization_state(), 'native_owner': native_state()}
                        for name in ('state', 'position_seconds', 'duration_seconds'):
                            try:
                                diagnostic[name] = getattr(overlay.player, name)
                            except BaseException as error:
                                diagnostic[name] = type(error).__name__ + ': ' + str(error)[:512]
                        raise RuntimeError('Recorded EPUB playback did not advance: ' +
                            json.dumps(diagnostic, sort_keys=True)) from exc
                    first_prepared = overlay.transport.prepared
                    filename_units = len(str(first_prepared.path).encode('utf-16-le')) // 2
                    expected_backend = 'Windows MCI' if filename_units + 1 <= 128 else 'Windows waveOut'
                    native_wave = overlay.player._wave
                    owns_output = ((expected_backend == 'Windows MCI' and overlay.player._open
                                    and native_wave is None)
                        or (expected_backend == 'Windows waveOut' and not overlay.player._open
                            and native_wave is not None and bool(native_wave.handle.value)
                            and not native_wave.stopping and native_wave.path == first_prepared.path
                            and native_wave.reader is not None and native_wave.stream is not None
                            and not native_wave.stream.closed and native_wave.levels is None))
                    report['recorded_native_output'] = dict(native_state(),
                        filename_utf16_units=filename_units, expected_backend=expected_backend)
                    check('real WinMM/MCI endpoint is playing prepared recorded PCM',
                          overlay.player.backend == expected_backend and owns_output
                          and not overlay.player._live_controls)
                    press(button(overlay.dialog, 'Pause recorded audio'))
                    pump(lambda: overlay.player.state == 'paused')
                    check('native Pause preserves prepared segment', overlay.transport.prepared.path.exists())
                    press(button(overlay.dialog, 'Resume recorded audio'))
                    pump(lambda: overlay.player.state == 'playing')
                    press(button(overlay.dialog, 'Next recorded segment'))
                    check('explicit segment navigation stops sound without autoplay',
                          overlay.transport.index == 1 and overlay.player.state == 'stopped')
                    press(button(overlay.dialog, 'Play from selected segment'))
                    pump(lambda: not overlay.busy and overlay.player.state == 'playing')
                    prepared = overlay.transport.prepared
                    stopped_wave = overlay.player._wave
                    press(button(overlay.dialog, 'Stop recorded audio'))
                    check('native Stop closes endpoint and releases its prepared file',
                          overlay.player.state == 'stopped' and not overlay.player._open
                          and overlay.player._wave is None
                          and (stopped_wave is None or (not stopped_wave.handle.value
                              and not stopped_wave.pending and stopped_wave.reader is None
                              and stopped_wave.stream is None))
                          and not prepared.path.exists())
                    press(button(overlay.dialog, 'Where am I in recorded audio?'))
                    check('recorded position report keeps source timing limitations explicit',
                          'authored fragment timing' in overlay.status.GetValue())
                    press(button(overlay.dialog, 'Close recorded audio'))
                except BaseException:
                    failures.append(traceback.format_exc())
                    report['failure'] = failures[-1][-32000:]
                    checkpoint('failed-native-scenario')
                    if overlay is not None:
                        overlay.force_close()
                        if not overlay.destroyed and overlay.dialog.IsModal():
                            overlay.dialog.EndModal(wx.ID_CANCEL)

            wx.CallLater(30, scenario)
            press(button(reading.dialog, 'EPUB synchronized recorded audio...'))
            if failures: raise RuntimeError(failures[0])
            check('recorded modal returned without source or quotation mutation',
                  reading.controller.current == original_page and epub.read_bytes() == original
                  and library.assets.resolve(desk.identity.asset_sha256).read_bytes() == original
                  and library._selections == {})
            check('recorded source and PCM stages are closed',
                  overlay.closed and not overlay.session.root.exists())
            press(button(reading.dialog, 'Close reader'))
            reading.dialog.Destroy(); reading = None
            report.update(audio_runtime=audio_hashes,
                module_sha256={name: sha256(path.read_bytes()).hexdigest() for name, path in origins.items()},
                native_keys='wx.UIActionSimulator',
                recorded_epub_playback='actual owned Windows output with synthetic PCM',
                daisy_hardware_reader='NOT_RUN', full_daisy_rule_validation='NOT_RUN', manual_nvda='NOT_RUN')
        finally:
            if overlay is not None:
                overlay.force_close()
            if reading is not None:
                reading.close(); reading.executor.shutdown(wait=True, cancel_futures=True)
                if not reading.destroyed: reading.dialog.Destroy()
            if publisher is not None:
                publisher.on_close()
            if frame is not None:
                frame.cancel_event.set(); frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
            if publisher is not None and not publisher.destroyed: publisher.Destroy()
            if service is not None: service.close()
            if frame is not None:
                frame.Destroy(); app.Yield()
            if binding is not None: binding.close()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'status': 'FAIL', 'phase': 'incomplete', 'checks': [], 'manual_nvda': 'NOT_RUN'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def checkpoint(phase):
        report['phase'] = phase
        args.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
        print('EPUB_DAISY_PHASE ' + phase, flush=True)
    checkpoint('starting')
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run this native diagnostic with actual Windows Python -I -B.')
    if args.source is not None and args.expect_installed:
        raise ValueError('Installed acceptance cannot inject checkout source.')
    faulthandler.dump_traceback_later(240, repeat=False, exit=True)
    handle = None
    try:
        source = args.source.resolve(strict=True) if args.source is not None else None
        if source is not None:
            sys.path[:0] = [str(source / 'src'), str(source)]
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        run_native(source, report, checkpoint)
        report['status'] = 'PASS'; checkpoint('complete')
        return 0
    except BaseException:
        report['failure'] = traceback.format_exc()[-32000:]; checkpoint('failed')
        raise
    finally:
        try:
            if handle is not None: handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
