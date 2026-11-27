"""Actual installed Windows COM, reader punctuation and passive state recovery.

Public synthetic words only. No audio-device playback, voice-quality, NVDA,
32-bit registry, generic frozen-build or third-party voice qualification.
"""
from argparse import ArgumentParser
from contextlib import ExitStack
from hashlib import sha256
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback
import wave

MODULES = (
    'carr_library.audio.engines', 'carr_library.audio.sapi_native_worker',
    'carr_library.audio.windows_process', 'carr_library.audio.jobs',
    'carr_library.audio.player', 'carr_library.audio.playback',
    'carr_library.reader.live_speech', 'carr_library.reader.live_playback',
    'carr_library.ui.live_reading', 'carr_library.library_backup',
    'carr_library.__main__',
)


def identity():
    if not sys.flags.isolated:
        raise ValueError('Use the isolated installed interpreter.')
    prefix, result = Path(sys.prefix).resolve(strict=True), {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('A native SAPI implementation escaped the installed wheel.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def private_entry_diagnostic(reply):
    """Only the worker's fixed failure fields may escape its private directory."""
    from carr_library.audio.jobs import file_identity, regular_file, verified_reader
    phases = {'validating-request', 'loading-com-client', 'creating-speaker',
              'enumerating-voices', 'selecting-voice', 'creating-audio-stream',
              'opening-audio-stream', 'synthesizing', 'closing-audio-stream'}
    diagnostic = {'worker_reply': 'absent-or-invalid'}
    try:
        info = regular_file(reply)
        if not 0 < info.st_size <= 4096:
            return diagnostic
        with verified_reader(reply, expected_identity=file_identity(info)) as stream:
            raw = stream.read(4097)
        from carr_library.audio.engines import _sapi_json
        value = _sapi_json(raw)
        if (type(value) is not dict
                or set(value) not in ({'version', 'status', 'phase'},
                                      {'version', 'status', 'phase', 'hresult'})
                or type(value.get('version')) is not int or value['version'] != 1
                or value.get('status') != 'failed' or value.get('phase') not in phases):
            return diagnostic
        diagnostic = {'worker_reply': 'validated-failure', 'worker_version': 1,
                      'worker_status': 'failed', 'worker_phase': value['phase']}
        code = value.get('hresult')
        if type(code) is int and 0 <= code <= 0xffffffff:
            diagnostic['worker_hresult'] = f'0x{code:08X}'
    except Exception:
        # No exception text, local path, token catalogue or source text is public.
        pass
    return diagnostic


def run_native():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import engines
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.library_backup import create_backup, verify_backup, restore_backup
    from carr_library.reader.live_speech import SpeechPositionStore
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.ui.live_reading import LiveReadingDialog

    checks, failures, timers, processes = [], [], [], []
    state = {'window': None, 'phase': 'start', 'active': True, 'done': False}
    start_process = engines._start_process

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    def tracked(command, **options):
        value = start_process(command, **options)
        processes.append((value, tuple(map(str, command)), monotonic()))
        return value

    def failed():
        failures.append(traceback.format_exc())
        state['active'] = False
        window = state['window']
        if window is not None and not window.closed:
            window.close()

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            failed()
            return False

    application = App(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    check('actual Windows native reader controls', wx.Platform == '__WXMSW__')
    check('existing reviewed pywin32 dependency', importlib.metadata.version('pywin32') == '312')
    engines._start_process = tracked
    try:
        with TemporaryDirectory(prefix='carr-native-sapi-') as directory:
            root = Path(directory).resolve()
            windows = engines._windows_directory()
            host64 = engines.SAPI5Engine()
            host32 = engines.SAPI5Engine(engine_id='sapi5-32')
            check('actual compatibility hosts use the shared Windows directory and distinct architectures',
                  host64.binary == windows / 'System32/WindowsPowerShell/v1.0/powershell.exe'
                  and host32.binary == windows / 'SysWOW64/WindowsPowerShell/v1.0/powershell.exe'
                  and host64.id == 'sapi5' and host32.id == 'sapi5-32')
            previous_root = os.environ.get('SystemRoot')
            try:
                os.environ['SystemRoot'] = str(root / 'untrusted-environment-root')
                selected = engines._LazySAPI32Engine()._selected('sapi5-32:' + 'a' * 24)
                unchanged = (selected.binary == host32.binary
                             and engines.SAPI5Engine().binary == host64.binary)
            finally:
                if previous_root is None:
                    os.environ.pop('SystemRoot', None)
                else:
                    os.environ['SystemRoot'] = previous_root
            check('spoofed SystemRoot cannot redirect selected saved compatibility voice', unchanged)
            try:
                engines.SAPI5Engine(host64.binary, engine_id='sapi5-32')
            except engines.EngineUnavailable:
                refused = True
            else:
                refused = False
            check('wrong-architecture compatibility host is refused before any process',
                  refused and not processes)
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Public synthetic native speech')
            book = library.add_text(workspace, 'Literal selected text',
                'Native punctuation, one: two? Literal <speak>words</speak> remain text.')
            desk = ReadingDesk(library, workspace, book['edition'])
            page = desk.page(1)
            source = page.raw_text
            source_sha = sha256(source.encode()).hexdigest()
            engine = engines.NativeSAPI5Engine(timeout=45)
            registry = engines.VoiceRegistry((engine,))
            # Exercise the installed private main dispatch separately from the
            # ordinary source-file child. This is not a PyInstaller-build claim.
            dispatch = root / 'dispatch'
            dispatch.mkdir()
            request, reply = dispatch / 'request.json', dispatch / 'reply.json'
            request.write_text('{"version":1,"operation":"voices"}', encoding='utf-8')
            command = [sys.executable, '-I', '-B', '-X', 'utf8', '-m', 'carr_library',
                       '--carr-sapi-native-worker', request, reply]
            launch = {'argv': ['<installed-python>', *command[1:-2],
                               request.name, reply.name],
                      'argument_count': len(command), 'isolated_parent': bool(sys.flags.isolated),
                      'request_sha256': sha256(request.read_bytes()).hexdigest()}
            print('NATIVE_SAPI_PRIVATE_ENTRY ' + json.dumps(launch, sort_keys=True), flush=True)
            try:
                engines._run(command, timeout=45, output=reply)
            except engines.EngineUnavailable:
                print('NATIVE_SAPI_DIAGNOSTIC ' + json.dumps(
                    {**launch, **private_entry_diagnostic(reply)}, sort_keys=True), flush=True)
                raise
            entry = json.loads(reply.read_bytes())
            check('installed private entrypoint returns actual bounded COM inventory',
                  reply.stat().st_size <= 2_000_000 and entry.get('adapter') == 'native-pywin32'
                  and entry.get('client_version') == '312' and bool(entry.get('voices')))
            check('installed entrypoint reports actual interpreter architecture',
                  entry.get('architecture') == str(struct.calcsize('P') * 8))
            frame = wx.Frame(None, title='Native SAPI current reader qualification')
            frame.library, frame._closing, frame.working = library, False, False
            frame.Show()
            metrics = {}

            def press(key, label):
                button = state['window'].controls[key]
                if not button.IsEnabled() or not button.IsShownOnScreen():
                    raise AssertionError(label + ': control is unavailable')
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                event.SetEventObject(button)
                check(label, button.GetEventHandler().ProcessEvent(event))

            def schedule():
                if state['active']:
                    timers.append(wx.CallLater(40, tick))

            def tick():
                if not state['active']:
                    return
                window, phase = state['window'], state['phase']
                try:
                    if window.future is not None:
                        schedule()
                        return
                    if phase == 'start':
                        check('reader opens without automatic speech or playback',
                              window.session is None and window.player.path is None)
                        state['phase'] = 'voices'
                        press('voices', 'native refresh voices event handled')
                    elif phase == 'voices':
                        eligible = [(index, voice) for index, voice in enumerate(window.voice_rows)
                                    if voice.engine == 'sapi5' and any(code.lower().startswith('en')
                                                                    for code in voice.languages)]
                        check('actual native English SAPI voice enumerated', bool(eligible))
                        index, voice = eligible[0]
                        window.voice.SetSelection(index)
                        window.language.ChangeValue(voice.languages[0])
                        window._voice_controls()
                        record = engine._records[voice.id]
                        check('actual native voice retains prior token-leaf identifier',
                              voice.id == 'sapi5:' + sha256(record['id'].encode()).hexdigest()[:24]
                              and record['token'].rsplit('\\', 1)[-1] == record['id'])
                        state['voice'] = voice
                        cache = engine.cache_identity(voice.id)
                        check('native cache records exact worker and existing COM client',
                              cache['native_protocol'] == 'carr-sapi-native/1'
                              and cache['com_client_version'] == '312'
                              and cache['native_worker_sha256'] == sha256(
                                  Path(importlib.import_module('carr_library.audio.sapi_native_worker').__file__).read_bytes()).hexdigest())
                        check('native punctuation has an accessible capability-bound control',
                              window.punctuation.IsEnabled()
                              and 'punctuation' in window.punctuation.GetName().lower())
                        check('unsupported native SAPI pitch remains disabled', not window.pitch.IsEnabled())
                        window.punctuation.SetValue(True)
                        window.mode.SetSelection(window.modes.index('page'))
                        check('selected punctuation captured before preparation',
                              window._settings().punctuation is True)
                        state['phase'] = 'prepared'
                        press('prepare', 'native prepare event handled')
                    elif phase == 'prepared':
                        check('actual native SAPI utterance reached measured prepared state',
                              window.session is not None and window.session.current is not None)
                        excerpt = window.session.current
                        info = wav_info(excerpt.path)
                        with wave.open(str(excerpt.path), 'rb') as audio:
                            raw = audio.readframes(audio.getnframes())
                        metrics.update(frames=info.frames, sample_rate=info.rate, channels=info.channels,
                            pcm_sha256=sha256(excerpt.path.read_bytes()).hexdigest(),
                            voice=state['voice'].id, text_sha256=excerpt.text_sha256)
                        check('native output is actual nonempty mono PCM16 at 22050 Hz',
                              info.rate == 22050 and info.channels == 1 and info.frames > 0
                              and len(raw) == info.frames * 2)
                        check('actual native PCM contains nonzero samples',
                              any(value[0] for value in struct.iter_unpack('<h', raw)))
                        check('prepared native utterance retains exact selected text hash',
                              excerpt.text_sha256 == sha256(source[
                                  excerpt.selection.start:excerpt.selection.end].encode()).hexdigest())
                        check('prepared punctuation stays in the current request',
                              window.session.settings.punctuation is True)
                        check('PCM preparation never opens an output device or claims completion',
                              not window.playing and window.player.path is None
                              and window.session.cursor == 0)
                        state['phase'] = 'stopped'
                        press('stop', 'native stop event handled')
                    elif phase == 'stopped':
                        check('native stop settles session without inventing a played cursor',
                              window.session is None and window.player.path is None)
                        saved = SpeechPositionStore(library.root).load(source, page.location)
                        check('saved schema two retains explicit punctuation and zero observed cursor',
                              saved['settings']['punctuation'] is True and saved['cursor'] == 0
                              and json.loads(SpeechPositionStore(library.root).path.read_bytes())['schema']
                                  == 'carr-live-reading/2')
                        state['phase'] = 'restored'
                        state['process_count'] = len(processes)
                        press('resume', 'native restore cursor event handled')
                    elif phase == 'restored':
                        check('restored position never launches an old voice or playback',
                              len(processes) == state['process_count'] and window.session is None
                              and window.player.path is None)
                        state['active'], state['done'] = False, True
                        press('close', 'native close event handled')
                        return
                    schedule()
                except BaseException:
                    failed()

            def ready(window):
                state['window'] = window
                schedule()

            window = LiveReadingDialog(frame, library.root, source, title=page.title,
                location=page.location, registry=registry,
                guard=lambda: desk.validate(page.location), on_ready=ready)
            window.show()
            for timer in timers:
                timer.Stop()
            if failures:
                print('\n'.join(failures), flush=True)
                frame.Destroy()
                return {'status': 'FAIL', 'checks': checks, 'native_controls': 'EXECUTED'}
            check('native window closes after actual current owners settle',
                  window.closed and window.future is None and window.session is None)
            voice = state['voice']
            captured = engine._records[voice.id]
            old_token = captured['token']
            captured['token'] = 'HKEY_CURRENT_USER\\Absent-Synthetic-Token\\' + captured['id']
            rejected = root / 'rejected.wav'
            try:
                try:
                    engine.synthesize('Never substitute another voice.', rejected,
                                      voice=voice.id, language=voice.languages[0])
                except engines.EngineUnavailable:
                    denied = True
                else:
                    denied = False
                check('actual missing native token fails without an output or substitute',
                      denied and not rejected.exists())
            finally:
                captured['token'] = old_token

            started = len(processes)
            cancelled_target = root / 'cancelled.wav'
            def cancel():
                return len(processes) > started and monotonic() - processes[started][2] >= .10
            try:
                engine.synthesize(('Public synthetic cancellation words. ' * 200)[:7900],
                    cancelled_target, voice=voice.id, language=voice.languages[0], cancelled=cancel)
            except InterruptedError:
                stopped = True
            else:
                stopped = False
            check('actual owned native process cancellation is observed',
                  stopped and len(processes) > started)
            check('cancelled native synthesis publishes no partial PCM',
                  not cancelled_target.exists() and not list(root.glob('.speech-*')))
            check('every actual native worker leader and job handle is released',
                  bool(processes) and all(process.poll() is not None
                    and getattr(process, '_carr_job', None) is not None
                    and process._carr_job.handle is None for process, _, _ in processes))
            check('native discovery and synthesis never invoke PowerShell',
                  all(not any('powershell' in item.lower() for item in command)
                      for _, command, _ in processes))
            before = SpeechPositionStore(library.root).path.read_bytes()
            count = len(processes)
            snapshot, restored = root / 'backup', root / 'restored'
            proof = create_backup(library, snapshot)
            verify_backup(snapshot, proof)
            restore_backup(snapshot, restored, proof)
            check('complete backup restores exact optional reading state',
                  SpeechPositionStore(restored).path.read_bytes() == before)
            check('restored punctuation is passive and never starts an engine',
                  len(processes) == count
                  and SpeechPositionStore(restored).load(source, page.location)['settings']['punctuation'] is True)
            again = create_backup(LibraryApp(restored), root / 'rebackup')
            verify_backup(root / 'rebackup', again)
            check('restored native reading state supports complete rebackup', bool(again))
            check('source library text remains byte-identical',
                  sha256(desk.page(1).raw_text.encode()).hexdigest() == source_sha)
            frame.Destroy()
            return {'status': 'PASS' if state['done'] else 'FAIL', 'checks': checks,
                'native_controls': 'EXECUTED', 'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN',
                'actual_native_pcm': metrics, 'com_client': 'pywin32', 'com_client_version': '312',
                'private_entrypoint': 'EXECUTED', 'native_cancellation': 'OBSERVED',
                'compatibility_host_admission': 'PASSIVE_ACTUAL_PATH_CHECKS',
                'saved_cursor': 0, 'reading_state_sha256': sha256(before).hexdigest(),
                'restored_state_sha256': sha256(SpeechPositionStore(restored).path.read_bytes()).hexdigest()}
    finally:
        engines._start_process = start_process


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' or not args.native:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit installed Windows execution is required.'}))
        return 77
    settled = Event()
    def watchdog():
        if not settled.wait(240):
            print(json.dumps({'status': 'FAIL', 'reason': 'Native SAPI probe exceeded 240 seconds.'}), flush=True)
            os._exit(124)
    Thread(target=watchdog, daemon=True).start()
    try:
        with ExitStack() as resources:
            from carr_library.native_runtime import activate_bundled_runtime
            handle = activate_bundled_runtime()
            if handle is not None:
                resources.callback(handle.close)
            before = identity()
            result = run_native()
            if identity() != before:
                raise ValueError('Native SAPI source changed during actual execution.')
            result.update(installed_mode=True, isolated=True, module_origins=before,
                          source_commit=os.environ.get('CARR_EXPECTED_SOURCE_COMMIT', os.environ['GITHUB_SHA']))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if result['status'] == 'PASS' else 1
    except BaseException as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-16000:], flush=True)
        print(json.dumps({'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error)[:4000],
                          'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
        return 1
    finally:
        settled.set()


if __name__ == '__main__':
    raise SystemExit(main())
