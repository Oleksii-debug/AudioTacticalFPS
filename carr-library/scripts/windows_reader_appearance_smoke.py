"""Installed native appearance, unchanged reader text, and owner lifetime.

The real source reader, editable reader, wx appearance view, saved preferences
and native styles are exercised. Font/colour picker decisions are deterministic
seams returning real installed font/colour values; OS picker interaction, physical
keys, NVDA and acoustic reading are not claimed by this automated probe.
"""
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import traceback


def _bindings():
    spec = importlib.util.spec_from_file_location('_appearance_acceptance_bindings',
        Path(__file__).with_name('windows_publishing_smoke.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _run()
    finally:
        if runtime is not None:
            runtime.close()


def _run():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader import appearance, live_reading, reading_desk, reading_state
    from carr_library.ui import reader_appearance, reading_desk as source_ui, speech_dialog

    identities = _bindings().verify_installed_sources(appearance, live_reading, reading_desk,
        reading_state, reader_appearance, source_ui, speech_dialog)
    checks, failures, retained, contexts = [], [], [], []
    deadline = monotonic() + 120

    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def fail():
        failures.append(traceback.format_exc())
        for window in wx.GetTopLevelWindows():
            if window and isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail()
            return False

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    check('real Windows native controls', wx.Platform == '__WXMSW__')

    def later(callback, delay=25):
        def invoke():
            if failures:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Reader appearance native acceptance exceeded its time limit.')
                callback()
            except Exception:
                fail()
        wx.CallLater(delay, invoke)

    def until(predicate, continuation, timeout=15):
        end = monotonic() + timeout
        def poll():
            if predicate():
                continuation()
            elif monotonic() >= end:
                raise TimeoutError('Reader appearance native stage did not complete.')
            else:
                later(poll)
        later(poll)

    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)

    def named(window, name):
        return next(child for child in children(window) if child.GetName() == name)

    def click(button):
        check('named native button is enabled: ' + button.GetName(), button.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.GetEventHandler().ProcessEvent(event)

    def no_audio(*args, **kwargs):
        raise AssertionError('Appearance acceptance must not synthesize or play audio.')

    def live_factory(source, **kwargs):
        value = live_reading.LiveReader(source, discover=lambda **unused: (), render=no_audio,
                                      player_factory=no_audio, **kwargs)
        retained.append(value)
        return value

    with TemporaryDirectory(prefix='carr-reader-appearance-') as directory:
        library = LibraryApp(Path(directory) / 'library')
        workspace = library.create_workspace('Native appearance acceptance')
        raw = 'Reader source text.\r\nУкраїнський текст 😀.\nAnother paragraph.'
        book = library.add_text(workspace, 'Appearance source', raw, language='uk')
        desk = reading_desk.ReadingDesk(library, workspace, book['edition'])
        original = desk.page(1)
        frame = wx.Frame(None, title='Reader appearance acceptance host')
        frame.library = library
        frame.report = lambda value: None
        frame.Show()

        def state_bytes():
            path = library.root / 'state/ui/reader-appearance.json'
            return path.read_bytes() if path.exists() else None

        def exercise_reader(label, window, text, capture, close):
            text.SetSelection(2, 10)
            before = text.GetValue(), text.GetSelection(), capture()
            previous = state_bytes()

            def inspect_appearance():
                review = next(item for item in wx.GetTopLevelWindows()
                    if item and isinstance(item, wx.Dialog) and item.GetTitle() == 'Reader appearance')
                check(label + ': actual appearance modal is visible', review.IsShown() and review.IsModal())
                check(label + ': zoom has an accessible name', review.zoom.GetName() == 'Zoom percent (50 to 400)')
                chosen = wx.Font(text.GetDefaultAttributes().font)
                chosen.SetFractionalPointSize(chosen.GetFractionalPointSize() + 1)

                # Only picker decisions are synthetic. The selected objects,
                # handler, preview, save and final native text control are real.
                class PickerDecision:
                    def __init__(self, parent, data): self.closed = False
                    def __bool__(self): return not self.closed
                    def ShowModal(self): return wx.ID_OK
                    def GetFontData(self): return SimpleNamespace(GetChosenFont=lambda: chosen)
                    def GetColourData(self): return SimpleNamespace(GetColour=lambda: wx.Colour('#162536'))
                    def Destroy(self): self.closed = True
                old_font, old_colour = wx.FontDialog, wx.ColourDialog
                try:
                    wx.FontDialog = wx.ColourDialog = PickerDecision
                    click(review.buttons['font'])
                    if review.buttons['foreground'].IsEnabled():
                        click(review.buttons['foreground'])
                finally:
                    wx.FontDialog, wx.ColourDialog = old_font, old_colour
                review.zoom.SetValue(175)
                review.zoom.GetEventHandler().ProcessEvent(wx.SpinEvent(wx.EVT_SPINCTRL.typeId,
                                                                       review.zoom.GetId()))
                check(label + ': preview has not saved settings', state_bytes() == previous)
                check(label + ': preview has not changed the reader',
                      (text.GetValue(), text.GetSelection(), capture()) == before)
                click(review.buttons['save'])
                saved = appearance.load_appearance(library.root).preferences
                check(label + ': real saved font and zoom reload', saved.font.face == chosen.GetFaceName()
                      and saved.zoom_percent == 175)
                check(label + ': actual native font applied',
                      text.GetFont().GetFaceName().casefold() == chosen.GetFaceName().casefold()
                      and abs(text.GetFont().GetFractionalPointSize()
                              - chosen.GetFractionalPointSize() * 1.75) <= 1)
                contrast = reader_appearance.high_contrast_enabled()
                if contrast is False:
                    check(label + ': actual native text colour applied',
                          text.GetForegroundColour() == wx.Colour('#162536'))
                else:
                    check(label + ': actual OS contrast colours retained',
                        text.GetForegroundColour() == wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
                        and text.GetBackgroundColour() == wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
                check(label + ': saved appearance preserves text, selection and model',
                      (text.GetValue(), text.GetSelection(), capture()) == before)
                if label == 'editable reader':
                    saved_raw = state_bytes()
                    click(review.buttons['reset'])
                    check('reset is previewed before saving', state_bytes() == saved_raw)
                    click(review.buttons['save'])
                    check('system defaults reload after reset',
                          appearance.load_appearance(library.root).preferences == appearance.ReaderAppearancePreferences())
                    defaults = text.GetDefaultAttributes()
                    check('reset restores current OS font',
                          text.GetFont().GetFaceName() == defaults.font.GetFaceName()
                          and text.GetFont().GetFractionalPointSize() == defaults.font.GetFractionalPointSize())
                click(review.buttons['close'])

            later(inspect_appearance)
            click(named(window, 'Reader appearance...'))
            check(label + ': appearance return retains text and selection',
                  (text.GetValue(), text.GetSelection(), capture()) == before)
            close()

        def source_reader():
            view = source_ui.ReadingDialog(frame, desk, reading_state.ReadingState(library.root),
                                          lambda: None, live_factory=live_factory)
            contexts.append(view)
            until(lambda: not view.busy and bool(view.text.GetValue()),
                  lambda: exercise_reader('source reader', view.dialog, view.text,
                      lambda: desk.page(1), view.close))
            view.show()
            if not failures:
                check('retained source remains exact after source styling', desk.page(1) == original)
                later(editable_reader)

        def editable_reader():
            def ready(window, controls):
                def begin():
                    check('saved appearance loads in a new editable reader',
                          controls['editor'].GetFont().GetFaceName()
                          == appearance.load_appearance(library.root).preferences.font.face)
                    click(controls['buttons']['save'])
                    check('actual saved draft is clean before styling', not controls['document']().dirty)
                    exercise_reader('editable reader', window, controls['editor'],
                                    lambda: controls['document']().to_dict(), controls['close'])
                until(lambda: not controls['live'].core.busy, begin)
            speech_dialog.show_speech_reader(frame, text=raw, recovery_slot='native-reader-appearance',
                                            on_ready=ready, live_factory=live_factory)
            if not failures:
                check('retained source remains exact after editable styling', desk.page(1) == original)
                later(lambda: lifetime_case('direct'))

        def lifetime_case(mode):
            host = wx.Frame(frame, title='Appearance lifetime ' + mode)
            owner = wx.Dialog(host, title='Appearance text owner ' + mode)
            text = wx.TextCtrl(owner, value=raw, style=wx.TE_MULTILINE)
            binding = reader_appearance.ReaderAppearance(owner, text, library,
                current_library=lambda: frame.library)
            review = reader_appearance.create_appearance_dialog(binding, binding.snapshot)
            binding.active_dialog = review
            identifier, events, delivered = review.GetId(), [], []
            before = state_bytes()
            def destroyed(event):
                if event.GetId() == identifier:
                    events.append(event.GetId())
                event.Skip()
            review.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
            host.Show(); owner.Show(); review.Show()
            def destroy():
                if mode == 'direct':
                    review.Destroy()
                    check('direct destruction immediately detaches appearance', not review.alive)
                else:
                    # Actual native child deletion bypasses the Python override.
                    host.DestroyChildren()
                until(lambda: not review and events == [identifier], after_deletion)
            def after_deletion():
                check(mode + ': one real native destruction event and dead peer',
                      not review and not review.alive and events == [identifier])
                if mode == 'parent':
                    check('parent deletion detaches text binding', binding.closed and not owner and not text)
                def late():
                    review.on_save(); review.on_font(); review.on_colour('foreground')
                    review.on_reset(); review.on_zoom()
                    delivered.append(True)
                wx.CallAfter(late)
                until(lambda: delivered == [True], complete)
            def complete():
                check(mode + ': actual late callbacks preserved saved state', state_bytes() == before)
                binding.close()
                if owner: owner.Destroy()
                if host: host.Destroy()
                if mode == 'direct':
                    later(lambda: lifetime_case('parent'))
                else:
                    gui.ExitMainLoop()
            later(destroy)

        try:
            later(source_reader)
            gui.MainLoop()
        finally:
            for view in contexts:
                view.dispose()
                if view.dialog:
                    view.dialog.Destroy()
            for core in retained:
                completion = core.close()
                if completion is not None:
                    completion.result(timeout=15)
            if frame:
                frame.Destroy()
        if failures:
            raise AssertionError('\n'.join(failures))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'source_bindings': identities,
        'native_font_colour_picker_interaction': 'NOT_RUN', 'physical_keys': 'NOT_RUN',
        'NVDA': 'NOT_RUN', 'audio': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
