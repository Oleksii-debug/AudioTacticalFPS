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
    gui_errors = []
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            gui_errors.append(traceback.format_exc())
            return True
    app = ProbeApp(False)
    result = {'status': 'RUNNING', 'platform': sys.platform, 'manual_nvda': 'NOT_RUN', 'checks': []}
    with TemporaryDirectory(prefix='carr-native-v3-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native acceptance')
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
            missing_controls = []
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
                missing = [required for required in required_names
                           if not any(required in name for name in names)]
                missing_controls.extend(missing)
                inspected.append(not missing)
                if close_id is not None:
                    dialog.EndModal(close_id)
                else:
                    dialog.Close()
            wx.CallLater(500, visit)
            open_dialog(frame)
            assert inspected == [True], ('Native dialog lacked required accessible control names: '
                                         + repr(missing_controls))
            app.Yield()
        inspect_dialog(show_studio, ('Script', 'Production and save status', 'Default voice'))
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
            decisions = iter((wx.ID_OK, wx.ID_CANCEL, wx.ID_OK, wx.ID_CANCEL))
            choices = []
            deadline = monotonic() + 150

            def unexpected_dialog(*args, **kwargs):
                message = 'Unexpected modal dialog in unattended native Render acceptance.'
                gui_errors.append(message)
                raise AssertionError(message)

            class SelectedDirectory(original_dialogs['DirDialog']):
                def __init__(self, parent, message, *args, **kwargs):
                    assert parent is active[0]
                    assert message == 'Choose the parent folder for new audio outputs' or 'NEW production job' in message
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
                controls.update(editor=named('Script', wx.TextCtrl),
                    title=named('Project title', wx.TextCtrl),
                    voices=named('Default voice', wx.Choice),
                    format=named('Final audio format', wx.Choice),
                    stems=named('Export separate speaker and music stems', wx.CheckBox),
                    status=named('Production and save status', wx.TextCtrl),
                    render=button('Render / resume (F6)'), pause=button('Pause (F8)'),
                    close=button('Close'), preview=button('Listen to output'), jobs=button('Saved jobs'))
                if not controls['render'].IsEnabled() or not controls['voices'].GetCount():
                    later(ready)
                    return
                for key in ('editor', 'title', 'voices', 'format', 'stems', 'status', 'render'):
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
                assert controls['pause'].IsEnabled()
                assert not controls['editor'].IsEnabled() and not controls['render'].IsEnabled()
                assert not controls['format'].IsEnabled() and not controls['stems'].IsEnabled()
                later(completed)

            def completed():
                if controls['pause'].IsEnabled() or not controls['render'].IsEnabled():
                    later(completed)
                    return
                assert controls['status'].GetValue().startswith('Production complete.'), controls['status'].GetValue()
                assert wx.Window.FindFocus() is controls['status']
                assert controls['editor'].IsEnabled() and controls['preview'].IsEnabled()
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
                # Changing options must not silently reuse the previous job.
                assert controls['format'].SetStringSelection('wav')
                controls['stems'].SetValue(False)
                click(controls['render'])
                assert choices == [wx.ID_OK, wx.ID_CANCEL]
                assert not controls['pause'].IsEnabled()
                assert controls['format'].GetStringSelection() == 'flac' and controls['stems'].GetValue()
                assert 'Previous job and its output settings retained' in controls['status'].GetValue()
                assert service.get(row['id']) == row and len(list(destination.iterdir())) == 1
                assert controls['format'].SetStringSelection('wav')
                controls['stems'].SetValue(False)
                click(controls['render'])
                assert choices == [wx.ID_OK, wx.ID_CANCEL, wx.ID_OK]
                assert not controls['format'].IsEnabled() and not controls['stems'].IsEnabled()
                later(changed_output_completed)

            def changed_output_completed():
                if controls['pause'].IsEnabled() or not controls['render'].IsEnabled():
                    later(changed_output_completed)
                    return
                assert controls['status'].GetValue().startswith('Production complete.')
                row = completed_state['row']
                assert service.get(row['id']) == row
                new_rows = [item for item in service.jobs()
                            if item['id'] not in previous_jobs | {row['id']}]
                assert len(new_rows) == 1
                new = new_rows[0]
                assert new['status'] == 'complete' and new['format'] == 'wav' and not new['stems']
                assert (Path(new['destination']) / 'master.wav').is_file()
                assert not (Path(new['destination']) / 'stems').exists()
                assert len(list(destination.iterdir())) == 2
                # Select the original durable job using the same native dialog
                # handler; the test supplies only its synthetic selection.
                class SelectedJob(original_dialogs['SingleChoiceDialog']):
                    def __init__(self, parent, message, caption, entries, *args, **kwargs):
                        super().__init__(parent, message, caption, entries, *args, **kwargs)
                        index = next(i for i, job in enumerate(service.jobs()) if job['id'] == row['id'])
                        assert len(set(entries)) == len(entries)
                        assert 'flac; separate stems: Yes; job ' + row['id'] in entries[index]
                        self.SetSelection(index)
                    def ShowModal(self):
                        return wx.ID_OK
                wx.SingleChoiceDialog = SelectedJob
                try:
                    click(controls['jobs'])
                finally:
                    wx.SingleChoiceDialog = unexpected_dialog
                assert controls['format'].GetStringSelection() == 'flac' and controls['stems'].GetValue()
                assert 'Final format: flac; separate stems: Yes' in controls['status'].GetValue()
                assert wx.Window.FindFocus() is controls['status']
                # Editing back to the same exact script opens the chooser for a
                # new render. Declining it must not create another output/job.
                controls['editor'].SetValue(native_script + ' ')
                controls['editor'].SetValue(native_script)
                click(controls['render'])
                assert choices == [wx.ID_OK, wx.ID_CANCEL, wx.ID_OK, wx.ID_CANCEL]
                assert not controls['pause'].IsEnabled() and controls['render'].IsEnabled()
                assert {item['id'] for item in service.jobs()} == previous_jobs | {row['id'], new['id']}
                assert len(list(destination.iterdir())) == 2
                assert service.projects.load(row['project']['id']).to_dict() == completed_state['project']
                assert controls['editor'].GetValue() == native_script
                assert ReadingDesk(library, workspace, imported['edition']).page(1) == retained_before
                assert not gui_errors
                result['native_render'] = dict(status='PASS', button_event='PASS',
                    completed_job_resume='PASS', chooser_cancellation='PASS',
                    changed_output_settings='PASS', saved_job_settings='PASS',
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
            finally:
                hard_watchdog.cancel()
                for name, implementation in original_dialogs.items():
                    setattr(wx, name, implementation)

        exercise_native_render()
        inspect_dialog(show_assistant, ('Library scope', 'Goal', 'Plan preview'))
        inspect_dialog(show_provider_settings, ('Provider accounts', 'Connection configuration status'), wx.ID_CLOSE)
        inspect_dialog(show_engine_settings, ('Local engine configuration',), wx.ID_CLOSE)
        inspect_dialog(show_translation, ('Google API account and edition', 'Selected text', 'Preview, supported languages'), wx.ID_CLOSE)
        result['checks'].append('native studio/assistant/provider/engine dialogs with named keyboard controls')
        frame.Close()
        app.Yield()
    result['status'] = 'PASS'
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
