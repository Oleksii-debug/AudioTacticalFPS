"""Actual installed wx, current publication producer and selected FFmpeg probe."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from time import monotonic
import traceback
import wave


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows required.', 'nvda': 'NOT_RUN'}))
        return 77
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.audio.jobs import sha256_file
    from carr_library.audio.production import render_project
    from carr_library.audio.publishing import PublicationService
    from carr_library.audio.runtime import resolve_ffmpeg
    from carr_library.operations import ResourceBroker
    from carr_library.reader.publication_state import PublicationPositions
    from carr_library.ui.audio_studio import StudioController
    from carr_library.ui.media_overlay_dialog import MediaOverlayDialog

    checks, failures, metrics, timers = [], [], {}, []
    state = {'active': True, 'phase': 'prepare-epub', 'window': None, 'done': False}
    deadline = monotonic() + 90
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
        service = PublicationService(studio.output_dir, private_root=app.root, ffmpeg=ffmpeg,
                                     max_bytes=128 * 1024**2)
        paths = {'epub': root / 'Українська.epub', 'daisy': root / 'Українська.zip'}
        service.export_epub(result['run_key'], paths['epub'], language='en', creator='Synthetic author')
        service.export_daisy(result['run_key'], paths['daisy'], language='en', creator='Synthetic author',
                             publisher='Synthetic publisher')
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
                                     'pcm_sha256': sha256_file(document.audio),
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
                    if kind == 'epub':
                        state['epub_pcm'] = state['old_pcm']
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
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
    return 0 if evidence['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
