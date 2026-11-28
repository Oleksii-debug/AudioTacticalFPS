"""Actual Windows source-bound assistant controls and real SAPI preparation.

This gate uses the existing native dialogs, actual AssistantService checkpoints,
real SAPI PCM preparation and unified backup. It does not start hardware audio
or claim listening quality, physical speaker output, word timing or NVDA.
"""
import argparse
from dataclasses import asdict
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from time import monotonic
import traceback
from uuid import NAMESPACE_URL, uuid5


def run(work):
    if os.name != 'nt' or not sys.flags.isolated:
        raise RuntimeError('Actual isolated Windows execution is required.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService
    from carr_library.assistant.interactive_tools import validate_result
    from carr_library.audio.engines import SAPI5Engine, VoiceRegistry
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.audio.jobs import sha256_file
    from carr_library.library_backup import create_backup, verify_backup, restore_backup
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.ui.assistant_dialog import show_assistant
    from carr_library.ui.assistant_interactive import NativeReaderBridge
    from carr_library.ui.live_reading import LiveReadingDialog, selected_source_offsets

    work = Path(work).absolute()
    work.mkdir(mode=0o700)
    gui = wx.App(False)
    library = LibraryApp(work / 'library')
    workspace = library.create_workspace('Public interactive native fixture')
    source = 'Prefix 😀.\r\nSelected source sentence for actual speech preparation. Another sentence.'
    book = library.add_text(workspace, 'Immutable public source', source)
    page = ReadingDesk(library, workspace, book['edition']).page(1)
    source = page.raw_text
    first, last = source.index('Selected'), source.index(' Another')
    digest = sha256(source.encode('utf-8')).hexdigest()
    frame = wx.Frame(None, title='Native assistant reader acceptance')
    frame.library, frame.active, frame._closing = library, (workspace, None), False
    frame.Show()
    gui.SetTopWindow(frame)
    checks, failures, records, timers = [], [], [], []
    metrics = {}
    deadline = monotonic() + 180
    bridge = None
    state = {'window': None, 'phase': 'voices', 'active': True}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append({'name': name, 'passed': True})

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('Required native command button is unavailable.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The native button did not handle its event.')

    def assistant_command(window, tool, action, *, start=None, end=None):
        options = {} if start is None else dict(start=start, end=end)
        args = bridge.arguments(tool, action, **options)
        draft = dict(schema='carr-assistant-plan/1', steps=[dict(tool=tool, arguments=args)],
                     summary='One explicit actual native acceptance command.')
        stage = {'value': 'preview', 'controls': None, 'dialog': None, 'done': False}
        before = set(AssistantService(library, interactive=bridge).store.identities())
        def poll():
            if stage['done']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('The real native assistant command exceeded its deadline.')
                controls = stage['controls']
                if controls['pause'].IsEnabled() or not controls['run'].IsEnabled():
                    timers.append(wx.CallLater(25, poll))
                    return
                if stage['value'] == 'preview':
                    preview_text = controls['review'].GetValue()
                    if tool not in preview_text:
                        raise AssertionError('The exact interactive tool was not previewed.')
                    stage['value'] = 'run'
                    press(controls['run'])
                else:
                    if not controls['review'].GetValue().startswith('complete:'):
                        raise AssertionError('The native command did not produce a completed observed result: ' +
                                             controls['review'].GetValue())
                    stage['done'] = True
                    press(controls['close'])
                    return
                timers.append(wx.CallLater(25, poll))
            except Exception:
                failures.append(traceback.format_exc())
                stage['done'] = True
                if stage['controls'] is not None:
                    press(stage['controls']['close'])
        def ready(dialog, controls):
            stage.update(dialog=dialog, controls=controls)
            check('native_assistant_parent_' + action, dialog.GetParent() is window.dialog)
            check('native_named_review_controls_' + action,
                  all(control.GetName() for control in
                      (controls['instruction'], controls['preview'], controls['run'], controls['review'])))
            press(controls['preview'])
            timers.append(wx.CallLater(25, poll))
        show_assistant(frame, parent=window.dialog, interactive=bridge,
                       initial_goal=json.dumps(draft, ensure_ascii=False), on_ready=ready)
        if failures:
            raise RuntimeError('\n'.join(failures))
        service = AssistantService(library, interactive=bridge)
        created = set(service.store.identities()) - before
        if len(created) != 1:
            raise AssertionError('The native command must create one explicit durable assistant job.')
        identity = created.pop()
        record = service.store.load(identity)
        checkpoint = record['steps'][0]
        if record['status'] != 'complete' or checkpoint['status'] != 'complete':
            raise AssertionError('The durable native job is incomplete.')
        from carr_library.assistant.models import Step
        selected = Step(tool, args)
        validate_result(selected, checkpoint['result'], record['plan']['scope'], checkpoint['intent'])
        expected = uuid5(NAMESPACE_URL, 'carr-assistant:' + identity + ':0').hex
        check('durable_dispatch_identity_' + action,
              checkpoint['intent'] == {'interactive_dispatch': expected}
              and checkpoint['result']['interactive_receipt']['operation_id'] == expected)
        records.append(record)
        return checkpoint['result']['interactive_receipt']

    def poll_reader():
        nonlocal bridge
        if not state['active']:
            return
        try:
            if monotonic() >= deadline:
                raise TimeoutError('The native reader did not settle.')
            window = state['window']
            if window is None or window.future is not None:
                timers.append(wx.CallLater(25, poll_reader))
                return
            if state['phase'] == 'voices':
                eligible = [i for i, voice in enumerate(window.voice_rows)
                    if voice.available and any(code.lower().split('-')[0] == 'en' for code in voice.languages)]
                check('actual_sapi_voice_available', bool(eligible))
                index = eligible[0]
                window.voice.SetSelection(index)
                window.language.ChangeValue(window.voice_rows[index].languages[0])
                window._voice_controls()
                check('unsupported_sapi_pitch_stays_disabled', not window.pitch.IsEnabled())
                check('native_assistant_action_named_and_reachable',
                      window.controls['assistant'].IsEnabled() and bool(window.controls['assistant'].GetName()))
                bridge = NativeReaderBridge(frame, window)
                check('immutable_window_source_binding', bridge.target['location'] == asdict(page.location)
                      and bridge.target['source_chars'] == len(source))
                state['phase'] = 'commands'
                prepared = assistant_command(window, 'reader.control', 'prepare')
                check('real_preparation_receipt', prepared['effect'] == 'utterance_prepared'
                      and prepared['observation']['playback'] is None and not window.playing)
                excerpt = window.session.current
                info = wav_info(excerpt.path)
                actual = sha256_file(excerpt.path)
                metrics.update(frames=info.frames, sample_rate=info.rate, pcm_sha256=actual,
                               source_sha256=digest, session_id=bridge.session_id)
                check('actual_pcm_metrics_match_receipt', info.frames > 0 and info.rate > 0
                      and prepared['observation']['pcm']['frames'] == info.frames
                      and prepared['observation']['pcm']['sample_rate'] == info.rate
                      and prepared['observation']['pcm']['sha256'] == actual)
                check('actual_utf16_source_selection', selected_source_offsets(window.text, source)
                      == (excerpt.selection.start, excerpt.selection.end))
                pcm_path = excerpt.path
                stopped = assistant_command(window, 'player.control', 'stop')
                check('actual_owned_pcm_released', stopped['effect'] == 'playback_stopped'
                      and window.session is None and window.future is None and not pcm_path.exists())
                selected = assistant_command(window, 'reader.control', 'select', start=7, end=8)
                check('actual_unicode_cursor_command', selected['observation']['selection'] == [7, 8]
                      and selected_source_offsets(window.text, source) == (7, 8))
                check('source_catalog_remains_exact', ReadingDesk(library, workspace, book['edition']).page(1).raw_text
                      == source and sha256(source.encode('utf-8')).hexdigest() == digest)
                state['active'] = False
                bridge.close()
                window.close()
        except Exception:
            failures.append(traceback.format_exc())
            state['active'] = False
            if bridge is not None:
                bridge.close()
            if state['window'] is not None:
                state['window'].close()

    def ready_reader(window):
        state['window'] = window
        check('actual_windows', os.name == 'nt')
        check('isolated_probe', bool(sys.flags.isolated))
        check('native_immutable_source_control', not window.text.IsEditable()
              and selected_source_offsets(window.text, source) == (first, last))
        window.reload()
        timers.append(wx.CallLater(25, poll_reader))

    window = LiveReadingDialog(frame, library.root, source, location=page.location,
        selection=(first, last), registry=VoiceRegistry((SAPI5Engine(),)), on_ready=ready_reader)
    try:
        window.show()
        if failures:
            raise RuntimeError('\n'.join(failures))
        check('native_commands_created_three_inert_receipts', len(records) == 3 and
              [record['steps'][0]['result']['interactive_receipt']['effect'] for record in records]
              == ['utterance_prepared', 'playback_stopped', 'selection_applied'])
        backup = work / 'backup'
        backup_digest = create_backup(library, backup)
        verify_backup(backup, backup_digest)
        restored = LibraryApp(restore_backup(backup, work / 'restored', backup_digest))
        service = AssistantService(restored)
        check('restored_receipts_preserve_exact_original_scope',
              all(service.store.load(record['id'])['plan'] == record['plan'] for record in records))
        check('restored_receipts_are_not_current_native_authority',
              all(service.get(record['id']).status == 'attention' for record in records))
        rejected = 0
        for record in records:
            try:
                service.resume(record['id'])
            except PermissionError:
                rejected += 1
        check('restored_interactive_jobs_cannot_replay', rejected == len(records))
    finally:
        state['active'] = False
        for timer in timers:
            timer.Stop()
        if bridge is not None:
            bridge.close()
        frame.Destroy()
        gui.Destroy()
    modules = ('carr_library.assistant.interactive_tools', 'carr_library.ui.assistant_interactive',
               'carr_library.ui.assistant_dialog', 'carr_library.ui.live_reading',
               'carr_library.assistant.service', 'carr_library.assistant.validation',
               'carr_library.assistant.models')
    origins = {}
    for name in modules:
        path = Path(importlib.import_module(name).__file__).absolute()
        origins[name] = dict(path=str(path), sha256=sha256(path.read_bytes()).hexdigest())
    return dict(schema=1, status='PASS', platform=sys.platform, isolated=bool(sys.flags.isolated),
        checks=checks, module_origins=origins, metrics=metrics, real_sapi_pcm=True,
        real_native_dialog=True, actual_assistant_jobs=True, actual_backup_restore=True,
        hardware_playback='NOT_RUN', nvda='NOT_RUN', speech_quality='NOT_RUN',
        native_pause_resume_seek='NOT_RUN', public_fixture=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work')
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    try:
        if not args.native:
            raise ValueError('Explicit native execution is required.')
        value = run(args.work)
    except Exception as exc:
        print(json.dumps(dict(schema=1, status='FAIL', platform=sys.platform,
                             error=type(exc).__name__ + ': ' + str(exc)), ensure_ascii=False))
        raise
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
