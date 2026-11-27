"""Actual Windows SAPI/FFmpeg/native-control acceptance. No manual NVDA claim."""
from dataclasses import asdict
from pathlib import Path
import json
import os
import sys
import struct
import wave
from tempfile import TemporaryDirectory
from time import monotonic


def check_saved_reading_drafts(frame, library, voice_id):
    """Drive the actual reader and native picker; retain every input snapshot."""
    import wx
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech import ReadingDocument
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    from carr_library.speech.source import read_source_range
    from carr_library.ui.speech_dialog import show_speech_reader

    workspace = library.create_workspace('Synthetic native draft recovery')
    original = 'Immutable Ukrainian source. Український оригінал.\fSecond source position.'
    imported = library.add_text(workspace, 'Native bound draft fixture', original)
    desk = ReadingDesk(library, workspace, imported['edition'])
    source = read_source_range(desk, 1, len(desk.pages))
    retained_asset = library.assets.resolve(imported['asset'])
    protected = {retained_asset: retained_asset.read_bytes()}
    fixtures = (ReadingDocument('Unidentified older original.', title='Native legacy draft fixture'),
                source.reading_document())
    store = ReaderDraftStore(library.root, slot='native-draft-probe')
    prepared = []
    for index, document in enumerate(fixtures):
        document.set_text(document.text + '\nSaved synthetic narration edit.')
        saved = ReaderDraftStore(library.root, slot=f'native-seeded-draft-{index}')
        saved.save(ReaderResume(document, voice_id, rate=2, volume=65, cursor=5))
        protected[saved.path] = saved.path.read_bytes()
        prepared.append((saved.key, document))

    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)

    def click(control):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        assert control.GetEventHandler().ProcessEvent(event), 'Native button event was not handled.'

    def preserved():
        assert all(path.read_bytes() == content for path, content in protected.items()), \
            'Recovery changed a retained original or a previously saved draft.'

    def recover(key, expected, *, addition='', cancel_choice=False):
        state = {'phase': 'opening', 'timer': None, 'error': None, 'copy': None}
        deadline = monotonic() + 45
        before = set(store.root.glob('*.reader.json'))

        def visit():
            if state['phase'] in ('complete', 'failed'):
                return
            # Schedule before dispatch: a native modal event runs its own loop.
            state['timer'] = wx.CallLater(100, visit)
            try:
                assert monotonic() < deadline, f"Native saved-draft probe timed out: {state['phase']}"
                shown = [window for window in wx.GetTopLevelWindows()
                         if isinstance(window, wx.Dialog) and window.IsShown()]
                readers = [window for window in shown if window.GetTitle() == 'Voice reader and reading copy']
                if not readers:
                    return
                dialog = readers[-1]
                controls = tuple(children(dialog))

                def named(name):
                    matched = [control for control in controls if control.GetName() == name]
                    assert len(matched) == 1, 'Missing or ambiguous accessible control: ' + name
                    return matched[0]

                def button(label):
                    return next(control for control in controls if isinstance(control, wx.Button)
                                and control.GetLabel().replace('&', '') == label)

                editor = named('Reading copy (editable; Ctrl+S saves)')
                status = named('Speech and save status').GetValue()
                assert not status.startswith(('Operation did not complete.', 'Saved drafts were not opened.')), status
                if state['phase'] == 'opening':
                    if not button('Reload voices').IsEnabled():
                        return
                    assert isinstance(editor, wx.TextCtrl) and editor.GetValue() == ''
                    control = named('Recover saved reading draft')
                    assert isinstance(control, wx.Button) and control.IsEnabled()
                    control.SetFocus()
                    state['phase'] = 'choice'
                    click(control)
                elif state['phase'] == 'choice':
                    choices = [window for window in shown if isinstance(window, wx.SingleChoiceDialog)
                               and window.GetTitle() == 'Recover saved reading draft']
                    if not choices:
                        return
                    choice = choices[-1]
                    native_list = next(control for control in children(choice) if isinstance(control, wx.ListBox))
                    matches = [index for index in range(native_list.GetCount())
                               if f'copy {key[:12]}' in native_list.GetString(index)]
                    assert len(matches) == 1, 'The exact saved snapshot is missing from the native picker.'
                    label = native_list.GetString(matches[0])
                    assert expected.title in label
                    assert ('source identity recorded' if expected.source_bound else 'unbound reading copy') in label
                    choice.SetSelection(matches[0])
                    assert choice.GetSelection() == matches[0]
                    state['phase'] = 'cancelled' if cancel_choice else 'restoring'
                    identifier = wx.ID_CANCEL if cancel_choice else wx.ID_OK
                    click(next(control for control in children(choice)
                               if isinstance(control, wx.Button) and control.GetId() == identifier))
                elif state['phase'] == 'cancelled':
                    if not status.startswith('Recovery cancelled.'):
                        return
                    assert editor.GetValue() == '', 'Cancelling recovery replaced the current reading copy.'
                    preserved()
                    state['phase'] = 'closing'
                    click(button('Close'))
                elif state['phase'] == 'restoring':
                    if not status.startswith('Saved draft recovered as a separate editable copy.'):
                        return
                    assert editor.GetValue() == expected.text
                    assert wx.Window.FindFocus() == editor, 'Recovered text did not receive native focus.'
                    assert named('Synthesis rate, minus ten to ten').GetValue() == 2
                    assert named('Speech volume, zero to one hundred').GetValue() == 65
                    lineage = named('Original and reading-copy status').GetValue()
                    assert ('Library source identity retained' if expected.source_bound
                            else 'Unbound reading copy') in lineage
                    editor.SetInsertionPointEnd()
                    editor.WriteText(addition)
                    click(button('Save reading copy'))
                    assert named('Speech and save status').GetValue().startswith(
                        'Reading draft, voice settings and current position saved for recovery.')
                    candidates = []
                    for path in set(store.root.glob('*.reader.json')) - before:
                        record = json.loads(path.read_text(encoding='utf-8'))
                        document = ReadingDocument.from_dict(record['document'])
                        if document.title == expected.title and document.text == expected.text + addition:
                            candidates.append((path, record, document))
                    assert len(candidates) == 1, 'Native Save did not persist exactly one separate recovered copy.'
                    path, record, document = candidates[0]
                    assert document.source_text == expected.source_text
                    assert document.source_origin == expected.source_origin
                    assert (record['voice_id'], record['rate'], record['volume']) == (voice_id, 2, 65)
                    assert not document.source_bound or document.source_origin['quotation_authority'] is False
                    preserved()
                    protected[path] = path.read_bytes()
                    state['copy'] = (path.name.removesuffix('.reader.json'), document)
                    state['phase'] = 'closing'
                    click(button('Close'))
            except Exception as exc:
                state['error'], state['phase'] = exc, 'failed'
                for window in reversed(wx.GetTopLevelWindows()):
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)

        state['timer'] = wx.CallLater(100, visit)
        try:
            show_speech_reader(frame, recovery_slot='native-empty-draft-' + key)
        finally:
            state['timer'].Stop()
        if state['error'] is not None:
            raise state['error']
        assert state['phase'] == 'closing', 'Reader closed before native draft recovery completed.'
        state['phase'] = 'complete'
        return state['copy']

    recover(*prepared[0], cancel_choice=True)
    for key, document in prepared:
        copied_key, copied = recover(key, document, addition='\nEdited through the actual native reader.')
        recover(copied_key, copied)
    preserved()
    return ['native Saved drafts choice and cancellation preserve input snapshots',
            'legacy and source-bound drafts recover, edit, save and reopen through actual native controls',
            'native recovered copies retain exact source identity without quotation authority']



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
        from carr_library.academic_integration import render_citations, export_citation_docx, read_citation_docx
        bibliography = render_citations([{'id': 'synthetic-reference', 'type': 'book',
            'title': 'The Synthetic Archive', 'author': [{'family': 'Example', 'given': 'Jane'}],
            'issued': {'date-parts': [[2026]]}, 'publisher': 'Fixture Press'}], style='apa')
        exported = export_citation_docx(bibliography, root / 'citations.docx')
        assert exported.is_file() and 'Synthetic Archive' in bibliography.bibliography[0].text
        assert read_citation_docx(exported)
        result['checks'].append('actual installed CSL processor and native DOCX provenance')
        from carr_library.conversion import ConversionService
        source = root / 'synthetic-reading.txt'
        source.write_text('Synthetic conversion source. Український текст.', encoding='utf-8')
        converted = ConversionService(root / 'converted', input_roots=(root,)).extract(source, 'reading.txt')
        assert 'Український' in converted.path.read_text(encoding='utf-8')
        result['checks'].append('actual isolated document conversion and atomic Windows publication')

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
        inspect_dialog(show_studio, ('Script', 'Production and save status', 'Default voice'))
        inspect_dialog(show_assistant, ('Library scope', 'Goal', 'Plan preview'))
        inspect_dialog(show_provider_settings, ('Provider accounts', 'Connection configuration status'), wx.ID_CLOSE)
        inspect_dialog(show_engine_settings, ('Local engine configuration',), wx.ID_CLOSE)
        inspect_dialog(show_translation, ('Google Basic v2 account', 'Selected text', 'Preview, supported languages'), wx.ID_CLOSE)
        result['checks'].append('native studio/assistant/provider/engine dialogs with named keyboard controls')
        result['checks'].extend(check_saved_reading_drafts(frame, library, voice.id))
        import runpy
        transcription_probe = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tests' / 'ui' / 'native_transcription_smoke.py'))
        result['transcription'] = transcription_probe['run_native_transcription'](frame, root / 'native-asr')
        result['checks'].append('native transcription controls, synthetic worker, pause/cancel/resume and reader handoff')
        frame.Close()
        app.Yield()
    result['status'] = 'PASS'
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
