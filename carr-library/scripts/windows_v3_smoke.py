"""Actual Windows SAPI/FFmpeg/native-control acceptance. No manual NVDA claim."""
from dataclasses import asdict
from pathlib import Path
import json
import os
import sys
import struct
import wave
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from hashlib import sha256
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

        def inspect_dialog(open_dialog, required_names, close_id=None, exercise=None):
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
                if exercise is not None:
                    exercise(dialog)
                if close_id is not None:
                    dialog.EndModal(close_id)
                else:
                    dialog.Close()
            wx.CallLater(500, visit)
            open_dialog(frame)
            assert inspected == [True], 'Native dialog lacked a required accessible control name'
            app.Yield()

        silence_errors, silence_checks = [], []
        def inspect_silence_controls(studio):
            """Actual threshold measurement, not a perceptual silence verdict."""
            timers, active, received = [], [True], []
            master = Path(report['destination']) / 'master.wav'
            original_master = master.read_bytes()
            original_job = service.get(job)

            def descendants(parent):
                for child in parent.GetChildren():
                    yield child
                    yield from descendants(child)

            def named(parent, name):
                found = [child for child in descendants(parent) if child.GetName() == name]
                assert len(found) == 1, (name, [child.GetName() for child in descendants(parent)])
                return found[0]

            def button(parent, label):
                found = [child for child in descendants(parent)
                         if isinstance(child, wx.Button) and child.GetLabel().replace('&', '') == label]
                assert len(found) == 1, label
                return found[0]

            def click(control):
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
                event.SetEventObject(control)
                assert control.GetEventHandler().ProcessEvent(event)

            def pump(predicate, description, timeout=30):
                deadline = monotonic() + timeout
                while not predicate():
                    if silence_errors:
                        raise AssertionError('\n'.join(silence_errors))
                    if monotonic() >= deadline:
                        status = named(studio, 'Production and save status').GetValue()
                        raise TimeoutError(description + ': ' + status)
                    app.Yield()
                    sleep(.01)

            def later_modal(title, action, timeout=30):
                deadline = monotonic() + timeout
                owner_id = studio.GetId()
                def inspect():
                    if not active[0] or silence_errors:
                        return
                    found = None
                    try:
                        for candidate in wx.GetTopLevelWindows():
                            try:
                                parent = candidate.GetParent()
                                if (isinstance(candidate, wx.Dialog) and candidate.IsShown()
                                        and candidate.IsModal() and candidate.GetTitle() == title
                                        and parent is not None and parent.GetId() == owner_id):
                                    found = candidate
                                    break
                            except RuntimeError:
                                continue  # A previously closed native wrapper is not this modal.
                        if found is None:
                            if monotonic() >= deadline:
                                raise TimeoutError('The current Studio modal did not appear: ' + title)
                            timers.append(wx.CallLater(25, inspect))
                            return
                        action(found)
                    except BaseException:
                        silence_errors.append(traceback.format_exc())
                        if found is not None:
                            try:
                                if found.IsModal():
                                    found.EndModal(wx.ID_CANCEL)
                            except RuntimeError:
                                pass
                timers.append(wx.CallLater(25, inspect))

            try:
                jobs_button = button(studio, 'Saved jobs')
                pump(jobs_button.IsEnabled, 'Initial Studio inventory did not finish')
                def select_job(chooser):
                    lists = [child for child in descendants(chooser) if isinstance(child, wx.ListBox)]
                    assert len(lists) == 1
                    choices = lists[0]
                    indices = [index for index in range(choices.GetCount())
                               if ('job ' + job) in choices.GetString(index)]
                    assert len(indices) == 1
                    choices.SetSelection(indices[0])
                    okay = [child for child in descendants(chooser)
                            if isinstance(child, wx.Button) and child.GetId() == wx.ID_OK]
                    assert len(okay) == 1
                    click(okay[0])
                later_modal('Production jobs', select_job)
                click(jobs_button)
                assert not silence_errors
                inspect_button = named(studio, 'Inspect long silences in completed audio')
                assert inspect_button.IsEnabled() and inspect_button.AcceptsFocusFromKeyboard()
                silence_checks.append('explicit saved-job selection enables the named inspection action')

                def criteria(window):
                    threshold = named(window, 'Silence threshold in dBFS (-192 through 0)')
                    minimum = named(window, 'Minimum silence in seconds (greater than 0; at most 31536000)')
                    accept = named(window, 'Inspect completed audio with these criteria')
                    feedback = named(window, 'Silence inspection criteria and validation status')
                    assert threshold.GetValue() == '-50' and minimum.GetValue() == '2'
                    assert all(control.AcceptsFocusFromKeyboard()
                               for control in (threshold, minimum, accept, feedback))
                    minimum.SetValue('0')
                    click(accept)
                    assert window.IsModal() and feedback.GetValue()
                    assert not button(studio, 'Pause (F8)').IsEnabled()
                    # A deliberate inclusive full-scale threshold proves exact
                    # whole-file frame accounting; it is not a quality claim.
                    threshold.SetValue('0')
                    minimum.SetValue('0.25')
                    click(accept)
                    silence_checks.append('named criteria require explicit valid finite values before work')
                later_modal('Inspect long silences — criteria', criteria)
                click(inspect_button)
                assert not silence_errors
                cancel_inspection = named(studio, 'Cancel inspection (F8)')
                assert cancel_inspection.IsEnabled() and not inspect_button.IsEnabled()
                silence_checks.append('existing F8 action names inspection cancellation while work is active')

                def result_view(window):
                    value = window.inspection_result
                    output = named(window, 'Long-silence inspection results in seconds')
                    assert output.AcceptsFocusFromKeyboard() and not output.IsEditable()
                    assert value['schema'] == 'carr-silence-inspection/1' and value['status'] == 'complete'
                    assert value['job_id'] == job
                    assert value['source']['sha256'] == sha256(original_master).hexdigest()
                    assert value['source']['size'] == len(original_master)
                    assert value['criteria']['threshold_dbfs'] == 0
                    assert value['criteria']['minimum_seconds'] == .25
                    assert value['criteria']['amplitude_limit_s32'] == 2**31
                    assert value['criteria']['comparison'] == 'absolute-sample-at-or-below'
                    assert value['interval_count'] == 1 and value['omitted_count'] == 0
                    interval = value['intervals'][0]
                    assert interval['start_frame'] == 0 and interval['end_frame'] == value['audio']['frames']
                    assert interval['end_seconds'] == value['audio']['frames'] / value['audio']['sample_rate']
                    assert '0.000000 to ' in output.GetValue() and 'not a quality verdict' in output.GetValue()
                    assert f"frames [0, {value['audio']['frames']})" in output.GetValue()
                    assert value['source']['sha256'] in output.GetValue()
                    assert 'Matching intervals: 1; shown: 1; omitted: 0.' in output.GetValue()
                    assert getattr(studio, '_audio_player', None) is None
                    received.append(value)
                    click(named(window, 'Close long-silence inspection'))
                    silence_checks.append('read-only native result preserves exact frames and original source hash')
                later_modal('Long-silence inspection — completed output', result_view)
                pump(lambda: bool(received), 'Completed inspection result was not delivered')
                assert not silence_errors
                pump(lambda: not button(studio, 'Pause (F8)').IsEnabled(), 'Inspection did not release the shared worker')
                assert named(studio, 'Pause (F8)').GetLabel() == '&Pause (F8)'
                assert master.read_bytes() == original_master
                assert service.get(job) == original_job
                assert getattr(studio, '_audio_player', None) is None
                silence_checks.append('completed audio and job remain unchanged with no playback and restored Pause label')
                import carr_library
                from carr_library.audio import silence
                from carr_library.studio import render, silence_inspection as studio_inspection
                from carr_library.ui import studio_dialog, studio_silence_inspection
                package = Path(carr_library.__file__).resolve().parent
                origins = []
                for module, relative in (
                    (silence, 'audio/silence.py'), (render, 'studio/render.py'),
                    (studio_inspection, 'studio/silence_inspection.py'),
                    (studio_dialog, 'ui/studio_dialog.py'),
                    (studio_silence_inspection, 'ui/studio_silence_inspection.py')):
                    path = Path(module.__file__).resolve()
                    assert path == package / relative
                    origins.append(dict(module=module.__name__, path=str(path),
                                        sha256=sha256(path.read_bytes()).hexdigest()))
                result['silence_inspection'] = dict(status='PASS', checks=silence_checks,
                    measurement=received[0], module_origins=origins, audible_playback='NOT_STARTED',
                    perceptual_quality='NOT_CLAIMED', manual_nvda='NOT_RUN')
            except BaseException:
                silence_errors.append(traceback.format_exc())
            finally:
                active[0] = False
                for timer in timers:
                    timer.Stop()

        inspect_dialog(show_studio, ('Script', 'Production and save status', 'Default voice'),
                       exercise=inspect_silence_controls)
        assert not silence_errors, '\n'.join(silence_errors)
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
