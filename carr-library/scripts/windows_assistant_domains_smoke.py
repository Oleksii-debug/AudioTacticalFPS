"""Native review/run of the existing assistant and its exact current-reader bridge.

Uses a temporary real library, the real wx dialogs and existing job store.
Status/Pause/Stop never create a player or start output. No model quality,
microphone, human listening or manual NVDA qualification is claimed.
"""
from argparse import ArgumentParser
import faulthandler
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
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


MODULES = (
    'carr_library.assistant.models', 'carr_library.assistant.service',
    'carr_library.assistant.local_tools', 'carr_library.assistant.player_tools',
    'carr_library.assistant.book_tools', 'carr_library.ui.assistant_dialog',
    'carr_library.ui.desktop_integration', 'carr_library.ui.speech_dialog',
    'carr_library.speech.player',
)


def run_native(source, report, checkpoint):
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.ui.desktop_integration import DesktopIntegration
    from carr_library.ui.productivity import KeyboardActions, collect_commands
    from carr_library.ui import speech_dialog

    application = wx.App(False)
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    simulator = wx.UIActionSimulator()
    origins = {name: str(Path(importlib.import_module(name).__file__).resolve()) for name in MODULES}
    report.update(origins=origins, module_sha256={
        name: sha256(Path(path).read_bytes()).hexdigest() for name, path in origins.items()})

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report['checks'].append(name)
        checkpoint(name)

    checkout = Path(__file__).resolve().parents[1]
    if source is None:
        from importlib.metadata import distributions
        installed = _installed_package_root(distributions(name='universal-research-library'),
                                            carr_library.__file__, checkout)
        check('all nine assistant and reader modules load from the exact installed wheel record',
              all(Path(path) == installed.joinpath(*name.split('.')[1:]).with_suffix('.py').resolve(strict=True)
                  for name, path in origins.items()))
    else:
        expected = (source / 'src' / 'carr_library').resolve(strict=True)
        check('all nine assistant and reader modules load from the explicit source package',
              all(Path(path) == expected.joinpath(*name.split('.')[1:]).with_suffix('.py').resolve(strict=True)
                  for name, path in origins.items()))
    check('all nine imported assistant and reader modules match the exact checkout bytes',
          all(report['module_sha256'][name] == sha256(
              checkout.joinpath('src', *name.split('.')).with_suffix('.py').read_bytes()).hexdigest()
              for name in origins))

    def pump(predicate, timeout=30):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            application.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def descendants(window):
        pending = list(window.GetChildren())
        while pending:
            child = pending.pop(0)
            yield child
            pending.extend(child.GetChildren())

    def button(window, label):
        found = [child for child in descendants(window) if isinstance(child, wx.Button)
                 and child.GetLabel().replace('&', '') == label]
        if len(found) != 1:
            raise AssertionError('Expected exactly one native button: ' + label)
        return found[0]

    def click(control):
        if not control.IsEnabled():
            raise AssertionError('Native control is not enabled: ' + control.GetName())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        control.GetEventHandler().ProcessEvent(event)

    with TemporaryDirectory(prefix='resonance-assistant-native-') as temporary:
        root = Path(temporary)
        raw = 'Railway records remain exact.\nУкраїнський оригінал без прихованих команд.'
        original = root / 'original-source.txt'
        original.write_bytes(raw.encode('utf-8'))
        source_hash = sha256(original.read_bytes()).hexdigest()
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Explicit assistant source')
        imported = library.add_text(workspace, 'Native assistant source', raw)
        hit, = library.search(workspace, 'Railway records')
        frame = wx.Frame(None, title='Native assistant domain fixture', size=(1100, 850))
        frame.library, frame.active, frame._closing = library, (workspace, None), False
        notices = []
        frame.report = notices.append
        menu, menu_bar = wx.Menu(), wx.MenuBar()
        menu.Append(wx.ID_ANY, 'Fixture action\tCtrl+O')
        menu_bar.Append(menu, '&Fixture')
        frame.SetMenuBar(menu_bar)
        frame.menu_bar = menu_bar
        frame.keyboard_actions = KeyboardActions(frame, collect_commands(menu_bar))
        frame.keyboard_actions.reload()
        # Reuse the real integration owner. Tray/hotkey/clipboard setup is not
        # part of this fixture; its exact reader registry/dispatch is production.
        desktop = DesktopIntegration.__new__(DesktopIntegration)
        desktop.frame, desktop.wx, desktop.closed = frame, wx, False
        desktop.reader = desktop._assistant_reader = None
        frame.desktop_integration = desktop
        frame.Show()
        frame.Raise()
        failures, seen_reader, seen_bridge = [], [], []
        observer = AssistantService(library)
        slot = 'native-assistant-current-reader'

        def source_guard():
            if frame.library is not library or frame.active != (workspace, None):
                raise PermissionError('Native fixture library scope changed.')
            if sha256(original.read_bytes()).hexdigest() != source_hash:
                raise ValueError('The retained original fixture changed.')
            with library.read(workspace):
                pass

        def drive_assistant(reader):
            deadline = monotonic() + 15
            def drive():
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if hasattr(window, 'assistant_service') and window.IsModal() and window.IsShown()]
                if not dialogs:
                    if monotonic() < deadline:
                        wx.CallLater(20, drive)
                        return
                    failures.append(AssertionError('The actual current-reader assistant did not open.'))
                    if reader.IsModal():
                        reader.EndModal(wx.ID_CANCEL)
                    return
                dialog = dialogs[0]
                controls = dialog.assistant_controls
                bridge = dialog.assistant_player_bridge
                seen_bridge.append(bridge)
                try:
                    check('the native assistant is bound to the exact existing reader and player',
                          bridge.reader_window is reader and desktop._assistant_reader['player'] is reader.reader_player)
                    check('the current-reader assistant controls expose native accessible names',
                          bool(controls['instruction'].GetName()) and bool(controls['review'].GetName())
                          and bool(reader.reader_assistant_button.GetName()))

                    def execute(steps):
                        previous = {job.id for job in observer.jobs()}
                        controls['instruction'].SetValue(json.dumps({'steps': steps}, ensure_ascii=False))
                        click(controls['buttons']['preview'])
                        if not pump(lambda: controls['buttons']['run'].IsEnabled()):
                            raise AssertionError('Native typed preview failed: ' + controls['review'].GetValue())
                        check('native Preview leaves the existing assistant job store unchanged',
                              {job.id for job in observer.jobs()} == previous)
                        click(controls['buttons']['run'])
                        def finished():
                            jobs = [job for job in observer.jobs() if job.id not in previous]
                            return (len(jobs) == 1 and jobs[0].status in
                                    ('complete', 'failed', 'attention', 'paused', 'cancelled')
                                    and controls['buttons']['run'].IsEnabled())
                        if not pump(finished):
                            raise AssertionError('Native typed execution failed to settle: ' + controls['review'].GetValue())
                        job, = [job for job in observer.jobs() if job.id not in previous]
                        check('native result status agrees with the durable assistant checkpoint',
                              job.status == 'complete' and job.completed == len(steps))
                        return job

                    owned = reader.reader_player
                    check('opening this assistant starts no audio output',
                          owned.state == 'stopped' and owned.path is None)
                    result = execute([
                        {'tool': 'reader.read', 'arguments': {'edition': imported['edition'],
                            'revision': hit.revision, 'first': 1, 'last': 1}},
                        {'tool': 'audio.effects', 'arguments': {}},
                        {'tool': 'player.status', 'arguments': {}},
                    ])
                    read, effects, status = result.results
                    check('real native reader command returns the exact retained Unicode source',
                          read['text'] == raw and read['source_sha256'] == source_hash
                          and read['quotation_authority'] is False)
                    check('native effect discovery uses the existing finite effect catalog',
                          bool(effects) and effects.get('artifacts') == [])
                    check('native player status refers to the current owner without replacing playback',
                          status['workspace'] == workspace and status['state'] == 'stopped'
                          and reader.reader_player is owned)
                    # Safe control on a stopped player remains a real dispatch;
                    # it must not synthesize, load a file, or toggle into playback.
                    command = {key: status[key] for key in ('session', 'source_sha256')}
                    control = execute([
                        {'tool': 'player.control', 'arguments': {**command, 'action': 'pause'}},
                        {'tool': 'player.control', 'arguments': {**command, 'action': 'stop'}},
                    ])
                    check('native reviewed Pause and Stop retain the same stopped player',
                          all(row['state'] == 'stopped' for row in control.results)
                          and owned.state == 'stopped' and owned.path is None)
                    check('both safe native actions have durable exact dispatch acknowledgements',
                          all(row['intent'].get('player_dispatch_complete') is True
                              for row in observer.store.load(control.id)['steps']))
                    check('the reader copy and retained original remain unchanged',
                          reader.reader_editor.GetValue() == raw and original.read_bytes() == raw.encode('utf-8'))
                except BaseException as exc:
                    failures.append(exc)
                    traceback.print_exc()
                finally:
                    try:
                        click(controls['buttons']['close'])
                    except BaseException as exc:
                        failures.append(exc)
                        if dialog.IsModal():
                            dialog.EndModal(wx.ID_CANCEL)
            wx.CallLater(20, drive)

        deadline = monotonic() + 20
        def drive_reader():
            dialogs = [window for window in wx.GetTopLevelWindows()
                       if hasattr(window, 'reader_assistant_button') and window.IsModal() and window.IsShown()]
            if not dialogs:
                if monotonic() < deadline:
                    wx.CallLater(20, drive_reader)
                    return
                failures.append(AssertionError('The production Voice reader did not open.'))
                return
            reader = dialogs[0]
            seen_reader.append(reader)
            try:
                check('the real reader enables its explicit assistant action after loading',
                      pump(lambda: reader.reader_assistant_button.IsEnabled()))
                reader.reader_assistant_button.SetFocus()
                check('the current-reader assistant action accepts native keyboard focus',
                      pump(lambda: wx.Window.FindFocus() is reader.reader_assistant_button, timeout=5))
                drive_assistant(reader)
                # Schedule the nested scenario before the actual OS key opens
                # the modal, so the real reader event handler owns its lifetime.
                check('the native Space key was dispatched to the reader assistant control',
                      simulator.Char(wx.WXK_SPACE))
                check('the nested native assistant returned to its original reader',
                      pump(lambda: bool(seen_bridge) and seen_bridge[-1].closed.is_set(), timeout=60))
                if failures:
                    raise failures[0]
                check('closing the assistant withdraws only its queued player authority',
                      seen_bridge[-1].closed.is_set() and desktop.reader[1] is reader
                      and reader.reader_player.state == 'stopped')
                # The initial fixture is a new reading copy. Exercise its real
                # save workflow before Close so no unsaved decision is bypassed.
                click(button(reader, 'Save reading copy'))
                def saved():
                    value = ReaderDraftStore(library.root, slot=slot).load()
                    return value is not None and value.document.text == raw
                check('the real reading-copy Save persists its unchanged recovery document', pump(saved))
                click(button(reader, 'Close'))
            except BaseException as exc:
                if not failures or failures[-1] is not exc:
                    failures.append(exc)
                traceback.print_exc()
                if reader.IsModal():
                    reader.EndModal(wx.ID_CANCEL)

        try:
            wx.CallLater(20, drive_reader)
            speech_dialog.show_speech_reader(frame, text=raw, source_guard=source_guard, recovery_slot=slot)
            if failures:
                raise failures[0]
            check('reader destruction detaches the exact desktop playback authority',
                  bool(seen_reader) and desktop.reader is None and desktop._assistant_reader is None)
            check('the original source remains unchanged after both modal lifetimes',
                  original.read_bytes() == raw.encode('utf-8'))
            report.update(wx=wx.version(), native_keys='wx.UIActionSimulator Space',
                          speech_inference='NOT_RUN', playback_device='NOT_RUN',
                          human_listening='NOT_RUN', manual_nvda='NOT_RUN')
        finally:
            frame._closing = True
            frame.Destroy()
            application.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source', type=Path)
    mode.add_argument('--expect-installed', action='store_true')
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'FAIL', 'phase': 'incomplete', 'checks': [],
              'human_listening': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}
    def checkpoint(phase):
        report['phase'] = phase
        args.report.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n',
                               encoding='utf-8')
        print('ASSISTANT_DOMAINS_PHASE ' + phase, flush=True)
    checkpoint('starting')
    if sys.platform != 'win32' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run this native probe with Windows Python -I -B.')
    if args.expect_installed and args.source is not None:
        raise ValueError('Installed acceptance must not inject checkout source.')
    faulthandler.dump_traceback_later(180, repeat=False, exit=True)
    handle = None
    try:
        source = args.source.resolve(strict=True) if args.source is not None else None
        if source is not None:
            if source != Path(__file__).resolve().parents[1]:
                raise ValueError('Choose this exact checkout for native source acceptance.')
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
