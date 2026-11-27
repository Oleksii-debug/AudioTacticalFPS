"""Native Voice reader find/replace and pronunciation acceptance.

Uses actual wx controls, OS keyboard events, the isolated production regex child,
and real reading/dictionary persistence. Only external FileDialog answers use
fixed temporary fixture paths. No speech quality, audio endpoint or NVDA claim.
"""
from argparse import ArgumentParser
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def run_native(source, report, checkpoint):
    import wx
    from carr_library.application import LibraryApp
    from carr_library.keybindings import KeybindingStore, validate_bindings
    from carr_library.speech.reading import import_dictionary
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.speech import _io
    from carr_library.ui.productivity import KeyboardActions, collect_commands
    from carr_library.ui import speech_dialog
    import carr_library.speech.reading_tools
    import carr_library.ui.reading_tools
    import carr_library.ui.keyboard_settings

    application = wx.App(False)
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    simulator = wx.UIActionSimulator()
    checks = report['checks']
    module_names = ('carr_library.speech.reading', 'carr_library.speech.reading_tools',
                    'carr_library.ui.reading_tools', 'carr_library.ui.speech_dialog',
                    'carr_library.ui.keyboard_settings', 'carr_library.ui.productivity',
                    'carr_library.keybindings')
    origins = {name: str(Path(sys.modules[name].__file__).resolve()) for name in module_names}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        checkpoint(name)

    if source is None:
        from importlib.metadata import distribution
        installed = Path(distribution('universal-research-library').locate_file('carr_library')).resolve(strict=True)
        check('all seven reading-tool and keymap modules load from the installed package',
              all(Path(path).is_relative_to(installed) for path in origins.values()))

    def pump(predicate, timeout=10):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            application.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def settle(seconds=.15):
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            application.Yield()
            wx.MilliSleep(10)

    def descendants(window):
        pending = list(window.GetChildren())
        while pending:
            value = pending.pop(0)
            yield value
            pending.extend(value.GetChildren())

    def button(window, label):
        matches = [value for value in descendants(window) if isinstance(value, wx.Button)
                   and value.GetLabel().replace('&', '') == label]
        if len(matches) != 1:
            raise AssertionError(f'Expected one actual native button {label!r}, found {len(matches)}.')
        return matches[0]

    def click(control):
        if not control.IsEnabled():
            raise AssertionError('The actual native control is disabled: ' + control.GetName())
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() == control, timeout=3):
            raise AssertionError('The actual native control did not receive focus: ' + control.GetName())
        if not simulator.Char(wx.WXK_SPACE):
            raise RuntimeError('Native button keyboard dispatch is unavailable.')
        settle()

    def key(code, modifiers=wx.MOD_NONE):
        if not simulator.Char(code, modifiers):
            raise RuntimeError('Native key dispatch is unavailable.')
        settle()

    with TemporaryDirectory(prefix='carr-native-reading-tools-') as temporary:
        root = Path(temporary)
        raw = '😀 Alpha A12\r\nAlpha B34\r\ncolour Kyiv.\r\nEnd.'
        original = root / 'source.txt'
        original.write_bytes(raw.encode('utf-8'))
        source_hash = sha256(original.read_bytes()).hexdigest()
        library = LibraryApp(root / 'library')
        frame = wx.Frame(None, title='Reading tools native fixture', size=(1000, 850))
        frame.library = library
        frame.report = lambda message: None
        menu, menu_bar = wx.Menu(), wx.MenuBar()
        menu.Append(wx.ID_ANY, 'Host open fixture\tCtrl+O')
        menu_bar.Append(menu, '&Fixture')
        frame.SetMenuBar(menu_bar)
        frame.menu_bar = menu_bar
        frame.keyboard_actions = KeyboardActions(frame, collect_commands(menu_bar))
        frame.keyboard_actions.reload()
        frame.Show()
        frame.Raise()
        allowed = [True]
        failures = []
        slot = 'native-reading-tools'
        dictionary_path = root / 'pronunciation.json'
        active = {}
        original_file_dialog = wx.FileDialog
        original_popen = _io.subprocess.Popen

        def guard():
            if not allowed[0]:
                raise ValueError('The retained source scope was revoked for this test.')
            if sha256(original.read_bytes()).hexdigest() != source_hash:
                raise ValueError('The original fixture source changed.')

        def focus_editor(reader):
            reader.Raise()
            editor = reader.reader_editor
            editor.SetFocus()
            if not pump(lambda: wx.Window.FindFocus() == editor, timeout=3):
                raise AssertionError('The Voice reader editor did not receive native focus.')
            return editor

        def with_tools(reader, code, modifiers, scenario):
            completed, seen, nested_errors = [], [], []
            deadline = monotonic() + 8
            def drive():
                tools = [window for window in wx.GetTopLevelWindows()
                         if hasattr(window, 'reading_tools') and window.IsModal()]
                if not tools:
                    if monotonic() < deadline:
                        wx.CallLater(20, drive)
                        return
                    nested_errors.append(AssertionError('Native reader shortcut did not open the real tools dialog.'))
                    completed.append(True)
                    return
                view = tools[0].reading_tools
                seen.append(view)
                try:
                    scenario(view)
                except BaseException as exc:
                    nested_errors.append(exc)
                finally:
                    if not view.destroyed and not view.session.closed:
                        try:
                            click(view.close_button)
                        except BaseException as exc:
                            nested_errors.append(exc)
                            if not view.destroyed:
                                view.close()
                    completed.append(True)
            focus_editor(reader)
            wx.CallLater(20, drive)
            key(code, modifiers)
            if not pump(lambda: bool(completed), timeout=10):
                raise AssertionError('Native reading tools scenario did not finish.')
            if nested_errors:
                raise nested_errors[0]
            if len(seen) != 1:
                raise AssertionError('Exactly one production reading tools dialog was required.')

        def wait_work(view):
            check('bounded reading operation returns through the existing reader worker',
                  pump(lambda: not view.session.busy, timeout=8))

        class FixtureChooser:
            def __init__(self, *args, **kwargs):
                self.path = dictionary_path
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def ShowModal(self):
                return wx.ID_OK
            def GetPath(self):
                return str(self.path)

        def first_reader(reader):
            editor, host = focus_editor(reader), reader.reader_edit_host
            check('initial local voice inventory finishes without starting speech',
                  pump(lambda: not host.busy(), timeout=35))
            initial = host.capture()
            check('native CRLF and non-BMP source checksum is retained exactly',
                  initial.source_sha256 == source_hash and original.read_bytes() == raw.encode('utf-8'))
            host.select(0, 0)
            def find_case(view):
                view.pattern.SetValue('Alpha')
                view.whole_word.SetValue(True)
                click(button(view.dialog, 'Find next'))
                wait_work(view)
                first = initial.text.index('Alpha')
                check('real Ctrl+F and Find Next select the exact Unicode fragment',
                      host.selection() == (first, first + 5) and editor.GetStringSelection() == 'Alpha')
                click(button(view.dialog, 'Find next'))
                wait_work(view)
                second = initial.text.index('Alpha', first + 5)
                check('Find Next advances beyond the current match',
                      host.selection() == (second, second + 5))
                click(button(view.dialog, 'Find previous'))
                wait_work(view)
                check('Find Previous returns to the earlier exact native selection',
                      host.selection() == (first, first + 5))
                view.wrap.SetValue(False)
                click(button(view.dialog, 'Find previous'))
                wait_work(view)
                check('disabled wrap does not jump to the final match',
                      host.selection() == (first, first + 5) and 'No matching' in view.status.GetValue())
            with_tools(reader, ord('F'), wx.MOD_CONTROL, find_case)

            before = editor.GetValue()
            host.select(0, 0)
            def replace_case(view):
                view.pattern.SetValue(r'([AB])(\d+)')
                view.replacement.SetValue(r'\2-\1')
                view.regex.SetValue(True)
                click(view.preview_button)
                wait_work(view)
                check('actual isolated regex backreferences preview both matches without editing',
                      view.session.preview is not None and view.session.preview.count == 2
                      and editor.GetValue() == before)
                click(view.reject_button)
                check('Reject clears the preview and leaves the reading copy unchanged',
                      view.session.preview is None and editor.GetValue() == before)
                click(button(view.dialog, 'Find next'))
                wait_work(view)
                click(button(view.dialog, 'Preview replace one'))
                wait_work(view)
                check('Replace One previews only the exact current native selection',
                      view.session.preview is not None and view.session.preview.count == 1)
                click(view.apply_button)
                check('explicit Apply changes one reading fragment',
                      editor.GetValue() == before.replace('A12', '12-A', 1))
            with_tools(reader, ord('H'), wx.MOD_CONTROL, replace_case)
            click(button(reader, 'Undo reading edit'))
            check('Undo restores the complete accepted replacement and source identity',
                  editor.GetValue() == before and host.capture().source_sha256 == source_hash)

            def all_case(view):
                view.pattern.SetValue(r'([AB])(\d+)')
                view.replacement.SetValue(r'\2-\1')
                view.regex.SetValue(True)
                click(view.preview_button)
                wait_work(view)
                click(view.apply_button)
            with_tools(reader, ord('H'), wx.MOD_CONTROL, all_case)
            expected = before.replace('A12', '12-A').replace('B34', '34-B')
            check('Replace All applies both reviewed matches in one reading operation',
                  editor.GetValue() == expected)
            click(button(reader, 'Undo reading edit'))
            check('one Undo restores the whole Replace All operation', editor.GetValue() == before)
            click(button(reader, 'Redo reading edit'))
            check('Redo restores the complete accepted reading operation', editor.GetValue() == expected)

            def revoked_case(view):
                view.pattern.SetValue('End')
                view.replacement.SetValue('forbidden')
                click(view.preview_button)
                wait_work(view)
                allowed[0] = False
                try:
                    click(view.apply_button)
                    check('source revocation between preview and Apply blocks the edit',
                          editor.GetValue() == expected and 'revoked' in view.status.GetValue())
                finally:
                    allowed[0] = True
            with_tools(reader, ord('H'), wx.MOD_CONTROL, revoked_case)

            def pronunciation_case(view):
                view.language.ChangeValue('')
                view.voice_id.ChangeValue('')
                view.pattern.SetValue('Kyiv')
                view.replacement.SetValue('Київ')
                click(button(view.dialog, 'Add rule'))
                view.pattern.SetValue('colour')
                view.replacement.SetValue('color')
                click(button(view.dialog, 'Add rule'))
                check('native pronunciation manager retains two literal scoped rules', len(view.rules) == 2)
                click(button(view.dialog, 'Move rule up'))
                check('rule reordering changes only the reviewed dictionary', view.rules[0].pattern == 'colour')
                wx.FileDialog = FixtureChooser
                try:
                    click(button(view.dialog, 'Export dictionary...'))
                    wait_work(view)
                    check('existing dictionary schema is written to a create-only actual file',
                          len(import_dictionary(dictionary_path)) == 2)
                    exported = dictionary_path.read_bytes()
                    click(button(view.dialog, 'Remove selected rule'))
                    check('Remove changes only the manager until a new preview', len(view.rules) == 1)
                    click(button(view.dialog, 'Import dictionary...'))
                    wait_work(view)
                    check('actual dictionary import restores rules without editing the copy',
                          len(view.rules) == 2 and editor.GetValue() == expected)
                    click(button(view.dialog, 'Export dictionary...'))
                    wait_work(view)
                    check('a repeated export cannot overwrite an occupied dictionary',
                          dictionary_path.read_bytes() == exported and 'did not complete' in view.status.GetValue())
                finally:
                    wx.FileDialog = original_file_dialog
                click(view.preview_button)
                wait_work(view)
                check('pronunciation preview remains a separate unapplied reading layer',
                      view.session.preview is not None and editor.GetValue() == expected)
                click(view.apply_button)
                check('explicit pronunciation Apply preserves original source and updates only the copy',
                      editor.GetValue() == expected.replace('colour', 'color').replace('Kyiv', 'Київ')
                      and host.capture().source_sha256 == source_hash)
            with_tools(reader, ord('D'), wx.MOD_CONTROL | wx.MOD_SHIFT, pronunciation_case)
            click(button(reader, 'Undo reading edit'))
            check('one Undo reverses the entire accepted pronunciation layer', editor.GetValue() == expected)
            click(button(reader, 'Redo reading edit'))
            expected = expected.replace('colour', 'color').replace('Kyiv', 'Київ')
            check('pronunciation Redo retains reviewed changes', editor.GetValue() == expected)

            # Observe the real production regex child without altering its argv,
            # environment, input, cancellation or output implementation.
            children = []
            def tracked_popen(command, *args, **kwargs):
                child = original_popen(command, *args, **kwargs)
                if isinstance(command, (list, tuple)) and command and Path(command[-1]).name == 'regex_worker.py':
                    children.append(child)
                return child
            _io.subprocess.Popen = tracked_popen
            try:
                for destroy in (False, True):
                    editor.ChangeValue('a' * 30000 + '!')
                    admitted = len(children)
                    def cancel_case(view):
                        view.pattern.SetValue(r'(a+)+$')
                        view.replacement.SetValue('never')
                        view.regex.SetValue(True)
                        click(view.preview_button)
                        check('native cancellation waits for an actual live isolated regex child',
                              pump(lambda: len(children) > admitted and children[-1].poll() is None, timeout=3))
                        if destroy:
                            view.dialog.Destroy()
                            check('forced native tool destruction marks the owner and cancels its session',
                                  pump(lambda: view.destroyed, timeout=3)
                                  and view.session.closed and view.session.cancelled.is_set())
                        else:
                            click(view.cancel_button)
                            wait_work(view)
                            check('Cancel interrupts the real child without applying a proposed edit',
                                  view.session.preview is None and editor.GetValue() == 'a' * 30000 + '!')
                    with_tools(reader, ord('H'), wx.MOD_CONTROL, cancel_case)
                    check('reader worker and actual child finish before the next operation',
                          pump(lambda: not host.busy() and all(child.poll() is not None
                                                              for child in children[admitted:]), timeout=8))
                    editor.ChangeValue(expected)
                    check('cancelled and destroyed tool results cannot change the restored reading copy',
                          host.capture().text == expected and host.capture().source_sha256 == source_hash)
            finally:
                _io.subprocess.Popen = original_popen

            saved = KeybindingStore(library.root).load()
            frame.keyboard_actions.save({**saved, 'voice_reader.find': 'Ctrl+Shift+J',
                                         'voice_reader.replace': ''})
            stored = KeybindingStore(library.root).path.read_bytes()
            try:
                frame.keyboard_actions.save({**saved, 'voice_reader.find': 'F8'})
            except ValueError:
                pass
            else:
                raise AssertionError('A collision with the reader Stop command was accepted.')
            check('shared keymap rejects an in-context collision without altering saved preferences',
                  KeybindingStore(library.root).path.read_bytes() == stored)
            for reserved in ('Ctrl+A', 'Ctrl+Left', 'NVDA+F3', 'Insert', 'CapsLock'):
                try:
                    validate_bindings(frame.keyboard_actions.commands, {'voice_reader.find': reserved})
                except ValueError:
                    continue
                raise AssertionError('Reserved editing or screen-reader key accepted: ' + reserved)
            check('typing and screen-reader prefixes remain unassigned by the reader', True)
            with_tools(reader, ord('J'), wx.MOD_CONTROL | wx.MOD_SHIFT,
                       lambda view: check('custom saved key opens the real reading tools dialog',
                                          not view.pronunciation))
            focus_editor(reader)
            key(ord('A'), wx.MOD_CONTROL)
            check('ordinary native Ctrl+A still selects the reading text',
                  editor.GetStringSelection() == editor.GetValue())
            host.select(len(expected), len(expected))
            key(ord('x'))
            check('ordinary native typing remains available', editor.GetValue() == expected + 'x')
            key(ord('Z'), wx.MOD_CONTROL)
            check('native text Undo remains available', editor.GetValue() == expected)
            focus_editor(reader)
            allowed[0] = False
            try:
                key(wx.WXK_F8)
                status = next(value for value in descendants(reader)
                              if value.GetName() == 'Speech and save status')
                check('Stop remains available after source revocation',
                      'Stopping synthesis or playback' in status.GetValue())
            finally:
                allowed[0] = True
            focus_editor(reader)
            key(ord('S'), wx.MOD_CONTROL)
            recovered = ReaderDraftStore(library.root, slot=slot).load()
            check('real Ctrl+S persists the reviewed copy and exact original source',
                  recovered is not None and recovered.document.text == expected
                  and recovered.document.source_text == raw and recovered.document.source_sha256 == source_hash)
            active['expected'] = expected
            click(button(reader, 'Close'))

        def reopened_reader(reader):
            editor, host = focus_editor(reader), reader.reader_edit_host
            check('reopened reader finishes its local inventory', pump(lambda: not host.busy(), timeout=35))
            check('reopen restores saved pronunciation edits and original source identity',
                  editor.GetValue() == active['expected'] and host.capture().source_sha256 == source_hash)
            with_tools(reader, ord('J'), wx.MOD_CONTROL | wx.MOD_SHIFT,
                       lambda view: check('stored Voice reader binding survives reopen', not view.pronunciation))
            focus_editor(reader)
            key(ord('F'), wx.MOD_CONTROL)
            check('remapped default no longer opens a competing tools dialog',
                  not any(hasattr(window, 'reading_tools') and window.IsModal()
                          for window in wx.GetTopLevelWindows()))
            click(button(reader, 'Save reading copy'))
            click(button(reader, 'Close'))

        def launch(text, scenario):
            deadline = monotonic() + 10
            def drive():
                candidates = [window for window in wx.GetTopLevelWindows()
                              if hasattr(window, 'reader_edit_host') and window.IsModal()]
                if not candidates:
                    if monotonic() < deadline:
                        wx.CallLater(20, drive)
                        return
                    failures.append(AssertionError('Production Voice reader did not open.'))
                    return
                reader = candidates[0]
                try:
                    scenario(reader)
                except BaseException as exc:
                    failures.append(exc)
                    traceback.print_exc()
                    if reader.IsModal():
                        reader.EndModal(wx.ID_CANCEL)
            wx.CallLater(20, drive)
            speech_dialog.show_speech_reader(frame, text=text, source_guard=guard, recovery_slot=slot)
            if failures:
                raise failures[0]

        try:
            launch(raw, first_reader)
            launch('', reopened_reader)
            check('original source bytes are unchanged after both real reader sessions',
                  original.read_bytes() == raw.encode('utf-8'))
            report.update(wx=wx.version(), origins=origins,
                          module_sha256={name: sha256(Path(path).read_bytes()).hexdigest()
                                         for name, path in origins.items()},
                          source_override=source is not None, native_keys='wx.UIActionSimulator',
                          speech_inference='NOT_RUN', manual_nvda='NOT_RUN')
        finally:
            wx.FileDialog = original_file_dialog
            _io.subprocess.Popen = original_popen
            frame.Destroy()
            application.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    args = parser.parse_args()
    report = {'status': 'FAIL', 'phase': 'incomplete', 'checks': [], 'manual_nvda': 'NOT_RUN'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def checkpoint(phase):
        report['phase'] = phase
        args.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n',
                               encoding='utf-8')
        print('READING_TOOLS_PHASE ' + phase, flush=True)
    checkpoint('starting')
    if sys.platform != 'win32' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run the native reading-tools probe with Windows Python -I -B.')
    if args.expect_installed and args.source is not None:
        raise ValueError('Installed acceptance must not inject checkout source.')
    faulthandler.dump_traceback_later(180, repeat=False, exit=True)
    handle = None
    try:
        source = args.source.resolve(strict=True) if args.source is not None else None
        if source is not None:
            sys.path.insert(0, str(source))
            sys.path.insert(0, str(source / 'src'))
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        run_native(source, report, checkpoint)
        report['status'] = 'PASS'
        checkpoint('complete')
        return 0
    except BaseException:
        report['failure'] = traceback.format_exc()[-32000:]
        checkpoint('failed')
        raise
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
