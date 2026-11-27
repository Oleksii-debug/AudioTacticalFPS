"""Actual wx menus/dialogs, background completion and cancellation boundaries.

Only synthetic model segments and an injected Google transport are used. The
real transcription exporter, provider ledger, native controls and shared desktop
executor run. Selected file paths and approval answers are supplied through test
seams; operating the OS file picker, real API/model inference, NVDA and acoustic
quality are NOT_RUN. Missing native wx/display exits 77, never PASS.

Installed: python -I tests/ui/native_language_tools_smoke.py --native
Checkout:  python tests/ui/native_language_tools_smoke.py --native --source .
"""
from argparse import ArgumentParser
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback
from types import SimpleNamespace
from unittest.mock import patch
import wave


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None):
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('No native display is available.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    if source is not None:
        sys.path[:0] = [str(source / 'src'), str(source)]
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HUB_DISABLE_TELEMETRY='1', ORT_DISABLE_TELEMETRY='1')
    import carr_library.audio.transcription as asr_module
    import carr_library.providers as provider_module
    import carr_library.ui.desktop_app as desktop_module
    import carr_library.ui.transcription_dialog as asr_ui
    import carr_library.ui.translation_dialog as translation_ui
    from carr_library.application import LibraryApp
    from carr_library.ui.api_connections import ConnectionsController

    try:
        app = wx.App(False)
    except (Exception, SystemExit) as exc:
        return not_run('A native wx application could not open: ' + str(exc))
    native_loop = wx.GUIEventLoop()
    loop_activation = wx.EventLoopActivator(native_loop)
    checks, errors, state = [], [], {}
    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in
        (asr_module, provider_module, desktop_module, asr_ui, translation_ui)}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def pump(predicate, timeout=10):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            app.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def button_event(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        return button.GetEventHandler().ProcessEvent(event)

    def menu_item(frame, text):
        for index in range(frame.menu_bar.GetMenuCount()):
            for item in frame.menu_bar.GetMenu(index).GetMenuItems():
                if item.GetItemLabelText() == text:
                    return item
        raise AssertionError('Missing native menu item: ' + text)

    def modal(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.IsModal()
                     and window.GetTitle() == title), None)

    if source is None:
        checkout = Path(__file__).resolve().parents[2]
        check('language tools are imported from the installed package',
              all(not Path(path).is_relative_to(checkout) for path in origins.values()))

    class SyntheticModel:
        supported_languages = ['en', 'uk']

        def __init__(self):
            self.calls, self.closed = 0, 0
            self.block = False
            self.started, self.release = Event(), Event()

        def transcribe(self, source, **options):
            self.calls += 1
            def segments():
                try:
                    self.started.set()
                    if self.block and not self.release.wait(15):
                        raise TimeoutError('Synthetic inference was not released by the native probe.')
                    yield SimpleNamespace(start=0., end=1., text='Синтетичний текст — Україна.',
                        words=[SimpleNamespace(start=0., end=1., word='Синтетичний')]
                        if options['word_timestamps'] else None)
                finally:
                    self.closed += 1
            return segments(), SimpleNamespace(language='uk', duration=1.)

    class MemoryVault:
        def __init__(self): self.values = {}
        def put(self, reference, binding, secret): self.values[reference, binding] = secret
        def get(self, reference, binding): return self.values[reference, binding]
        def delete(self, reference, binding): self.values.pop((reference, binding), None)

    class SyntheticGoogle:
        def __init__(self):
            self.calls, self.block = [], False
            self.started, self.release = Event(), Event()

        def post(self, profile, route, payload, headers, *, cancelled):
            self.calls.append((route, json.loads(payload)))
            self.started.set()
            deadline = monotonic() + 15
            while self.block and not self.release.wait(0.01):
                if cancelled():
                    raise InterruptedError('Synthetic provider operation cancelled after transmission.')
                if monotonic() >= deadline:
                    raise TimeoutError('Synthetic provider was not cancelled.')
            return json.dumps({'data': {'translations': [{
                'translatedText': 'Native translated text — Україна.',
                'detectedSourceLanguage': 'uk', 'model': 'nmt'}]}}, ensure_ascii=False).encode()

    model, google = SyntheticModel(), SyntheticGoogle()
    with TemporaryDirectory(prefix='resonance-native-language-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        frame = desktop_module.DesktopFrame(library)
        app.SetTopWindow(frame)
        frame.Show()
        frame.Raise()
        output = root / 'transcripts'
        output.mkdir()
        model_root = root / 'synthetic-model'
        model_root.mkdir()
        files = {}
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            data = b'synthetic fixture, not a real model: ' + name.encode()
            (model_root / name).write_bytes(data)
            files[name] = sha256(data).hexdigest()
        manifest = model_root / 'manifest.json'
        manifest.write_text(json.dumps({'schema_version': 1, 'engine': 'faster-whisper',
            'model_id': 'native-probe-fixture', 'revision': 'a' * 40,
            'license': 'synthetic-fixture', 'license_accepted': True,
            'trusted_weights': True, 'files': files}), encoding='utf-8')
        sources = []
        for index in range(2):
            audio = root / f'Синтетичне-{index}.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                stream.writeframes(bytes([index, 0]) * 8000)
            sources.append(audio)
        source_hashes = [sha256(path.read_bytes()).hexdigest() for path in sources]
        runtime = {'python': 'synthetic-native-probe', 'distributions': {
            'faster-whisper': {'version': 'synthetic', 'files_sha256': 'b' * 64}}}
        frame.transcription_controller = asr_ui.TranscriptionController(library.root,
            session_factory=lambda selected: asr_module.TranscriptionSession(selected,
                _model_factory=lambda *args, **kwargs: model, _runtime_identity=lambda: runtime))
        vault = MemoryVault()
        connection = ConnectionsController(library.root, vault=vault, transport=google)
        connection.store.save(provider_module.ProviderProfile('native-google', 'Synthetic Google account',
            'google-translate-v2', 'https://translation.googleapis.com', 'nmt', ('translation',)),
            secret='synthetic-key-never-sent-to-network')

        def abort():
            errors.append(traceback.format_exc())
            frame.cancel_event.set()
            model.release.set()
            google.release.set()
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)

        def later(predicate, then, deadline=None):
            try:
                deadline = deadline or monotonic() + 10
                if predicate():
                    then()
                elif monotonic() >= deadline:
                    raise TimeoutError('Native language-tools condition timed out.')
                else:
                    wx.CallLater(30, later, predicate, then, deadline)
            except BaseException:
                abort()

        def raise_errors():
            if errors:
                raise AssertionError('\n'.join(errors))

        try:
            check('both language tools use the shared command registry',
                all(identity in frame.keyboard_commands.bindings for identity in
                    ('audio.transcription.open', 'translation.text.open')))
            asr_item = menu_item(frame, 'Local transcription...')
            translation_item = menu_item(frame, 'Translate text...')

            def cancelled_asr():
                dialog = state['asr']
                check('native cancellation returns controls to editable state',
                    not frame.working and dialog.start.IsEnabled() and not dialog.cancel.IsEnabled())
                check('cancelled inference is not reported as completed transcription',
                    'cancelled' in dialog.status.GetValue().lower()
                    and 'Transcription complete' not in dialog.status.GetValue())
                check('cancellation preserves the first completed transcript and closes the iterator',
                    len(list(output.glob('transcript-*/complete-*.json'))) == 1 and model.closed == 2)
                check('guarded Close button ends the transcription dialog',
                    button_event(next(child for child in dialog.GetChildren()
                                      if isinstance(child, wx.Button) and child.GetId() == wx.ID_CLOSE)))
                state['asr_complete'] = True

            def after_escape():
                dialog = state['asr']
                check('Escape keeps an active transcription dialog alive until cancellation completes',
                    dialog.IsModal() and dialog.pending and frame.cancel_event.is_set())
                model.release.set()
                later(lambda: not dialog.pending, cancelled_asr)

            def inference_started():
                dialog = state['asr']
                dialog.Raise()
                dialog.status.SetFocus()
                check('Escape is delivered through the native input simulator',
                      wx.UIActionSimulator().Char(wx.WXK_ESCAPE))
                wx.CallLater(100, lambda: later(lambda: frame.cancel_event.is_set(), after_escape))

            def completed_asr():
                dialog = state['asr']
                check('shared executor completes actual local transcript exports',
                    not frame.working and 'Transcription complete: 1 file(s)' in dialog.status.GetValue())
                paths = list(output.glob('transcript-*/transcript.*'))
                check('all four transcript formats exist after native completion',
                      {path.suffix for path in paths} == {'.txt', '.srt', '.vtt', '.json'})
                details = json.loads(next(path for path in paths if path.suffix == '.json').read_bytes())
                check('native word-timestamp choice reaches the real service',
                    details['settings']['word_timestamps'] is True and bool(details['segments'][0]['words']))
                check('original audio bytes remain unchanged',
                      [sha256(path.read_bytes()).hexdigest() for path in sources] == source_hashes)
                dialog.sources = (sources[1],)
                model.block = True
                model.started.clear()
                check('native Start can launch the next selected source', button_event(dialog.start))
                later(model.started.is_set, inference_started)

            def begin_asr():
                try:
                    dialog = modal('Resonance — local transcription')
                    check('shared Audio menu opens a native transcription dialog', dialog is not None)
                    state['asr'] = dialog
                    check('source and status are named read-only native text controls',
                        isinstance(dialog.files, wx.TextCtrl) and not dialog.files.IsEditable()
                        and dialog.files.GetName() == 'Selected audio or video files'
                        and not dialog.status.IsEditable()
                        and dialog.status.GetName() == 'Transcription progress and result')
                    check('language task and device use standard native controls',
                        isinstance(dialog.language, wx.TextCtrl) and isinstance(dialog.task, wx.Choice)
                        and isinstance(dialog.device, wx.Choice) and isinstance(dialog.words, wx.CheckBox))
                    dialog.sources = (sources[0],)
                    dialog.files.SetValue(str(sources[0]))
                    dialog.manifest.SetValue(str(manifest))
                    dialog.output.SetValue(str(output))
                    dialog.words.SetValue(True)
                    check('native Start event dispatches the existing asynchronous job',
                        button_event(dialog.start) and dialog.pending and frame.working)
                    check('busy transcription disables mutable settings and enables cancellation',
                        not dialog.manifest.IsEnabled() and not dialog.words.IsEnabled() and dialog.cancel.IsEnabled())
                    translation_ui.show_translation_dialog(frame, 'Unsent text while transcription runs')
                    check('translation shortcut guard does not open a second dialog during transcription',
                          modal('Translate text — Google Basic v2') is None)
                    later(lambda: not dialog.pending, completed_asr)
                except BaseException:
                    abort()

            wx.CallAfter(begin_asr)
            event = wx.CommandEvent(wx.EVT_MENU.typeId, asr_item.GetId())
            check('real transcription menu event is handled', frame.GetEventHandler().ProcessEvent(event))
            raise_errors()
            check('native transcription completion and cancellation workflow finished',
                  state.get('asr_complete') is True)

            selected_output = [root / 'translation.json']
            source_text = 'Оригінальний текст — Україна.\nДругий рядок.'

            class SelectedFileDialog:
                def __init__(self, *args, **kwargs): pass
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(selected_output[0])
                def Destroy(self): pass

            def begin_translation():
                try:
                    dialog = modal('Translate text — Google Basic v2')
                    check('shared menu opens native Google translation controls', dialog is not None)
                    check('source text and language fields are named editable controls',
                        dialog.text.GetName() == 'Original text to translate' and dialog.text.IsEditable()
                        and dialog.target_language.GetName() == 'Target language code'
                        and isinstance(dialog.profiles, wx.Choice))
                    dialog.text.SetValue(source_text)
                    dialog.target_language.SetValue('en')
                    dialog.budget.SetValue('0.000100')
                    check('opening translation sent no provider request',
                          len(google.calls) == (1 if google.block else 0))
                    button = next(child for child in dialog.GetChildren()
                                  if isinstance(child, wx.Button) and child.GetLabel() == '&Review and translate...')
                    check('native translation review button event is handled', button_event(button))
                except BaseException:
                    abort()

            def inspect_result():
                dialog = modal('Translation result')
                text = next(child for child in descendants(dialog)
                            if isinstance(child, wx.TextCtrl) and child.GetName() == 'Translated text')
                check('completed translation displays literal result in a named read-only control',
                      not text.IsEditable() and text.GetValue() == 'Native translated text — Україна.')
                check('translation result returns native keyboard focus to its text',
                      pump(lambda: wx.Window.FindFocus() is text))
                document = json.loads(selected_output[0].read_bytes())
                check('actual native completion saves source-bound translation metadata',
                      document['source']['text'] == source_text
                      and document['translation']['text'] == text.GetValue()
                      and document['translation']['language'] == 'en')
                check('shared executor restores cancellation and busy controls',
                      not frame.working and not frame.cancel_button.IsEnabled())
                close = next(child for child in descendants(dialog)
                             if isinstance(child, wx.Button) and child.GetId() == wx.ID_CLOSE)
                check('result Close button uses its actual native event handler', button_event(close))
                state['translation_complete'] = True

            with patch.object(translation_ui, 'ConnectionsController', lambda root: connection), \
                    patch.object(wx, 'FileDialog', SelectedFileDialog), \
                    patch.object(wx, 'MessageBox', return_value=wx.YES):
                wx.CallAfter(begin_translation)
                wx.CallLater(30, later, lambda: modal('Translation result') is not None, inspect_result)
                event = wx.CommandEvent(wx.EVT_MENU.typeId, translation_item.GetId())
                check('real translation menu event is handled', frame.GetEventHandler().ProcessEvent(event))
                check('translation background completion reaches its native result dialog',
                      pump(lambda: state.get('translation_complete') or bool(errors)))
                raise_errors()
                check('one approved native translation makes one synthetic request', len(google.calls) == 1)

                google.block = True
                google.started.clear()
                selected_output[0] = root / 'cancelled-translation.json'
                wx.CallAfter(begin_translation)
                event = wx.CommandEvent(wx.EVT_MENU.typeId, translation_item.GetId())
                frame.GetEventHandler().ProcessEvent(event)
                check('second translation runs asynchronously before cancellation',
                      pump(google.started.is_set) and frame.working)
                check('native main-window Cancel event reaches the provider callback',
                      button_event(frame.cancel_button) and frame.cancel_event.is_set())
                check('cancelled translation returns control without an automatic retry',
                      pump(lambda: not frame.working) and len(google.calls) == 2
                      and 'uncertain charge' in frame.status.GetValue().lower()
                      and not selected_output[0].exists())
                with closing(sqlite3.connect(library.root / 'api-operations.sqlite')) as db:
                    states = [row[0] for row in db.execute('SELECT state FROM operation ORDER BY rowid')]
                check('actual provider ledger retains complete and uncertain terminal results',
                      states == ['complete', 'uncertain'])
                raise_errors()
        finally:
            frame._closing = True
            frame.cancel_event.set()
            model.release.set()
            google.release.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            frame.Destroy()
            app.Yield()

    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
        'platform': platform.system(), 'origins': origins,
        'source_override': str(source) if source is not None else None,
        'input_events': 'wx native command events; actual native Escape key',
        'file_picker_operation': 'NOT_RUN', 'approval_answer': 'synthetic test seam',
        'asr_engine': 'synthetic segments through real transcription service',
        'google_transport': 'injected synthetic responses; no network or paid requests',
        'real_asr_inference': 'NOT_RUN', 'language_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path, help='Explicit checkout override for debugging only.')
    args = parser.parse_args()
    return run_native(args.source.resolve() if args.source is not None else None)


if __name__ == '__main__':
    raise SystemExit(main())
