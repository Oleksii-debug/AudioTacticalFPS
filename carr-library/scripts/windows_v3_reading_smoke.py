"""Exercise installed V3 reading/save/shortcut controls on native Windows.

Synthetic documents only. This records real wx event handling, native controls,
durable state and backup round trips. NVDA speech and listening are NOT_RUN.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import threading
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.keyboard_preferences import KeymapStore
    from carr_library.library_backup import restore_backup
    from carr_library.reader.narration import NarrationStore, new_project
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.narration_editor import open_narration_editor
    from carr_library.speech import Voice
    from carr_library.studio import ProjectStore

    checks, failures = [], []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)

    def pump(predicate, timeout=15):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() > deadline:
                raise TimeoutError('Native reading smoke did not complete.')
            gui.Yield()
            sleep(0.01)

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native button event: ' + button.GetLabel(), button.GetEventHandler().ProcessEvent(event))

    from carr_library.ui import productivity, narration_editor
    check('productivity and editor load from the installed wheel', all(
        'site-packages' in [part.casefold() for part in Path(module.__file__).parts]
        for module in (productivity, narration_editor)))
    check('real Windows wx runtime', wx.Platform == '__WXMSW__')

    with TemporaryDirectory(prefix='research-v3-reading-native-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic V3 reading')
        topic = library.create_topic(workspace, 'Reading notes')
        source = 'Original railway text. Українська мова. Slovenská veta.'
        imported = library.add_text(workspace, 'Synthetic V3 source', source)
        hit, = library.search(workspace, 'railway')
        identity = library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original note')
        original_bytes = library.assets.resolve(imported['asset']).read_bytes()
        frame = DesktopFrame(library)
        gui.SetTopWindow(frame)
        frame.Show()
        frame._open_workspace(workspace, topic)
        pump(lambda: not frame.working and frame.evidence is not None and frame.evidence.displayed is not None)
        try:
            check('File is the first Alt menu', frame.menu_bar.GetMenuLabel(0).replace('&', '') == 'File')
            check('Options and Reading remain accessible', frame.menu_bar.FindMenu('Options') >= 0
                  and frame.menu_bar.FindMenu('Reading') >= 0)
            keyboard = frame.keyboard_commands
            save_id = 'file.save'
            save_binding = keyboard.bindings[save_id]
            check('main Save exposes Ctrl+S', save_binding.item.GetItemLabel().endswith('\tCtrl+S'))
            frame.evidence.note.SetValue('Saved through the native File command.')
            event = wx.CommandEvent(wx.EVT_MENU.typeId, save_binding.item.GetId())
            check('native File Save dispatches', frame.GetEventHandler().ProcessEvent(event))
            pump(lambda: not frame.working)
            check('File Save commits the selected evidence note',
                  library.evidence(workspace, topic)[0].note == 'Saved through the native File command.')
            keyboard.apply_overrides({save_id: 'Ctrl+Alt+S'})
            check('shortcut change updates native menu', save_binding.item.GetItemLabel().endswith('\tCtrl+Alt+S'))
            check('shortcut persists after reopening preferences',
                  KeymapStore(library.root / 'keyboard-shortcuts.json').load()[save_id] == 'Ctrl+Alt+S')
            old_labels = tuple(binding.item.GetItemLabel() for binding in keyboard.bindings.values())
            try:
                keyboard.apply_overrides({save_id: 'Ctrl+I'})
            except ValueError:
                pass
            else:
                raise AssertionError('Conflicting shortcut was accepted.')
            check('conflicting key leaves native menu unchanged',
                  tuple(binding.item.GetItemLabel() for binding in keyboard.bindings.values()) == old_labels)

            desk = ReadingDesk(library, workspace, imported['edition'])
            page = desk.page(1)
            state = ReadingState(library.root)
            state.remember(page.location)
            state.add_bookmark(page.location, 'Synthetic start')
            store = NarrationStore(library.root)
            project = new_project(page.raw_text, title='Synthetic reading copy', source_location=page.location)
            changed = source + '\nExplicit reading-copy edit.'
            recovered = changed + '\nAutosaved recovery sentence.'
            modal_errors, later_calls, discovery_threads = [], [], []
            main_thread = threading.get_ident()
            fixture_voice = Voice('fixture:reading-handoff', 'fixture', 'Synthetic handoff voice', 'uk', ())

            def fixture_discovery(*, cancelled):
                discovery_threads.append(threading.get_ident())
                if len(discovery_threads) == 1:
                    deadline = monotonic() + 5
                    while not cancelled() and monotonic() < deadline:
                        sleep(.005)
                    if not cancelled():
                        raise TimeoutError('Native cancellation did not reach the discovery worker.')
                    raise InterruptedError('Synthetic local voice discovery cancelled.')
                if cancelled():
                    raise InterruptedError('Synthetic local voice discovery cancelled.')
                return (fixture_voice,)

            def safely(dialog, action):
                try:
                    action()
                except Exception:
                    modal_errors.append(traceback.format_exc())
                    for window in reversed(wx.GetTopLevelWindows()):
                        if isinstance(window, wx.Dialog) and window.IsModal():
                            window.EndModal(wx.ID_CANCEL)

            def ready(dialog, controller, controls):
                def begin():
                    check('original text is a native read-only multiline control',
                          isinstance(controls['original'], wx.TextCtrl)
                          and bool(controls['original'].GetWindowStyleFlag() & wx.TE_READONLY)
                          and controls['original'].GetValue() == source)
                    check('editor permits Tab navigation',
                          not bool(controls['editor'].GetWindowStyleFlag() & wx.TE_PROCESS_TAB))
                    check('provenance distinguishes a reading copy from quotation evidence',
                          'reading' in controls['origin'].GetValue().lower())
                    controls['editor'].SetValue(changed)
                    click(controls['save'])
                    saved, _ = store.load(project.id)
                    check('native Save persists edits but retains exact original',
                          saved.current_text == changed and saved.original_text == source)
                    controls['editor'].SetValue(recovered)

                    def finish():
                        draft, _ = store.load(project.id, draft=True)
                        check('native timer publishes a recoverable draft', draft.current_text == recovered)
                        check('autosave does not silently replace saved version',
                              store.load(project.id)[0].current_text == changed)
                        click(controls['save'])
                        click(controls['studio'])
                        check('voice discovery leaves the native event loop available',
                              controls['cancel_studio'].IsEnabled() and not controls['editor'].IsEnabled())
                        click(controls['cancel_studio'])
                        stages = {'phase': 'cancel'}

                        def handoff():
                            if stages['phase'] == 'cancel':
                                if not controls['studio'].IsEnabled():
                                    later_calls.append(wx.CallLater(30, lambda: safely(dialog, handoff)))
                                    return
                                check('native setup cancellation creates no audio project',
                                      not (library.root / 'audio-projects').exists()
                                      and 'cancel' in controls['status'].GetValue().lower())
                                stages['phase'] = 'voice'
                                click(controls['studio'])
                            elif stages['phase'] == 'voice':
                                choices = [window for window in wx.GetTopLevelWindows()
                                           if window.GetTitle() == 'Choose local narration voice']
                                if choices:
                                    chooser, = choices
                                    check('handoff requires a native explicit voice choice',
                                          isinstance(chooser, wx.SingleChoiceDialog) and chooser.IsModal())
                                    chooser.SetSelection(0)
                                    stages['phase'] = 'studio'
                                    chooser.EndModal(wx.ID_OK)
                            elif stages['phase'] == 'studio':
                                children = [window for window in wx.GetTopLevelWindows()
                                            if window.GetTitle() == 'Audio Studio — local narration and dialogue']
                                if children:
                                    studio_dialog, = children
                                    audio_folders = tuple((library.root / 'audio-projects').iterdir())
                                    check('handoff creates one separate durable audio project', len(audio_folders) == 1)
                                    audio_store = ProjectStore(audio_folders[0])
                                    audio = audio_store.load()
                                    check('nested native studio keeps exact reading text and chosen voice',
                                          studio_dialog.IsModal() and audio.authored_script == recovered
                                          and audio.scenes[0].lines[0].text == recovered
                                          and audio.roles[0].voice_id == fixture_voice.id)
                                    check('handoff records source provenance without rendering',
                                          audio.source_reference['reading_project_id'] == project.id
                                          and audio.source_reference['source_workspace'] == workspace
                                          and audio.source_reference['quotation_approval'].startswith('none;')
                                          and not audio_store.jobs())
                                    close = next(child for child in studio_dialog.GetChildren()
                                                 if isinstance(child, wx.Button) and child.GetId() == wx.ID_CANCEL)
                                    stages['phase'] = 'returned'
                                    click(close)
                            elif controls['studio'].IsEnabled():
                                check('closing nested studio restores the native reading editor',
                                      controls['editor'].IsEnabled() and controls['editor'].GetValue() == recovered)
                                check('local discovery ran outside the native UI thread',
                                      len(discovery_threads) == 2 and all(value != main_thread for value in discovery_threads))
                                click(controls['close'])
                                return
                            later_calls.append(wx.CallLater(30, lambda: safely(dialog, handoff)))

                        later_calls.append(wx.CallLater(30, lambda: safely(dialog, handoff)))

                    later_calls.append(wx.CallLater(1800, lambda: safely(dialog, finish)))
                safely(dialog, begin)

            def expired():
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        modal_errors.append('Native reading-copy dialog timed out.')
                        window.EndModal(wx.ID_CANCEL)

            watchdog = wx.CallLater(25000, expired)
            try:
                open_narration_editor(frame, store, project, on_ready=ready,
                                      voice_discovery=fixture_discovery)
            finally:
                watchdog.Stop()
                for call in later_calls:
                    call.Stop()
            if modal_errors:
                raise RuntimeError('\n'.join(modal_errors))
            saved, _ = store.load(project.id)
            check('reading copy survives closing and reopening the store',
                  NarrationStore(library.root).load(project.id)[0] == saved and saved.current_text == recovered)
            check('source bytes and authenticated citation remain unchanged',
                  library.assets.resolve(imported['asset']).read_bytes() == original_bytes
                  and library.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote)

            audio_folder, = tuple((library.root / 'audio-projects').iterdir())
            audio_project = ProjectStore(audio_folder).load()
            backup = root / 'backup'
            checksum = library.backup(backup)
            manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
            check('reading handoff requires the full version-four backup',
                  manifest['version'] == 4
                  and [row['id'] for row in manifest['recovery']['projects']] == [audio_folder.name])
            restored = root / 'restored'
            restore_backup(backup, restored, expected_manifest_sha256=checksum)
            restored_audio = restored / 'audio-projects' / audio_folder.name
            check('complete backup publishes the saved Audio Studio database',
                  (restored_audio / 'studio.sqlite').is_file())
            restored_audio_store = ProjectStore(restored_audio)
            check('complete backup restores exact authored script and source provenance without rendering',
                  restored_audio_store.load() == audio_project
                  and audio_project.authored_script == recovered
                  and audio_project.source_reference['reading_project_id'] == project.id
                  and not restored_audio_store.jobs())
            check('complete backup restores saved reading-copy revisions',
                  NarrationStore(restored).load(project.id)[0] == saved)
            check('complete backup restores page resume and bookmark',
                  ReadingState(restored).resume(workspace, imported['edition']) == page.location
                  and len(ReadingState(restored).bookmarks(workspace, imported['edition'])) == 1)
            check('complete backup restores keyboard preferences',
                  KeymapStore(restored / 'keyboard-shortcuts.json').load() == KeymapStore(library.root / 'keyboard-shortcuts.json').load())
        finally:
            if not frame.IsBeingDeleted():
                frame.Close()
                gui.Yield()
            if native_handle is not None:
                native_handle.close()
    if failures:
        raise RuntimeError('\n'.join(failures))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'count': len(checks), 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
