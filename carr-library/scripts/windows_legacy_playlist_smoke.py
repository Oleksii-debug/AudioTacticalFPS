"""Installed native review/import/restore of a passive canonical playback queue."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import sysconfig
import tempfile
from time import monotonic
import traceback
import wave


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit native Windows run required.'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import legacy_playlist, legacy_playlist_state, playlist
    from carr_library.audio.playback_processing import PlaybackControls
    from carr_library.ui import legacy_playlist as native
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController

    checks, failures = [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            return False

    gui = ProbeApp(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    def pump(condition, seconds=45):
        deadline = monotonic() + seconds
        while not condition():
            if failures:
                raise AssertionError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError('The native legacy queue operation did not settle within its acceptance deadline.')
            gui.Yield()
            wx.MilliSleep(10)
        if failures:
            raise AssertionError('\n'.join(failures))

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def button(window, name):
        matches = [child for child in descendants(window)
                   if isinstance(child, wx.Button) and child.GetName() == name]
        if len(matches) != 1:
            raise AssertionError('Expected exactly one owned native action: ' + name)
        return matches[0]

    def id_button(window, identity):
        matches = [child for child in descendants(window)
                   if isinstance(child, wx.Button) and child.GetId() == identity]
        if len(matches) != 1:
            raise AssertionError('Expected one button under this exact owned modal dialog.')
        return matches[0]

    def press(control):
        if not control.IsEnabled() or not control.IsShown():
            raise AssertionError('The reviewed native action is not currently available.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        if not control.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The actual native button event was not handled.')

    installed = Path(sysconfig.get_paths()['purelib']).resolve()
    check('exact installed migration and current native UI', all(
        installed in Path(module.__file__).resolve().parents
        for module in (legacy_playlist, legacy_playlist_state, playlist, native)))
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    window = frame = timer = None
    with tempfile.TemporaryDirectory(prefix='carr-native-legacy-queue-') as temporary:
        root = Path(temporary)
        app = LibraryApp(root / 'library')
        original = root / 'source.wav'
        with wave.open(str(original), 'wb') as audio:
            audio.setparams((1, 2, 8000, 8000, 'NONE', 'PCM'))
            audio.writeframes(b'\x31\x09' * 8000)
        raw = original.read_bytes()
        checksum = sha256(raw).hexdigest()
        member = 'state/studio/inputs/' + checksum + '.audio'
        retained = app.root / member
        retained.parent.mkdir(parents=True)
        retained.write_bytes(raw)
        item = dict(filename=member, label='Україна — retained original', sha256=checksum,
                    size_bytes=len(raw), frames=8000, sample_rate=8000, channels=1, sample_width=2)
        old = dict(schema='carr-playback-queue/1', items=[item, dict(item, label='Second occurrence')],
                   index=1, frame=4000, volume=29, muted=True, balance=.6, skips=[11, 43],
                   output=dict(index=127, name='Old device', interface='historical-interface',
                               manufacturer=1, product=2, driver=3, channels=2))
        queue_path = app.root / legacy_playlist_state.NAME
        queue_path.parent.mkdir(parents=True)
        queue_bytes = (json.dumps(old, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
        queue_path.write_bytes(queue_bytes)
        controller = StudioController(private_root=app.root)
        controller.new('Existing studio narration remains unchanged.', title='Before explicit queue import')
        controller.save_in_library()
        initial_id = controller.project_id
        frame = wx.Frame(None, title='Native previous playback queue acceptance')
        frame.library = app
        frame.report = lambda text: None
        frame.Show()
        gui.SetTopWindow(frame)
        state = dict(mode='cancel', selected=0, cancelled=0, accepted=0,
                     focus=None, handled=set(), started=monotonic())
        def owned_modals():
            if window is None:
                return []
            return [dialog for dialog in wx.GetTopLevelWindows()
                    if isinstance(dialog, wx.Dialog) and dialog.GetParent() is window.dialog
                    and dialog.IsShown() and dialog.IsModal()]
        def dismiss():
            for dialog in owned_modals():
                dialog.EndModal(wx.ID_CANCEL)
        def drive(event):
            try:
                if failures:
                    dismiss()
                    return
                if monotonic() - state['started'] >= 90:
                    raise TimeoutError('Native queue modal driver exceeded its bounded lifetime.')
                dialogs = owned_modals()
                if not dialogs:
                    state['focus'] = None
                    return
                if len(dialogs) != 1:
                    raise AssertionError('An unexpected additional owned modal appeared.')
                dialog = dialogs[0]
                title = dialog.GetTitle()
                key = (dialog.GetId(), title)
                if key in state['handled']:
                    return
                if state['focus'] is not None:
                    previous_key, control, phase = state['focus']
                    if previous_key != key or not control or wx.Window.FindFocus() is not control:
                        return
                    state['focus'] = None
                    state['handled'].add(key)
                    if phase == 'select':
                        state['selected'] += 1
                    elif phase == 'cancel':
                        state['cancelled'] += 1
                    else:
                        state['accepted'] += 1
                    press(control)
                    return
                if title == 'Import previous playback queue':
                    choices = [child for child in descendants(dialog) if isinstance(child, wx.ListBox)]
                    if len(choices) != 1 or choices[0].GetCount() < 2:
                        raise AssertionError('The native queue chooser did not expose the retained source and folder option.')
                    choices[0].SetSelection(0)
                    selection = wx.CommandEvent(wx.EVT_LISTBOX.typeId, choices[0].GetId())
                    selection.SetEventObject(choices[0])
                    selection.SetInt(0)
                    choices[0].GetEventHandler().ProcessEvent(selection)
                    control, phase = id_button(dialog, wx.ID_OK), 'select'
                elif title == 'Review previous playback queue':
                    texts = [child for child in descendants(dialog)
                             if isinstance(child, wx.TextCtrl) and child.GetName() == 'Previous playback queue review']
                    if len(texts) != 1 or 'Import 2 recordings' not in texts[0].GetValue():
                        raise AssertionError('The native review did not show the exact selected queue.')
                    phase = state['mode']
                    control = button(dialog, 'Cancel queue import' if phase == 'cancel' else 'Import as new project')
                else:
                    raise AssertionError('Unexpected owned modal: ' + title)
                control.SetFocus()
                state['focus'] = key, control, phase
            except BaseException:
                failures.append(traceback.format_exc())
                dismiss()

        try:
            window = AudioStudioDialog(frame, controller)
            window.dialog.Show()
            timer = wx.Timer(frame)
            frame.Bind(wx.EVT_TIMER, drive, timer)
            timer.Start(40)
            pump(lambda: window.dialog.IsShown())
            pages = [index for index in range(window.notebook.GetPageCount())
                     if window.notebook.GetPageText(index) == 'Playback']
            check('native Playback page is uniquely reachable', len(pages) == 1)
            window.notebook.SetSelection(pages[0])
            gui.Yield()
            import_button = button(window.dialog, 'Import previous playback queue')
            import_button.SetFocus()
            gui.Yield()
            press(import_button)
            pump(lambda: state['cancelled'] == 1 and not window.busy)
            check('native cancellation creates no new managed project',
                  controller.project_id == initial_id
                  and window.legacy_queue_action.last_result is None
                  and len(list((app.root / 'audio-projects').iterdir())) == 1)
            state['mode'], state['handled'], state['focus'] = 'accept', set(), None
            import_button.SetFocus()
            gui.Yield()
            press(import_button)
            pump(lambda: state['accepted'] == 1 and not window.busy
                 and window.legacy_queue_action.last_result is not None)
            result = window.legacy_queue_action.last_result
            check('native explicit review imports once into a new managed project',
                  state['selected'] == 2 and result.durability_confirmed
                  and controller.project_id == result.project_id != initial_id)
            store = playlist.PlaylistStore(app.root, result.project_id)
            reviewed = store.review()
            check('real retained queue keeps exact labels, order and historical cursor',
                  [row.label for row in reviewed.state.items] == [row['label'] for row in old['items']]
                  and (reviewed.state.index, reviewed.state.frame, reviewed.state.accuracy)
                  == (1, 4000, 'historical_cursor'))
            check('import itself starts no playback and applies no old controls',
                  window.playback.status().state == 'stopped'
                  and reviewed.state.controls == PlaybackControls()
                  and window.playback.sleep_remaining_seconds is None)
            window.notebook.SetSelection(pages[0])
            gui.Yield()
            press(button(window.dialog, 'Restore saved queue'))
            pump(lambda: not window.busy and window.queue_state.review is not None)
            check('native Restore uses the same existing player without autoplay',
                  window.playback.playlist == window.queue_state.store.paths(window.queue_state.review)
                  and window.queue_state.cursor == (1, 4000, 'historical_cursor')
                  and window.playback.status().state == 'stopped')
            check('current output remains a separate explicit choice',
                  window.playback_device.GetSelection() == 0)
            check('old queue and original PCM remain unchanged',
                  queue_path.read_bytes() == queue_bytes
                  and retained.read_bytes() == raw and original.read_bytes() == raw)
            proof = json.loads((store.audio / legacy_playlist_state.PROVENANCE_NAME).read_bytes())
            check('raw old output declaration survives only as provenance',
                  proof['queue_utf8'].encode('utf-8') == queue_bytes)
            check('all actual native callbacks settled without exception', not failures)
            report = dict(status='PASS', checks=checks, wx=wx.version(), native_events='PASS',
                          saved_cursor='historical_fixture', audio_output='NOT_RUN',
                          physical_audio='NOT_RUN', nvda='NOT_RUN')
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            if timer is not None:
                timer.Stop()
            dismiss()
            if window is not None:
                window.cancelled.set()
                window.executor.shutdown(wait=True, cancel_futures=True)
                window.playback.close()
                window.timer.Stop()
                window.playback_timer.Stop()
                if not window.closed:
                    window.close()
                window.dialog.Destroy()
            if frame:
                frame.Destroy()
            gui.Yield()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
