"""Actual native manual reading tabs, shared Save All and optional visual pacing.

The explicit empty voice inventory keeps this acceptance about text/recovery,
keyboard dispatch and native controls. It does not qualify speech inference,
measured-audio playback, word alignment or manual NVDA use.
"""
from argparse import ArgumentParser
import faulthandler
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback


def run_native(source, report, checkpoint):
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range
    from carr_library.speech import VoiceRegistry
    from carr_library.speech.reading import ReadingDocument
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    from carr_library.speech.tabs import ReadingTabsStore
    from carr_library.ui.productivity import KeyboardActions, collect_commands
    from carr_library.ui.save_all import SaveCoordinator
    from carr_library.ui import speech_dialog
    import carr_library.runtime_settings as settings
    import carr_library.speech.tabs as tabs_module

    application = wx.App(False)
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    simulator = wx.UIActionSimulator()
    gui_thread = threading.get_ident()
    deadline = monotonic() + 130
    checks = report['checks']
    names = ('carr_library.durable_state', 'carr_library.speech.reading',
        'carr_library.speech.session', 'carr_library.speech.tabs', 'carr_library.speech.focus',
        'carr_library.speech.reading_tools', 'carr_library.ui.reader_tabs',
        'carr_library.ui.reading_focus', 'carr_library.ui.speech_dialog',
        'carr_library.ui.reader_presentation', 'carr_library.ui.keyboard_settings',
        'carr_library.ui.save_all', 'carr_library.ui.productivity')
    modules = [importlib.import_module(name) for name in names]
    origins = {module.__name__: Path(module.__file__).resolve(strict=True) for module in modules}
    checkout = Path(__file__).resolve().parents[1]
    from importlib.metadata import distribution
    installed = Path(distribution('universal-research-library').locate_file('carr_library')).resolve(strict=True)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        checkpoint(label)

    for name, actual in origins.items():
        expected = checkout / 'src' / Path(*name.split('.')).with_suffix('.py')
        check('exact imported module: ' + name,
              sha256(actual.read_bytes()).hexdigest() == sha256(expected.read_bytes()).hexdigest()
              and (actual.is_relative_to(source / 'src') if source is not None
                   else actual.is_relative_to(installed) and not actual.is_relative_to(checkout)))
    check('actual Windows native controls', wx.Platform == '__WXMSW__')

    def pump(predicate, timeout=10):
        until = min(deadline, monotonic() + timeout)
        while monotonic() < until:
            application.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def settle(seconds=.15):
        until = monotonic() + seconds
        while monotonic() < until:
            if monotonic() > deadline:
                raise TimeoutError('Native tabs acceptance exceeded its complete deadline.')
            application.Yield()
            wx.MilliSleep(10)

    def descendants(window):
        pending = list(window.GetChildren())
        while pending:
            value = pending.pop(0)
            try:
                if not value:
                    continue
                yield value
                pending.extend(value.GetChildren())
            except RuntimeError:
                continue

    def button(window, label):
        candidates = [value for value in descendants(window)
            if isinstance(value, wx.Button) and value.IsShownOnScreen()
            and wx.GetTopLevelParent(value) is window
            and value.GetLabel().replace('&', '') == label]
        if len(candidates) != 1:
            raise AssertionError(f'Expected one current native button {label!r}; found {len(candidates)}.')
        return candidates[0]

    def focus(control):
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() is control, timeout=3):
            raise AssertionError('The native control did not acquire keyboard focus.')

    def key(code, modifiers=wx.MOD_NONE):
        if not simulator.Char(code, modifiers):
            raise AssertionError('Native keyboard dispatch was not accepted.')
        settle()

    def click(control):
        if not control.IsEnabled():
            raise AssertionError('The native control is disabled: ' + control.GetName())
        focus(control)
        key(wx.WXK_SPACE)

    with TemporaryDirectory(prefix='carr-native-reading-tabs-') as temporary:
        root = Path(temporary)
        raw = '😀 Original first.\r\nДругий рядок. Several more retained words for visual reading.'
        original = root / 'original.txt'
        original.write_bytes(raw.encode('utf-8'))
        source_hash = sha256(original.read_bytes()).hexdigest()
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native independent reading copies')
        imported = library.add_text(workspace, 'Retained source', raw)
        desk = ReadingDesk(library, workspace, imported['edition'])
        frame = wx.Frame(None, title='Native reading tabs acceptance', size=(1050, 850))
        frame.library, frame._closing = library, False
        frame.cancel_event = threading.Event()
        frame._save_coordinator = SaveCoordinator()
        frame.report = lambda message: print('READER:', message, flush=True)
        menu, bar = wx.Menu(), wx.MenuBar()
        menu.Append(wx.ID_ANY, 'Host open fixture\tCtrl+O')
        bar.Append(menu, '&Fixture')
        frame.SetMenuBar(bar)
        frame.menu_bar = bar
        frame.keyboard_actions = KeyboardActions(frame, collect_commands(bar))
        frame.keyboard_actions.reload()
        frame.keyboard_actions.save({'voice_reader.stop': 'Ctrl+Alt+Shift+F8'})
        frame.Show()
        frame.Raise()
        allowed, failures, old_readers, threads = [True], [], set(), []
        original_registry, original_prepare = settings.voice_registry, tabs_module.prepare_tabs
        settings.voice_registry = lambda *_args, **_kwargs: VoiceRegistry()
        def observed_prepare(*args, **kwargs):
            threads.append(threading.get_ident())
            return original_prepare(*args, **kwargs)
        tabs_module.prepare_tabs = observed_prepare
        store = ReadingTabsStore(library.root)
        ReaderDraftStore(library.root).save(ReaderResume(ReadingDocument(raw),
            'fixture:unavailable-voice', rate=2, volume=71, cursor=3, audio_seconds=4.25))
        evidence = {}

        def guard():
            if not allowed[0]:
                raise ValueError('The retained reading source was revoked for this acceptance case.')
            if sha256(original.read_bytes()).hexdigest() != source_hash:
                raise ValueError('The immutable fixture source changed.')

        def drafts():
            index, _ = store.load()
            return index, [store.draft(entry).load() for entry in index.tabs] if index else []

        def save(reader):
            focus(reader.reader_editor)
            key(ord('S'), wx.MOD_CONTROL)
            check('actual Ctrl+S completed its guarded reader save',
                  wx.FindWindowByName('Speech and save status', reader).GetValue().startswith(
                      'Reading draft, voice settings and current position saved for recovery.')
                  and not reader.reader_edit_host.capture().document.dirty)

        def child_modal(reader, trigger, title, scenario):
            finished = []
            until = monotonic() + 15
            def drive_child():
                matches = [window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and window.IsShown() and window.IsModal()
                    and window.GetTitle() == title and getattr(window, 'alive', True)]
                if not matches:
                    if monotonic() < until:
                        wx.CallLater(20, drive_child)
                        return
                    failures.append(AssertionError('The actual child dialog did not open: ' + title))
                    if reader.IsModal():
                        reader.EndModal(wx.ID_CANCEL)
                    return
                child = matches[0]
                try:
                    scenario(child)
                    finished.append(True)
                except BaseException as error:
                    failures.append(error)
                    traceback.print_exc()
                    if child.IsModal():
                        child.EndModal(wx.ID_CANCEL)
            wx.CallLater(20, drive_child)
            click(trigger)
            if not pump(lambda: bool(finished or failures), timeout=20):
                raise TimeoutError('The native child scenario did not finish: ' + title)
            if failures:
                raise failures[0]

        def first_reader(reader):
            tabs, host = reader.reader_tabs, reader.reader_edit_host
            editor = reader.reader_editor
            check('one recovered native notebook page has the exact original',
                  tabs.notebook.GetPageCount() == 1 and isinstance(tabs.notebook, wx.Notebook)
                  and host.capture().document.source_text == raw)
            check('recovery preparation ran on the existing reader worker',
                  threads and all(identity != gui_thread for identity in threads))
            check('an unavailable remembered voice stays passive without fallback',
                  tabs.current.resume.voice_id == 'fixture:unavailable-voice'
                  and wx.FindWindowByName('Voice (installed locally)', reader).GetSelection() == wx.NOT_FOUND)
            initial_display = editor.GetValue()
            evidence['initial_display'] = initial_display
            editor.SetInsertionPointEnd()
            focus(editor)
            key(wx.WXK_SPACE)
            check('actual keyboard editing changes only the first reading copy',
                  editor.GetValue() == initial_display + ' ' and host.capture().document.source_text == raw)
            save(reader)
            editor.SetInsertionPoint(speech_dialog.native_position(editor, 3))
            click(button(reader, 'New reading tab'))
            check('new native tab is a distinct blank with no automatic speech',
                  tabs.notebook.GetPageCount() == 2 and tabs.notebook.GetSelection() == 1
                  and reader.reader_editor.GetValue() == '' and not host.capture().document.dirty)
            focus(reader.reader_editor)
            key(ord('B'))
            save(reader)
            index, saved = drafts()
            evidence['ids'] = [tab.id for tab in index.tabs]
            check('two durable identities retain separate originals and undo histories',
                  len(set(evidence['ids'])) == 2 and saved[0].document.source_text == raw
                  and saved[1].document.source_text == '' and saved[1].document.text == 'b'
                  and saved[1].document.can_undo)
            focus(reader.reader_editor)
            key(ord('A'), wx.MOD_CONTROL)
            key(wx.WXK_BACK)
            check('an intentionally erased copy remains a dirty edit',
                  reader.reader_editor.GetValue() == '' and host.capture().document.dirty)
            click(button(reader, 'Save all open drafts'))
            index, saved = drafts()
            check('native shared Save All persists the erased copy and all histories',
                  not frame._save_coordinator.busy and not host.capture().document.dirty
                  and saved[1].document.text == '' and saved[1].document.can_undo
                  and saved[0].document.text == initial_display + ' ')
            click(button(reader, 'Undo reading edit'))
            check('the erased tab has its own real undo history', reader.reader_editor.GetValue() == 'b')
            save(reader)
            focus(reader.reader_editor)
            key(wx.WXK_TAB, wx.MOD_CONTROL)
            check('native Control Tab restores first text and saved Unicode cursor',
                  tabs.notebook.GetSelection() == 0
                  and reader.reader_editor.GetValue() == initial_display + ' '
                  and speech_dialog.reading_offset(reader.reader_editor,
                      reader.reader_editor.GetInsertionPoint()) == 3)
            check('appearance follows the active editor without replacing its text',
                  reader.reader_presentation.editor is reader.reader_editor)
            def paced(child):
                check('paced reading opens paused and leaves source text intact',
                      not child.pacer.running and not child.following and child.mode.GetSelection() == 0)
                first_word = child.word.GetValue()
                child.speed.SetValue(120)
                click(child.buttons['start'])
                check('the actual native timer advances only after explicit Start',
                      pump(lambda: child.word.GetValue() != first_word, timeout=5))
                click(child.buttons['pause'])
                held_word = child.word.GetValue()
                settle(.7)
                check('native Pause stops paced movement without moving editor focus',
                      not child.pacer.running and child.word.GetValue() == held_word
                      and wx.Window.FindFocus() is child.buttons['pause'])
                click(child.buttons['previous'])
                evidence['focus_cursor'] = child.span[0]
                click(child.buttons['goto'])
            child_modal(reader, button(reader, 'Reading focus...'),
                        'Reading focus and audio follow', paced)
            check('explicit focus cursor return uses verified native Unicode offsets',
                  speech_dialog.reading_offset(reader.reader_editor,
                      reader.reader_editor.GetInsertionPoint()) == evidence['focus_cursor']
                  and host.capture().document.source_text == raw)
            def no_audio_and_remap(child):
                focus(child.mode)
                key(wx.WXK_DOWN)
                click(child.buttons['start'])
                check('audio follow refuses to invent a recording or word timings',
                      child.mode.GetSelection() == 1 and not child.following
                      and 'Render this exact reading text' in child.status.GetValue())
                focus(child.word)
                key(wx.WXK_F8)
                check('the replaced default Stop binding is inactive in focus', not host.cancelled())
                allowed[0] = False
                key(wx.WXK_F8, wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT)
                check('the shared remapped Stop remains reachable after source revocation',
                      host.cancelled() and not child.pacer.running and not child.following)
                allowed[0] = True
                click(child.buttons['close'])
            try:
                child_modal(reader, button(reader, 'Reading focus...'),
                            'Reading focus and audio follow', no_audio_and_remap)
            finally:
                allowed[0] = True
            save(reader)
            check('all first-session tab drafts are clean before native Close',
                  all(not state.resume.document.dirty for state in tabs.states))
            click(button(reader, 'Close'))

        def reopened_reader(reader):
            tabs, host = reader.reader_tabs, reader.reader_edit_host
            index, saved = drafts()
            check('fresh reader restores both durable identities and active tab',
                  [tab.id for tab in index.tabs] == evidence['ids'] and tabs.active == 0
                  and reader.reader_editor.GetValue() == evidence['initial_display'] + ' ')
            check('fresh reader retains source bytes, other history and passive voice identity',
                  saved[0].document.source_text == raw and saved[1].document.text == 'b'
                  and all(state.resume.voice_id == 'fixture:unavailable-voice' for state in tabs.states))
            click(button(reader, 'Undo reading edit'))
            check('first tab undo after restart does not alter the second copy',
                  reader.reader_editor.GetValue() == evidence['initial_display']
                  and tabs.states[1].resume.document.text == 'b')
            click(button(reader, 'Save all open drafts'))
            check('recovered dirty histories save through the existing coordinator',
                  not frame._save_coordinator.busy
                  and all(not state.resume.document.dirty for state in tabs.states))
            click(button(reader, 'Close'))

        def linked_reader(reader):
            host = reader.reader_edit_host
            check('source-linked reader remains a separately guarded single editor',
                  not hasattr(reader, 'reader_tabs')
                  and host.capture().document.source_anchor is not None
                  and host.capture().document.source_anchor['quotation_authority'] is False)
            save(reader)
            click(button(reader, 'Close'))

        def launch(scenario, *, linked=False):
            until = monotonic() + 15
            def drive():
                candidates = [window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and id(window) not in old_readers
                    and hasattr(window, 'reader_edit_host') and window.IsShown() and window.IsModal()
                    and window.reader_edit_host.alive()]
                current = candidates[0] if candidates else None
                if (current is None or current.reader_edit_host.busy()
                        or not linked and not hasattr(current, 'reader_tabs')):
                    if monotonic() < until:
                        wx.CallLater(20, drive)
                        return
                    failures.append(AssertionError('The current native reader did not finish loading.'))
                    if current is not None and current.IsModal():
                        current.EndModal(wx.ID_CANCEL)
                    return
                old_readers.add(id(current))
                try:
                    scenario(current)
                except BaseException as error:
                    failures.append(error)
                    traceback.print_exc()
                    checkpoint('failed native reader scenario')
                    for child in reversed(wx.GetTopLevelWindows()):
                        if isinstance(child, wx.Dialog) and child.IsModal():
                            child.EndModal(wx.ID_CANCEL)
            wx.CallLater(20, drive)
            if linked:
                speech_dialog.show_speech_reader(frame, source_guard=guard,
                    recovery_slot='native-source-linked-tabs-isolation',
                    source_loader=lambda cancelled: read_source_range(desk, 1, 1, cancelled=cancelled))
            else:
                speech_dialog.show_speech_reader(frame, source_guard=guard)
            if failures:
                raise failures[0]
            check('closed reader unregistered its shared save participant',
                  not frame._save_coordinator.participants)

        try:
            launch(first_reader)
            launch(reopened_reader)
            launch(linked_reader, linked=True)
            check('immutable original fixture bytes remain unchanged',
                  original.read_bytes() == raw.encode('utf-8'))
            report.update(wx=wx.version(),
                module_sha256={name: sha256(path.read_bytes()).hexdigest() for name, path in origins.items()},
                source_override=source is not None, native_keys='wx.UIActionSimulator',
                inventory='EXPLICIT_EMPTY_FIXTURE', speech_inference='NOT_RUN',
                audio_follow_playback='NOT_RUN', manual_nvda='NOT_RUN')
        finally:
            allowed[0] = True
            settings.voice_registry, tabs_module.prepare_tabs = original_registry, original_prepare
            frame._closing = True
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
        print('READING_TABS_PHASE ' + phase, flush=True)
    checkpoint('starting')
    if sys.platform != 'win32' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run this native reader-tabs acceptance with Windows Python -I -B.')
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
