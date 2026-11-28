"""Actual installed wx, current publication producer and selected FFmpeg probe."""
from argparse import ArgumentParser
from dataclasses import asdict
import importlib
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import sys
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
import traceback
import wave



MODULES = (
    'carr_library.reader.media_overlay', 'carr_library.reader.publication_state',
    'carr_library.audio.playback_processing', 'carr_library.ui.media_overlay_dialog',
    'carr_library.audio.recording_process', 'carr_library.audio.cleanup',
    'carr_library.audio.jobs', 'carr_library.audio.publishing', 'carr_library.audio.daisy',
    'carr_library.audio.player', 'carr_library.audio.playback', 'carr_library.library_backup',
)


def installed_identity():
    if not sys.flags.isolated:
        raise ValueError('Use the exact isolated installed Python with -I.')
    prefix = Path(sys.prefix).resolve(strict=True)
    result = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('A native publication-reader module escaped the installed application.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def export_fixture_publications(project_dir, library_root, run_key, paths, *, ffmpeg):
    """Build the actual probe books with the producer's full staging allowance.

    Publication reserves up to 128 MiB for validated XML in addition to its PCM,
    MP3 and independent verification files. A 128 MiB total cannot admit even
    this small fixture. The reader still uses its separate 16 MiB input budget.
    """
    from carr_library.audio.publishing import PublicationService
    service = PublicationService(project_dir, private_root=library_root, ffmpeg=ffmpeg,
                                 max_bytes=512 * 1024**2)
    service.export_epub(run_key, paths['epub'], language='en', creator='Synthetic author')
    service.export_daisy(run_key, paths['daisy'], language='en', creator='Synthetic author',
                         publisher='Synthetic publisher')


def run_native():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.audio.jobs import sha256_file
    from carr_library.audio.production import render_project
    from carr_library.audio.runtime import resolve_ffmpeg
    from carr_library.operations import ResourceBroker
    from carr_library.reader.publication_state import PublicationPositions
    from carr_library.ui.audio_studio import StudioController
    from carr_library.ui.media_overlay_dialog import MediaOverlayDialog

    checks, failures, metrics, timers = [], [], {}, []
    state = {'active': True, 'phase': 'prepare-epub', 'window': None, 'done': False}
    deadline = monotonic() + 120
    gui = wx.App(False)
    frame = wx.Frame(None, title='Installed synchronized publication acceptance')
    frame.Show()
    frame.working = frame.busy = frame._closing = False
    gui.SetTopWindow(frame)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('Expected an enabled named publication action.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('Native publication action was not handled.')

    class Synthetic:
        def cache_identity(self, voice, **kwargs):
            return {'fixture': 'native-recorded-reading-v1', 'voice': voice}
        def synthesize(self, text, output, **kwargs):
            with wave.open(str(output), 'wb') as stream:
                stream.setparams((1, 2, 8000, 1600, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 1600)
            return output

    with tempfile.TemporaryDirectory(prefix='carr-native-publication-reader-') as temporary:
        root = Path(temporary)
        frame.library = app = LibraryApp(root / 'library')
        app.create_workspace('Synthetic native recorded reading')
        ffmpeg = resolve_ffmpeg()
        check('selected real FFmpeg exists', ffmpeg is not None and ffmpeg.is_file())
        studio = StudioController(private_root=app.root, ffmpeg=ffmpeg)
        script = ('[output sample_rate=16000 stems=false]\n# Перша\n'
                  'Alice: Original 😀 & <text>.\n[pause 0.1]\n# Second\nBob: Last line.')
        studio.new(script, title='Книжка 🙂', input_kind='script')
        studio.configure(voice='fixture:pcm', language='en')
        studio.save_in_library()
        result = render_project(studio.plan(), studio.output_dir, engine=Synthetic(), ffmpeg=ffmpeg)
        paths = {'epub': root / 'Українська.epub', 'daisy': root / 'Українська.zip'}
        export_fixture_publications(studio.output_dir, app.root, result['run_key'], paths,
                                    ffmpeg=ffmpeg)
        original = {kind: sha256_file(path) for kind, path in paths.items()}
        saved = {}

        def schedule():
            timers.append(wx.CallLater(20, poll))

        def poll():
            if not state['active']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('The actual recorded-reading workflow exceeded its deadline.')
                window, phase = state['window'], state['phase']
                if window is None or not window.dialog.IsModal():
                    schedule()
                    return
                if phase in ('prepare-epub', 'prepare-daisy'):
                    kind = phase.split('-', 1)[1]
                    check('native named read-only text ' + kind, isinstance(window.text, wx.TextCtrl)
                          and not window.text.IsEditable() and bool(window.text.GetName()))
                    check('thirteen named native reader actions ' + kind, len(window.controls) == 13
                          and all(isinstance(control, wx.Button) and control.GetName()
                                  for control in window.controls.values()))
                    window.path.ChangeValue(str(paths[kind]))
                    window.max_mib.ChangeValue('16')
                    press(window.controls['load'])
                    state['phase'] = 'loaded-' + kind
                elif phase in ('loaded-epub', 'loaded-daisy'):
                    if window.future is not None:
                        schedule()
                        return
                    kind = phase.split('-', 1)[1]
                    check('actual verified PCM prepared ' + kind, window.session is not None
                          and window.session.document.kind == kind)
                    document = window.session.document
                    info = wav_info(document.audio)
                    metrics[kind] = {'frames': info.frames, 'sample_rate': info.rate,
                                     'channels': info.channels, 'pcm_sha256': sha256_file(document.audio),
                                     'publication_sha256': document.publication_sha256}
                    check('exact full publication identity ' + kind,
                          document.publication_sha256 == original[kind])
                    check('native exact Unicode interval ' + kind,
                          window.text.GetValue() == 'Original 😀 & <text>.')
                    check('whole measured interval has native selection ' + kind,
                          window.text.GetSelection() == (0, window.text.GetLastPosition()))
                    check('preparation starts no output device ' + kind,
                          window.player.path is None and not window.session._transport_active)
                    check('resource reservations released ' + kind,
                          ResourceBroker.for_app(app.root).snapshot()['leases'] == [])
                    window.playback_speed.ChangeValue('2')
                    window.playback_pitch.ChangeValue('4')
                    window.playback_volume.ChangeValue('.5')
                    window.playback_pan.ChangeValue('-.25')
                    deferred = []
                    def defer(begin, **kwargs):
                        deferred.append(begin)
                        return SimpleNamespace(close=lambda: deferred.clear())
                    # Drive the actual Play event and worker, but hold the
                    # existing media-approval callback before any device opens.
                    window.progress = SimpleNamespace(media_action=defer)
                    state['deferred'] = deferred
                    press(window.controls['play'])
                    state['phase'] = 'controlled-' + kind
                elif phase in ('controlled-epub', 'controlled-daisy'):
                    if window.future is not None:
                        schedule()
                        return
                    kind = phase.split('-', 1)[1]
                    controlled = window.session._prepared_playback
                    document = window.session.document
                    check('actual independent recorded-reader controls ' + kind,
                          controlled is not None and controlled.audio_frames == round(document.frames / 2)
                          and controlled.source_sha256 == document.audio_sha256)
                    check('deferred native playback keeps both PCM owners ' + kind,
                          len(state['deferred']) == 1 and window.player.path is None
                          and controlled._reader is not None and window.session._reader is not None)
                    check('recorded-reader source mapping is labelled estimated ' + kind,
                          window.session.source_time_accuracy == 'estimated_after_time_stretch')
                    metrics[kind]['controlled_frames'] = controlled.audio_frames
                    metrics[kind]['controlled_pcm_sha256'] = sha256_file(controlled.audio)
                    metrics[kind]['source_time_accuracy'] = controlled.source_time_accuracy
                    state['controlled_pcm'] = controlled.audio
                    window.chapter.SetFocus()
                    state['phase'] = 'focus-' + kind
                elif phase in ('focus-epub', 'focus-daisy'):
                    if wx.Window.FindFocus() is not window.chapter:
                        schedule()
                        return
                    kind = phase.split('-', 1)[1]
                    check('actual native chapter keyboard focus ' + kind, True)
                    press(window.controls['next_chapter'])
                    check('native next chapter selects exact measured text ' + kind,
                          window.session.index == 2 and window.text.GetValue() == 'Last line.')
                    check('navigation does not start output ' + kind, window.player.path is None)
                    saved[kind] = window.session.cursor
                    press(window.controls['where'])
                    check('accessible where-am-I names actual chapter ' + kind,
                          'Chapter 2' in window.status.GetValue())
                    state['old_pcm'] = window.session.document.audio
                    press(window.controls['stop'])
                    state['phase'] = 'saved-' + kind
                elif phase in ('saved-epub', 'saved-daisy'):
                    if window.future is not None or window.stop_pending:
                        schedule()
                        return
                    kind = phase.split('-', 1)[1]
                    check('exact-source position saved ' + kind,
                          PublicationPositions(app.root).load(window.session.document) == saved[kind])
                    check('stop preserves prepared PCM for replay ' + kind, state['old_pcm'].is_file())
                    check('stop releases both playback verification readers ' + kind,
                          window.session._reader is None and window.session._prepared_playback._reader is None)
                    if kind == 'epub':
                        state['epub_pcm'] = state['old_pcm']
                        state['epub_controlled_pcm'] = state['controlled_pcm']
                        state['phase'] = 'prepare-daisy'
                    else:
                        state['phase'] = 'reload-epub'
                        window.path.ChangeValue(str(paths['epub']))
                        press(window.controls['load'])
                elif phase == 'reload-epub':
                    if window.future is not None:
                        schedule()
                        return
                    check('new load released old owned PCM', not state['epub_pcm'].exists())
                    check('new load released old independent playback PCM',
                          not state['epub_controlled_pcm'].exists())
                    check('exact publication cursor restored after another book',
                          window.session.document.kind == 'epub' and window.session.cursor == saved['epub'])
                    check('restored native text is exact', window.text.GetValue() == 'Last line.')
                    check('restore starts no output device', window.player.path is None)
                    state['final_pcm'] = window.session.document.audio
                    state['done'] = True
                    state['phase'] = 'closing'
                    press(window.controls['close'])
                    return
                schedule()
            except BaseException:
                failures.append(traceback.format_exc())
                state['active'] = False
                if state['window'] is not None:
                    state['window'].close()

        def ready(window):
            state['window'] = window
            schedule()

        window = MediaOverlayDialog(frame, on_ready=ready)
        window.show()
        state['active'] = False
        for timer in timers:
            timer.Stop()
        check('modal exits only after actual owners settle',
              window.closed and window.future is None and window.session is None)
        check('final private PCM removed after close',
              state.get('final_pcm') is not None and not state['final_pcm'].exists())
        check('both original publications remain byte-identical',
              all(sha256_file(path) == original[kind] for kind, path in paths.items()))
    frame.Destroy()
    if failures:
        print('\n'.join(failures), flush=True)
    evidence = {'status': 'PASS' if state['done'] and not failures else 'FAIL',
                'checks': checks, 'actual_publication_pcm': metrics,
                'native_controls': 'EXECUTED', 'physical_playback': 'NOT_RUN',
                'nvda': 'NOT_RUN', 'word_alignment': 'NOT_CLAIMED',
                'language_quality': 'NOT_CLAIMED', 'third_party_interoperability': 'NOT_RUN'}
    return evidence



def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' or not args.native:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit native Windows execution is required.',
                          'nvda': 'NOT_RUN'}))
        return 77
    settled = Event()
    def watchdog():
        if not settled.wait(180):
            print(json.dumps({'status': 'FAIL', 'reason': 'Recorded-reader native probe exceeded 180 seconds.'}), flush=True)
            os._exit(1)
    Thread(target=watchdog, daemon=True).start()
    native = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        from carr_library.audio.media_conversion import runtime_identity
        from carr_library.audio.runtime import FFMPEG_WINDOWS_SHA256, FFMPEG_VERSION
        native = activate_bundled_runtime()
        origins = installed_identity()
        runtime = runtime_identity(Path(os.environ['CARR_FFMPEG']).resolve(strict=True))
        if runtime.sha256 != FFMPEG_WINDOWS_SHA256 or not runtime.version.startswith('ffmpeg version ' + FFMPEG_VERSION):
            raise ValueError('The actual FFmpeg runtime differs from its reviewed byte/version pin.')
        evidence = run_native()
        if installed_identity() != origins:
            raise ValueError('Installed reader implementations changed during native execution.')
        if runtime_identity(Path(os.environ['CARR_FFMPEG']).resolve(strict=True)) != runtime:
            raise ValueError('The actual FFmpeg runtime changed during native execution.')
        if native is not None:
            native.close()
            native = None
        evidence.update(installed_mode=True, isolated=True, module_origins=origins,
                        actual_ffmpeg=asdict(runtime), source_commit=os.environ['GITHUB_SHA'])
        print(json.dumps(evidence, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if evidence['status'] == 'PASS' else 1
    except BaseException as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-16000:], flush=True)
        print(json.dumps({'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error)[:4000],
                          'nvda': 'NOT_RUN'}), flush=True)
        return 1
    finally:
        try:
            if native is not None:
                native.close()
        finally:
            settled.set()


if __name__ == '__main__':
    raise SystemExit(main())
