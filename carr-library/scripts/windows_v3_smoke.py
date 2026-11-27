"""Actual Windows SAPI/FFmpeg/native-control acceptance. No manual NVDA claim."""
from dataclasses import asdict
from pathlib import Path
import json
import os
import sys
import struct
import wave
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback


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
    from carr_library.ui.book_audio_dialog import create_book_audio_dialog
    from carr_library.ui.book_batch_dialog import create_book_batch_dialog
    from carr_library.studio.book_audio import BookAudioService, SplitOptions, capture_plan
    from carr_library.studio.book_batch import BookBatchService, select_books
    from carr_library.studio.project import Role
    from carr_library.audio_runtime import bundled_audio_tools, verify_audio_runtime
    from carr_library.runtime_settings import load_settings, save_settings
    from carr_library.progress import ProgressState
    from carr_library.speech.progress_speech import activate_progress_voice, SnapshotProgressSpeech
    from carr_library.ui.progress_panel import NativeProgressPanel
    from carr_library.ui import worker_cleanup
    gui_errors = []
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            gui_errors.append(traceback.format_exc())
            return True
    app = ProbeApp(False)
    app.SetExitOnFrameDelete(False)
    result = {'status': 'RUNNING', 'platform': sys.platform, 'manual_nvda': 'NOT_RUN', 'checks': []}
    temporary = TemporaryDirectory(prefix='carr-native-v3-')
    root = Path(temporary.name)
    frame = library = registry = service = selected_progress = None
    bootstrap = None
    destroyed, bootstrap_destroyed, loop_started = (threading.Event() for _ in range(3))
    bootstrap_closing = [False]
    child_progress, cleanup_futures, cleanup_executors = [], [], []
    failures, completed = [], threading.Event()
    cleanup_deadline = [None]
    original_cleanup = worker_cleanup.close_after_worker

    def track_cleanup(executor, owner):
        # Observe the real serial-worker release; never replace or run it on wx.
        future = original_cleanup(executor, owner)
        cleanup_futures.append(future)
        cleanup_executors.append(executor)
        return future

    def remember_failure(error):
        failures.append((error, error.__traceback__))

    def bootstrap_closed(event):
        if event.GetEventObject() is bootstrap:
            bootstrap_destroyed.set()
        event.Skip()

    def scenario():
        nonlocal frame, library, registry, service, selected_progress
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native acceptance')
        # CARR_V3_AUDIO is an explicit acceptance fixture, not a product override.
        # An installed sealed bundle still verifies and supplies its own tools.
        declared_audio = os.environ.get('CARR_V3_AUDIO')
        bundled_tools = bundled_audio_tools()
        if declared_audio:
            audio_report = verify_audio_runtime(Path(declared_audio))
            tools = audio_report['tools']
            if bundled_tools is not None:
                assert all(Path(tools[name]).samefile(bundled_tools[name])
                           for name in ('ffmpeg', 'ffprobe'))
                tools = bundled_tools
            result['audio_runtime'] = {key: audio_report[key] for key in
                ('package_id', 'source_lock_sha256', 'manifest_sha256')}
        else:
            if bundled_tools is None:
                raise RuntimeError('Select the verified CARR_V3_AUDIO acceptance runtime.')
            tools = bundled_tools
            result['audio_runtime'] = {'selection': 'verified sealed audio-runtime'}
        settings = load_settings(library.root / 'state')
        settings.update(ffmpeg=tools['ffmpeg'], ffprobe=tools['ffprobe'])
        save_settings(library.root / 'state', settings)
        assert load_settings(library.root / 'state') == settings
        result['checks'].append('verified canonical audio-runtime and explicit local engine selection')
        registry = VoiceRegistry.discover(include_flite=False, ffmpeg=tools['ffmpeg'])
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
        restarted_service = ProductionService(service.root, voices=registry)
        try:
            resumed = restarted_service.run(job)
        finally:
            restarted_service.close()
        assert resumed['artifacts'] == report['artifacts']
        result['checks'] += ['actual local three-role production with score and pause',
                             'WAV/FLAC, chapter and stem publication', 'completed-job restart and checksum verification']
        result['audio'] = {'duration': report['result']['duration'], 'artifacts': len(report['artifacts']),
                           'loudness': report['result']['loudness'],
                           'distinct_voice_tokens': len({role.voice_id for role in project.roles})}
        frame = DesktopFrame(library)
        app.SetTopWindow(frame)
        def frame_destroyed(event):
            if event.GetEventObject() is frame:
                destroyed.set()
            event.Skip()
        frame.Bind(wx.EVT_WINDOW_DESTROY, frame_destroyed)
        frame.Show()
        assert isinstance(frame.progress_panel, NativeProgressPanel)
        assert frame.operation_progress is frame.progress_panel
        selected_progress = activate_progress_voice(library.root, voice.id)
        frame.progress_panel.set_speech_provider(selected_progress, root=library.root)
        assert isinstance(frame.progress_panel.speech_provider, SnapshotProgressSpeech)
        assert frame.progress_panel.speech_provider.provider is selected_progress
        assert frame.progress_panel.preferences.mode == 'off'
        assert not selected_progress._closed.is_set()
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

        def exercise_native_render():
            """Drive this architecture's real Render/resume button and worker."""
            from hashlib import sha256
            from carr_library.reader.reading_desk import ReadingDesk
            original_text = 'A retained original.\r\nIts source text must stay unchanged.'
            imported = library.add_text(workspace, 'Native Render source sentinel', original_text)
            assert imported['state'] == 'complete'
            retained_before = ReadingDesk(library, workspace, imported['edition']).page(1)
            assert retained_before.raw_text == original_text
            native_script = ('[Scene: Native button]\nNarrator: A verified audio render.\n'
                             '[Pause: 0.1]\n[Scene: Output]\nNarrator: The original stays unchanged.')
            destination = root / 'chosen native render outputs'
            destination.mkdir()
            previous_jobs = {row['id'] for row in service.jobs()}
            original_dialogs = {name: getattr(wx, name) for name in
                ('DirDialog', 'FileDialog', 'MessageDialog', 'TextEntryDialog',
                 'SingleChoiceDialog', 'MessageBox')}
            active, controls, completed_state = [None], {}, {}
            decisions = iter((wx.ID_OK, wx.ID_CANCEL))
            choices = []
            deadline = monotonic() + 150

            def unexpected_dialog(*args, **kwargs):
                message = 'Unexpected modal dialog in unattended native Render acceptance.'
                gui_errors.append(message)
                raise AssertionError(message)

            class SelectedDirectory(original_dialogs['DirDialog']):
                def __init__(self, parent, message, *args, **kwargs):
                    assert parent is active[0]
                    assert message == 'Choose the parent folder for new audio outputs'
                    super().__init__(parent, message, *args, **kwargs)
                    self.SetPath(str(destination))
                    assert Path(self.GetPath()).samefile(destination)

                def ShowModal(self):
                    choice = next(decisions)
                    choices.append(choice)
                    return choice

            def children(window):
                for child in window.GetChildren():
                    yield child
                    yield from children(child)

            def named(prefix, kind):
                matches = [control for control in children(active[0])
                           if isinstance(control, kind) and control.GetName().startswith(prefix)]
                assert len(matches) == 1, (prefix, len(matches))
                return matches[0]

            def button(label):
                return next(control for control in children(active[0])
                            if isinstance(control, wx.Button)
                            and control.GetLabel().replace('&', '') == label)

            def click(control):
                assert control.IsShown() and control.IsEnabled()
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
                event.SetEventObject(control)
                assert control.GetEventHandler().ProcessEvent(event)

            def guarded(callback):
                try:
                    if gui_errors:
                        raise AssertionError('\n'.join(gui_errors))
                    if monotonic() >= deadline:
                        raise TimeoutError('The native Render workflow exceeded its deadline.')
                    callback()
                except BaseException:
                    gui_errors.append(traceback.format_exc())
                    if active[0] is not None and active[0].IsModal():
                        active[0].Close()

            def later(callback):
                wx.CallLater(40, lambda: guarded(callback))

            def ready():
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsShown()
                           and window.GetTitle() == 'Audio Studio — script, voices and production']
                if not dialogs:
                    later(ready)
                    return
                assert len(dialogs) == 1
                active[0] = dialogs[0]
                panel = active[0].progress_panel
                assert isinstance(panel, NativeProgressPanel)
                assert active[0].operation_progress is panel and panel is not frame.progress_panel
                assert panel.hub is frame.progress_panel.hub
                assert isinstance(panel.speech_provider, SnapshotProgressSpeech)
                assert panel.speech_provider is not frame.progress_panel.speech_provider
                assert panel.speech_provider.provider is not selected_progress
                assert panel.speech_provider.provider.registry is selected_progress.registry
                assert not panel.speech_provider.provider._closed.is_set()
                child_progress.append(panel.speech_provider.provider)
                assert isinstance(panel.gauge, wx.Gauge) and panel.gauge.GetParent() is panel.panel
                assert panel.gauge.GetName().startswith('Current operation progress: ')
                assert panel.text.GetName() == 'Audio Studio current progress and phase'
                assert panel.query_button.GetName() == 'Query Audio Studio progress'
                assert panel.text.AcceptsFocusFromKeyboard() and panel.query_button.AcceptsFocusFromKeyboard()
                controls.update(editor=named('Script', wx.TextCtrl),
                    title=named('Project title', wx.TextCtrl),
                    voices=named('Default voice', wx.Choice),
                    format=named('Final audio format', wx.Choice),
                    status=named('Production and save status', wx.TextCtrl),
                    render=button('Render / resume (F6)'), pause=button('Pause (F8)'),
                    close=button('Close'), preview=button('Listen to output'))
                if not controls['render'].IsEnabled() or not controls['voices'].GetCount():
                    later(ready)
                    return
                for key in ('editor', 'title', 'voices', 'format', 'status', 'render'):
                    assert controls[key].GetName() and controls[key].AcceptsFocusFromKeyboard()
                selected = controls['voices'].FindString(voice.name + '; ' + voice.language)
                assert selected != wx.NOT_FOUND, 'The verified installed SAPI voice is absent from the native choice.'
                controls['voices'].SetSelection(selected)
                controls['title'].SetValue('Native button production')
                controls['editor'].SetValue(native_script)
                assert controls['format'].SetStringSelection('flac')
                assert not list(destination.iterdir())
                click(controls['render'])
                assert choices == [wx.ID_OK]
                assert active[0].progress_panel.active_task.snapshot().state == ProgressState.RUNNING
                assert controls['pause'].IsEnabled()
                assert not controls['editor'].IsEnabled() and not controls['render'].IsEnabled()
                later(completed)

            def completed():
                if controls['pause'].IsEnabled() or not controls['render'].IsEnabled():
                    later(completed)
                    return
                assert controls['status'].GetValue().startswith('Production complete.'), controls['status'].GetValue()
                assert wx.Window.FindFocus() is controls['status']
                assert controls['editor'].IsEnabled() and controls['preview'].IsEnabled()
                snapshot = active[0].progress_panel.active_task.snapshot()
                assert snapshot.state == ProgressState.SUCCEEDED and snapshot.percent == 100
                assert frame.progress_panel.hub.snapshot(snapshot.operation_id) == snapshot
                new_jobs = [row for row in service.jobs() if row['id'] not in previous_jobs]
                assert len(new_jobs) == 1
                row = new_jobs[0]
                assert row['status'] == 'complete' and row['format'] == 'flac' and row['stems']
                project = service.projects.load(row['project']['id'])
                assert project.original_script == native_script
                assert project.fingerprint == row['fingerprint']
                assert all(role.voice_id in {item.id for item in sapi} for role in project.roles)
                output = Path(row['destination'])
                assert output.parent.samefile(destination) and len(list(destination.iterdir())) == 1
                assert len(row['result']['chapters']) == 2 and not row['result']['loudness']['silent']
                with wave.open(str(output / 'master.wav'), 'rb') as audio:
                    frames, rate, channels = audio.getnframes(), audio.getframerate(), audio.getnchannels()
                    assert frames > 0 and rate == project.sample_rate and channels == 2
                    assert audio.getsampwidth() == 2 and audio.getcomptype() == 'NONE'
                    read_frames, peak = 0, 0
                    while raw := audio.readframes(8192):
                        peak = max(peak, max(abs(value[0]) for value in struct.iter_unpack('<h', raw)))
                        read_frames += len(raw) // (2 * channels)
                    assert read_frames == frames and peak > 100
                retained = ReadingDesk(library, workspace, imported['edition']).page(1)
                assert retained.raw_text == original_text
                assert retained.location.text_sha256 == sha256(original_text.encode('utf-8')).hexdigest()
                completed_state.update(row=row, project=project.to_dict(), frames=frames,
                                       sample_rate=rate, channels=channels, peak=peak)
                # A second native Render event reopens the completed durable job,
                # verifies its artifacts and returns without starting another job.
                click(controls['render'])
                assert controls['pause'].IsEnabled() and not controls['render'].IsEnabled()
                later(resumed)

            def resumed():
                if controls['pause'].IsEnabled() or not controls['render'].IsEnabled():
                    later(resumed)
                    return
                assert controls['status'].GetValue().startswith('Production complete.'), controls['status'].GetValue()
                assert wx.Window.FindFocus() is controls['status']
                row = completed_state['row']
                assert service.get(row['id']) == row
                assert {item['id'] for item in service.jobs()} == previous_jobs | {row['id']}
                # Editing back to the same exact script opens the chooser for a
                # new render. Declining it must not create another output/job.
                controls['editor'].SetValue(native_script + ' ')
                controls['editor'].SetValue(native_script)
                click(controls['render'])
                assert choices == [wx.ID_OK, wx.ID_CANCEL]
                assert not controls['pause'].IsEnabled() and controls['render'].IsEnabled()
                assert {item['id'] for item in service.jobs()} == previous_jobs | {row['id']}
                assert len(list(destination.iterdir())) == 1
                assert service.projects.load(row['project']['id']).to_dict() == completed_state['project']
                assert controls['editor'].GetValue() == native_script
                assert ReadingDesk(library, workspace, imported['edition']).page(1) == retained_before
                assert not gui_errors
                result['native_render'] = dict(status='PASS', button_event='PASS',
                    completed_job_resume='PASS', chooser_cancellation='PASS',
                    selected_output_parent='PASS', frames=completed_state['frames'],
                    sample_rate=completed_state['sample_rate'], channels=completed_state['channels'],
                    peak_pcm16=completed_state['peak'], completed_accessible_status=True,
                    project_and_source_unchanged=True, unexpected_dialog_or_callback_errors=0)
                result['checks'] += ['actual native Render button to verified SAPI PCM and FLAC',
                    'native completed-job resume and chooser cancellation without duplicate output',
                    'native completion focus and unchanged project/source']
                click(controls['close'])

            def hard_timeout():
                import faulthandler
                print(json.dumps({'status': 'FAIL', 'native_render': 'Native modal exceeded 180 seconds.',
                                  'errors': gui_errors}), flush=True)
                faulthandler.dump_traceback(all_threads=True)
                os._exit(1)

            hard_watchdog = threading.Timer(180, hard_timeout)
            hard_watchdog.daemon = True
            try:
                for name in original_dialogs:
                    setattr(wx, name, unexpected_dialog)
                wx.DirDialog = SelectedDirectory
                hard_watchdog.start()
                later(ready)
                show_studio(frame)
                assert not gui_errors, '\n'.join(gui_errors)
                assert result.get('native_render', {}).get('status') == 'PASS'
                assert child_progress and all(item._closed.is_set() for item in child_progress)
                assert not selected_progress._closed.is_set()
                assert selected_progress.registry.get(voice.id).id == voice.id
                result['checks'].append('native private progress task and independently closed voice selection')
                result['native_progress'] = {'status': 'PASS', 'shared_hub': True,
                    'private_view': True, 'durable_completion': True,
                    'child_voice_closed': True, 'parent_voice_retained': True}
            finally:
                hard_watchdog.cancel()
                for name, implementation in original_dialogs.items():
                    setattr(wx, name, implementation)

        exercise_native_render()
        # Actual local SAPI book items reuse the same canonical Studio owner.
        book_text = '[Music: suspense]\nThis is literal book text.\n\nThe second reviewed part.'
        book_source = library.add_text(workspace, 'Native book split source', book_text)
        assert book_source['state'] == 'complete'
        books = BookAudioService(library, workspace, production=service)
        book_output = root / 'explicit native book outputs'
        book_output.mkdir()
        try:
            plan = capture_plan(library, workspace, book_source['edition'],
                options=SplitOptions('blank-lines'),
                role=Role('narrator', 'Narrator', voice.id, voice.language))
            reviewed = books.review(plan, book_output)
            ready = books.prepare(plan, book_output, approval=reviewed.fingerprint)
            assert ready.prepared and len(plan.parts) == 2 and not list(book_output.iterdir())
            assert all(not service.get(plan.job_id(index))['nodes'] for index in (1, 2))
            completed = books.run(plan.id, book_output, approval=ready.fingerprint)
            assert all(row['status'] == 'complete' for row in completed)
            assert ''.join(plan.project(index).original_script for index in (1, 2)) == book_text
            assert all(scene.music is None and not scene.cues
                       for index in (1, 2) for scene in plan.project(index).scenes)
            for index, row in enumerate(completed, 1):
                assert json.loads(Path(row['destination'], 'book-source.json').read_bytes()) == plan.provenance(index)
                assert not service.get(row['job'])['result']['loudness']['silent']
            assert books.run(plan.id, book_output, approval=ready.fingerprint) == completed
            assert library.assets.resolve(book_source['asset']).read_bytes() == book_text.encode('utf-8')
            frame._open_workspace(workspace)
            assert frame.active == (workspace, None)
            def book_dialog(host):
                dialog, close = create_book_audio_dialog(host, service=books)
                try:
                    dialog.ShowModal()
                finally:
                    close()
                    dialog.Destroy()
            inspect_dialog(book_dialog, ('Selected library source', 'Split mode',
                'Installed local voice', 'Part name template', 'Book split preview and saved job status',
                'Saved voice profile', 'Voice rate', 'Voice volume', 'Multiple books',
                'Selected bookmark boundaries', 'Choose reading bookmarks',
                'Prepare saved items', 'Run or resume saved items', 'Measured duration parts'), wx.ID_CANCEL)

            from carr_library.ui.book_duration_dialog import create_book_duration_dialog
            def measured_dialog(host):
                dialog, close = create_book_duration_dialog(host, service=books, plan=plan)
                try:
                    dialog.ShowModal()
                finally:
                    close()
                    dialog.Destroy()
            inspect_dialog(measured_dialog, ('Completed Book part', 'Maximum measured part milliseconds',
                'Measured part name template', 'Reviewed measured-parts ZIP',
                'Measured audio preview and publication status', 'Preview measured parts',
                'Publish reviewed parts', 'Cancel current operation'), wx.ID_CANCEL)

            def exercise_book_batch():
                """Actual shared desktop worker and native batch review/Run controls."""
                from hashlib import sha256
                second_text = 'A separate second book keeps its own reviewed voice and output format.'
                second_source = library.add_text(workspace, 'Native second batch book', second_text)
                second_voice = sapi[1] if len(sapi) > 1 else voice
                second = capture_plan(library, workspace, second_source['edition'], options=SplitOptions('document'),
                    role=Role('narrator', 'Narrator', second_voice.id, second_voice.language, volume=55), format='flac')
                second_output = root / 'explicit native second book output'
                second_output.mkdir()
                group_service = BookBatchService(books)
                group = select_books((plan, second), title='Native ordered two-book batch')
                parents = (book_output, second_output)
                previewed = group_service.review(group, parents, plans=(plan, second))
                prepared = group_service.prepare(group, parents, plans=(plan, second), approval=previewed.fingerprint)
                assert prepared.prepared and not list(second_output.iterdir())
                assert service.get(second.job_id(1))['status'] == 'pending'
                dialog, close = create_book_batch_dialog(frame, service=books)
                original_choice, original_message = wx.SingleChoiceDialog, wx.MessageBox
                approvals, before, passed = [], {}, []
                deadline = monotonic() + 90

                class ExactBatch(original_choice):
                    def __init__(self, parent, message, caption, choices):
                        assert parent is dialog and caption == 'Saved book batches'
                        super().__init__(parent, message, caption, choices)
                        selected = [index for index, label in enumerate(choices) if group.id in label]
                        assert len(selected) == 1
                        self.SetSelection(selected[0])
                    def ShowModal(self):
                        return wx.ID_OK

                def approve(message, caption, style, parent):
                    assert parent is dialog and caption == 'Run reviewed book batch'
                    assert '2 reviewed books' in message and style & wx.NO_DEFAULT
                    approvals.append(message)
                    return wx.YES

                def children(parent):
                    for child in parent.GetChildren():
                        yield child
                        yield from children(child)

                def named(name, kind):
                    found = [child for child in children(dialog)
                             if isinstance(child, kind) and child.GetName() == name]
                    assert len(found) == 1, (name, len(found))
                    return found[0]

                def click(name):
                    control = named(name, wx.Button)
                    assert control.IsShown() and control.IsEnabled() and control.AcceptsFocusFromKeyboard()
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
                    event.SetEventObject(control)
                    assert control.GetEventHandler().ProcessEvent(event)

                def later(callback):
                    def guarded():
                        try:
                            assert not gui_errors, '\n'.join(gui_errors)
                            if monotonic() >= deadline:
                                raise TimeoutError('Native multi-book review/Run did not finish.')
                            callback()
                        except BaseException:
                            gui_errors.append(traceback.format_exc())
                            dialog.Close()
                    wx.CallLater(40, guarded)

                def opened():
                    if frame.working:
                        later(opened)
                        return
                    assert not approvals and not list(second_output.iterdir())
                    assert named('Ordered books; review each book before Preview', wx.Choice).AcceptsFocusFromKeyboard()
                    click('Saved batches')
                    later(loaded)

                def loaded():
                    if frame.working:
                        later(loaded)
                        return
                    choice = named('Ordered books; review each book before Preview', wx.Choice)
                    assert choice.GetCount() == 2
                    assert not named('Run or resume batch', wx.Button).IsEnabled()
                    assert not list(second_output.iterdir())
                    click('Preview entire batch')
                    later(reviewed)

                def reviewed():
                    if frame.working:
                        later(reviewed)
                        return
                    status = named('Multiple book review and existing Studio checkpoints', wx.TextCtrl)
                    assert 'Batch review SHA-256:' in status.GetValue()
                    assert voice.id in status.GetValue() and second_voice.id in status.GetValue()
                    displayed_review = status.GetValue()
                    assert '; volume 55' in displayed_review, displayed_review
                    # The producer reviews a canonical path. Windows TEMP can
                    # use a short spelling for the same selected directory.
                    second_review = prepared.books[1]
                    assert second_review.output_parent == str(second_output.resolve()), (
                        second_review.output_parent, str(second_output), str(second_output.resolve()))
                    assert 'Output parent: ' + second_review.output_parent in displayed_review, displayed_review
                    assert all('Output folder: ' + path in displayed_review
                               for path in second_review.destinations), displayed_review
                    assert wx.Window.FindFocus() is status and not list(second_output.iterdir())
                    click('Run or resume batch')
                    assert approvals and frame.working
                    later(rendered)

                def rendered():
                    if frame.working:
                        later(rendered)
                        return
                    status = named('Multiple book review and existing Studio checkpoints', wx.TextCtrl)
                    assert status.GetValue().startswith('Batch invocation: complete'), status.GetValue()
                    assert wx.Window.FindFocus() is status
                    states = [service.get(item.job_id(index)) for item in (plan, second)
                              for index in range(1, len(item.parts) + 1)]
                    assert all(state['status'] == 'complete' for state in states)
                    assert states[-1]['format'] == 'flac' and not states[-1]['result']['loudness']['silent']
                    actual = {artifact['path']: (sha256(Path(artifact['path']).read_bytes()).hexdigest(),
                                                Path(artifact['path']).stat().st_mtime_ns)
                              for state in states for artifact in state['artifacts']}
                    if not before:
                        before.update(actual)
                        click('Run or resume batch')
                        later(rendered)
                        return
                    assert before == actual and len(approvals) == 2
                    assert library.assets.resolve(second_source['asset']).read_bytes() == second_text.encode()
                    assert library.assets.resolve(book_source['asset']).read_bytes() == book_text.encode()
                    passed.append(True)
                    click('Close')

                try:
                    wx.SingleChoiceDialog, wx.MessageBox = ExactBatch, approve
                    later(opened)
                    dialog.ShowModal()
                    assert passed == [True] and not gui_errors, '\n'.join(gui_errors)
                    assert not books._closed.is_set() and not service._closed.is_set()
                    result['book_batch'] = {'status': 'PASS', 'books': 2, 'parts': 3,
                        'shared_worker': True, 'explicit_run_confirmations': len(approvals),
                        'verified_resume_preserves_hashes_and_mtimes': True,
                        'per_book_formats': ['wav', 'flac'], 'source_originals_preserved': True,
                        'manual_keyboard': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}
                    result['checks'].append('actual native two-book review, explicit Run, separate settings and verified resume')
                finally:
                    wx.SingleChoiceDialog, wx.MessageBox = original_choice, original_message
                    close()
                    dialog.Destroy()

            exercise_book_batch()
            result['checks'].append('actual installed SAPI source-bound book split, prepared names, verified resume and native controls')
            result['book_audio'] = {'parts': len(completed), 'voice_id': voice.id,
                                    'original_preserved': True, 'quotation_approval': False}
        finally:
            books.close()
        inspect_dialog(show_assistant, ('Library scope', 'Goal', 'Plan preview'))
        inspect_dialog(show_provider_settings, ('Provider accounts', 'Connection configuration status'), wx.ID_CLOSE)
        inspect_dialog(show_engine_settings, ('Local engine configuration',), wx.ID_CLOSE)
        inspect_dialog(show_translation, ('Google API account and edition', 'Selected text', 'Preview, supported languages'), wx.ID_CLOSE)
        result['checks'].append('native studio/assistant/provider/engine dialogs with named keyboard controls')

    def poll_cleanup():
        try:
            executors = cleanup_executors + ([frame.executor] if frame is not None else [])
            workers = [thread for executor in executors for thread in executor._threads]
            voice_workers = [thread for thread in threading.enumerate()
                             if thread.name == 'progress-voice-cleanup']
            waiting = ((frame is not None and not destroyed.is_set())
                       or any(not future.done() for future in cleanup_futures)
                       or any(thread.is_alive() for thread in workers + voice_workers))
            if waiting:
                if monotonic() >= cleanup_deadline[0]:
                    raise TimeoutError('Native window destruction or owned worker cleanup did not finish.')
                wx.CallLater(25, poll_cleanup)
                return
            # Keep a real top-level owner until all actual GUI/worker cleanup
            # has finished, then observe its native destruction before exiting.
            if bootstrap is not None and not bootstrap_destroyed.is_set():
                if monotonic() >= cleanup_deadline[0]:
                    raise TimeoutError('Native event-loop bootstrap destruction did not finish.')
                if not bootstrap_closing[0]:
                    bootstrap_closing[0] = True
                    bootstrap.Destroy()
                wx.CallLater(25, poll_cleanup)
                return
            assert loop_started.is_set() and bootstrap_destroyed.is_set()
            for future in cleanup_futures:
                future.result()
            if frame is not None:
                assert frame.progress_panel.closed
            if selected_progress is not None:
                assert selected_progress._closed.is_set()
                assert not selected_progress.registry.voices()
            if not failures:
                assert not gui_errors, '\n'.join(gui_errors)
                assert cleanup_futures, 'The real Studio worker cleanup was not observed.'
                result['checks'].append('actual wx MainLoop, Destroy event and completed owner cleanup')
                result['native_cleanup'] = {'status': 'PASS', 'destroy_event': destroyed.is_set(),
                    'private_worker_releases': len(cleanup_futures), 'voice_owner_released': True,
                    'scenario_started_in_main_loop': loop_started.is_set(),
                    'bootstrap_destroy_event': bootstrap_destroyed.is_set()}
        except BaseException as error:
            remember_failure(error)
        completed.set()
        app.ExitMainLoop()

    def run_scenario():
        try:
            assert app.IsMainLoopRunning(), 'The native scenario must run inside the actual wx MainLoop.'
            loop_started.set()
            scenario()
        except BaseException as error:
            remember_failure(error)
        finally:
            cleanup_deadline[0] = monotonic() + 60
            try:
                if frame is not None:
                    frame.Close()
                elif selected_progress is not None:
                    selected_progress.close()
            except BaseException as error:
                remember_failure(error)
            wx.CallLater(25, poll_cleanup)

    def loop_timeout():
        import faulthandler
        print(json.dumps({'status': 'FAIL', 'native_loop': 'Native acceptance exceeded 420 seconds.',
                          'errors': gui_errors}), flush=True)
        faulthandler.dump_traceback(all_threads=True)
        os._exit(1)

    watchdog = threading.Timer(420, loop_timeout)
    watchdog.daemon = True
    worker_cleanup.close_after_worker = track_cleanup
    try:
        watchdog.start()
        # wxPython 4.2.4 wxPyApp::MainLoop returns without dispatching anything
        # when there are no top-level windows, even with exit-on-delete disabled.
        # The real application frame is created by the queued scenario, so keep
        # a hidden native bootstrap owner alive before entering that first loop.
        bootstrap = wx.Frame(None, title='Native acceptance event-loop owner', size=(1, 1))
        bootstrap.Bind(wx.EVT_WINDOW_DESTROY, bootstrap_closed)
        app.SetTopWindow(bootstrap)
        assert bootstrap in wx.GetTopLevelWindows()
        wx.CallAfter(run_scenario)
        app.MainLoop()
        if not completed.is_set():
            raise AssertionError('The actual wx event loop exited before scenario cleanup. '
                                 f'Scenario started: {loop_started.is_set()}; '
                                 f'bootstrap destroyed: {bootstrap_destroyed.is_set()}.')
    except BaseException as error:
        remember_failure(error)
    finally:
        watchdog.cancel()
        worker_cleanup.close_after_worker = original_cleanup
        # These two explicitly borrowed owners have no pending GUI work now.
        for owner in (service, registry):
            if owner is not None:
                try:
                    owner.close()
                except BaseException as error:
                    remember_failure(error)
        try:
            temporary.cleanup()
        except BaseException as error:
            remember_failure(error)
    if failures:
        error, failure_traceback = failures[0]
        raise error.with_traceback(failure_traceback)
    result['status'] = 'PASS'
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
