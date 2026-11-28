"""Native Import Center and full/incremental backup access on one Desktop.

The saved scan, source snapshots, existing isolated import workers, actual
Desktop executor, resource dialogs, checksum entry, generation review and all
backup/restore results are real. Only folder-picker answers and one pause after
the real third source snapshot are controlled. No file-picker, physical audio
or NVDA qualification is claimed. Small authored TXT sources exercise an
explicit 128 MiB allowance; large-original capacity is a separate API gate.
"""
import argparse
import ctypes
import faulthandler
from hashlib import sha256
import base64
import csv
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import sys
import threading
from tempfile import mkdtemp
from time import monotonic, sleep
from types import SimpleNamespace
import traceback


def _installed_sources(*modules):
    """Bind the actual installed Python namespace and RECORD to this checkout."""
    import sysconfig
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Installed native capacity acceptance requires Python -I -B.')
    source = Path(__file__).resolve().parents[1] / 'src' / 'carr_library'
    distribution = importlib.metadata.distribution('universal-research-library')
    installed = Path(distribution.locate_file('carr_library')).resolve(strict=True)
    roots = {Path(sysconfig.get_path(key)).resolve(strict=True) for key in ('purelib', 'platlib')}
    if installed == source.resolve(strict=True) or not any(installed.is_relative_to(root) for root in roots):
        raise RuntimeError('Native capacity acceptance requires the current installed package.')
    def inventory(directory):
        result = {}
        for path in sorted(directory.rglob('*.py')):
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('The native capacity Python inventory contains an indirect file.')
            relative = path.relative_to(directory).as_posix()
            if len(result) >= 4096:
                raise RuntimeError('The native capacity Python inventory exceeds its bound.')
            result[relative] = {'sha256': sha256(path.read_bytes()).hexdigest(),
                                'bytes': path.stat().st_size}
        if '__init__.py' not in result:
            raise RuntimeError('The native capacity package has no exact Python inventory.')
        return result
    expected, actual = inventory(source), inventory(installed)
    if actual != expected:
        raise RuntimeError('The complete installed Python namespace differs from the checkout.')
    record = distribution.read_text('RECORD')
    if record is None or len(record) > 8 * 1024**2:
        raise RuntimeError('The installed wheel RECORD is unavailable or exceeds its bound.')
    records = {}
    for name, digest, size in csv.reader(record.splitlines()):
        if name in records:
            raise RuntimeError('The installed wheel RECORD repeats a path.')
        records[name] = digest, size
    for relative, value in expected.items():
        digest, size = records.get('carr_library/' + relative, ('', ''))
        encoded = base64.urlsafe_b64encode(bytes.fromhex(value['sha256'])).decode('ascii').rstrip('=')
        if digest != 'sha256=' + encoded or size != str(value['bytes']):
            raise RuntimeError('The installed Python file differs from its wheel RECORD.')
    result = {}
    for module in modules:
        actual_path = Path(module.__file__).resolve(strict=True)
        relative = Path(*module.__name__.split('.')[1:]).with_suffix('.py')
        if actual_path != installed / relative:
            raise RuntimeError('The native capacity module escaped the installed package: ' + module.__name__)
        result[module.__name__] = dict(path=str(actual_path), **actual[relative.as_posix()])
    return {'modules': result, 'python_files': len(actual), 'python_inventory': actual,
            'installed_root': str(installed), 'source_root': str(source.resolve(strict=True))}


class _OwnedModalAbort:
    """Unwind one owned modal loop per GUI turn, never its suspended parent."""

    def __init__(self, wx, app, owners, report):
        self.wx, self.app, self.owners, self.report = wx, app, owners, report
        self.active = False
        self.timer = None
        self.reported = set()

    def request(self):
        if not self.active:
            self.active = True
            self.wx.CallAfter(self._advance)

    def stop(self):
        self.active = False
        if self.timer is not None:
            self.timer.Stop()
            self.timer = None

    def _advance(self):
        self.timer = None
        if not self.active:
            return
        try:
            # Owners are innermost first. EndModal() requests a return; its
            # IsModal() remains true until that ShowModal() actually unwinds.
            for owner in self.owners():
                if owner is None:
                    continue
                dialog = owner.dialog
                if dialog and dialog.IsModal():
                    if dialog.IsShown():
                        owner.close()
                    self.timer = self.wx.CallLater(10, self._advance)
                    return
            self.active = False
            self.app.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            detail = traceback.format_exc()
            if detail not in self.reported:
                self.reported.add(detail)
                self.report(detail)
            # Cleanup refusal remains a failure. Do not force-delete a native
            # owner or report silence; the outer owned process bound applies.
            self.timer = self.wx.CallLater(25, self._advance)


