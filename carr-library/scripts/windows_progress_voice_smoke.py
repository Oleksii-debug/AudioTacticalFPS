"""Installed Windows progress-voice acceptance through real SAPI5 and WinMM.

All text and PCM inputs are public synthetic fixtures. The player subclass only
observes calls and delegates unchanged to the actual WavePlayer. A missing
native output device is UNAVAILABLE (exit 77), never a playback PASS. No model,
voice audio, raw document, or private library is uploaded.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import traceback
import wave


def run_native(output):
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library import progress
    from carr_library.application import LibraryApp
    from carr_library.audio import progress_speech
    from carr_library.audio.engines import SAPI5Engine, VoiceRegistry
    from carr_library.audio.playback import playback_capabilities
    from carr_library.audio.player import WavePlayer
    from carr_library.ui import audio_studio, progress_view

    final = dict(schema='carr-native-progress-voice/1', status='NOT_RUN',
                 physical_listening='NOT_RUN', nvda='NOT_RUN', neural_engines='NOT_RUN')
    checks, errors, players, attempts = [], [], [], []
    final['notification_attempts'] = attempts

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def report(code):
        # The caller publishes only after finally confirms owned cleanup.
        final['checks'] = checks
        return code, final

    source_root = Path(__file__).resolve().parents[1] / 'src/carr_library'
    installed_root = Path(progress.__file__).resolve().parent
    check('product is loaded from the installed package',
          'site-packages' in [part.casefold() for part in installed_root.parts])
    modules = ('progress.py', 'audio/progress_speech.py', 'audio/engines.py', 'audio/player.py',
               'audio/playback.py', 'ui/progress_view.py', 'ui/audio_studio.py')
    hashes = {}
    for name in modules:
        raw = (source_root / name).read_bytes()
        check('installed bytes match checkout: ' + name, (installed_root / name).read_bytes() == raw)
        hashes[name] = sha256(raw).hexdigest()
    final['source_sha256'] = hashes

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    class ObservingPlayer(WavePlayer):
        """Read-only acceptance instrumentation; no playback or engine replacement."""
        def __init__(self):
            super().__init__()
            self.observed_states, self.closed_confirmed, self.record = set(), False, None
            players.append(self)

        def play(self, path, **kwargs):
            selected = Path(path)
            with wave.open(str(path), 'rb') as pcm:
                raw = pcm.readframes(pcm.getnframes())
                self.record = dict(frames=pcm.getnframes(), rate=pcm.getframerate(),
                    channels=pcm.getnchannels(), width=pcm.getsampwidth(),
                    pcm_sha256=sha256(raw).hexdigest(), nonzero=any(raw), path=selected)
            return super().play(path, **kwargs)

        @property
        def state(self):
            value = super().state
            self.observed_states.add(value)
            return value

        def close(self):
            super().close()
            self.closed_confirmed = True

    app = App(False)
    event_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(event_loop)
    deadline = monotonic() + 65

    def pump(predicate, label, seconds=20):
        end = min(deadline, monotonic() + seconds)
        while not predicate():
            if errors:
                raise AssertionError(errors[-1])
            if monotonic() >= end:
                raise TimeoutError(label)
            app.Yield()
            sleep(.01)
        app.Yield()
        if errors:
            raise AssertionError(errors[-1])

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def click(parent, label):
        control, = [child for child in descendants(parent)
                    if isinstance(child, wx.Button) and child.GetLabel() == label]
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        check('native command reaches ' + label, control.GetEventHandler().ProcessEvent(event))

    frame = studio = None
    try:
        check('actual Windows wx controls', wx.Platform == '__WXMSW__')
        with TemporaryDirectory(prefix='carr-progress-voice-native-') as temporary:
            root = Path(temporary)
            library = LibraryApp(root / 'library')
            library.create_workspace('Public synthetic progress voice')
            catalog = library.root / 'library.sqlite'
            catalog_hash = sha256(catalog.read_bytes()).hexdigest()
            frame = wx.Frame(None, title='Synthetic local progress voice', size=(900, 700))
            frame.library = library
            frame.progress_view = progress_view.ProgressView(frame, settings_root=library.root)
            app.SetTopWindow(frame)
            frame.Show()
            registry = VoiceRegistry([SAPI5Engine(timeout=20)])
            controller = audio_studio.StudioController(private_root=library.root, registry=registry)
            controller.new('Literal source: keep every character.\n  No private material.',
                           title='Synthetic unchanged project')
            project = controller.save(root / 'unchanged-project.json')
            project_hash = sha256(project.read_bytes()).hexdigest()
            original_text = controller.script
            studio = audio_studio.AudioStudioDialog(frame, controller)
            studio.dialog.Show()
            studio.reload_voices()
            pump(lambda: not studio.busy and bool(studio.voice_rows), 'Actual SAPI catalogue did not load.')
            selected = next((index for index, voice in enumerate(studio.voice_rows)
                             if voice['engine'] == 'sapi5' and any(
                                 language.lower().split('-')[0] == 'en' for language in voice['languages'])), None)
            check('an actual installed English SAPI5 voice is available', selected is not None)
            studio.voice.SetSelection(selected)
            studio.language.ChangeValue('en')
            click(studio.dialog, 'Use voice for progress')
            pump(lambda: not studio.busy and studio.progress_view.speech_provider is not None,
                 'Explicit progress voice selection did not complete.')
            provider = studio.progress_view.speech_provider
            check('selection binds the displayed exact local voice',
                  provider.voice.id == studio.voice_rows[selected]['id'] and provider.language == 'en')
            check('selection does not enable sound or change the reading project',
                  progress.ProgressSettingsStore(library.root).load().mode == 'off'
                  and controller.script == original_text and not controller.dirty
                  and sha256(project.read_bytes()).hexdigest() == project_hash)
            check('parent and Studio use independent selection ownership',
                  frame.progress_view.speech_provider is not provider
                  and frame.progress_view.speech_provider.voice == provider.voice)
            # Observation delegates every actual player operation unchanged.
            provider._player_factory = ObservingPlayer
            native_speak = provider.speak_progress

            def observed_speak(text, **kwargs):
                # Only this public synthetic probe records an unchanged call.
                attempt = dict(text=text, volume=kwargs['volume'], started=monotonic())
                attempts.append(attempt)
                try:
                    result = native_speak(text, **kwargs)
                    attempt['returned'] = result
                    return result
                except BaseException as exc:
                    attempt['exception'] = type(exc).__name__ + ': ' + str(exc)[:1500]
                    attempt['traceback'] = traceback.format_exc()[-4000:]
                    raise
                finally:
                    attempt['elapsed_seconds'] = round(monotonic() - attempt.pop('started'), 3)
                    attempt['provider_notice'] = provider.last_notice
                    attempt['selection_closed'] = provider._closed.is_set()
                    attempt['cancelled_at_return'] = bool(kwargs['cancelled']())
                    attempt['media_suppressed'] = progress.progress_audio_suppressed()

            provider.speak_progress = observed_speak
            tracker = progress.ProgressTracker('Public synthetic work', total=4, completed=1)
            tracker.update(phase='Verifying a public synthetic result')
            studio.progress_view.track(tracker)

            def enable_speech():
                try:
                    dialog, = [window for window in wx.GetTopLevelWindows()
                               if isinstance(window, wx.Dialog) and window.IsModal()
                               and window.GetTitle() == 'Progress sound settings']
                    controls = {child.GetName(): child for child in descendants(dialog)}
                    mode = controls['Application progress output']
                    check('all four output modes are native choices', mode.GetCount() == 4)
                    check('the selected voice is exposed in the native settings dialog',
                          provider.voice.name in controls['Selected local progress voice'].GetLabel())
                    mode.SetSelection(2)
                    controls['Progress sound volume'].SetValue(20)
                    controls['Minimum progress sound interval in seconds'].SetValue(60)
                    checkboxes = {child.GetLabel(): child for child in descendants(dialog)
                                  if isinstance(child, wx.CheckBox)}
                    checkboxes['Mute application progress sounds'].SetValue(False)
                    checkboxes['Allow sounds while this window is in the background'].SetValue(True)
                    click(dialog, 'Save settings')
                except Exception:
                    errors.append(traceback.format_exc())
                    for dialog in wx.GetTopLevelWindows():
                        if isinstance(dialog, wx.Dialog) and dialog.IsModal():
                            dialog.EndModal(wx.ID_CANCEL)

            configured = wx.CallLater(100, enable_speech)
            try:
                studio.progress_view.configure()
            finally:
                configured.Stop()
            if errors:
                raise AssertionError(errors[-1])
            check('native settings persist Speech without persisting a voice authorization',
                  json.loads((library.root / progress.FILENAME).read_bytes()) == dict(
                      kind='carr-progress-settings', version=2, mode='speech', volume=20,
                      interval_seconds=60, muted=False, background=True))
            pump(lambda: bool(players) and players[0].closed_confirmed,
                 'The actual notification worker did not confirm cleanup.')
            record = players[0].record
            check('actual SAPI notification produced bounded nonzero PCM',
                  record is not None and record['width'] == 2 and record['nonzero']
                  and 0 < record['frames'] / record['rate'] <= 20)
            check('private notification PCM is gone only after confirmed player close',
                  not record['path'].parent.exists() and provider.quiescent)
            final['actual_sapi_pcm'] = {key: value for key, value in record.items() if key != 'path'}
            final['voice'] = dict(id=provider.voice.id, name=provider.voice.name,
                                  language=provider.language, engine=provider.voice.engine)
            final['native_output'] = playback_capabilities()
            if not final['native_output']['available']:
                check('missing output device does not change the operation result',
                      tracker.snapshot().state == 'running' and tracker.snapshot().percent == 25)
                final.update(status='UNAVAILABLE', actual_sapi_synthesis='PASS',
                             reason=final['native_output']['reason'], native_playback='NOT_RUN')
                return report(77)
            check('actual WinMM delivery completes without replacing the operation outcome',
                  'playing' in players[0].observed_states and 'stopped' in players[0].observed_states
                  and not progress.progress_sound_notice() and tracker.snapshot().percent == 25)

            # A real content request interrupts the next real notification.
            second = progress.ProgressTracker('Content interruption', total=4, completed=2)
            second.update(phase='This synthetic progress message must stop before the selected recording starts')
            studio.progress_view.track(second)
            pump(lambda: len(players) >= 2 and 'playing' in players[1].observed_states,
                 'The second actual notification did not start.')
            content = root / 'public-silent-content.wav'
            with wave.open(str(content), 'wb') as pcm:
                pcm.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                pcm.writeframes(b'\0\0' * 48000)
            content_hash = sha256(content.read_bytes()).hexdigest()
            controller.result = {'output': str(content)}
            studio.notebook.SetSelection(2)
            click(studio.dialog, 'Play rendered master')
            pump(lambda: studio.playback.status().state == 'playing',
                 'Content playback was not admitted after speech cleanup.', seconds=5)
            check('native content waits for owned progress speech to be quiet',
                  players[1].closed_confirmed and provider.quiescent
                  and progress.progress_audio_suppressed())
            check('cancelled notification left no private PCM',
                  players[1].record is not None and not players[1].record['path'].parent.exists())
            studio.stop_playback()
            pump(lambda: not progress.progress_audio_suppressed(), 'Media suppression was not released.')
            studio.progress_view.settings = progress.ProgressSettings('speech', muted=True)
            second.finish(verified=True)
            studio.progress_view.refresh(force=True)
            check('actual accepted operation remains complete independently of notification cancellation',
                  second.snapshot().state == 'completed' and second.snapshot().percent == 100)
            check('source project, synthetic content and catalog are unchanged',
                  sha256(project.read_bytes()).hexdigest() == project_hash
                  and sha256(content.read_bytes()).hexdigest() == content_hash
                  and sha256(catalog.read_bytes()).hexdigest() == catalog_hash
                  and controller.script == original_text)
            check('the shared broker released notification leases',
                  not list((library.root / 'runtime/resources/leases').glob('*.json')))
            for name, digest in hashes.items():
                check('qualified installed source remains unchanged: ' + name,
                      sha256((installed_root / name).read_bytes()).hexdigest() == digest
                      and sha256((source_root / name).read_bytes()).hexdigest() == digest)
            final.update(status='PASS', actual_sapi_synthesis='PASS', native_playback='PASS',
                         native_content_interruption='PASS', wx=wx.version())
            return report(0)
    except Exception as exc:
        final.update(status='FAIL', reason=type(exc).__name__ + ': ' + str(exc)[:2000],
                     traceback=traceback.format_exc()[-5000:])
        return report(1)
    finally:
        if studio is not None:
            studio.closed = True
            studio.cancelled.set()
            studio._invalidate_playback_start()
            studio.timer.Stop()
            studio.playback_timer.Stop()
            studio.progress_view.close()
            studio.playback.close()
            studio.executor.shutdown(wait=True, cancel_futures=True)
            studio.dialog.Destroy()
        if frame is not None:
            frame.progress_view.close()
            frame.Destroy()
        if not progress.shutdown_progress_feedback():
            raise RuntimeError('The owned progress worker did not confirm shutdown.')
        app.Yield()
        if native_handle is not None:
            native_handle.close()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Choose a new progress-voice acceptance report.')
    if not args.native or os.name != 'nt':
        print(json.dumps(dict(status='NOT_RUN', reason='An explicit native Windows run is required.')), flush=True)
        return 77
    finished = Event()
    def watchdog():
        if not finished.wait(90):
            print(json.dumps(dict(status='FAIL', reason='Native progress voice exceeded 90 seconds.')), flush=True)
            os._exit(1)  # Only this owned synthetic acceptance process.
    Thread(target=watchdog, name='native-progress-voice-watchdog', daemon=True).start()
    try:
        try:
            code, final = run_native(args.output)
        except Exception as exc:
            code, final = 1, dict(schema='carr-native-progress-voice/1', status='FAIL',
                reason=type(exc).__name__ + ': ' + str(exc)[:2000],
                traceback=traceback.format_exc()[-5000:], physical_listening='NOT_RUN', nvda='NOT_RUN')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(final, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(final, ensure_ascii=False), flush=True)
        return code
    finally:
        finished.set()


if __name__ == '__main__':
    raise SystemExit(main())
