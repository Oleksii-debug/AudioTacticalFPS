"""Actual installed wx controls for current structured Studio roles and output.

Synthetic advertised voice metadata exercises editing only. No model,
speech/language quality, physical playback or NVDA claim is made.
"""
from argparse import ArgumentParser
import faulthandler
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from time import monotonic
import traceback


MODULES = (
    'carr_library.audio.studio_controls', 'carr_library.ui.studio_controls',
    'carr_library.ui.audio_studio', 'carr_library.audio.models',
    'carr_library.audio.production', 'carr_library.audio.effect_editing',
    'carr_library.audio.managed', 'carr_library.reader.narration_text',
    'carr_library.ui.save_all',
)
ROWS = (
    {'id': 'synthetic:uk', 'name': 'Synthetic complete control row',
     'languages': ('uk', 'en-US'), 'controls': ('rate', 'pitch', 'volume'), 'available': True},
    {'id': 'synthetic:limited', 'name': 'Synthetic limited control row',
     'languages': ('en-US',), 'controls': ('volume',), 'available': True},
    {'id': 'synthetic:disabled', 'name': 'Synthetic disabled row',
     'languages': ('uk',), 'controls': (), 'available': False, 'reason': 'Explicitly disabled fixture.'},
)


def identities():
    prefix = Path(sys.prefix).resolve(strict=True)
    values = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('The native form imported an uninstalled implementation: ' + name)
        values[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return values


def run(work):
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.production import parse_script
    from carr_library.audio.studio_controls import ScriptProductionEditor
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
    from carr_library.ui.studio_controls import show_production_editor

    checks, failures, timers = [], [], []
    metrics = {'forms_applied': 0, 'forms_cancelled': 0, 'capability_refusals': 0,
               'stale_refusals': 0, 'accepted_draft_edits': 0, 'save_all_commits': 0,
               'initial_load_cleanup': 0}
    before_origins = identities()
    work.mkdir(parents=False, exist_ok=False)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    class Registry:
        def voices(self, *, cancelled=lambda: False):
            if cancelled():
                raise InterruptedError('Synthetic metadata discovery cancelled')
            return ROWS

        def capabilities(self):
            return ({'engine': 'synthetic', 'available': True},)

    class NativeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            abort()
            return True

    gui = NativeApp(False)
    frame = studio = None
    state = {'phase': 'initial', 'active': True, 'complete': False, 'abort_started': None}
    deadline = monotonic() + 150
    source = ("[role 'Ірина Павло' voice=synthetic:uk language=uk pan=-.25]\r\n"
              "[effect 'Ірина Павло' highpass frequency=80]\r\n"
              "[role Missing voice=retained:old language=uk]\r\n"
              "[output sample_rate=24000 disk_budget_mb=96 stems=true]\r\n"
              "# Читання\r\nІрина Павло: Точні слова.\r\nNarrator: Original source.\r\n")
    original_file = None
    original_saved = None
    accepted_script = None

    def alive(window):
        return bool(window) and not window.IsBeingDeleted() and window.IsShown()

    def children(root):
        pending, result = list(root.GetChildren()), []
        while pending:
            item = pending.pop()
            result.append(item)
            if len(result) > 1024:
                raise ValueError('The native control inventory exceeded its bound.')
            pending.extend(item.GetChildren())
        return result

    def named(root, name, kind):
        rows = [item for item in children(root) if isinstance(item, kind) and item.GetName() == name]
        if len(rows) != 1:
            raise AssertionError('Expected one named native control: ' + name)
        return rows[0]

    def press(button):
        if not isinstance(button, wx.Button) or not alive(button) or not button.IsEnabled():
            raise AssertionError('The requested native button is not enabled and visible.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.GetEventHandler().ProcessEvent(event)

    def choose(control, index):
        if not isinstance(control, wx.Choice) or not alive(control) or not control.IsEnabled():
            raise AssertionError('The current native Choice is unavailable.')
        if not 0 <= index < control.GetCount():
            raise AssertionError('The exact native selection is out of range.')
        control.SetSelection(index)
        event = wx.CommandEvent(wx.EVT_CHOICE.typeId, control.GetId())
        event.SetEventObject(control)
        event.SetInt(index)
        event.SetString(control.GetString(index))
        control.GetEventHandler().ProcessEvent(event)
        if control.GetSelection() != index:
            raise AssertionError('The current native selection did not retain its exact index.')

    def choose_text(control, text):
        matches = [index for index in range(control.GetCount()) if control.GetString(index) == text]
        if len(matches) != 1:
            raise AssertionError('The exact native choice is not unique: ' + text)
        choose(control, matches[0])

    def voice_index(control, identifier):
        matches = [index for index in range(control.GetCount())
                   if control.GetString(index).endswith(' — ' + identifier)]
        if len(matches) != 1:
            raise AssertionError('The exact selected voice ID is absent or ambiguous.')
        return matches[0]

    def forms():
        result = [window for window in wx.GetTopLevelWindows()
                  if isinstance(window, wx.Dialog) and alive(window) and window.IsModal()
                  and window.GetName() == 'Structured Studio roles and final audio']
        if any(window.GetParent() is not studio.dialog for window in result) or len(result) > 1:
            raise AssertionError('The active form belongs to another owner or is duplicated.')
        return result

    def controls(form):
        return {
            'role': named(form, 'Production role', wx.Choice),
            'voice': named(form, 'Installed voice for this role', wx.Choice),
            'language': named(form, 'Exact advertised voice language', wx.Choice),
            'pan': named(form, 'Pan: minus 1 left, 0 centre, 1 right', wx.TextCtrl),
            'rate': named(form, 'Speech rate: engine scale, minus 10 to 10', wx.TextCtrl),
            'pitch': named(form, 'Speech pitch: engine scale, minus 10 to 10', wx.TextCtrl),
            'volume': named(form, 'Speech volume: 0 to 100 percent', wx.TextCtrl),
            'gain_db': named(form, 'Final role gain: minus 120 to 24 dB', wx.TextCtrl),
            'gain_enabled': named(form, 'Enable final role gain', wx.CheckBox),
            'format': named(form, 'Final audio format', wx.Choice),
            'stems': named(form, 'Export separate role and background stems', wx.CheckBox),
            'status': named(form, 'Role and output editing status', wx.TextCtrl),
            'capabilities': named(form, 'Selected voice capabilities and retained effects', wx.TextCtrl),
            'accept': named(form, 'Apply role and output changes', wx.Button),
            'cancel': named(form, 'Cancel role and output changes', wx.Button),
        }

    def schedule(callback=None):
        timers.append(wx.CallLater(25, callback or poll))

    def abort():
        if not state['active']:
            return
        if state['abort_started'] is None:
            state['abort_started'] = monotonic()
        state['phase'] = 'aborting'
        schedule(cleanup_poll)

    def cleanup_poll():
        try:
            if studio is None or studio.closed:
                return
            active = forms()
            if active:
                active[0].EndModal(wx.ID_CANCEL)
                schedule(cleanup_poll)
                return
            if studio.busy:
                studio.cancelled.set()
                if monotonic() - state['abort_started'] > 10:
                    raise TimeoutError('Owned Studio worker did not settle during failure cleanup.')
                schedule(cleanup_poll)
                return
            # Only the qualification-owned outer modal remains. Failure
            # cleanup discards its synthetic unsaved draft without a new save.
            if studio.dialog.IsModal():
                studio.dialog.EndModal(wx.ID_CANCEL)
        except Exception:
            failures.append(traceback.format_exc())
            raise

    def open_form(name, next_phase, page):
        studio.notebook.SetSelection(page)
        state['origin'] = named(studio.dialog, name, wx.Button)
        state['phase'] = next_phase
        schedule()
        press(state['origin'])

    def poll():
        nonlocal accepted_script
        if not state['active'] or state['phase'] == 'aborting':
            return
        try:
            if monotonic() >= deadline:
                raise TimeoutError('The native Studio form sequence exceeded its finite deadline.')
            if studio is None or not alive(studio.dialog) or not studio.dialog.IsModal():
                schedule()
                return
            phase = state['phase']
            active = forms()
            if phase == 'initial':
                if studio.busy:
                    schedule()
                    return
                check('actual installed Windows Studio owner', sys.platform == 'win32'
                      and sys.flags.isolated and studio.dialog.GetParent() is frame)
                check('role and output actions are named native buttons',
                      isinstance(named(studio.dialog, 'Edit roles and voices', wx.Button), wx.Button)
                      and isinstance(named(studio.dialog, 'Final format and stems', wx.Button), wx.Button))
                studio.controller.configure(voice='sapi5:default')
                studio._refresh_text()
                index = studio.voice.GetSelection()
                check('unlisted saved default ID remains visible without automatic substitution',
                      studio.voice_rows[index]['id'] == 'sapi5:default'
                      and 'not in the current list' in studio.voice.GetString(index)
                      and studio.controller.voice == 'sapi5:default')
                choose(studio.voice, next(i for i, row in enumerate(studio.voice_rows)
                                         if row['id'] == 'synthetic:uk'))
                studio._capture()
                check('default voice changes only after explicit native selection',
                      studio.controller.voice == 'synthetic:uk')
                studio.controller.result = {'synthetic': 'previous completed result'}
                open_form('Edit roles and voices', 'cancel_form', 0)
                return
            if phase == 'cancel_form':
                if not active:
                    schedule()
                    return
                form, values = active[0], controls(active[0])
                check('role form has exact current modal owner', form.GetParent() is studio.dialog)
                check('all role and output controls have native accessible names',
                      len(values) == 15 and all(item.GetName() for item in values.values()))
                choose(values['role'], 1)
                values['pan'].SetValue('.75')
                choose_text(values['format'], 'FLAC')
                check('pending native fields do not mutate the current draft', studio.controller.script == source)
                metrics['forms_cancelled'] += 1
                state['phase'] = 'cancelled'
                press(values['cancel'])
            elif phase == 'cancelled':
                if active or studio.busy:
                    schedule()
                    return
                check('native Cancel preserves source draft undo history and saved bytes',
                      studio.controller.script == source and not studio.controller.draft.edits
                      and original_file.read_bytes() == original_saved)
                check('cancelled native form returns focus to its current opening button',
                      wx.Window.FindFocus() is state['origin'])
                open_form('Edit roles and voices', 'accept_ready', 0)
                return
            elif phase == 'accept_ready':
                if not active:
                    schedule()
                    return
                values = controls(active[0])
                choose(values['role'], 1)
                values['role'].SetFocus()
                state['phase'] = 'accept_focus'
            elif phase == 'accept_focus':
                values = controls(active[0])
                check('actual role Choice receives keyboard focus', wx.Window.FindFocus() is values['role'])
                check('role form loads exact saved pan voice and language',
                      float(values['pan'].GetValue()) == -.25
                      and values['voice'].GetStringSelection().endswith(' — synthetic:uk')
                      and values['language'].GetStringSelection() == 'uk')
                values['rate'].SetValue('1')
                values['pitch'].SetValue('2')
                choose(values['voice'], voice_index(values['voice'], 'synthetic:limited'))
                check('new voice does not silently choose an advertised language',
                      values['language'].GetStringSelection() == 'Unavailable saved language — uk')
                check('unsupported saved controls remain visible for explicit reset',
                      values['rate'].GetValue() == '1' and values['pitch'].GetValue() == '2'
                      and values['rate'].IsEnabled() and values['pitch'].IsEnabled()
                      and 'not advertised' in values['capabilities'].GetValue())
                choose_text(values['language'], 'en-US')
                press(values['accept'])
                state['phase'] = 'capability_refused'
            elif phase == 'capability_refused':
                values = controls(active[0])
                check('native Apply refuses unsupported nondefault voice controls',
                      'does not support rate' in values['status'].GetValue()
                      and studio.controller.script == source and active[0].IsModal())
                metrics['capability_refusals'] += 1
                choose(values['voice'], voice_index(values['voice'], 'synthetic:uk'))
                choose_text(values['language'], 'uk')
                values['pan'].SetValue('.5')
                values['rate'].SetValue('2')
                values['pitch'].SetValue('-1')
                values['volume'].SetValue('60')
                values['gain_db'].SetValue('-6')
                values['gain_enabled'].SetValue(True)
                choose_text(values['format'], 'M4B')
                values['stems'].SetValue(False)
                state['phase'] = 'accepted'
                press(values['accept'])
            elif phase == 'accepted':
                if active or studio.busy:
                    schedule()
                    return
                accepted_script = studio.controller.script
                plan = studio.controller.plan()
                role = next(item for item in plan.roles if item.name == 'Ірина Павло')
                check('native Apply commits exactly one undoable draft edit',
                      len(studio.controller.draft.edits) == 1 and accepted_script != source)
                check('native role controls use existing exact units',
                      (role.voice, role.language, role.pan, role.rate, role.pitch, role.volume)
                      == ('synthetic:uk', 'uk', .5, 2, -1, .6))
                check('final gain retains the earlier effect chain',
                      len(role.effects) == 2 and role.effects[0].kind == 'highpass'
                      and role.effects[-1].kind == 'gain'
                      and dict(role.effects[-1].parameters) == {'gain_db': -6}
                      and role.effects[-1].enabled is True)
                check('native output controls retain sample rate and disk budget',
                      (plan.output_format, plan.export_stems, plan.sample_rate, plan.disk_budget_bytes)
                      == ('m4b', False, 24000, 96 * 1024**2))
                check('accepted controls invalidate old result and displayed preview',
                      studio.controller.result is None and 'Preview the current plan' in studio.plan_text.GetValue())
                check('accepted form preserves immutable source and waits for explicit save',
                      studio.controller.draft.source_text == source and original_file.read_bytes() == original_saved
                      and studio.controller.dirty)
                check('accepted native form returns focus to its opening button',
                      wx.Window.FindFocus() is state['origin'])
                metrics['forms_applied'] += 1
                metrics['accepted_draft_edits'] += 1
                studio.notebook.SetSelection(1)
                press(named(studio.dialog, 'Undo preparation edit', wx.Button))
                check('existing native Undo restores exact original script', studio.controller.script == source)
                press(named(studio.dialog, 'Redo preparation edit', wx.Button))
                check('existing native Redo restores accepted controls', studio.controller.script == accepted_script)
                studio.notebook.SetSelection(2)
                press(named(studio.dialog, 'Preview plan', wx.Button))
                expected_preview = studio.controller.preview()
                # wx.TextEntry.GetValue exposes canonical LF even on Windows;
                # the exact original script retains its separately checked CRLF.
                expected_display = expected_preview.replace('\r\n', '\n').replace('\r', '\n')
                displayed_preview = studio.plan_text.GetValue()
                if displayed_preview != expected_display:
                    first = next((index for index, pair in enumerate(zip(displayed_preview, expected_display))
                                  if pair[0] != pair[1]), min(len(displayed_preview), len(expected_display)))
                    print('DIAGNOSTIC preview display: ' + json.dumps({
                        'first_difference': first, 'actual_length': len(displayed_preview),
                        'expected_length': len(expected_display),
                        'actual_context': displayed_preview[max(0, first - 32):first + 64],
                        'expected_context': expected_display[max(0, first - 32):first + 64]},
                        ensure_ascii=True), flush=True)
                check('explicit native Preview describes the current edited plan',
                      displayed_preview == expected_display and studio.controller.script == accepted_script)
                press(named(studio.dialog, 'Save all open drafts', wx.Button))
                check('current native Save All durably saves the accepted script',
                      not studio.controller.dirty and not studio._save_coordinator.busy
                      and original_file.read_bytes() != original_saved)
                metrics['save_all_commits'] += 1
                open_form('Final format and stems', 'missing_role', 2)
                return
            elif phase == 'missing_role':
                if not active:
                    schedule()
                    return
                values = controls(active[0])
                check('output route initially focuses the actual final-format Choice',
                      wx.Window.FindFocus() is values['format'])
                choose(values['role'], 2)
                check('unlisted saved role ID stays visibly selected without replacement',
                      values['voice'].GetStringSelection().endswith(' — retained:old')
                      and 'not in the current list' in values['voice'].GetStringSelection())
                check('unavailable role keeps exact language and disabled unsupported defaults',
                      values['language'].GetStringSelection() == 'Unavailable saved language — uk'
                      and not values['rate'].IsEnabled() and not values['pitch'].IsEnabled())
                choose_text(values['format'], 'OPUS')
                values['stems'].SetValue(True)
                state['phase'] = 'output_accepted'
                press(values['accept'])
            elif phase == 'output_accepted':
                if active or studio.busy:
                    schedule()
                    return
                plan = studio.controller.plan()
                missing = next(role for role in plan.roles if role.name == 'Missing')
                check('output-only acceptance preserves the unlisted role and original source',
                      missing.voice == 'retained:old' and missing.language == 'uk'
                      and plan.output_format == 'opus' and plan.export_stems
                      and studio.controller.draft.source_text == source
                      and len(studio.controller.draft.edits) == 2)
                metrics['forms_applied'] += 1
                metrics['accepted_draft_edits'] += 1
                open_form('Final format and stems', 'stale_form', 2)
                return
            elif phase == 'stale_form':
                if not active:
                    schedule()
                    return
                values = controls(active[0])
                choose_text(values['format'], 'FLAC')
                studio.controller.edit_script(studio.controller.script + 'Narrator: Later owner edit.\r\n')
                state['later_script'] = studio.controller.script
                press(values['accept'])
                state['phase'] = 'stale_refused'
            elif phase == 'stale_refused':
                values = controls(active[0])
                check('native Apply refuses a changed current draft and leaves the form open',
                      'owner changed' in values['status'].GetValue() and active[0].IsModal())
                check('stale form cannot replace the later draft or its output settings',
                      studio.controller.script == state['later_script']
                      and studio.controller.plan().output_format == 'opus')
                metrics['stale_refusals'] += 1
                metrics['forms_cancelled'] += 1
                state['phase'] = 'stale_cancelled'
                press(values['cancel'])
            elif phase == 'stale_cancelled':
                if active or studio.busy:
                    schedule()
                    return
                studio._refresh_text()
                press(named(studio.dialog, 'Save all open drafts', wx.Button))
                check('Save All retains the newer edit after stale form refusal',
                      not studio.controller.dirty and studio.controller.script == state['later_script'])
                metrics['save_all_commits'] += 1
                open_form('Final format and stems', 'close_form', 2)
                return
            elif phase == 'close_form':
                if not active:
                    schedule()
                    return
                state['phase'] = 'close_form_done'
                metrics['forms_cancelled'] += 1
                active[0].Close()
            elif phase == 'close_form_done':
                if active or studio.busy:
                    schedule()
                    return
                check('native window Close cancels only the owned form and preserves the draft',
                      not studio.closed and studio.controller.script == state['later_script']
                      and not studio.controller.dirty)
                check('form controls never create a second Studio save participant',
                      len(studio._save_coordinator.participants) == 1)
                state['complete'] = True
                state['active'] = False
                press(named(studio.dialog, 'Close Audio Studio', wx.Button))
                return
            schedule()
        except Exception:
            failures.append(traceback.format_exc())
            abort()

    try:
        library = LibraryApp(work / 'library')
        library.create_workspace('Synthetic Studio form qualification')
        frame = wx.Frame(None, title='Studio form qualification owner', size=(980, 840))
        frame.Show()
        controller = StudioController(private_root=library.root, registry=Registry(),
                                      ffmpeg=os.environ.get('CARR_FFMPEG'))
        controller.new(source, input_kind='script')
        controller.configure(voice='synthetic:uk', language='uk')
        controller.save_in_library()
        original_file = controller.project_path
        original_saved = original_file.read_bytes()
        studio = AudioStudioDialog(frame, controller)
        schedule()
        studio.show()
        if failures:
            raise AssertionError('\n'.join(failures))
        check('native Studio close settles the owned executor and unregisters its draft',
              state['complete'] and studio.closed and not studio._save_coordinator.participants)
        studio.executor.shutdown(wait=True, cancel_futures=True)
        check('no Studio worker thread remains after native close',
              all(not thread.is_alive() for thread in studio.executor._threads))
        gui.Yield()
        before_windows = tuple(window for window in wx.GetTopLevelWindows()
                               if window and not window.IsBeingDeleted())
        bad = ScriptProductionEditor("[role Narrator voice='saved\tvoice']\nExact words.")
        try:
            show_production_editor(frame, bad, ROWS)
        except ValueError:
            pass
        else:
            raise AssertionError('A malformed saved role unexpectedly opened the native form.')
        gui.Yield()
        check('native initial-load failure destroys its allocated form',
              tuple(window for window in wx.GetTopLevelWindows()
                    if window and not window.IsBeingDeleted()) == before_windows)
        metrics['initial_load_cleanup'] += 1
        check('native flow preserves the exact immutable Unicode source',
              controller.draft.source_text == source and 'Точні слова.' in controller.script)
        if identities() != before_origins:
            raise ValueError('Installed Studio implementation bytes changed during native execution.')
        check('exact installed form implementation remains unchanged', True)
        report = {'status': 'PASS', 'actual_windows': True, 'isolated': True,
                  'native_controls': True, 'native_events': 'PASS', 'draft_lifecycle': 'PASS',
                  'source_preserved': True, 'cleanup_complete': True,
                  'checks': checks, 'total_checks': len(checks), 'metrics': metrics,
                  'module_origins': before_origins, 'wx': wx.version(),
                  'voice_metadata': 'SYNTHETIC_PROTOCOL_ONLY', 'speech_quality': 'NOT_QUALIFIED',
                  'physical_audio': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    finally:
        state['active'] = False
        for timer in timers:
            timer.Stop()
        if studio is not None:
            studio.cancelled.set()
            studio.timer.Stop()
            studio.playback_timer.Stop()
            studio.executor.shutdown(wait=True, cancel_futures=True)
            studio.playback.close()
            getattr(studio, '_save_unregister', lambda: None)()
            getattr(studio, '_progress_unregister_player', lambda: None)()
            if studio.dialog and not studio.dialog.IsBeingDeleted():
                if studio.dialog.IsModal():
                    raise RuntimeError('An owned Studio modal did not unwind before final cleanup.')
                studio.dialog.Destroy()
        if frame:
            frame.Destroy()
        gui.Yield()
        gui.Destroy()
        if failures:
            raise AssertionError('\n'.join(failures))
    return report


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32' or not sys.flags.isolated:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Use actual isolated installed Windows Python.',
                          'nvda': 'NOT_RUN'}), flush=True)
        return 77
    handle, report = None, {'status': 'FAIL', 'reason': 'The native form probe did not finish.'}
    faulthandler.enable()
    faulthandler.dump_traceback_later(210, exit=True)
    print('PHASE installed Studio role and output controls started', flush=True)
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        report = run(args.work.absolute())
    except Exception as error:
        traceback.print_exc()
        report = {'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error),
                  'actual_windows': sys.platform == 'win32', 'isolated': bool(sys.flags.isolated),
                  'nvda': 'NOT_RUN', 'physical_audio': 'NOT_RUN'}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                report = {'status': 'FAIL', 'reason': 'Native runtime cleanup: ' + str(error),
                          'nvda': 'NOT_RUN', 'physical_audio': 'NOT_RUN'}
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
