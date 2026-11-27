"""Real Windows reading preparation controls, history and source preservation.

This probe uses the production reader, its shared worker and native keyboard
dispatch. One observer holds the actual preparation call to test cancellation
and destruction; it never supplies a replacement result. No speech/model or
NVDA quality is asserted.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback

def _installed_package_root(distributions, package_file, checkout):
    """Bind installed acceptance to the imported package's actual wheel record.

    A checkout egg-info entry may precede the wheel in metadata discovery.
    Its project-relative locate_file('carr_library') is not package evidence.
    Source acceptance never consults installed distribution metadata.
    """
    actual = Path(package_file).resolve(strict=True)
    checkout = Path(checkout).resolve(strict=True)
    if actual.is_relative_to(checkout):
        raise RuntimeError('Installed acceptance imported the checkout package.')
    matches = []
    for distribution in distributions:
        for member in distribution.files or ():
            if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                continue
            try:
                declared = Path(distribution.locate_file(member)).resolve(strict=True)
            except FileNotFoundError:
                continue  # An unrelated stale metadata entry is not this package.
            if declared == actual:
                matches.append(declared.parent)
    if len(matches) != 1:
        raise RuntimeError('Exactly one installed wheel record must own the imported package.')
    return matches[0]



def _write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n',
                    encoding='utf-8')


def _run_probe(args, report, checkpoint):
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.keybindings import KeybindingStore, validate_bindings
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.ui.productivity import KeyboardActions, collect_commands
    from carr_library.ui import speech_dialog, reader_preparation
    import carr_library.speech.preparation
    import carr_library.speech.reading
    import carr_library.speech.reading_tools
    import carr_library.ui.reading_tools
    import carr_library.ui.keyboard_settings

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        if name not in report['checks']:
            report['checks'].append(name)
        checkpoint(name)

    checkout = Path(__file__).resolve(strict=True).parents[1]
    expected_root = checkout / 'src' / 'carr_library'
    if args.source is not None:
        check('explicit source directory is this exact candidate',
              args.source.resolve(strict=True) == checkout / 'src')
        actual_root = expected_root
        check('actual source package',
              Path(carr_library.__file__).resolve(strict=True) == actual_root / '__init__.py')
    else:
        actual_root = _installed_package_root(
            importlib.metadata.distributions(), carr_library.__file__, checkout)
    module_names = (
        'carr_library.speech.preparation', 'carr_library.speech.reading',
        'carr_library.speech.reading_tools', 'carr_library.speech.session',
        'carr_library.ui.reader_preparation', 'carr_library.ui.reading_tools',
        'carr_library.ui.speech_dialog', 'carr_library.ui.keyboard_settings',
        'carr_library.ui.productivity', 'carr_library.keybindings')
    for name in module_names:
        actual = Path(sys.modules[name].__file__).resolve(strict=True)
        relative = Path(*name.split('.')[1:]).with_suffix('.py')
        check('exact module origin: ' + name, actual == actual_root / relative)
        digest = sha256(actual.read_bytes()).hexdigest()
        check('exact candidate module: ' + name,
              digest == sha256((expected_root / relative).read_bytes()).hexdigest())
        report['source_sha256'][name] = digest
    check('actual wxMSW controls', wx.Platform == '__WXMSW__')

    application = wx.App(False)
    application.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    simulator = wx.UIActionSimulator()

    def pump(predicate, timeout=10):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            application.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def settle():
        deadline = monotonic() + .10
        while monotonic() < deadline:
            application.Yield()
            wx.MilliSleep(10)

    def key(code, modifiers=wx.MOD_NONE):
        if not simulator.Char(code, modifiers):
            raise RuntimeError('Native keyboard dispatch is unavailable.')
        settle()

    def focus(control):
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() == control, timeout=3):
            raise AssertionError('Native control did not receive focus: ' + control.GetName())

    def click(control):
        if not control.IsEnabled():
            raise AssertionError('Native control is disabled: ' + control.GetName())
        focus(control)
        key(wx.WXK_SPACE)

    def descendants(window):
        pending = list(window.GetChildren())
        while pending:
            child = pending.pop(0)
            yield child
            pending.extend(child.GetChildren())

    def button(window, label):
        rows = [child for child in descendants(window)
                if isinstance(child, wx.Button) and child.GetLabel().replace('&', '') == label]
        if len(rows) != 1:
            raise AssertionError('Expected one native button: ' + label)
        return rows[0]

    with TemporaryDirectory(prefix='carr-native-reading-preparation-') as temporary:
        root = Path(temporary)
        raw = '😀 12 + 3.\r\n2024-02-29.\r\nThe original remains unchanged.'
        original = root / 'original.txt'
        original.write_bytes(raw.encode('utf-8'))
        source_hash = sha256(original.read_bytes()).hexdigest()
        library = LibraryApp(root / 'library')
        frame = wx.Frame(None, title='Native reading preparation fixture', size=(1000, 850))
        frame.library = library
        frame.report = lambda message: None
        menu, bar = wx.Menu(), wx.MenuBar()
        menu.Append(wx.ID_ANY, 'Open fixture\tCtrl+O')
        bar.Append(menu, '&Fixture')
        frame.SetMenuBar(bar)
        frame.menu_bar = bar
        frame.keyboard_actions = KeyboardActions(frame, collect_commands(bar))
        frame.keyboard_actions.reload()
        frame.Show()
        frame.Raise()
        allowed = [True]
        failures = []
        seen_readers = []
        expected = [raw]
        slot = 'native-reading-preparation'
        original_prepare = reader_preparation.preview_preparation
        pending_releases = []

        def source_guard():
            if not allowed[0]:
                raise ValueError('The reading fixture source was revoked.')
            if sha256(original.read_bytes()).hexdigest() != source_hash:
                raise ValueError('The exact original fixture bytes changed.')

        def focus_editor(reader):
            reader.Raise()
            focus(reader.reader_editor)
            return reader.reader_edit_host

        def with_tool(reader, scenario, *, shortcut=False):
            completed, nested_errors, seen = [], [], []
            deadline = monotonic() + 10
            def drive():
                rows = [window for window in wx.GetTopLevelWindows()
                        if hasattr(window, 'reader_preparation')
                        and not window.reader_preparation.destroyed
                        and window.IsModal() and window.IsShown()
                        and window.GetParent() == reader]
                if not rows:
                    if monotonic() < deadline:
                        wx.CallLater(20, drive)
                        return
                    nested_errors.append(AssertionError('Actual reading preparation dialog did not open.'))
                    completed.append(True)
                    return
                view = rows[0].reader_preparation
                seen.append(view)
                try:
                    scenario(view)
                except BaseException as exc:
                    traceback.print_exc()
                    nested_errors.append(exc)
                finally:
                    if not view.destroyed and not view.session.closed:
                        try:
                            click(view.close_button)
                        except BaseException as exc:
                            nested_errors.append(exc)
                            if not view.destroyed:
                                view.dialog.EndModal(wx.ID_CANCEL)
                    completed.append(True)
            wx.CallLater(20, drive)
            if shortcut:
                focus_editor(reader)
                key(ord('P'), wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT)
            else:
                click(reader.reader_preparation_button)
            if not pump(lambda: bool(completed), timeout=12):
                raise AssertionError('The nested preparation scenario did not finish.')
            if nested_errors:
                raise nested_errors[0]
            check('one actual modal preparation instance per invocation', len(seen) == 1)

        def choose(view, language_index, *flags):
            focus(view.language)
            key(wx.WXK_HOME)
            for _ in range(language_index):
                key(wx.WXK_DOWN)
            check('native language choice is explicit', view.language.GetSelection() == language_index)
            for name in flags:
                click(view.options[name])
                check('native option selected: ' + name, view.options[name].GetValue() is True)

        def preview(view):
            click(view.preview_button)
            check('actual preparation returns through the existing reader worker',
                  pump(lambda: not view.session.busy and view.session.preview is not None, timeout=8))

        def save(reader, text):
            host = focus_editor(reader)
            key(ord('S'), wx.MOD_CONTROL)
            status = next(child for child in descendants(reader)
                          if child.GetName() == 'Speech and save status')
            check('native Save completes before reader Close',
                  pump(lambda: not host.busy() and 'saved' in status.GetValue().lower(), timeout=10))
            recovered = ReaderDraftStore(library.root, slot=slot).load()
            check('durable copy retains the exact source and accepted derivative',
                  recovered is not None and recovered.document.text == text
                  and recovered.document.source_text == raw
                  and recovered.document.source_sha256 == source_hash)

        def first(reader):
            host = focus_editor(reader)
            check('initial voice inventory completes without synthesis',
                  pump(lambda: not host.busy(), timeout=35))
            check('opening the reader preserves exact Unicode source',
                  host.capture().text == raw and host.capture().source_sha256 == source_hash)
            start, end = raw.index('12'), raw.index('3.') + 1
            host.select(start, end)
            accepted = raw[:start] + 'один два плюс три' + raw[end:]

            def apply_case(view):
                check('language starts unselected and every transformation starts off',
                      view.language.GetSelection() == 0
                      and not any(field.GetValue() for field in view.options.values())
                      and not view.preview_button.IsEnabled() and not view.apply_button.IsEnabled())
                check('captured native selection is the initial explicit scope',
                      view.selected_only.GetValue() and view.scope == (start, end))
                choose(view, 2, 'numbers', 'symbols')
                preview(view)
                check('Preview alone does not edit the reader or original',
                      host.capture().text == raw and original.read_bytes() == raw.encode('utf-8'))
                check('proposed Ukrainian digit and symbol text is visible',
                      'один два плюс три' in view.preview_text.GetValue())
                click(view.reject_button)
                check('Reject discards only the proposal',
                      view.session.preview is None and host.capture().text == raw)
                preview(view)
                click(view.apply_button)
                check('explicit Apply changes only the selected reading derivative',
                      host.capture().text == accepted and host.capture().source_sha256 == source_hash)
            with_tool(reader, apply_case)
            focus_editor(reader)
            key(ord('Z'), wx.MOD_CONTROL)
            check('one real native Undo restores the accepted transformation',
                  host.capture().text == raw and host.capture().source_sha256 == source_hash)

            date_start, date_end = raw.index('2024'), raw.index('2024') + 10
            host.select(date_start, date_end)
            date_words = 'rok dva nula dva štyri, mesiac nula dva, deň dva deväť'
            expected[0] = raw[:date_start] + date_words + raw[date_end:]
            def date_case(view):
                choose(view, 3, 'iso_dates')
                preview(view)
                check('Slovak ISO preview names all three fields explicitly',
                      date_words in view.preview_text.GetValue() and host.capture().text == raw)
                click(view.apply_button)
                check('ISO acceptance retains both leading digit zeros and punctuation',
                      host.capture().text == expected[0])
            with_tool(reader, date_case)

            host.select(start, end)
            def revoked_case(view):
                choose(view, 1, 'numbers')
                preview(view)
                before = reader.reader_editor.GetValue()
                allowed[0] = False
                try:
                    click(view.apply_button)
                    check('revoked source denies Apply through the existing reader guard',
                          'revoked' in view.status.GetValue().lower()
                          and reader.reader_editor.GetValue() == before)
                finally:
                    allowed[0] = True
                check('denied Apply leaves accepted text and original source intact',
                      host.capture().text == expected[0])
            with_tool(reader, revoked_case)

            def held_case(view, destroy):
                choose(view, 1, 'numbers')
                entered, release = threading.Event(), threading.Event()
                pending_releases.append(release)
                threads = []
                gui_thread = threading.get_ident()
                def held(*values, **kwargs):
                    threads.append(threading.get_ident())
                    entered.set()
                    if not release.wait(6):
                        raise TimeoutError('The native preparation observer was not released.')
                    return original_prepare(*values, **kwargs)
                reader_preparation.preview_preparation = held
                try:
                    click(view.preview_button)
                    check('the real preparation callback enters only on the existing worker',
                          pump(entered.is_set, timeout=5)
                          and threads and all(item != gui_thread for item in threads))
                    if destroy:
                        view.dialog.Destroy()
                    else:
                        click(view.cancel_button)
                        check('native Cancel marks the outstanding preparation',
                              view.session.cancelled.is_set())
                    release.set()
                    check('held work finishes before the reader scenario continues',
                          pump(lambda: not host.busy(), timeout=8))
                    check('cancelled or destroyed preview cannot apply text',
                          host.capture().text == expected[0]
                          and original.read_bytes() == raw.encode('utf-8'))
                    if destroy:
                        check('direct native destruction revokes queued completion',
                              view.destroyed and view.session.closed)
                    else:
                        check('cancelled preview is not available for Apply',
                              view.session.preview is None and not view.apply_button.IsEnabled())
                finally:
                    release.set()
                    reader_preparation.preview_preparation = original_prepare
            with_tool(reader, lambda view: held_case(view, False))
            with_tool(reader, lambda view: held_case(view, True))

            saved = KeybindingStore(library.root).load()
            overrides = {**saved, 'voice_reader.prepare': 'Ctrl+Alt+Shift+P'}
            validate_bindings(frame.keyboard_actions.commands, overrides)
            frame.keyboard_actions.save(overrides)
            check('preparation uses the existing one keyboard preference store',
                  KeybindingStore(library.root).load()['voice_reader.prepare'] == 'Ctrl+Alt+Shift+P')
            with_tool(reader, lambda view: check('custom native shortcut opens preparation with no implicit options',
                      view.language.GetSelection() == 0 and not any(field.GetValue() for field in view.options.values())),
                      shortcut=True)
            save(reader, expected[0])
            click(button(reader, 'Close'))

        def reopened(reader):
            host = focus_editor(reader)
            check('reopened reader inventory completes', pump(lambda: not host.busy(), timeout=35))
            check('saved prepared copy reopens with unchanged original identity',
                  host.capture().text == expected[0] and host.capture().source_sha256 == source_hash)
            with_tool(reader, lambda view: check('language and flags remain explicit after reopen',
                      view.language.GetSelection() == 0 and not any(field.GetValue() for field in view.options.values())),
                      shortcut=True)
            save(reader, expected[0])
            click(button(reader, 'Close'))

        def launch(text, scenario):
            deadline = monotonic() + 10
            finished = []
            def drive():
                rows = [window for window in wx.GetTopLevelWindows()
                        if hasattr(window, 'reader_edit_host')
                        and window not in seen_readers and window.IsModal() and window.IsShown()
                        and window.reader_edit_host.alive()]
                if not rows:
                    if monotonic() < deadline:
                        wx.CallLater(20, drive)
                        return
                    failures.append(AssertionError('The current live Voice reader did not open.'))
                    return
                reader = rows[0]
                seen_readers.append(reader)
                try:
                    scenario(reader)
                    finished.append(True)
                except BaseException as exc:
                    failures.append(exc)
                    traceback.print_exc()
                    # A failed synthetic scenario must not open an unsaved-copy question.
                    if reader.reader_edit_host.alive() and reader.IsModal():
                        reader.EndModal(wx.ID_CANCEL)
            wx.CallLater(20, drive)
            speech_dialog.show_speech_reader(frame, text=text, source_guard=source_guard, recovery_slot=slot)
            if failures:
                raise failures[0]
            check('actual reader modal completes after its worker cleanup', bool(finished))

        try:
            launch(raw, first)
            launch('', reopened)
            check('all original bytes survive both complete native reader sessions',
                  original.read_bytes() == raw.encode('utf-8'))
            report.update(wx=wx.version(), native_keys='wx.UIActionSimulator',
                          source_override=args.source is not None,
                          speech_inference='NOT_RUN', manual_nvda='NOT_RUN')
        finally:
            for release in pending_releases:
                release.set()
            reader_preparation.preview_preparation = original_prepare
            frame.Destroy()
            application.Yield()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source', type=Path)
    mode.add_argument('--expect-installed', '--installed', dest='installed', action='store_true')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    report = {'result': 'FAIL', 'failed_stage': 'incomplete', 'checks': [],
              'errors': [], 'source_sha256': {}, 'manual_nvda': 'NOT_RUN',
              'speech_inference': 'NOT_RUN'}
    def checkpoint(stage):
        report['failed_stage'] = stage
        _write_report(args.report, report)
        print('READING_PREPARATION_PHASE ' + stage, flush=True)
    checkpoint('starting')
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run the native preparation probe with Windows Python -I -B.')
    faulthandler.enable()
    faulthandler.dump_traceback_later(180, exit=True)
    handle = None
    code = 1
    try:
        if args.source is not None:
            sys.path.insert(0, str(args.source.resolve(strict=True)))
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        _run_probe(args, report, checkpoint)
        report.update(result='PASS', failed_stage=None)
        code = 0
    except BaseException:
        detail = traceback.format_exc()
        report['errors'].append(detail)
        print(detail, file=sys.stderr, flush=True)
    finally:
        try:
            if handle is not None:
                handle.close()
        except BaseException:
            code = 1
            report.update(result='FAIL', failed_stage='native runtime cleanup')
            report['errors'].append(traceback.format_exc())
        finally:
            _write_report(args.report, report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            faulthandler.cancel_dump_traceback_later()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