def _nested_modal_failure_acceptance(wx, app, check, select_failure_handler):
    """Real Windows negative control for the probe's former nested-loop hang."""
    events, returned, failures = [], [], []
    owners = []
    abort = _OwnedModalAbort(wx, app, lambda: tuple(reversed(owners)), failures.append)

    def fail(detail):
        failures.append(detail)
        abort.request()

    def owner(label, parent):
        dialog = wx.Dialog(parent, title='Owned access failure control — ' + label)
        native_id = dialog.GetId()
        record = SimpleNamespace(dialog=dialog, loop=None, native_id=native_id)

        def destroyed(event):
            if event.GetId() == native_id:
                events.append(label)
            event.Skip()

        def close():
            if wx.EventLoopBase.GetActive() != record.loop:
                raise AssertionError('Attempted to close a suspended ' + label + ' loop.')
            dialog.EndModal(wx.ID_CANCEL)

        record.close = close
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        owners.append(record)
        return record

    def provoke(inner):
        inner.loop = wx.EventLoopBase.GetActive()
        try:
            raise AssertionError('deliberate owned nested-modal failure')
        except AssertionError:
            fail(traceback.format_exc())

    def open_inner(outer):
        outer.loop = wx.EventLoopBase.GetActive()
        inner = owner('inner', outer.dialog)
        wx.CallAfter(provoke, inner)
        try:
            inner.dialog.ShowModal()
            returned.append('inner')
        finally:
            if inner.dialog:
                inner.dialog.Destroy()

    # wxPython MainLoop returns immediately with no top-level windows. Own the
    # actual outer dialog before starting the loop, then show it on that loop.
    outer = owner('outer', None)

    def open_outer():
        wx.CallAfter(open_inner, outer)
        try:
            outer.dialog.ShowModal()
            returned.append('outer')
        finally:
            if outer.dialog:
                outer.dialog.Destroy()
        app.ExitMainLoop()

    select_failure_handler(fail)
    try:
        wx.CallAfter(open_outer)
        app.MainLoop()
    finally:
        abort.stop()
        select_failure_handler(None)
    check('actual nested-modal failure returns child before parent',
          returned == ['inner', 'outer']
          and len(failures) == 1
          and 'deliberate owned nested-modal failure' in failures[0])
    check('actual failed modal owners receive one native destruction each',
          events.count('inner') == 1 and events.count('outer') == 1
          and all(not item.dialog for item in owners))



