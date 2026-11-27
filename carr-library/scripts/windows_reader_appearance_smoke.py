"""Installed native reader appearance without changing source or reading text.

Uses current editable/live readers, real wx controls, preference persistence and
the production native-dialog handlers. Font/colour picker decisions are seams
returning real installed font/colour values; OS picker interaction, physical
keys, screen-reader speech and synthesized audio are not claimed.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader import appearance
    from carr_library.reader.narration import NarrationStore, encode_project, new_project
    from carr_library.ui import reader_appearance, paced_reading
    from carr_library.ui.live_reading import LiveReadingDialog, native_offset, selected_source_offsets
    from carr_library.ui.narration_editor import open_narration_editor

    done = Event()
    def watchdog():
        if not done.wait(90):
            print('FAIL: reader appearance native watchdog expired', flush=True)
            os._exit(124)
    Thread(target=watchdog, daemon=True).start()
    checks, failures, timers = [], [], []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    for module in (appearance, reader_appearance, paced_reading):
        check('installed source ' + module.__name__,
              'site-packages' in [part.casefold() for part in Path(module.__file__).parts])
    frame = wx.Frame(None, title='Installed reader appearance acceptance')
    frame.Show()
    app.SetTopWindow(frame)
    native_font, native_colour = wx.FontDialog, wx.ColourDialog

    class FontDecision(native_font):
        def __init__(self, parent, data):
            super().__init__(parent, data)
            self.reviewed = data
            chosen = wx.Font(wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT))
            chosen.SetFractionalPointSize(13.5)
            self.reviewed.SetChosenFont(chosen)
        def ShowModal(self):
            return wx.ID_OK
        def GetFontData(self):
            return self.reviewed

    class ColourDecision(native_colour):
        def __init__(self, parent, data):
            super().__init__(parent, data)
            self.reviewed = data
            self.reviewed.SetColour(wx.Colour('#123456'))
        def ShowModal(self):
            return wx.ID_OK
        def GetColourData(self):
            return self.reviewed

    wx.FontDialog, wx.ColourDialog = FontDecision, ColourDecision
    state = {'phase': '', 'reader': None, 'controller': None, 'controls': None, 'live': None, 'paced': None}
    deadline = monotonic() + 75
    contrast = reader_appearance.high_contrast_enabled()

    def press(button):
        check('enabled named button ' + button.GetName(), button and button.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native handler ' + button.GetName(), button.GetEventHandler().ProcessEvent(event))

    def later():
        timers.append(wx.CallLater(25, poll))

    def fail(error):
        failures.append(str(error))
        print('FAIL:', str(error), flush=True)
        reader = state.get('reader')
        live = state.get('live')
        if state.get('paced'):
            state['paced'].on_close()
        binding = (getattr(reader, '_reader_appearance', None) if reader else None)
        if live is not None:
            binding = live.appearance
        if binding is not None and binding.active_dialog:
            binding.active_dialog.on_close()
        if live is not None and not live.closed:
            live.close()
        elif reader and reader.IsModal():
            press(state['controls']['close'])

    def poll():
        if failures:
            return
        try:
            if monotonic() >= deadline:
                raise TimeoutError('Reader appearance acceptance exceeded its deadline.')
            phase = state['phase']
            if phase == 'editor':
                state['phase'] = 'editor-appearance'
                later()
                press(state['controls']['appearance'])
                return
            if phase == 'editor-appearance':
                binding = state['reader']._reader_appearance
                view = binding.active_dialog
                if view is None:
                    later()
                    return
                check('actual modal appearance', view.IsModal())
                press(view.buttons['font'])
                if contrast is False:
                    press(view.buttons['foreground'])
                view.zoom.SetValue(175)
                press(view.buttons['save'])
                current = appearance.load_appearance(root)
                check('reviewed native font and zoom saved', current.preferences.font is not None
                      and current.preferences.zoom_percent == 175)
                check('editable and original styles both applied',
                      abs(state['controls']['editor'].GetFont().GetFractionalPointSize()
                          - current.preferences.font.points * 1.75) < 1.0
                      and abs(state['controls']['original'].GetFont().GetFractionalPointSize()
                          - current.preferences.font.points * 1.75) < 1.0)
                state['phase'] = 'editor-saved'
                later()
                press(view.buttons['close'])
                return
            if phase == 'editor-saved':
                control = state['controls']['editor']
                check('native text and selected offsets unchanged',
                      (control.GetValue(), control.GetSelection()) == state['before_native'])
                check('reading project exact bytes unchanged',
                      encode_project(state['controller'].project) == original_project)
                try:
                    recovery, _ = store.load(project.id, draft=True)
                except FileNotFoundError:
                    recovery = project
                check('any existing autosave preserves the same text',
                      recovery.current_text == project.current_text
                      and recovery.original_text == project.original_text)
                state['phase'] = 'editor-pacing'
                later()
                press(state['controls']['paced_reading'])
                return
            if phase == 'editor-pacing':
                view = state.get('paced')
                if view is None:
                    later()
                    return
                check('actual paced-reading modal', view.IsModal() and not view.timer.IsRunning())
                view.range.SetSelection(2)
                view.rate.SetValue(1200)
                view.group.SetValue(1)
                state['phase'] = 'pace-configured'
                later()
                return
            if phase == 'pace-configured':
                view = state['paced']
                press(view.buttons['start'])
                check('first group is actually shown before any timer advance',
                      view.model.current.index == 0 and view.preview.GetValue() == 'Exact')
                state['phase'] = 'pace-timer'
                later()
                return
            if phase == 'pace-timer':
                view = state['paced']
                if view.model.current.index == 0:
                    later()
                    return
                press(view.buttons['pause'])
                check('native pacing timer stops on Pause', not view.timer.IsRunning())
                current = view.model.current
                check('pacing highlights exact original source offsets',
                      selected_source_offsets(state['controls']['editor'], source)
                      == (current.start, current.end))
                press(view.buttons['next'])
                press(view.buttons['previous'])
                check('manual next and previous preserve the source group',
                      view.model.current.index == current.index)
                press(view.buttons['stop'])
                check('Stop restores the previous reader selection',
                      state['controls']['editor'].GetSelection() == state['before_native'][1])
                check('pacing never changes saved or pending text',
                      encode_project(state['controller'].project) == original_project
                      and state['controller'].pending_text == source)
                state['phase'] = 'editor-paced'
                later()
                press(view.buttons['close'])
                return
            if phase == 'editor-paced':
                state['paced'] = None
                state['phase'] = 'editor-done'
                press(state['controls']['close'])
                return
            if phase == 'live':
                live = state['live']
                check('existing playback controls unchanged', len(live.controls) == 10)
                check('saved appearance applies to the live source reader',
                      live.appearance.snapshot.preferences.zoom_percent == 175
                      and live.text.GetValue().replace('\r\n', '\n') == source.replace('\r\n', '\n'))
                state['phase'] = 'live-appearance'
                later()
                press(live.appearance_button)
                return
            if phase == 'live-appearance':
                live = state['live']
                view = live.appearance.active_dialog
                if view is None:
                    later()
                    return
                press(view.buttons['reset'])
                check('reset remains a preview before Save',
                      appearance.load_appearance(root).preferences.zoom_percent == 175)
                press(view.buttons['save'])
                check('explicit reset is retained', appearance.load_appearance(root).preferences
                      == appearance.ReaderAppearancePreferences())
                state['phase'] = 'live-reset'
                later()
                press(view.buttons['close'])
                return
            if phase == 'live-reset':
                live = state['live']
                check('live source and cursor unchanged by style reset',
                      live.source == source and live.text.GetSelection() == state['live_selection'])
                state['phase'] = 'done'
                press(live.controls['close'])
        except Exception as exc:
            fail(exc)

    try:
        with TemporaryDirectory(prefix='carr-native-appearance-') as temporary:
            base = Path(temporary)
            root = base / 'library'
            library = LibraryApp(root)
            library.create_workspace('Native appearance')
            frame.library = library
            source_path = base / 'Оригінал.txt'
            source = 'Exact original 😀.\r\nУкраїнський текст.\r\nThird line.'
            source_path.write_bytes(source.encode('utf-8'))
            original_bytes = source_path.read_bytes()
            store = NarrationStore(root)
            project = new_project(source, title='Native appearance', original_path=str(source_path))
            original_project = encode_project(project)
            saved = store.save(project)

            def ready(dialog, controller, controls):
                first, last = source.index('😀'), source.index('Український') + len('Український')
                controls['editor'].SetSelection(native_offset(controls['editor'], source, first),
                                               native_offset(controls['editor'], source, last))
                state.update(reader=dialog, controller=controller, controls=controls,
                             before_native=(controls['editor'].GetValue(), controls['editor'].GetSelection()),
                             phase='editor')
                later()
            def pace_factory(parent, *args, **kwargs):
                return paced_reading.show_paced_reader(parent, *args, **kwargs,
                    on_ready=lambda view: state.update(paced=view))
            result = open_narration_editor(frame, store, project, expected_digest=saved, on_ready=ready,
                                           pacing_factory=pace_factory)
            check('editable reader completed native appearance flow',
                  not failures and state['phase'] == 'editor-done')
            check('saved reader and source remain identical',
                  encode_project(result) == original_project
                  and encode_project(store.load(project.id)[0]) == original_project)

            class NoVoiceAccess:
                def voices(self, **kwargs):
                    raise AssertionError('Appearance must not discover or start speech.')
            def live_ready(live):
                state.update(reader=None, live=live, phase='live',
                             live_selection=live.text.GetSelection())
                later()
            live = LiveReadingDialog(frame, root, source, registry=NoVoiceAccess(),
                                     selection=(0, 5), on_ready=live_ready)
            live.show()
            check('live reader closed through its current owner', live.closed and live.destroyed)
            check('native appearance flow settled', state['phase'] == 'done' and not failures)
            check('original text file remained byte-identical', source_path.read_bytes() == original_bytes)
            check('no synthesized audio session was created', live.session is None and live.future is None)
    finally:
        wx.FontDialog, wx.ColourDialog = native_font, native_colour
        for timer in timers:
            timer.Stop()
        if frame:
            frame.Destroy()
        if runtime is not None:
            runtime.close()
        done.set()
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'high_contrast': contrast,
                      'os_picker_interaction': 'NOT_RUN', 'nvda': 'NOT_RUN',
                      'speech_or_audio': 'NOT_RUN'}, sort_keys=True))
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows required.'}))
        return 77
    try:
        return run_native()
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
