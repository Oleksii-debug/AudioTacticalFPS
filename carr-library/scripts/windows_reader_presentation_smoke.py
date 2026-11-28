"""Actual Windows keyboard acceptance for the existing reader's appearance."""
import argparse
import faulthandler
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def run(arguments, report):
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance requires actual Windows.')
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech.presentation import ReadingAppearance, ReadingAppearanceStore, ReadingFont
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.ui.desktop_app import DesktopFrame
    import carr_library.speech.presentation as state_module
    import carr_library.ui.reader_presentation as appearance_module
    import carr_library.ui.speech_dialog as reader_module
    errors, checks = [], report['checks']
    deadline = monotonic() + 90
    stage, reader, frame, finished = 0, None, None, False
    previous_reader = None
    baseline, snapshot, selection = None, None, None

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    checkout = Path(__file__).resolve().parents[1]
    for module in (state_module, appearance_module, reader_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('exact module source: ' + module.__name__,
              (not actual.is_relative_to(checkout) if arguments.expect_installed else actual == expected)
              and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    def failure():
        detail = traceback.format_exc()
        errors.append(detail)
        report.update(failed_stage=stage, error=detail)
        print(f'FAILED: appearance stage {stage}\n{detail}', flush=True)
        arguments.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        abort()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failure()
            return True

    app = ProbeApp(False)
    simulator = wx.UIActionSimulator()

    with TemporaryDirectory(prefix='resonance-reading-appearance-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native appearance')
        original = 'First line\nУкраїна 🧭 second line.\nLast line.'
        library.add_text(workspace, 'Original reading source', original)
        source = root / 'original.txt'
        source.write_bytes(original.encode('utf-8'))
        source_digest = sha256(source.read_bytes()).digest()
        store = ReadingAppearanceStore(library.root)
        chosen = ReadingAppearance(foreground='#FFFFFF', background='#001122', zoom=175)

        def source_guard():
            if sha256(source.read_bytes()).digest() != source_digest:
                raise ValueError('Original source changed')

        def abort():
            # This app owns only synthetic probe windows. End modal loops
            # directly on failure: Close would open an unsaved-copy question
            # and conceal the original exception behind the watchdog.
            for window in reversed(wx.GetTopLevelWindows()):
                if isinstance(window, wx.Dialog) and bool(window) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            app.ExitMainLoop()

        def guarded(callback):
            def invoke():
                try:
                    callback()
                except Exception:
                    failure()
            return invoke

        def focused(target):
            window = wx.Window.FindFocus()
            while window is not None:
                if window == target:
                    return True
                window = window.GetParent()
            return False

        def press(target, key=wx.WXK_SPACE, modifiers=wx.MOD_NONE):
            target.SetFocus()
            def dispatch():
                check('native focus reaches ' + target.GetName(), focused(target))
                check('native key dispatch: ' + target.GetName(), simulator.Char(key, modifiers))
            wx.CallLater(25, guarded(dispatch))

        def type_text(target, value):
            target.SetFocus()
            def dispatch():
                check('native focus reaches ' + target.GetName(), focused(target))
                check('native select text: ' + target.GetName(), simulator.Char(ord('A'), wx.MOD_CONTROL))
                check('native text entry: ' + target.GetName(), simulator.Text(value.encode('ascii')))
                check('native text commit: ' + target.GetName(), simulator.Char(wx.WXK_TAB))
            wx.CallLater(25, guarded(dispatch))

        def descendants(window):
            for child in window.GetChildren():
                yield child
                yield from descendants(child)

        def saved_copy():
            status = next((child for child in descendants(reader)
                           if child.GetName() == 'Speech and save status'), None)
            if status is None or 'saved for recovery' not in status.GetValue():
                return False
            saved = ReaderDraftStore(library.root, slot='native-appearance').load()
            check('native Ctrl+S retains the exact reading copy and source',
                  saved is not None and saved.document.text == original
                  and saved.document.source_text == original
                  and saved.document.source_sha256 == source_digest.hex())
            return True

        def drive():
            nonlocal stage, reader, baseline, snapshot, selection
            if finished or errors:
                return
            if monotonic() > deadline:
                raise TimeoutError('Native reader appearance deadline exceeded')
            if reader is None:
                reader = next((window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and bool(window)
                    and hasattr(window, 'reader_presentation')
                    and hasattr(window, 'reader_edit_host')
                    and window.IsModal() and window.IsShown()
                    and window.GetParent() == frame
                    and window.reader_edit_host.alive()
                    and window is not previous_reader), None)
            if reader is None:
                wx.CallLater(30, guarded(drive))
                return
            control = reader.reader_presentation
            host, editor = reader.reader_edit_host, reader.reader_editor
            view = control.view
            if stage == 0 and not host.busy():
                snapshot = host.capture()
                host.select(11, 21)
                selection = editor.GetSelection()
                baseline = editor.GetFont().GetFractionalPointSize()
                check('appearance opens on the actual editable reader', isinstance(editor, wx.TextCtrl)
                      and snapshot.text == original and not store.path.exists())
                stage = 1
                press(reader.appearance_button)
            elif stage == 1 and view is not None:
                stage = 2
                type_text(view.zoom, '200')
            elif stage == 2 and view is not None and view.zoom.GetValue() == 200:
                stage = 3
                type_text(view.foreground, '#FFFFFF')
            elif stage == 3 and view is not None and view.foreground.GetValue() == '#FFFFFF':
                stage = 4
                type_text(view.background, '#001122')
            elif stage == 4 and view is not None and view.background.GetValue() == '#001122' and view.apply_button.IsEnabled():
                check('preview changes its own native font without changing the reader',
                      abs(view.preview.GetFont().GetFractionalPointSize() - 2 * baseline) < .2
                      and abs(editor.GetFont().GetFractionalPointSize() - baseline) < .2
                      and editor.GetSelection() == selection and not store.path.exists())
                stage = 5
                press(view.cancel_button)
            elif stage == 5 and view is None:
                check('Cancel preserves text, selection and unsaved appearance',
                      host.capture() == snapshot and editor.GetSelection() == selection
                      and control.current == ReadingAppearance() and not store.path.exists())
                stage = 6
                press(reader.appearance_button)
            elif stage == 6 and view is not None:
                stage = 7
                type_text(view.zoom, '175')
            elif stage == 7 and view is not None and view.zoom.GetValue() == 175:
                stage = 8
                type_text(view.foreground, '#FFFFFF')
            elif stage == 8 and view is not None and view.foreground.GetValue() == '#FFFFFF':
                stage = 9
                type_text(view.background, '#001122')
            elif stage == 9 and view is not None and view.background.GetValue() == '#001122' and view.apply_button.IsEnabled():
                stage = 10
                press(view.apply_button)
            elif stage == 10 and view is None:
                check('Apply persists the exact explicit appearance', control.current == store.load() == chosen)
                check('Apply changes only the real reader presentation',
                      abs(editor.GetFont().GetFractionalPointSize() - 1.75 * baseline) < .2
                      and editor.GetForegroundColour() == wx.Colour('#FFFFFF')
                      and editor.GetBackgroundColour() == wx.Colour('#001122')
                      and host.capture() == snapshot and editor.GetSelection() == selection)
                stage = 101
                press(editor, ord('S'), wx.MOD_CONTROL)
            elif stage == 101 and saved_copy():
                stage = 11
                press(editor, wx.WXK_ESCAPE)
            elif stage == 12 and not host.busy():
                check('reopening selects a distinct live owned modal reader',
                      reader is not previous_reader and reader.IsModal()
                      and reader.IsShown() and reader.GetParent() == frame and host.alive())
                check('reopening restores the saved appearance on the actual reader',
                      control.current == chosen and store.load() == chosen
                      and abs(editor.GetFont().GetFractionalPointSize() - 1.75 * baseline) < .2
                      and host.capture().text == original)
                resolved = control._resolved(ReadingAppearance(
                    ReadingFont(editor.GetFont().GetFaceName(), 18, 700, 'italic'), zoom=125))
                check('actual installed font resolves weight, style and fractional zoom',
                      resolved[0].IsOk() and abs(resolved[0].GetFractionalPointSize() - 22.5) < .2
                      and resolved[0].GetNumericWeight() == 700
                      and resolved[0].GetStyle() == wx.FONTSTYLE_ITALIC)
                stage = 13
                press(reader.appearance_button)
            elif stage == 13 and view is not None:
                stage = 14
                press(view.reset_button)
            elif stage == 14 and view is not None and view.zoom.GetValue() == 100 and not view.foreground.GetValue():
                check('Reset previews native system appearance before acceptance',
                      not view.background.GetValue() and store.load() == chosen)
                stage = 15
                press(view.apply_button)
            elif stage == 15 and view is None:
                check('Reset applies and persists native defaults', control.current == store.load() == ReadingAppearance()
                      and abs(editor.GetFont().GetFractionalPointSize() - baseline) < .2)
                check('all original source bytes and text remain unchanged',
                      source.read_bytes() == original.encode('utf-8') and host.capture().text == original)
                stage = 151
                press(editor, ord('S'), wx.MOD_CONTROL)
            elif stage == 151 and saved_copy():
                stage = 16
                press(editor, wx.WXK_ESCAPE)
            wx.CallLater(30, guarded(drive))

        def open_reader():
            nonlocal reader, stage, finished, previous_reader
            if frame.working:
                wx.CallLater(30, guarded(open_reader))
                return
            wx.CallLater(30, guarded(drive))
            reader_module.show_speech_reader(frame, text=original, title='Appearance source',
                source_guard=source_guard, recovery_slot='native-appearance')
            if errors:
                app.ExitMainLoop()
                return
            check('first reader closes using native Escape', stage == 11)
            previous_reader = reader
            reader = None
            stage = 12
            reader_module.show_speech_reader(frame, text=original, title='Appearance source',
                source_guard=source_guard, recovery_slot='native-appearance')
            if not errors:
                check('reopened reader closes using native Escape', stage == 16)
                check('library source survives both appearance sessions', bool(library.search(workspace, 'Україна')))
            finished = True
            app.ExitMainLoop()

        try:
            frame = DesktopFrame(library)
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            wx.CallAfter(guarded(open_reader))
            app.MainLoop()
            if errors:
                raise AssertionError('\n'.join(errors))
        finally:
            if frame is not None:
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
                app.Yield()
            if handle is not None:
                handle.close()
    report.update(status='PASS', actual_native_keys=True, actual_native_editor=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    args = parser.parse_args()
    report = {'status': 'FAILED', 'checks': [], 'actual_native_keys': False,
              'actual_native_editor': False, 'manual_nvda': 'NOT_RUN',
              'font_dialog_human_qa': 'NOT_RUN', 'independent_deadline_seconds': 120}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    faulthandler.dump_traceback_later(120, exit=True)
    try:
        run(args, report)
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    main()
