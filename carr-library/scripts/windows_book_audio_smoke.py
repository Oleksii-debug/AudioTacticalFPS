"""Native Book and ordered-batch controls through the existing desktop worker.

Purpose-written PCM tones exercise local render/provenance protocols. This does
not qualify a model, cloning rights, acoustic quality, or manual screen readers.
"""
from argparse import ArgumentParser
import ctypes
import faulthandler
from hashlib import sha256
import importlib
import json
import math
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback
import wave

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




def run_native(source, report, checkpoint):
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import AudioBudget, AudioRuntime
    from carr_library.operations import ResourceBroker, ResourceLimits
    from carr_library.studio.book_audio import BookAudioService, MODES
    from carr_library.studio.book_batch import BookBatchService
    from carr_library.studio.render import ProductionService
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.book_audio_dialog import create_book_audio_dialog
    from carr_library.ui.book_batch_dialog import create_book_batch_dialog

    app = wx.App(False)
    event_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(event_loop)
    simulator = wx.UIActionSimulator()
    deadline, gui_thread = monotonic() + 210, threading.get_ident()
    checks, failures = report['checks'], []
    checkout = Path(__file__).resolve().parents[1]
    names = ('carr_library.studio.book_source', 'carr_library.studio.book_voice',
        'carr_library.studio.book_audio', 'carr_library.studio.book_batch',
        'carr_library.studio.book_landmarks', 'carr_library.studio.render',
        'carr_library.studio.book_cli', 'carr_library.studio.cli',
        'carr_library.ui.book_audio_dialog', 'carr_library.ui.book_batch_dialog',
        'carr_library.ui.book_duration_dialog', 'carr_library.ui.desktop_app',
        'carr_library.ui.worker_lifetime')
    modules = [importlib.import_module(name) for name in names]
    origins = {module.__name__: Path(module.__file__).resolve(strict=True) for module in modules}
    installed = None
    if source is None:
        import carr_library
        from importlib.metadata import distributions
        installed = _installed_package_root(distributions(name='universal-research-library'),
                                            carr_library.__file__, checkout)

    def check(label, value):
        if not value:
            raise AssertionError(label)
        checks.append(label)
        checkpoint(label)

    for name, actual in origins.items():
        expected = checkout / 'src' / Path(*name.split('.')).with_suffix('.py')
        check('exact imported Book module: ' + name,
            sha256(actual.read_bytes()).hexdigest() == sha256(expected.read_bytes()).hexdigest()
            and (actual.is_relative_to(source / 'src') if source is not None
                 else actual.is_relative_to(installed) and not actual.is_relative_to(checkout)))
    check('actual Windows Book controls', wx.Platform == '__WXMSW__')

    def pump(predicate, timeout=20):
        until = min(deadline, monotonic() + timeout)
        while monotonic() < until:
            app.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def settle():
        until = min(deadline, monotonic() + .08)
        while monotonic() < until:
            app.Yield()
            wx.MilliSleep(5)

    def named(window, label):
        values, pending = [], list(window.GetChildren())
        while pending:
            control = pending.pop(0)
            if control.GetName() == label and wx.GetTopLevelParent(control) is window:
                values.append(control)
            pending.extend(control.GetChildren())
        if len(values) != 1:
            raise AssertionError('Expected one current native control: ' + label)
        return values[0]

    def focus(control):
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() is control, 3):
            raise AssertionError('The native Book control did not acquire focus.')

    def key(code, modifiers=wx.MOD_NONE):
        if not simulator.Char(code, modifiers):
            raise AssertionError('Windows rejected native Book keyboard dispatch.')
        settle()

    def click(window, label):
        control = named(window, label)
        if not control.IsEnabled():
            raise AssertionError('Disabled native Book control: ' + label)
        focus(control)
        key(wx.WXK_SPACE)

    def choose(control, index):
        focus(control)
        key(wx.WXK_HOME)
        for _ in range(index):
            key(wx.WXK_DOWN)
        key(wx.WXK_RETURN)
        check('native choice selected requested item', control.GetSelection() == index)

    def type_text(control, value):
        focus(control)
        key(ord('A'), wx.MOD_CONTROL)
        # wx4.2.4 UIActionSimulator.Text takes bytes. These reviewed fixture
        # folder/control values are ASCII; original book text remains Unicode.
        if not simulator.Text(value.encode('ascii')):
            raise AssertionError('Native Book text entry failed.')
        settle()
        check('native field preserves exact typed value', control.GetValue() == value)

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetWindow.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t

    def approve(window, title, trigger):
        observed, until = [], min(deadline, monotonic() + 8)
        def drive():
            hwnd = user32.FindWindowW(None, title)
            if not hwnd:
                if monotonic() < until:
                    wx.CallLater(15, drive)
                else:
                    failures.append(AssertionError('The real Book confirmation did not open: ' + title))
                    if window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                return
            process = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
            if process.value != os.getpid() or user32.GetWindow(hwnd, 4) != window.GetHandle():
                failures.append(AssertionError('Unexpected owner of the real Book confirmation.'))
                return
            observed.append(True)
            # Same pinned wxMSW TaskDialogIndirect contract as provider controls:
            # TDM_CLICK_BUTTON targets the actual visible IDYES, not a fake answer.
            user32.SendMessageW(hwnd, 0x400 + 102, 6, 0)
        wx.CallLater(15, drive)
        trigger()
        if failures:
            raise failures[0]
        check('real explicit native confirmation: ' + title, len(observed) == 1)

    class ToneVoices:
        def __init__(self):
            self.calls, self.threads, self.closed = [], [], 0
            self.hold, self.entered = False, threading.Event()
        def voices(self):
            from types import SimpleNamespace
            self.threads.append(threading.get_ident())
            return (SimpleNamespace(id='book-native-fixture', name='Book protocol PCM fixture',
                language='en', engine='synthetic fixture', capabilities=('rate', 'volume')),)
        def synthesize(self, text, voice_id, destination, *, settings, cancelled):
            if voice_id != 'book-native-fixture' or cancelled():
                raise InterruptedError('Synthetic Book operation was cancelled before speech.')
            self.threads.append(threading.get_ident())
            self.calls.append(text)
            self.entered.set()
            until = monotonic() + 20
            while self.hold:
                if cancelled():
                    raise InterruptedError('Synthetic Book operation acknowledged native Pause.')
                if monotonic() > until:
                    raise TimeoutError('Native Book pause driver did not release its fixture.')
                threading.Event().wait(.01)
            with wave.open(str(destination), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b''.join(struct.pack('<h', int(2400 * math.sin(
                    2 * math.pi * 440 * index / 24000))) for index in range(4800)))
        def close(self):
            self.closed += 1

    with TemporaryDirectory(prefix='carr-native-book-audio-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native Book plans')
        original = '[Music: keep literal]\nNarrator: Original first.\n\nДругий розділ.'
        first = library.add_text(workspace, 'First native source', original)
        second = library.add_text(workspace, 'Second native source', 'A second exact source.\n\nLast literal part.')
        original_bytes = {item['asset']: library.assets.resolve(item['asset']).read_bytes()
                          for item in (first, second)}
        voices = ToneVoices()
        production = ProductionService(library.root / 'state/studio', voices=voices,
            broker=ResourceBroker.for_app(library.root, limits=ResourceLimits(cpu_slots=1)),
            runtime_factory=lambda directory, **kwargs: AudioRuntime(directory,
                budget=AudioBudget(threads=1), **kwargs))
        service = BookAudioService(library, workspace, production=production)
        frame = DesktopFrame(library)
        frame.Show()
        frame.Raise()
        if not pump(lambda: frame.active == (workspace, None) and not frame.working and not frame.busy):
            raise AssertionError('The actual desktop library did not become ready.')
        commands = frame.keyboard_actions.commands
        check('Book command belongs to the existing host registry',
              any(getattr(command, 'identity', '') == 'Audio Studio/Book audio: split and render'
                  for command in commands))
        output = root / 'book-output'
        output.mkdir()
        dialogs = []

        def wait_idle(label):
            started = monotonic()
            complete = pump(lambda: not frame.working and not frame.busy, 80)
            if not complete:
                # Preserve the actual blocked worker stack before cancellation
                # or modal teardown can erase the wait boundary.
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                evidence = {
                    'label': label, 'elapsed_seconds': monotonic() - started,
                    'working': bool(frame.working), 'busy': bool(frame.busy),
                    'cancelled': frame.cancel_event.is_set(),
                    'fixture_hold': voices.hold, 'voice_calls': len(voices.calls),
                    'remaining_probe_seconds': max(0, deadline - monotonic()),
                    'modal_status': [],
                }
                def visible_status(control):
                    try:
                        return str(control.GetValue())[:4096]
                    except (RuntimeError, AttributeError) as error:
                        return type(error).__name__ + ': ' + str(error)[:512]
                evidence['frame_status'] = visible_status(frame.status)
                for window, _ in tuple(dialogs)[-4:]:
                    if getattr(window, '_book_destroyed', False):
                        evidence['modal_status'].append('Owner already destroyed')
                        continue
                    for name in ('Book split preview and saved job status',
                                 'Multiple book review and existing Studio checkpoints'):
                        try:
                            control = named(window, name)
                        except (AssertionError, RuntimeError):
                            continue
                        evidence['modal_status'].append({'name': name, 'text': visible_status(control)})
                report['idle_failure'] = evidence
                checkpoint('failed idle wait: ' + label)
                print('BOOK_AUDIO_IDLE_FAILURE ' + json.dumps(evidence, ensure_ascii=False), flush=True)
            check(label, complete)
            if failures:
                raise failures[0]

        def modal(dialog, close, scenario):
            dialogs.append((dialog, close))
            def drive():
                try:
                    scenario(dialog)
                except BaseException as error:
                    failures.append(error)
                    traceback.print_exc()
                    checkpoint('failed Book scenario')
                    for child in reversed(wx.GetTopLevelWindows()):
                        if isinstance(child, wx.Dialog) and child.IsModal():
                            child.EndModal(wx.ID_CANCEL)
            wx.CallLater(20, drive)
            try:
                dialog.ShowModal()
                if failures:
                    raise failures[0]
            finally:
                close()
                if not dialog._book_destroyed:
                    dialog.Destroy()
                dialogs.remove((dialog, close))

        def configure(dialog, destination, *, split='blank-lines', use=False):
            wait_idle('source inventory finished on existing desktop worker')
            click(dialog, 'Discover local voices')
            wait_idle('voice inventory finished on existing desktop worker')
            choose(named(dialog, 'Installed local voice'), 0)
            choose(named(dialog, 'Split mode'), MODES.index(split))
            type_text(named(dialog, 'Reviewed output folder'), str(destination))
            click(dialog, 'Preview split and names')
            wait_idle('exact source and part preview finished')
            check('preview alone starts no new speech',
                  named(dialog, 'Book split preview and saved job status').GetValue().startswith('PREVIEW ONLY'))
            if use:
                approve(dialog, 'Use reviewed book in batch', lambda: click(dialog, 'Use reviewed book'))
                wait_idle('selected Book review captured without execution')

        def single(dialog):
            configure(dialog, output)
            check('Preview creates no production jobs', production.jobs() == () and not voices.calls)
            approve(dialog, 'Prepare reviewed book items', lambda: click(dialog, 'Prepare saved items'))
            wait_idle('Prepare saves ordinary production jobs')
            check('prepared exact two literal parts without audio',
                len(production.jobs()) == 2 and not voices.calls and not list(output.iterdir()))
            voices.hold, voices.entered = True, threading.Event()
            approve(dialog, 'Run reviewed book audio', lambda: click(dialog, 'Run or resume saved items'))
            check('actual renderer entered the explicit synthetic voice', pump(voices.entered.is_set, 40))
            click(dialog, 'Pause')
            wait_idle('native Pause waits for owned production to settle')
            voices.hold = False
            check('Pause retained work before publication',
                  any(job['status'] == 'paused' for job in production.jobs())
                  and not list(output.glob('*/master.wav')))
            approve(dialog, 'Run reviewed book audio', lambda: click(dialog, 'Run or resume saved items'))
            wait_idle('explicit resume completed actual FFmpeg output')
            states = production.jobs()
            check('all prepared Book jobs completed', all(job['status'] == 'complete' for job in states))
            saved_plan, = service.saved()
            ordered = [production.get(saved_plan.job_id(index))
                       for index in range(1, len(saved_plan.parts) + 1)]
            check('literal source directives never became cues',
                  ''.join(line['text'] for job in ordered for scene in job['project']['scenes']
                          for line in scene['lines']) == original
                  and all(not scene.get('cues', []) for job in ordered for scene in job['project']['scenes']))
            artifacts = {row['path']: (sha256(Path(row['path']).read_bytes()).hexdigest(),
                                       Path(row['path']).stat().st_mtime_ns)
                         for job in states for row in job['artifacts']}
            calls = len(voices.calls)
            approve(dialog, 'Run reviewed book audio', lambda: click(dialog, 'Run or resume saved items'))
            wait_idle('completed replay verified exact files')
            check('completed replay uses no new synthesis or file replacement',
                len(voices.calls) == calls and all(
                    (sha256(Path(path).read_bytes()).hexdigest(), Path(path).stat().st_mtime_ns) == receipt
                    for path, receipt in artifacts.items()))
            click(dialog, 'Close')

        def review_child(batch_dialog, index, destination):
            choose(named(batch_dialog, 'Ordered books; review each book before Preview'), index)
            observed, until = [], min(deadline, monotonic() + 20)
            def drive():
                matches = [window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and window.IsModal() and window.IsShown()
                    and window.GetName() == 'Book audio workspace'
                    and not getattr(window, '_book_destroyed', False)]
                if not matches:
                    if monotonic() < until:
                        wx.CallLater(20, drive)
                    else:
                        failures.append(AssertionError('The real per-book review did not open.'))
                        if batch_dialog.IsModal():
                            batch_dialog.EndModal(wx.ID_CANCEL)
                    return
                try:
                    configure(matches[0], destination, use=True)
                    observed.append(True)
                except BaseException as error:
                    failures.append(error)
                    traceback.print_exc()
                    if matches[0].IsModal():
                        matches[0].EndModal(wx.ID_CANCEL)
            wx.CallLater(20, drive)
            click(batch_dialog, 'Review selected book')
            check('actual per-book modal returned its guarded review', observed == [True] and not failures)

        def batch_scenario(dialog):
            picked, until = [], min(deadline, monotonic() + 15)
            def choose_books():
                matches = [window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.MultiChoiceDialog) and window.IsModal() and window.IsShown()]
                if not matches:
                    if monotonic() < until:
                        wx.CallLater(20, choose_books)
                    else:
                        failures.append(AssertionError('The native library-book picker did not open.'))
                        if dialog.IsModal():
                            dialog.EndModal(wx.ID_CANCEL)
                    return
                picker = matches[0]
                children = []
                pending = list(picker.GetChildren())
                while pending:
                    child = pending.pop(0)
                    if isinstance(child, wx.CheckListBox):
                        children.append(child)
                    pending.extend(child.GetChildren())
                if len(children) != 1 or children[0].GetCount() != 2:
                    failures.append(AssertionError('Expected the actual two-book native checklist.'))
                    picker.EndModal(wx.ID_CANCEL)
                    return
                focus(children[0])
                key(wx.WXK_HOME)
                key(wx.WXK_SPACE)
                key(wx.WXK_DOWN)
                key(wx.WXK_SPACE)
                key(wx.WXK_RETURN)
                picked.append(True)
            wx.CallLater(20, choose_books)
            click(dialog, 'Choose library books')
            check('native book picker completed', pump(lambda: bool(picked or failures), 15) and picked == [True])
            wait_idle('actual library book selection finished')
            destinations = (root / 'batch-first', root / 'batch-second')
            for destination in destinations:
                destination.mkdir()
            for index, destination in enumerate(destinations):
                review_child(dialog, index, destination)
            before = len(production.jobs())
            click(dialog, 'Move selected book up')
            click(dialog, 'Preview entire batch')
            wait_idle('ordered multi-book preview finished')
            check('batch Preview creates no jobs', len(production.jobs()) == before)
            approve(dialog, 'Prepare reviewed batch', lambda: click(dialog, 'Prepare batch items'))
            wait_idle('batch Prepare retained ordinary Studio jobs')
            groups = BookBatchService(service)
            saved, = groups.saved()
            check('batch retained exactly two independently reviewed plans', len(saved.items) == 2)
            expected_order = [plan.source['source_anchor']['edition'] for plan in groups.plans(saved)]
            approve(dialog, 'Run reviewed book batch', lambda: click(dialog, 'Run or resume batch'))
            wait_idle('ordered batch completed through the same renderer')
            status = groups.status(saved.id)
            check('all ordered batch items completed', all(item['status'] == 'complete'
                  for book in status['books'] for item in book['items']))
            check('saved order and literal originals remain unchanged',
                  expected_order == [plan.source['source_anchor']['edition'] for plan in groups.plans(saved)]
                  and original_bytes == {key: library.assets.resolve(key).read_bytes() for key in original_bytes})
            check('native Book voice work never ran on the GUI thread',
                  voices.threads and all(identity != gui_thread for identity in voices.threads))
            click(dialog, 'Close')

        try:
            dialog, close = create_book_audio_dialog(frame, service=service, edition_id=first['edition'])
            modal(dialog, close, single)
            dialog, close = create_book_batch_dialog(frame, service=service)
            modal(dialog, close, batch_scenario)
            check('borrowed voice registry survives all Book window closes', voices.closed == 0)
            report.update(wx=wx.version(), source_override=source is not None,
                module_sha256={name: sha256(path.read_bytes()).hexdigest() for name, path in origins.items()},
                native_keys='wx.UIActionSimulator', audio_protocol='synthetic PCM through actual FFmpeg',
                voice_model_quality='NOT_RUN', manual_nvda='NOT_RUN')
        finally:
            voices.hold = False
            frame.cancel_event.set()
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            service.close()
            production.close()
            for dialog, close in reversed(dialogs):
                if not dialog._book_destroyed:
                    close()
                    dialog.Destroy()
            frame.Destroy()
            app.Yield()


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
        args.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
        print('BOOK_AUDIO_PHASE ' + phase, flush=True)
    checkpoint('starting')
    if sys.platform != 'win32' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run the native Book acceptance with Windows Python -I -B.')
    if args.expect_installed and args.source is not None:
        raise ValueError('Installed Book acceptance must not inject checkout source.')
    faulthandler.dump_traceback_later(240, repeat=False, exit=True)
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
