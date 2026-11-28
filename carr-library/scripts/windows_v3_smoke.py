"""Actual Windows SAPI/FFmpeg/native-control acceptance. No manual NVDA claim."""
from dataclasses import asdict
from pathlib import Path
import json
import os
import sys
import struct
import wave
from tempfile import TemporaryDirectory



def verify_source_translation(app, frame, library, result):
    """Exercise retained-source controls; no account, provider or model call."""
    import threading
    from hashlib import sha256
    from time import monotonic
    from unittest.mock import patch

    import wx
    from carr_library.assistant.providers import Gateway
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range
    from carr_library.ui.translation_dialog import show_translation

    workspace = frame.active[0]
    parts = ('First original position. Україна.',
             'Second original position. Slovenská citácia.',
             'Third original position. Preserve source bytes.')
    raw = '\f'.join(parts)
    book = library.add_text(workspace, 'Synthetic translation source', raw)
    desk = ReadingDesk(library, workspace, book['edition'])
    original = library.assets.resolve(desk.identity.asset_sha256)
    before = original.read_bytes()
    assert before == raw.encode('utf-8')
    digest = sha256(before).hexdigest()
    panel = frame.book.GetPage(1)
    panel.refresh()
    assert panel.ready and book['edition'] in panel.book_ids
    panel.books.SetSelection(panel.book_ids.index(book['edition']))
    menu_items = [item for index in range(frame.menu_bar.GetMenuCount())
                  for item in frame.menu_bar.GetMenu(index).GetMenuItems()]
    menu_item = next(item for item in menu_items
                     if item.GetItemLabel().replace('&', '').startswith(
                         'Translate selected book or source range'))

    def children(parent):
        for child in parent.GetChildren():
            yield child
            yield from children(child)

    def control(dialog, name):
        found = [item for item in children(dialog) if item.GetName() == name]
        assert len(found) == 1, 'Missing or ambiguous native control: ' + name
        return found[0]

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event), 'Native button event was not handled.'

    def menu():
        event = wx.CommandEvent(wx.EVT_MENU.typeId, menu_item.GetId())
        event.SetEventObject(frame)
        assert frame.GetEventHandler().ProcessEvent(event), 'Native translation menu was not handled.'

    def wait_for(predicate, title):
        deadline = monotonic() + 30
        while not predicate():
            if monotonic() >= deadline:
                raise TimeoutError(title)
            app.Yield()
            wx.MilliSleep(10)

    def drive(open_dialog, visit):
        # The timer also runs inside ShowModal's nested native event loop.
        deadline, errors, finished, timers = monotonic() + 30, [], [], []
        def tick():
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native source translation did not reach its expected boundary.')
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsShown()
                           and window.GetParent() is frame]
                if not finished and dialogs and visit(dialogs[-1]):
                    finished.append(True)
            except BaseException as exc:
                errors.append(exc)
                finished.append(False)
                frame.cancel_event.set()
                for window in wx.GetTopLevelWindows():
                    if (isinstance(window, wx.Dialog) and window.IsShown()
                            and window.GetParent() is frame and window.IsModal()):
                        window.EndModal(wx.ID_CANCEL)
                return
            timers.append(wx.CallLater(25, tick))
        timers.append(wx.CallLater(25, tick))
        try:
            open_dialog()
            wait_for(lambda: bool(finished), 'Native modal observer did not finish.')
        finally:
            for timer in timers:
                timer.Stop()
        if errors:
            raise errors[0]
        assert finished == [True]

    def close_button(dialog):
        return next(item for item in children(dialog)
                    if isinstance(item, wx.Button)
                    and item.GetLabel().replace('&', '') == 'Close')

    with patch.object(Gateway, 'request',
                      side_effect=AssertionError('Local source loading must not call a provider.')) as requests:
        seen_range = []
        def loaded(dialog):
            if dialog.GetTitle() == 'Translate a book or source range':
                assert isinstance(dialog, wx.TextEntryDialog)
                assert not seen_range
                seen_range.append(True)
                dialog.SetValue('2-3')
                click(dialog.FindWindowById(wx.ID_OK))
                return False
            selected = control(dialog, 'Selected text; originals remain unchanged')
            status = control(dialog, 'Preview, supported languages and job status')
            if 'Source loaded locally.' not in status.GetValue():
                return False
            origin = control(dialog, 'Source selection and limits').GetValue()
            assert seen_range == [True]
            assert selected.GetValue() == '\n\n'.join(parts[1:])
            assert isinstance(selected, wx.TextCtrl) and selected.IsEnabled()
            assert not selected.IsEditable()
            assert 'Synthetic translation source' in origin
            assert 'source reading positions 2-3 (2 positions)' in origin
            assert 'not quotation evidence' in origin
            click(close_button(dialog))
            return True

        drive(menu, loaded)
        wait_for(lambda: not frame.working, 'Source load did not release the host worker.')
        assert original.read_bytes() == before
        result['checks'].append('native selected-book range 2-3 loads exact read-only retained source')

        cancelled_range = []
        def cancel_range(dialog):
            assert dialog.GetTitle() == 'Translate a book or source range'
            cancelled_range.append(True)
            click(dialog.FindWindowById(wx.ID_CANCEL))
            return True
        drive(menu, cancel_range)
        wait_for(lambda: not frame.working, 'Range cancellation did not release the host worker.')
        app.Yield()
        assert cancelled_range == [True]
        assert not any(isinstance(window, wx.Dialog) and window.IsShown()
                       and window.GetParent() is frame for window in wx.GetTopLevelWindows())
        result['checks'].append('native source-range Cancel opens no translation workspace')

        started, observed, ended = threading.Event(), threading.Event(), threading.Event()
        reader_cancelled = threading.Event()
        release = threading.Event()
        def paused_source(cancelled):
            started.set()
            try:
                deadline = monotonic() + 30
                while not cancelled():
                    if monotonic() >= deadline or release.wait(.01):
                        raise TimeoutError('The native dialog did not cancel its source loader.')
                observed.set()
                # Re-enter the real reader with the actual cancellation callback.
                try:
                    read_source_range(desk, 2, 3, cancelled=cancelled)
                except InterruptedError:
                    reader_cancelled.set()
                    raise
                raise AssertionError('The cancelled source reader returned a document.')
            finally:
                ended.set()

        def close_loading(dialog):
            selected = control(dialog, 'Selected text; originals remain unchanged')
            assert selected.GetValue() == '' and not selected.IsEditable()
            if not started.is_set():
                return False
            assert frame.working
            click(close_button(dialog))
            return True

        try:
            drive(lambda: show_translation(frame, source_loader=paused_source), close_loading)
            wait_for(lambda: ended.is_set() and not frame.working,
                     'Closing translation did not stop the real source worker.')
            assert observed.is_set() and reader_cancelled.is_set()
        finally:
            release.set()
        requests.assert_not_called()
        assert original.read_bytes() == before
        assert sha256(original.read_bytes()).hexdigest() == digest
        assert library._selections == {}
        result['checks'].append('native close cancels active source loading without modifying originals')
        result['translation_source'] = {
            'fixture': 'synthetic retained Unicode TXT',
            'selected_positions': [2, 3],
            'original_sha256': digest,
            'provider_calls': requests.call_count,
            'manual_nvda': 'NOT_RUN',
        }


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance script requires actual Windows execution.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech import VoiceRegistry, SpeechSettings
    from carr_library.studio.render import ProductionService
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.studio_dialog import show_studio
    from carr_library.ui.assistant_dialog import show_assistant, show_provider_settings
    from carr_library.ui.engine_settings import show_engine_settings
    from carr_library.ui.translation_dialog import show_translation
    app = wx.App(False)
    result = {'status': 'RUNNING', 'platform': sys.platform, 'manual_nvda': 'NOT_RUN', 'checks': []}
    with TemporaryDirectory(prefix='carr-native-v3-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Native acceptance')
        registry = VoiceRegistry.discover(include_flite=False)
        sapi = tuple(voice for voice in registry.voices() if voice.engine.startswith('SAPI 5'))
        if not sapi:
            raise RuntimeError('Native SAPI discovery failed: ' + '; '.join(registry.notices))
        voice = sapi[0]
        spoken = registry.synthesize('This is an actual offline native voice acceptance check.', voice.id,
                                      root / 'spoken.wav', settings=SpeechSettings(volume=65))
        assert spoken.duration_seconds > .2 and spoken.frames > 0
        peak = 0
        with wave.open(str(spoken.path), 'rb') as speech:
            assert speech.getsampwidth() == 2
            while raw := speech.readframes(65536):
                peak = max(peak, max(abs(value[0]) for value in struct.iter_unpack('<h', raw)))
        assert peak > 100, 'The installed speech engine emitted silent PCM.'
        result['checks'].append('actual installed SAPI voice to measured PCM')
        result['voices'] = [{'id': item.id, 'name': item.name, 'language': item.language} for item in sapi]
        result['speech_peak_pcm16'] = peak
        service = ProductionService(library.root / 'state' / 'studio', voices=registry)
        script = ('[Scene: First chapter]\n[Music: suspense; tempo=80]\n'
                  'Narrator [center]: A quiet archive.\nAlice [left]: I found the letter.\n'
                  '[Pause: 2]\nBob [right]: Preserve the original.\n'
                  '[Scene: Second chapter]\nNarrator: The work is saved.')
        project = service.create(script, voice_ids=tuple(item.id for item in sapi[:3]))
        job = service.submit(project, root, stems=True, format='flac')
        report = service.run(job)
        assert project.original_script == script
        assert report['status'] == 'complete' and report['result']['duration'] > 3
        assert len(report['result']['chapters']) == 2 and not report['result']['loudness']['silent']
        resumed = ProductionService(service.root, voices=registry).run(job)
        assert resumed['artifacts'] == report['artifacts']
        result['checks'] += ['actual local three-role production with score and pause',
                             'WAV/FLAC, chapter and stem publication', 'completed-job restart and checksum verification']
        result['audio'] = {'duration': report['result']['duration'], 'artifacts': len(report['artifacts']),
                           'loudness': report['result']['loudness'],
                           'distinct_voice_tokens': len({role.voice_id for role in project.roles})}
        frame = DesktopFrame(library)
        frame.Show()
        app.Yield()
        labels = [frame.menu_bar.GetMenuLabel(i).replace('&', '') for i in range(frame.menu_bar.GetMenuCount())]
        assert all(name in labels for name in ('File', 'Edit', 'Library', 'Research', 'Audio Studio', 'Tools'))
        shortcuts = []
        for index in range(frame.menu_bar.GetMenuCount()):
            for item in frame.menu_bar.GetMenu(index).GetMenuItems():
                label = item.GetItemLabel()
                if '\t' in label:
                    shortcuts.append(label.split('\t', 1)[1].casefold())
        assert len(shortcuts) == len(set(shortcuts)), 'Global menu shortcut collision'
        result['checks'].append('native menu integration and unique global shortcuts')

        def inspect_dialog(open_dialog, required_names, close_id=None):
            inspected = []
            def visit():
                dialogs = [window for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog) and window.IsShown()]
                if not dialogs:
                    wx.CallLater(100, visit)
                    return
                dialog = dialogs[-1]
                names = []
                def children(parent):
                    for child in parent.GetChildren():
                        names.append(child.GetName())
                        children(child)
                children(dialog)
                inspected.append(all(any(required in name for name in names) for required in required_names))
                if close_id is not None:
                    dialog.EndModal(close_id)
                else:
                    dialog.Close()
            wx.CallLater(500, visit)
            open_dialog(frame)
            assert inspected == [True], 'Native dialog lacked a required accessible control name'
            app.Yield()
        inspect_dialog(show_studio, ('Script', 'Production and save status', 'Default voice',
                                     'Audio Studio current progress and phase', 'Query Audio Studio progress'))
        inspect_dialog(show_assistant, ('Library scope', 'Goal', 'Plan preview'))
        inspect_dialog(show_provider_settings, ('Provider accounts', 'Connection configuration status'), wx.ID_CLOSE)
        inspect_dialog(show_engine_settings, ('Local engine configuration',), wx.ID_CLOSE)
        inspect_dialog(show_translation, ('Google API account and edition', 'Selected text', 'Preview, supported languages'), wx.ID_CLOSE)
        result['checks'].append('native studio/assistant/provider/engine dialogs with named keyboard controls')
        verify_source_translation(app, frame, library, result)
        frame.Close()
        app.Yield()
    result['status'] = 'PASS'
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