def _run_owned_native():
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    from carr_library.audio.cleanup import drain_pending_cleanup
    try:
        result = run_process(
            [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
             '--native-child'],
            budget=AudioBudget(timeout=540, max_log_bytes=2 * 1024 * 1024),
            owned_tree=True, allow_failure=True)
    except BaseException as error:
        drain_pending_cleanup(error)
        raise
    interruption = drain_pending_cleanup()
    if interruption is not None:
        raise interruption
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace'))
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
    sys.stdout.flush()
    sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows session required'}))
        return 77
    if not arguments.native_child:
        return _run_owned_native()
    faulthandler.dump_traceback_later(510, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime_handle = activate_bundled_runtime()
    try:
        return _run_native()
    finally:
        try:
            if runtime_handle is not None:
                runtime_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native():
    from ctypes import wintypes
    import wx
    from carr_library import (application, durable_state, library_backup, pending_backup,
        backup_incremental, backup_budget, backup_options, backup_sidecars)
    from carr_library.application import LibraryApp
    from carr_library.audio import cleanup, process
    from carr_library.catalog import intake
    from carr_library.ingest import (batches, batch_pages, batch_scan, resource_budget,
        streamed_text, streamed_ingest, pipeline, worker, session, retained_assets)
    from carr_library.storage import revisions
    from carr_library.ui import (desktop_app, import_center, import_resources,
        backup_resources, backup_actions, backup_incremental_dialog)

    probe_identity = {str(Path(__file__).resolve()): sha256(Path(__file__).read_bytes()).hexdigest()}
    modules = (application, durable_state, library_backup, pending_backup,
        backup_incremental, backup_budget, backup_options, backup_sidecars,
        cleanup, process, intake, batches, batch_pages, batch_scan, resource_budget,
        streamed_text, streamed_ingest, pipeline, worker, session, retained_assets, revisions, desktop_app, import_center,
        import_resources, backup_resources, backup_actions, backup_incremental_dialog)
    identities = _installed_sources(*modules)
    checks, errors, jobs, peers, directory_answers = [], [], [], [], []
    frame, failure_handler = None, None
    gui_thread = threading.get_ident()
    scan_hold = SimpleNamespace(armed=False, ready=threading.Event(),
        release=threading.Event(), manifest=None, thread=None)
    actual_picker, actual_snapshot = wx.DirDialog, batch_scan.planned_snapshot

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def failure(detail):
        errors.append(detail)
        print('FAIL: native backup and import access\n' + detail, file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failure(detail)
                scan_hold.release.set()
                if frame is not None:
                    frame.cancel_event.set()
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND

    def pump(predicate, description, timeout=8):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)

    def inside(peer, ancestor):
        for _ in range(64):
            if peer is None or not peer:
                return False
            if peer.GetId() == ancestor.GetId():
                return True
            peer = peer.GetParent()
        raise ValueError('Native parent chain exceeds its bound.')

    def visible(control):
        rectangle = control.GetScreenRect()
        parent = control.GetParent()
        for _ in range(64):
            if parent is None:
                return True
            if isinstance(parent, wx.ScrolledWindow):
                origin = parent.ClientToScreen((0, 0))
                size = parent.GetClientSize()
                viewport = wx.Rect(origin.x, origin.y, size.width, size.height)
                if not viewport.Contains(rectangle):
                    return False
            parent = parent.GetParent()
        raise ValueError('Native viewport chain exceeds its bound.')

    def raw_key(code, *, modifiers=()):
        pressed = []
        try:
            for modifier in modifiers:
                check('native modifier down', simulator.KeyDown(modifier))
                pressed.append(modifier)
            check('native key down', simulator.KeyDown(code))
            pressed.append(code)
            sleep(.015)
        finally:
            for selected in reversed(pressed):
                check('native key released', simulator.KeyUp(selected))

    def focus(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: inside(wx.Window.FindFocus(), control)
             and user32.GetForegroundWindow() == top.GetHandle() and visible(control),
             'A named native control did not receive visible foreground focus.')
        check('visible native focus: ' + control.GetName(), visible(control))

    def key(control, code, *, modifiers=()):
        focus(control)
        raw_key(code, modifiers=modifiers)
        # The caller yields before the button can enter its actual nested loop.

    def press(control):
        key(control, wx.WXK_SPACE)

    def descendants(parent):
        pending, result = list(parent.GetChildren()), []
        while pending:
            if len(result) + len(pending) > 512:
                raise ValueError('Native access control inventory exceeds its bound.')
            item = pending.pop()
            if not item:
                raise ValueError('Native control disappeared during selection.')
            result.append(item)
            pending.extend(item.GetChildren())
        return result

    def named(parent, name):
        rows = [item for item in descendants(parent) if item.GetName() == name
                or isinstance(item, wx.Button) and item.GetLabel().replace('&', '') == name]
        check('one actual named control: ' + name, len(rows) == 1)
        return rows[0]

    def button_id(parent, identity):
        rows = [item for item in descendants(parent)
                if isinstance(item, wx.Button) and item.GetId() == identity]
        check('one actual standard dialog button', len(rows) == 1)
        return rows[0]

    def observe_peer(peer):
        identity = peer.GetId()
        if not any(row.dialog is peer for row in peers):
            record = SimpleNamespace(dialog=peer, native_id=identity,
                                     title=peer.GetTitle(), events=[])
            def destroyed(event):
                if event.GetId() == identity:
                    record.events.append(identity)
                event.Skip()
            peer.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
            peers.append(record)
        return peer

    def window(title):
        rows = [item for item in wx.GetTopLevelWindows()
                if item and isinstance(item, wx.Dialog) and item.GetTitle() == title]
        if len(rows) > 1:
            raise AssertionError('More than one native owner has the same access title.')
        return observe_peer(rows[0]) if rows else None

    def owners():
        if frame is None or not frame:
            return ()
        windows = tuple(wx.GetTopLevelWindows())
        if len(windows) > 256:
            raise ValueError('Native owned-window inventory exceeds its bound.')
        modals = [peer for peer in windows if peer and isinstance(peer, wx.Dialog)
                  and inside(peer, frame) and peer.IsModal()]
        # EndModal can re-enable a suspended owner before the child ShowModal
        # returns. Hidden still-modal peers are unconditional unwind barriers.
        hidden = [peer for peer in modals if not peer.IsShown()]
        if hidden:
            chosen = hidden[0]
        elif modals:
            focused = wx.Window.FindFocus()
            focused_top = wx.GetTopLevelParent(focused) if focused else None
            active = [peer for peer in modals if peer.IsEnabled()]
            chosen = next((peer for peer in active if peer == focused_top), None)
            if chosen is None:
                if len(active) != 1:
                    raise RuntimeError('The active owned modal has not completed its return.')
                chosen = active[0]
        else:
            return ()
        # A checksum dialog is parented to Desktop even when opened from base
        # review. Actual active focus, not tree depth, identifies its modal loop.
        return (SimpleNamespace(dialog=chosen,
            close=lambda value=chosen: value.EndModal(wx.ID_CANCEL)),)

    abort = _OwnedModalAbort(wx, gui, owners, failure)

    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value

    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)
    check('actual native Windows controls', wx.Platform == '__WXMSW__')

    root = Path(mkdtemp(prefix='carr-native-backup-import-')).resolve(strict=True)
    source_root, backups, restored_parent = (root / 'Authored sources', root / 'Backups', root / 'Restored')
    for directory in (source_root, backups, restored_parent):
        directory.mkdir()
    originals, phrases = [], {}
    for number in range(1, 4):
        path = source_root / ('%02d — Україна.txt' % number)
        phrase = 'Native source %d — Україна α 😀 & exact quotation.' % number
        raw = ('\ufeff' + phrase + '\r\n\fSecond segment %d.\r\n' % number).encode('utf-8')
        path.write_bytes(raw)
        originals.append(path)
        phrases[path.name] = (phrase, raw, sha256(raw).hexdigest())
    app = LibraryApp(root / 'Private library')
    workspace = app.create_workspace('Native import and backup library')
    from carr_library.keybindings import encode_preferences
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    frame = desktop_app.DesktopFrame(app)
    frame.SetSize((1000, 850))
    frame.Show()
    gui.SetTopWindow(frame)
    check('actual Desktop exists before its main event loop',
          bool(frame) and frame.IsShown() and frame.library is app)
    desktop_executor = frame.executor
    actual_submit = desktop_executor.submit

    def submit(operation, *args, **kwargs):
        record = SimpleNamespace(thread=None, future=None)
        def observed():
            record.thread = threading.get_ident()
            return operation(*args, **kwargs)
        future = actual_submit(observed)
        record.future = future
        jobs.append(record)
        return future

    desktop_executor.submit = submit

    def snapshot(*args, **kwargs):
        captured = actual_snapshot(*args, **kwargs)
        selected = Path(args[0]).absolute()
        if scan_hold.armed and selected == originals[2]:
            scan_hold.armed = False
            scan_hold.manifest = captured.manifest()
            scan_hold.thread = threading.get_ident()
            scan_hold.ready.set()
            if not scan_hold.release.wait(25):
                raise TimeoutError('The actual source-snapshot hold was not released.')
        return captured

    batch_scan.planned_snapshot = snapshot

    class SelectedFolder(actual_picker):
        """Real native peer with disclosed, authored existing-folder answers."""
        def __init__(self, parent, title, **kwargs):
            check('folder picker belongs to the current native Desktop',
                  bool(parent) and inside(parent, frame))
            check('one explicit authored folder answer is queued', bool(directory_answers))
            expected, path = directory_answers.pop(0)
            check('folder picker prompt matches the explicit source selection', title == expected)
            check('authored folder answer already exists', path.is_dir())
            super().__init__(parent, title, **kwargs)
            self.path = path
        def ShowModal(self):
            return wx.ID_OK
        def GetPath(self):
            if not self or self.IsBeingDeleted():
                raise RuntimeError('The folder picker was already destroyed.')
            return str(self.path)

    wx.DirDialog = SelectedFolder
    menu_state = SimpleNamespace(opened=0, highlighted=None)
    def opened(event):
        menu_state.opened += 1
        event.Skip()
    def highlighted(event):
        menu_state.highlighted = event.GetId()
        event.Skip()
    frame.Bind(wx.EVT_MENU_OPEN, opened)
    frame.Bind(wx.EVT_MENU_HIGHLIGHT_ALL, highlighted)
    state = SimpleNamespace(waiting=None, description='', entered=False, finished=False,
                            started=monotonic())
    timer = wx.Timer(frame)

    def waiting(predicate, description, seconds=60):
        return predicate, description, monotonic() + seconds

    def quiet():
        return not frame.working and all(item.future.done() for item in jobs)

    def text_value(control):
        if isinstance(control, wx.SpinCtrl):
            return str(control.GetValue())
        return control.GetValue()

    def type_text(control, value):
        check('authored native entry is ASCII only', value.isascii())
        key(control, ord('A'), modifiers=(wx.WXK_CONTROL,))
        check('native text input accepted bytes', simulator.Text(value.encode('ascii')))
        yield waiting(lambda: text_value(control) == value, 'Native entry did not retain its exact value', 8)

    def command(label, title):
        check('menu operation starts with the same idle Desktop owner', quiet() and not owners())
        menu = frame.GetMenuBar().GetMenu(0)
        items = [item for item in menu.GetMenuItems() if not item.IsSeparator() and item.IsEnabled()]
        matches = [index for index, item in enumerate(items) if item.GetItemLabelText() == label]
        check('one actual Library menu command: ' + label, len(matches) == 1)
        before = menu_state.opened
        menu_state.highlighted = None
        key(frame.status, ord('L'), modifiers=(wx.WXK_ALT,))
        yield waiting(lambda: menu_state.opened > before, 'The actual Library menu did not open', 8)
        raw_key(wx.WXK_HOME)
        yield waiting(lambda: menu_state.highlighted == items[0].GetId(),
                      'The first actual Library menu command was not highlighted', 8)
        for item in items[1:matches[0] + 1]:
            raw_key(wx.WXK_DOWN)
            yield waiting(lambda identity=item.GetId(): menu_state.highlighted == identity,
                          'Keyboard navigation did not select the expected Library command', 8)
        check('native menu highlight is the exact selected command',
              menu_state.highlighted == items[matches[0]].GetId())
        raw_key(wx.WXK_RETURN)
        yield waiting(lambda: window(title) is not None, 'The actual command did not open ' + title, 15)

    def close_report(title):
        peer = window(title)
        check('actual report is still owned and modal', peer is not None and peer.IsModal())
        press(button_id(peer, wx.ID_CANCEL))
        yield waiting(lambda: window(title) is None, 'The actual completed report did not return', 10)

    def checked_file(path, maximum=8 * 1024**2):
        before = durable_state.regular(path)
        check('authored proof file stays within its bound', 0 <= before.st_size <= maximum)
        with durable_state._open_json_file(path) as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(before.st_size + 1)
            after = os.fstat(stream.fileno())
        current = durable_state.regular(path)
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        check('proof reads exact ordinary unchanged bytes', stat.S_ISREG(opened.st_mode)
              and opened.st_nlink == 1 and stamp(before) == stamp(opened) == stamp(after) == stamp(current)
              and len(raw) == before.st_size)
        return raw

    def inventory(directory):
        pending, rows = [directory], {}
        while pending:
            parent = pending.pop()
            with os.scandir(parent) as entries:
                for entry in entries:
                    if len(rows) + len(pending) >= 2048:
                        raise ValueError('Authored native proof inventory exceeds its bound.')
                    path = Path(entry.path)
                    info = path.lstat()
                    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        durable_state.regular(path, directory=True)
                        pending.append(path)
                    else:
                        raw = checked_file(path)
                        rows[path.relative_to(directory).as_posix()] = sha256(raw).hexdigest()
        return rows

    def journal_bytes():
        directory = app.root / 'import-batches'
        return {path.name: checked_file(path) for path in directory.glob('*.json')}

    def result_record():
        check('an actual Desktop job has returned', bool(jobs) and jobs[-1].future.done())
        return jobs[-1].future.result()

    def backup_review(title):
        peer = window(title)
        check('real resource review starts with historical defaults', peer is not None
              and peer.IsModal() and peer.reviewed.GetValue() is False)
        press(named(peer, 'Use reviewed backup resource limits'))
        yield waiting(lambda: peer.reviewed.GetValue() and all(item.IsEnabled() for item in peer.fields.values()),
                      'Reviewed backup fields were not enabled', 8)
        for field, value in (('original_bytes', '128'), ('seconds', '120'),
                             ('memory_bytes', '640'), ('staging_bytes', '256')):
            yield from type_text(peer.fields[field], value)
        if title == 'Backup options':
            if peer.include_audio.GetValue():
                press(peer.include_audio)
                yield waiting(lambda: not peer.include_audio.GetValue(), 'Explicit generated-audio exclusion', 8)
        press(named(peer, 'Accept reviewed backup options'))
        yield waiting(lambda: window(title) is None, 'The real resource review did not return', 10)
        chosen = peer.result
        check('accepted resource policy is the real immutable typed object',
              type(chosen) is backup_resources.BackupOptions
              and type(chosen.budget) is backup_budget.BackupBudget
              and chosen.budget.original_bytes == 128 * 1024**2
              and chosen.budget.seconds == 120 and chosen.budget.memory_bytes == 640 * 1024**2
              and chosen.budget.staging_bytes == 256 * 1024**2 and chosen.include_generated is False)

    def checksum(value):
        title = 'Verify the selected backup identity'
        yield waiting(lambda: window(title) is not None, 'Actual independent-checksum dialog', 15)
        peer = window(title)
        text = [item for item in descendants(peer) if isinstance(item, wx.TextCtrl)]
        check('one real native independent-checksum entry', len(text) == 1 and peer.IsModal())
        yield from type_text(text[0], value)
        check('native checksum value equals the independently retained receipt', peer.GetValue() == value)
        press(button_id(peer, wx.ID_OK))
        yield waiting(lambda: window(title) is None, 'Native checksum entry did not return', 10)

    def scenario():
        expected_policy = resource_budget.ImportBudget(source_bytes=128 * 1024**2,
            segment_bytes=1024**2, segments=16, steps=17, seconds=60,
            memory_bytes=512 * 1024**2, library_bytes=128 * 1024**2,
            temporary_bytes=256 * 1024**2)
        yield from command('Import Center: files, folders and queues', 'Import Center')
        center = window('Import Center')
        check('opening Import Center performs no import or worker admission',
              not jobs and not center.busy and center.batch is None and len(app.browse(workspace)) == 0)
        yield from type_text(center.page_files, '1')
        yield from type_text(center.page_entries, '20000')
        yield from type_text(center.page_seconds, '60')
        press(named(center, 'Review import resource allowances for new plans'))
        yield waiting(lambda: window('Import resource allowances') is not None,
                      'Open actual native import resource review', 10)
        resource = window('Import resource allowances')
        for field, value in (('source_mib', '128'), ('segment_mib', '1'), ('segments', '16'),
                             ('steps', '17'), ('seconds', '60'), ('memory_mib', '512'),
                             ('library_mib', '128'), ('temporary_mib', '256')):
            yield from type_text(resource.fields[field], value)
        press(named(resource, 'Use reviewed import allowances'))
        yield waiting(lambda: window('Import resource allowances') is None,
                      'Return from actual reviewed import resource choices', 10)
        check('future plans use exactly the reviewed 128 MiB typed import policy',
              center.import_budget == expected_policy and not jobs)
        directory_answers.append(('Select a folder of books', source_root))
        press(named(center, 'Add folder...'))
        yield waiting(lambda: quiet() and not center.busy and center.batch is not None,
                      'Save actual source scan page one', 90)
        first = center.batch
        check('first real saved page contains one exact queued source and no import',
              first['version'] == 3 and first['scan']['page'] == 0 and first['total'] == 1
              and first['rows'][0]['source'] == str(originals[0])
              and first['rows'][0]['state'] == 'queued'
              and streamed_text.profile_from_value(first['source_profile']).import_budget == expected_policy
              and app.browse(workspace) == ())
        check('reviewed scan does not create an import request for its source',
              not (app.root / 'import-requests' / (first['rows'][0]['request_id'] + '.json')).exists())
        press(named(center, 'Start queued files'))
        yield waiting(lambda: quiet() and not center.busy
                      and center.batch['counts'].get('complete') == 1,
                      'Explicitly import the first real saved page', 90)
        first_done = center.batch
        check('the first existing isolated worker completed its selected source',
              len(app.browse(workspace)) == 1 and first_done['rows'][0]['state'] == 'complete')
        press(named(center, 'Continue source scan'))
        yield waiting(lambda: quiet() and not center.busy and center.batch['scan']['page'] == 1,
                      'Save actual continuation page two', 90)
        second = center.batch
        check('second page preserves the source traversal and reviewed resources',
              second['scan']['previous'] == first_done['batch_id']
              and second['scan']['session_id'] == first_done['scan']['session_id']
              and second['total'] == 1 and second['rows'][0]['source'] == str(originals[1])
              and second['rows'][0]['state'] == 'queued'
              and streamed_text.profile_from_value(second['source_profile']).import_budget == expected_policy
              and len(app.browse(workspace)) == 1)
        press(named(center, 'Start queued files'))
        yield waiting(lambda: quiet() and not center.busy
                      and center.batch['counts'].get('complete') == 1,
                      'Explicitly import the second saved page', 90)
        second_done = center.batch
        check('only the first two sources are actually imported', len(app.browse(workspace)) == 2)
        before_cancel = journal_bytes()
        requests_before_cancel = {path.name: checked_file(path)
            for path in (app.root / 'import-requests').glob('*.json')}
        journal_before_cancel = checked_file(app.root / 'imports.sqlite')
        previous_cursor = json.loads(json.dumps(second_done['scan']['cursor']))
        scan_hold.armed = True
        press(named(center, 'Continue source scan'))
        yield waiting(scan_hold.ready.is_set, 'Hold the genuine third-file snapshot before cursor advance', 30)
        check('the scan hold contains an actual current source snapshot on the Desktop worker',
              scan_hold.thread != gui_thread
              and scan_hold.manifest == dict(sha256=phrases[originals[2].name][2],
                                            mime='text/plain', size=len(phrases[originals[2].name][1]))
              and center.busy and frame.working and center.cancel.IsEnabled())
        cancelled_record = jobs[-1]
        press(center.cancel)
        yield waiting(frame.cancel_event.is_set, 'Native Cancel operation requests cancellation', 8)
        scan_hold.release.set()
        yield waiting(lambda: quiet() and not center.busy,
                      'The original scanner observes cancellation after its real snapshot', 30)
        check('cancelled scan returns the actual InterruptedError, never a forged page',
              isinstance(cancelled_record.future.exception(), InterruptedError))
        check('cancellation preserves every saved page byte and exact prior source cursor',
              journal_bytes() == before_cancel
              and app.import_batch(workspace, second_done['batch_id'])['scan']['cursor'] == previous_cursor
              and len(app.import_batches(workspace)) == 2 and len(app.browse(workspace)) == 2)
        check('cancelled scan creates no request JSON, import journal change or catalog edition',
              {path.name: checked_file(path) for path in (app.root / 'import-requests').glob('*.json')}
              == requests_before_cancel
              and checked_file(app.root / 'imports.sqlite') == journal_before_cancel
              and len(app.browse(workspace)) == 2)
        press(named(center, 'Continue source scan'))
        yield waiting(lambda: quiet() and not center.busy and center.batch['scan']['page'] == 2,
                      'Explicitly resume the same saved traversal to page three', 90)
        third_queued = center.batch
        check('resumed page contains the exact skipped source once, still queued',
              third_queued['scan']['previous'] == second_done['batch_id']
              and third_queued['scan']['session_id'] == second_done['scan']['session_id']
              and third_queued['total'] == 1 and third_queued['rows'][0]['source'] == str(originals[2])
              and third_queued['rows'][0]['state'] == 'queued'
              and streamed_text.profile_from_value(third_queued['source_profile']).import_budget == expected_policy
              and len(app.browse(workspace)) == 2)
        queued_bytes = checked_file(app.root / 'import-batches' / (third_queued['batch_id'] + '.json'))
        press(button_id(center, wx.ID_CANCEL))
        yield waiting(lambda: window('Import Center') is None, 'Return from actual Import Center', 10)

        directory_answers.append(('Choose a parent folder for the library and project backup', backups))
        yield from command('Back up all libraries', 'Backup options')
        yield from backup_review('Backup options')
        yield waiting(lambda: window('Backup complete') is not None,
                      'Complete the real full backup on the existing Desktop executor', 120)
        full_digest = result_record()
        check('the full backup returns an actual independent manifest digest',
              isinstance(full_digest, str) and len(full_digest) == 64)
        roots = sorted(path for path in backups.iterdir() if path.name.startswith('ResearchLibrary-backup-'))
        check('the full backup publishes exactly one fresh directory', len(roots) == 1)
        full = roots[0]
        full_raw = checked_file(full / 'manifest.json')
        full_manifest = json.loads(full_raw)
        check('reported full backup checksum authenticates the published manifest',
              sha256(full_raw).hexdigest() == full_digest and full_manifest['version'] == 5
              and full_manifest['schema_version'] == revisions.CURRENT_SCHEMA_VERSION)
        check('actual full backup contains the queued reviewed page without importing it',
              checked_file(full / 'pending' / 'import-batches' / (third_queued['batch_id'] + '.json')) == queued_bytes
              and len(full_manifest['assets']) == 2 and len(app.browse(workspace)) == 2
              and app.import_batch(workspace, third_queued['batch_id'])['rows'][0]['state'] == 'queued')
        check('native completion displays the actual separately retained checksum',
              full_digest in named(window('Backup complete'), 'Backup complete').GetValue())
        full_before = inventory(full)
        yield from close_report('Backup complete')

        yield from command('Import Center: files, folders and queues', 'Import Center')
        center = window('Import Center')
        check('reopened saved scan remains passive', not center.busy and len(app.browse(workspace)) == 2)
        selections = [index for index, row in enumerate(center.summaries)
                      if row['batch_id'] == third_queued['batch_id']]
        check('the actual saved third page can be selected once', len(selections) == 1)
        key(center.batches, wx.WXK_HOME)
        yield waiting(lambda: center.batches.GetSelection() == 0, 'Native saved-page list first selection', 8)
        for index in range(selections[0]):
            key(center.batches, wx.WXK_DOWN)
            yield waiting(lambda target=index + 1: center.batches.GetSelection() == target,
                          'Native saved-page list selection', 8)
        yield waiting(lambda: center.batch is not None
                      and center.batch['batch_id'] == third_queued['batch_id'],
                      'The selected real saved page is displayed', 8)
        check('restored view retains its saved import policy and queued source',
              center.batch['rows'][0]['state'] == 'queued'
              and streamed_text.profile_from_value(center.batch['source_profile']).import_budget == expected_policy)
        press(named(center, 'Start queued files'))
        yield waiting(lambda: quiet() and not center.busy
                      and center.batch['counts'].get('complete') == 1,
                      'Explicitly import the retained third page', 90)
        third_done = center.batch
        check('all three exact source editions require their actual explicit run',
              len(app.browse(workspace)) == 3 and third_done['rows'][0]['state'] == 'complete')
        press(button_id(center, wx.ID_CANCEL))
        yield waiting(lambda: window('Import Center') is None, 'Return before incremental review', 10)

        directory_answers.extend((('Select the base backup', full),
            ('Choose a parent folder for a new incremental backup', backups)))
        yield from command('Create incremental backup...', 'Backup options')
        yield from backup_review('Backup options')
        yield from checksum(full_digest)
        yield waiting(lambda: window('Incremental backup complete') is not None,
                      'Create an actual delta from the explicit full generation', 120)
        delta_receipt = result_record()
        check('the actual incremental receipt binds the selected full backup',
              delta_receipt['kind'] == 'carr-library-incremental'
              and delta_receipt['base_required'] is True
              and delta_receipt['base_sha256'] == full_digest)
        delta, delta_digest = Path(delta_receipt['path']), delta_receipt['manifest_sha256']
        check('the delta publishes only inside the explicitly chosen backup parent',
              delta.parent == backups and delta != full and delta.is_dir())
        delta_raw = checked_file(delta / 'manifest.json')
        check('actual incremental checksum matches its real published manifest',
              sha256(delta_raw).hexdigest() == delta_digest
              and delta_digest in named(window('Incremental backup complete'), 'Incremental backup complete').GetValue())
        delta_before = inventory(delta)
        check('creating the delta leaves the entire full generation unchanged', inventory(full) == full_before)
        yield from close_report('Incremental backup complete')
        for path in originals:
            check('authored UTF8 BOM, CRLF and formfeed originals remain byte exact',
                  checked_file(path) == phrases[path.name][1])
        offline = root / 'Authored sources offline'
        source_root.rename(offline)
        check('the original selected source paths are unavailable before verify and restore',
              not source_root.exists() and all(not path.exists() for path in originals))

        directory_answers.extend((('Select the incremental backup to verify', delta),
                                  ('Select required base backup: ' + full_digest, full)))
        yield from command('Verify incremental backup chain...', 'Restore resources')
        yield from backup_review('Restore resources')
        yield from checksum(delta_digest)
        yield waiting(lambda: window('Backup chain verified') is not None,
                      'Verify the actual explicitly selected generation chain', 120)
        verified = result_record()
        check('verification returns the exact selected manifest and full-base chain',
              verified['manifest_sha256'] == delta_digest
              and verified['snapshot_sha256'] == delta_receipt['snapshot_sha256']
              and verified['chain'] == [delta_digest, full_digest])
        check('real verification leaves both source generations byte exact',
              inventory(full) == full_before and inventory(delta) == delta_before)
        yield from close_report('Backup chain verified')

        directory_answers.extend((('Select the incremental backup to restore', delta),
            ('Select required base backup: ' + full_digest, full),
            ('Choose a parent folder for the restored library', restored_parent)))
        yield from command('Restore incremental backup to a new folder...', 'Restore resources')
        yield from backup_review('Restore resources')
        yield from checksum(delta_digest)
        yield waiting(lambda: window('Backup restored') is not None,
                      'Restore the actual full-plus-delta snapshot into a new folder', 120)
        restored_result = result_record()
        restored_root = Path(restored_result['restored_root'])
        check('native restore publishes a new reviewed root without switching the live library',
              restored_root.parent == restored_parent and restored_root != app.root
              and restored_root.is_dir() and restored_result['manifest_sha256'] == delta_digest
              and frame.library is app and frame.library.root == app.root)
        check('restore completion explains explicit later root activation',
              'Open the restored folder' in named(window('Backup restored'), 'Backup restored').GetValue())
        yield from close_report('Backup restored')

        restored = LibraryApp(restored_root)
        try:
            check('all three actual catalog editions survive full-plus-delta restore',
                  len(restored.browse(workspace)) == 3)
            completed = (first_done, second_done, third_done)
            for number, (original, page) in enumerate(zip(originals, completed), 1):
                phrase, raw, digest = phrases[original.name]
                saved = restored.import_batch(workspace, page['batch_id'])
                check('restored scan policy stays typed and continuation requires fresh review',
                      saved['version'] == 3 and streamed_text.profile_from_value(saved['source_profile']).import_budget == expected_policy
                      and saved['scan']['review_required'] is True
                      and saved['rows'][0]['state'] == 'complete')
                retained = restored.assets.resolve(digest,
                    profile=streamed_text.profile_from_value(saved['source_profile']))
                check('restored retained original equals every authored input byte',
                      checked_file(retained) == raw and checked_file(offline / original.name) == raw)
                hits = restored.search(workspace, 'Native source ' + str(number),
                                       mode='phrase', result_mode='short', limit=2)
                check('restored source is a verified logical segment with exact current edition',
                      len(hits) == 1 and hits[0].verified is True
                      and hits[0].edition_id == page['rows'][0]['edition_id']
                      and hits[0].page == 'Segment 1 (unpaginated)' and hits[0].quote == phrase)
                check('actual source-authorized quotation copy preserves Unicode after restore',
                      restored.copy(workspace, hits[0].ref, hits[0].revision, 'quote') == phrase)
        finally:
            with restored.read(workspace) as restored_database:
                check('restored quotation verification leaves an ordinary fresh authorized catalog read',
                      restored_database.execute('SELECT count(*) FROM catalog_edition WHERE workspace=?',
                                                (workspace,)).fetchone()[0] == 3)
        check('all full and incremental archive bytes remain unchanged after restore and source copy',
              inventory(full) == full_before and inventory(delta) == delta_before)
        check('the actual current library and completed original imports remain unchanged',
              frame.library is app and len(app.browse(workspace)) == 3
              and all(app.import_batch(workspace, item['batch_id'])['rows'][0]['state'] == 'complete'
                      for item in (first_done, second_done, third_done)))
        check('every admitted job belongs to the single existing Desktop worker',
              frame.executor is desktop_executor and len({record.thread for record in jobs}) == 1
              and all(record.thread != gui_thread and record.future.done() for record in jobs)
              and all(record is cancelled_record or record.future.exception() is None for record in jobs))
        check('only the one explicitly cancelled scan failed',
              sum(record.future.exception() is not None for record in jobs) == 1)
        check('all authored folder choices were used by their exact native workflow prompts',
              not directory_answers)
        yield waiting(lambda: not owners() and all(not row.dialog and len(row.events) == 1 for row in peers),
                      'Actual native modal destruction completes after modal return', 8)
        check('every observed actual native modal returned and was destroyed exactly once',
              not owners() and all(not row.dialog and len(row.events) == 1 for row in peers))
        state.finished = True

    sequence = scenario()

    def drive(event=None):
        if state.entered or state.finished:
            return
        state.entered = True
        try:
            if monotonic() - state.started > 450:
                raise TimeoutError('The native backup/import access workflow exceeded its total bound.')
            if state.waiting is not None:
                predicate, state.description, deadline = state.waiting
                if not predicate():
                    if monotonic() > deadline:
                        raise TimeoutError(state.description)
                    return
                state.waiting = None
            try:
                state.waiting = next(sequence)
            except StopIteration:
                if not state.finished:
                    raise AssertionError('Native access scenario ended without its final checks.')
                timer.Stop()
                gui.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            failure(traceback.format_exc() + '\nWaiting: ' + state.description
                    + '\nDesktop status: ' + (frame.status.GetValue() if frame else '<deleted>'))
            timer.Stop()
            scan_hold.release.set()
            if frame:
                frame.cancel_event.set()
            abort.request()
        finally:
            state.entered = False

    frame.Bind(wx.EVT_TIMER, drive, timer)
    timer.Start(30)
    successful = False
    try:
        gui.MainLoop()
        if errors or not state.finished:
            raise RuntimeError('\n'.join(errors) or 'Native access workflow did not complete.')
        check('selected installed sources remain identical after all real operations',
              _installed_sources(*modules) == identities)
        check('selected native probe remains byte exact',
              {str(Path(__file__).resolve()): sha256(Path(__file__).read_bytes()).hexdigest()} == probe_identity)
        successful = True
    finally:
        timer.Stop()
        timer.SetOwner(None)
        scan_hold.release.set()
        abort.stop()
        wx.DirDialog = actual_picker
        batch_scan.planned_snapshot = actual_snapshot
        desktop_executor.submit = actual_submit
        if frame:
            frame.cancel_event.set()
            frame._closing = True
        desktop_executor.shutdown(wait=True, cancel_futures=False)
        from carr_library.audio.cleanup import drain_pending_cleanup
        interruption = drain_pending_cleanup()
        if frame:
            if getattr(frame, 'progress_panel', None) is not None:
                frame.progress_panel.close()
            frame.Destroy()
        gui.Yield()
        if successful:
            if interruption is not None:
                raise interruption
            if any(not record.future.done() for record in jobs):
                raise RuntimeError('A Desktop operation still owns the authored native fixture.')
            import shutil
            shutil.rmtree(root)
        else:
            print('RETAINED_FAILED_NATIVE_FIXTURE ' + str(root), file=sys.stderr, flush=True)

    print(json.dumps({'result': 'PASS', 'checks': len(checks),
        'native_keyboard': 'PASS', 'actual_scan_page_continuation': 'PASS',
        'actual_snapshot_then_native_cancel': 'PASS', 'reviewed_import_original_mib': 128,
        'full_incremental_verify_restore': 'PASS', 'independent_checksum_and_required_base_folders': 'PASS',
        'same_desktop_executor': 'PASS', 'restored_original_bytes_and_verified_copy': 'PASS',
        'source_identity': identities, 'probe_identity': probe_identity,
        'native_full_backup_format': 5, 'flat4_compatibility': 'SEPARATE_PORTABLE_GATE',
        'native_folder_picker': 'NOT_RUN', 'large_original_capacity': 'SEPARATE_API_GATE',
        'physical_output': 'NOT_RUN', 'NVDA': 'NOT_RUN',
        'controlled_seams': ['authored existing-folder picker answers',
                            'hold after actual third source snapshot before existing cancellation check']},
        ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
