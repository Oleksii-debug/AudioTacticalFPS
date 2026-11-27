"""Installed Windows local-profile controls and real deterministic helper children.

This qualifies the pinned Python protocol fixture and the current process owner.
No vendor voice, native vendor executable, acoustic quality, NVDA or physical
audio device is exercised. Native single-executable argv shape has source tests.
"""
from argparse import ArgumentParser
from contextlib import ExitStack
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback
import wave

MODULES = ('carr_library.audio.local_voice_helper', 'carr_library.runtime_settings',
           'carr_library.audio.model_manifest', 'carr_library.audio.engines',
           'carr_library.audio.windows_process', 'carr_library.audio.jobs',
           'carr_library.ui.engine_settings', 'carr_library.ui.transcription_v43_dialog',
           'carr_library.library_backup')
_fixture_spec = importlib.util.spec_from_file_location('_public_local_voice_fixture',
    Path(__file__).resolve().with_name('local_voice_contract_fixture.py'))
fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(fixture)


def identity():
    if not sys.flags.isolated:
        raise ValueError('Use the isolated installed interpreter.')
    prefix, result = Path(sys.prefix).resolve(strict=True), {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('The helper implementation escaped the installed wheel.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def run_native():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import engines
    from carr_library.audio.local_voice_helper import LocalVoiceHelperEngine
    from carr_library.library_backup import restore_backup, verify_backup
    from carr_library.runtime_settings import EngineProfileStore, configured_voice_registry
    from carr_library.ui.engine_settings import EngineSettingsDialog

    checks, errors, threads, timers, processes = [], [], [], [], []
    state = {'phase': 'review', 'active': True, 'profile': None}
    view = None
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)
    def fail():
        errors.append(traceback.format_exc())
        state['active'] = False
        if view is not None:
            view.close()

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            fail()
            return False

    application = App(False)
    frame = wx.Frame(None, title='Public local voice helper qualification')
    frame.Show()
    application.SetTopWindow(frame)
    frame.cancel_event, frame.working, frame._closing = Event(), False, False
    original_start, original_default = engines._start_process, engines.default_registry
    def tracked(command, **options):
        process = original_start(command, **options)
        processes.append((process, tuple(map(str, command)), str(options.get('cwd', ''))))
        return process
    engines._start_process = tracked
    # Do not enumerate unrelated SAPI/vendor engines in this focused fixture.
    engines.default_registry = lambda **options: engines.VoiceRegistry()
    try:
        with TemporaryDirectory(prefix='carr-local-helper-native-') as directory:
            root = Path(directory)
            manifest = fixture.installation(root / 'selected-pack')
            pack_before = fixture.identity(manifest)
            library = LibraryApp(root / 'library')
            library.create_workspace('Public synthetic helper protocol')
            frame.library = library
            store = EngineProfileStore(library.root)
            store.path.parent.mkdir(parents=True)
            legacy = {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
                      'voice_helpers': [{'executable': '/old/unselected-helper', 'engine_name': 'Earlier helper'}],
                      'music_models': [], 'fluidsynth': None}
            old_raw = (json.dumps(legacy, indent=3) + '\n\n').encode()
            store.path.write_bytes(old_raw)
            check('actual Windows local profile controls', wx.Platform == '__WXMSW__')
            check('public pinned helper pack has complete executable and script identity',
                  pack_before['files'] >= 3 and pack_before['bytes'] > 0)
            check('old helper declarations remain passive before explicit review',
                  store.snapshot().legacy and configured_voice_registry(library.root).voices() == () and not processes)
            reviewed = store.inspect(manifest)
            check('manifest review launches no helper process',
                  reviewed.manifest.manifest_sha256 == pack_before['manifest_sha256'] and not processes)

            def run_job(title, operation, *, done, failed, **options):
                if frame.working:
                    return False
                frame.working = True
                def completed(value=None, error=None):
                    frame.working = False
                    (failed(error) if error is not None else done(value))
                def work():
                    try:
                        value = operation()
                    except BaseException as error:
                        wx.CallAfter(completed, error=error)
                    else:
                        wx.CallAfter(completed, value=value)
                worker = Thread(target=work, name='public-helper-profile-owner', daemon=True)
                threads.append(worker)
                worker.start()
                return True
            frame.run_job = run_job
            view = EngineSettingsDialog(frame, store=store)
            view._reviewed(reviewed)
            deadline = monotonic() + 30
            def press(name):
                button = view.buttons[name]
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                event.SetEventObject(button)
                if not button.IsEnabled() or not button.GetEventHandler().ProcessEvent(event):
                    raise AssertionError('Native decision was not dispatched: ' + name)
            def poll():
                if not state['active']:
                    return
                try:
                    if monotonic() >= deadline:
                        raise TimeoutError('Native local-helper registration did not settle.')
                    phase = state['phase']
                    if phase == 'review':
                        check('native review exposes exact helper protocol engine and manifest hash',
                              'local-voice-helper' in view.details.GetValue()
                              and pack_before['manifest_sha256'] in view.details.GetValue()
                              and not view.details.IsEditable())
                        check('native reviewed registration is enabled and names are accessible',
                              view.buttons['register'].IsEnabled()
                              and view.name.GetName() == 'Local profile name'
                              and view.list.GetName() == 'Saved local engine profiles and enabled state')
                        view.name.ChangeValue('Перевірений голос — čítanie')
                        state['phase'] = 'registered'
                        press('register')
                        check('native Save owns the existing supervised operation', view.session.busy)
                    elif phase == 'registered' and not view.session.busy and not frame.working:
                        profiles = store.snapshot().profiles
                        check('native Save persists the exact reviewed pin and Unicode name',
                              len(profiles) == 1 and profiles[0].manifest_sha256 == pack_before['manifest_sha256']
                              and profiles[0].name == 'Перевірений голос — čítanie')
                        state['profile'] = profiles[0]
                        check('native registration never invokes the executable', not processes)
                        archive = store.path.parent / 'legacy' / ('engines-schema1-' + sha256(old_raw).hexdigest() + '.json')
                        check('explicit registration preserves exact schema1 raw history', archive.read_bytes() == old_raw)
                        view.list.SetSelection(0)
                        event = wx.CommandEvent(wx.EVT_LISTBOX.typeId, view.list.GetId())
                        event.SetEventObject(view.list)
                        view.list.GetEventHandler().ProcessEvent(event)
                        check('native selected helper can be rechecked disabled or removed',
                              all(view.buttons[key].IsEnabled() for key in ('review', 'toggle', 'remove')))
                        state['phase'] = 'disabled'
                        press('toggle')
                    elif phase == 'disabled' and not view.session.busy and not frame.working:
                        check('native disable keeps the reviewed runtime and saved identity',
                              not store.snapshot().profile(state['profile'].id).enabled
                              and fixture.identity(manifest) == pack_before)
                        view.list.SetSelection(0)
                        view.selected()
                        state['phase'] = 'enabled'
                        press('toggle')
                    elif phase == 'enabled' and not view.session.busy and not frame.working:
                        check('native enable retains the same explicit profile identity',
                              store.snapshot().profile(state['profile'].id).enabled)
                        state['phase'] = 'closed'
                        press('close')
                        return
                    timers.append(wx.CallLater(30, poll))
                except BaseException:
                    fail()
            timers.append(wx.CallLater(30, poll))
            try:
                view.show()
            finally:
                view = None  # show() has destroyed its owned native dialog.
            state['active'] = False
            for timer in timers:
                timer.Stop()
            for worker in threads:
                worker.join(5)
            if errors:
                raise AssertionError(errors[-1][-8000:])
            check('native Close settles registration workers without executing speech',
                  state['phase'] == 'closed' and all(not worker.is_alive() for worker in threads)
                  and not processes)
            view = None
            profile = state['profile']
            registry = configured_voice_registry(library.root)
            check('passive registry construction never discovers unselected helper code', not processes)
            voice = registry.voices()[0]
            check('actual helper catalogue uses the current registered voice namespace',
                  voice.engine == 'local-' + profile.id and voice.id.startswith(voice.engine + ':'))
            check('actual helper catalogue retains Unicode language license and finite controls',
                  voice.languages == ('uk-UA',) and 'Київ' in voice.name
                  and voice.license == fixture.VOICE['license']
                  and voice.controls == ('rate', 'volume', 'pitch', 'punctuation'))
            saved_id = registry.cache_identity(voice.id)
            check('actual helper cache is bound to the reviewed manifest and profile',
                  saved_id['local_profile_id'] == profile.id and saved_id['protocol'] == fixture.SCHEMA)
            target, words = root / 'accepted.wav', 'Київ, <speak>literal</speak>; "$()" — pôvodný text.'
            registry.synthesize(words, target, voice=voice.id, language='uk', rate=-2, pitch=3,
                                volume=.63, punctuation=True)
            with wave.open(str(target), 'rb') as audio:
                check('actual helper output is verified bounded PCM16',
                      (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes())
                      == (16000, 1, 2, 512))
                check('actual JSON stdin retains exact literal text and selected controls',
                      audio.readframes(512) == fixture.expected_pcm(words, rate=-2, pitch=3,
                                                                   volume=.63, punctuation=True))
            check('accepted helper output leaves no temporary speech stage', not list(root.glob('.speech-*')))
            reopened = configured_voice_registry(library.root)
            check('reopened explicit profile retains exact saved voice and cache identity',
                  reopened.voices()[0].id == voice.id and reopened.cache_identity(voice.id) == saved_id)
            output_sha = sha256(target.read_bytes()).hexdigest()
            before_count = len(processes)
            try:
                registry.synthesize('Do not replace.', target, voice=voice.id, language='uk')
            except FileExistsError:
                pass
            else:
                raise AssertionError('Occupied output was accepted.')
            check('occupied output refuses execution and preserves exact bytes',
                  len(processes) == before_count and sha256(target.read_bytes()).hexdigest() == output_sha)
            store.set_enabled(profile.id, False)
            try:
                registry.synthesize('Do not execute.', root / 'disabled.wav', voice=voice.id, language='uk')
            except ValueError:
                pass
            else:
                raise AssertionError('Disabled captured profile was accepted.')
            check('disabled captured profile cannot execute or publish',
                  len(processes) == before_count and not (root / 'disabled.wav').exists())
            store.set_enabled(profile.id, True)
            hang = fixture.installation(root / 'hanging-pack', template=manifest.parent / 'python', mode='hang-render')
            engine = LocalVoiceHelperEngine(hang)
            chosen = engine.voices()[0].id
            initial = len(processes)
            def stopped():
                return any(Path(cwd).name.startswith('.speech-') for _, _, cwd in processes[initial:])
            try:
                engine.synthesize('Cancel actual child.', root / 'cancelled.wav',
                                  voice=chosen, language='uk', cancelled=stopped)
            except InterruptedError:
                pass
            else:
                raise AssertionError('Actual helper cancellation was not observed.')
            check('actual cancellation settles its owned child before returning',
                  stopped() and all(process.poll() is not None for process, _, _ in processes))
            check('actual cancellation publishes no output and removes private PCM',
                  not (root / 'cancelled.wav').exists() and not list(root.glob('.speech-*')))
            check('every actual helper uses the fixed isolated Python argv',
                  len(processes) >= 6
                  and all(len(command) == 6 and command[1:5] == ('-I', '-B', '-X', 'utf8')
                          and Path(command[-1]).name == 'helper.py' for _, command, _ in processes))
            check('reviewed executable script and inventory remain unchanged',
                  fixture.identity(manifest) == pack_before)
            backup = root / 'backup'
            digest = library.backup(backup)
            verify_backup(backup, digest)
            recovered = LibraryApp(restore_backup(backup, root / 'restored', digest))
            check('complete backup restores no active machine executable registration',
                  not (recovered.root / 'state/audio/engines.json').exists()
                  and EngineProfileStore(recovered.root).snapshot().profiles == ()
                  and not list(recovered.root.rglob('helper.py')))
            second = root / 'second-backup'
            verify_backup(second, recovered.backup(second))
            check('passive restored library supports full rebackup',
                  fixture.identity(manifest) == pack_before)
            check('all helper process owners are settled at final acceptance',
                  all(process.poll() is not None for process, _, _ in processes))
            return {'checks': checks, 'fixture': pack_before, 'fixture_after': fixture.identity(manifest),
                    'output_sha256': output_sha, 'actual_processes': len(processes),
                    'sample_rate': 16000, 'frames': 512, 'all_children_settled': True,
                    'retained_schema1_sha256': sha256(old_raw).hexdigest()}
    finally:
        state['active'] = False
        for timer in timers:
            timer.Stop()
        if view is not None:
            view.session.cancel.set()
            if view.dialog.IsModal():
                view.dialog.EndModal(wx.ID_CANCEL)
        for worker in threads:
            worker.join(5)
        engines._start_process, engines.default_registry = original_start, original_default
        frame.Destroy()
        application.Destroy()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    report = {'schema': 1, 'status': 'FAIL', 'installed_mode': True,
              'isolated': bool(sys.flags.isolated), 'native_controls': 'NOT_RUN',
              'synthetic_helper_execution': 'NOT_RUN', 'native_vendor_execution': 'NOT_RUN',
              'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN', 'speech_quality': 'NOT_CLAIMED',
              'source_commit': os.environ.get('CARR_EXPECTED_SOURCE_COMMIT') or os.environ.get('GITHUB_SHA', ''),
              'fixture_source_sha256': fixture.sha(Path(__file__).resolve().with_name('local_voice_contract_fixture.py'))}
    try:
        if not args.native or os.name != 'nt':
            raise ValueError('This probe requires explicit actual native Windows qualification.')
        with ExitStack() as resources:
            from carr_library.native_runtime import activate_bundled_runtime
            handle = activate_bundled_runtime()
            if handle is not None:
                resources.callback(handle.close)
            before = identity()
            report.update(run_native())
            if identity() != before:
                raise ValueError('Installed application bytes changed during the native helper probe.')
            report.update(status='PASS', native_controls='EXECUTED', synthetic_helper_execution='EXECUTED',
                          module_origins=before)
    except BaseException as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:], flush=True)
        report['reason'] = type(error).__name__ + ': ' + str(error)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
