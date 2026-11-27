"""Actual installed Windows live-reader controls and SAPI PCM preparation.

A real native dialog, immutable source and selected installed SAPI voice are
used. No hardware playback, NVDA, word timing or language-quality claim.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from time import monotonic
import traceback


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows required.', 'nvda': 'NOT_RUN'}))
        return 77
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.engines import SAPI5Engine, VoiceRegistry
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.audio.jobs import sha256_file
    from carr_library.operations import ResourceBroker
    from carr_library.reader.live_speech import SpeechPositionStore
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.ui.live_reading import LiveReadingDialog, native_offset, selected_source_offsets

    checks, failures, timers, metrics = [], [], [], {}
    state = {'active': True, 'window': None, 'phase': 'ready', 'done': False}
    deadline = monotonic() + 60
    gui = wx.App(False)
    frame = wx.Frame(None, title='Live source reading acceptance')
    frame.Show()
    gui.SetTopWindow(frame)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('Expected an enabled native live-reader button.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('Native reader action was not handled.')

    with tempfile.TemporaryDirectory(prefix='carr-native-live-reader-') as temporary:
        root = Path(temporary)
        app = LibraryApp(root / 'library')
        workspace = app.create_workspace('Synthetic live reader')
        source = 'Prefix 😀.\r\nSelected source sentence 🦉. Second source sentence.\nLast paragraph.'
        book = app.add_text(workspace, 'Exact source — Україна', source)
        desk = ReadingDesk(app, workspace, book['edition'])
        page = desk.page(1)
        source = page.raw_text
        first = source.index('Selected')
        last = source.index(' Second')
        before = sha256(source.encode('utf-8')).hexdigest()
        registry = VoiceRegistry((SAPI5Engine(),))

        def load(ordinal, cancelled):
            if cancelled():
                raise InterruptedError('Native source reading cancelled.')
            result = desk.page(ordinal)
            if cancelled():
                raise InterruptedError('Native source reading cancelled.')
            return result

        def schedule():
            timers.append(wx.CallLater(20, poll))

        def poll():
            if not state['active']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('The native reader sequence exceeded its deadline.')
                window, phase = state['window'], state['phase']
                if window is None or not window.dialog.IsModal():
                    schedule()
                    return
                if phase == 'ready':
                    check('named read-only native source control', isinstance(window.text, wx.TextCtrl)
                          and not window.text.IsEditable() and window.dialog.GetName() == 'Live source reading')
                    check('all ten live reading actions are native and named', len(window.controls) == 10
                          and all(isinstance(control, wx.Button) and control.GetName()
                                  for control in window.controls.values()))
                    check('native emoji and newline selection identifies exact source',
                          selected_source_offsets(window.text, source) == (first, last))
                    for offset in (0, source.index('😀'), source.index('😀') + 1, first, last, len(source)):
                        position = native_offset(window.text, source, offset)
                        window.text.SetSelection(position, position)
                        check('native Unicode cursor roundtrip ' + str(offset),
                              selected_source_offsets(window.text, source) == (offset, offset))
                    window._select((first, last))
                    check('native reading range includes whole book', 'book' in window.modes)
                    press(window.controls['voices'])
                    state['phase'] = 'voices'
                elif phase == 'voices':
                    if window.future is not None:
                        schedule()
                        return
                    eligible = [index for index, voice in enumerate(window.voice_rows)
                                if voice.available and any(code.lower().split('-')[0] == 'en'
                                                           for code in voice.languages)]
                    check('actual installed English SAPI voice enumerated', bool(eligible))
                    check('voice enumeration requires explicit selection', window.voice.GetSelection() == wx.NOT_FOUND)
                    index = eligible[0]
                    window.voice.SetSelection(index)
                    window.language.ChangeValue(window.voice_rows[index].languages[0])
                    event = wx.CommandEvent(wx.EVT_CHOICE.typeId, window.voice.GetId())
                    event.SetEventObject(window.voice)
                    window.voice.GetEventHandler().ProcessEvent(event)
                    check('unsupported SAPI pitch control stays disabled', not window.pitch.IsEnabled())
                    window.mode.SetSelection(window.modes.index('selection'))
                    window.voice.SetFocus()
                    state['phase'] = 'focus'
                elif phase == 'focus':
                    if wx.Window.FindFocus() is not window.voice:
                        schedule()
                        return
                    check('actual native keyboard focus reaches selected voice', True)
                    press(window.controls['prepare'])
                    state['phase'] = 'prepared'
                elif phase == 'prepared':
                    if window.future is not None:
                        schedule()
                        return
                    check('actual SAPI utterance became prepared PCM', window.session is not None
                          and window.session.current is not None)
                    excerpt = window.session.current
                    info = wav_info(excerpt.path)
                    metrics.update(frames=info.frames, sample_rate=info.rate,
                                   pcm_sha256=sha256_file(excerpt.path), source_sha256=before)
                    check('measured PCM and exact spoken source digest', info.frames > 0
                          and excerpt.text_sha256 == sha256(source[first:last].encode('utf-8')).hexdigest())
                    check('prepared excerpt highlights exact native source range',
                          selected_source_offsets(window.text, source) == (first, last))
                    check('preparing does not start a device or advance cursor',
                          not window.playing and window.player.path is None and window.session.cursor == first)
                    state['temporary_pcm'] = excerpt.path
                    press(window.controls['stop'])
                    state['phase'] = 'stopped'
                elif phase == 'stopped':
                    if window.future is not None or window.session is not None:
                        schedule()
                        return
                    row = SpeechPositionStore(app.root).load(source, page.location)
                    check('stop retains only the last completed text cursor',
                          row is not None and row['cursor'] == first)
                    check('stop releases private utterance PCM', not state['temporary_pcm'].exists())
                    check('book resume is exact source-bound',
                          ReadingState(app.root).resume(workspace, book['edition']) == page.location)
                    check('speech resource reservation is released', ResourceBroker.for_app(app.root).snapshot()['leases'] == [])
                    window._select((0, 0))
                    press(window.controls['resume'])
                    state['phase'] = 'restored'
                elif phase == 'restored':
                    if window.future is not None:
                        schedule()
                        return
                    check('restored cursor uses exact native character position',
                          selected_source_offsets(window.text, source) == (first, first)
                          and window.modes[window.mode.GetSelection()] == 'cursor')
                    press(window.controls['where'])
                    check('where-am-I is exposed through named native status',
                          'Source location 1 of 1.' in window.status.GetValue())
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

        window = LiveReadingDialog(frame, app.root, source, title=page.title,
            cursor=first, selection=(first, last), location=page.location,
            source_loader=load, registry=registry, on_ready=ready)
        window.show()
        state['active'] = False
        for timer in timers:
            timer.Stop()
        check('native modal exits only after reader owners settle',
              window.closed and window.future is None and window.session is None)
        check('immutable source remains byte-identical after the native workflow',
              desk.page(1).raw_text == source and sha256(source.encode('utf-8')).hexdigest() == before)
    frame.Destroy()
    if failures:
        print('\n'.join(failures), flush=True)
    result = {'status': 'PASS' if state['done'] and not failures else 'FAIL',
              'checks': checks, 'actual_sapi_pcm': metrics, 'native_controls': 'EXECUTED',
              'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN',
              'word_alignment': 'NOT_CLAIMED', 'language_quality': 'NOT_CLAIMED'}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

