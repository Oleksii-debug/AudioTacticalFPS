"""Actual Windows menu/background/result controls with synthetic backup choices.

Directory selections are supplied by the fixture. The canonical create/verify/
restore, DesktopFrame executor and native result dialog remain real. This does
not claim manual file-picker, human NVDA, disk-power-loss or engine qualification.
"""
import argparse
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import sysconfig
from tempfile import TemporaryDirectory
import threading
import time
import traceback


def _run_probe(args):
    if os.name != 'nt':
        raise RuntimeError('This probe requires actual Windows and wx controls.')
    import wx
    from carr_library import backup_incremental, backup_incremental_cli, library_backup
    from carr_library.application import LibraryApp
    from carr_library.speech.reading import ReadingDocument
    from carr_library.reader.reading_state import ReadingLocation, ReadingState
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    from carr_library.ui import backup_incremental_dialog, desktop_app

    checks, errors = [], []
    def check(label, value):
        if not value:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)
    if args.installed:
        checkout = Path(__file__).resolve().parents[1]
        installed = (Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR')
                     else Path(sysconfig.get_path('purelib')).resolve())
        for module in (backup_incremental, backup_incremental_cli, library_backup, backup_incremental_dialog):
            actual = Path(module.__file__).resolve()
            expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
            check('exact installed origin: ' + module.__name__,
                  actual.is_relative_to(installed) and not actual.is_relative_to(checkout)
                  and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False
    app = App(False)
    app.SetExitOnFrameDelete(False)
    ui_thread = threading.get_ident()
    def pump(predicate, label, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() > deadline:
                raise TimeoutError(label)
            app.Yield()
            time.sleep(.01)
        if errors:
            raise RuntimeError('\n'.join(errors))

    class Choices:
        def __init__(self, frame, folders, checksum):
            self.frame, self.folders, self.sha256 = frame, iter(folders), checksum
            self.results = []
        def folder(self, title):
            check('native action choices stay on UI thread', threading.get_ident() == ui_thread)
            return next(self.folders)
        def checksum(self):
            return self.sha256
        def show(self, title, body):
            check('native completion returns to UI thread', threading.get_ident() == ui_thread)
            dialog = wx.Dialog(self.frame, title=title, size=(740, 460))
            try:
                layout = wx.BoxSizer(wx.VERTICAL)
                reader = wx.TextCtrl(dialog, value=body, style=wx.TE_READONLY | wx.TE_MULTILINE)
                reader.SetName('Verified backup completion and checksum')
                close = wx.Button(dialog, wx.ID_CANCEL, 'Close')
                layout.Add(reader, 1, wx.EXPAND | wx.ALL, 8)
                layout.Add(close, 0, wx.ALL, 8)
                dialog.SetSizer(layout)
                def inspect():
                    try:
                        reader.SetFocus()
                        check('real readable completion text', reader.GetValue() == body)
                        check('real completion focus', wx.Window.FindFocus() is reader)
                        self.results.append((title, json.loads(body.split('\n\n', 1)[1])))
                    except BaseException:
                        errors.append(traceback.format_exc())
                    finally:
                        dialog.EndModal(wx.ID_CANCEL)
                wx.CallLater(120, inspect)
                dialog.ShowModal()
            finally:
                dialog.Destroy()

    with TemporaryDirectory(prefix='carr-native-incremental-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native incremental recovery')
        library.add_text(workspace, 'Exact source', 'Immutable Unicode original. Київ 😀', edition_id='source')
        document = ReadingDocument('Original reading copy.')
        ReaderDraftStore(library.root).save(ReaderResume(document, 'missing:exact-voice', 1, 70, 0, 0))
        full = root / 'full'
        checksum = library_backup.create_backup(library, full)
        document.set_text('Revised reading copy.')
        with library.read(workspace) as db:
            row = db.execute('SELECT e.active_revision,e.asset,p.ordinal,p.physical,p.digest '
                'FROM catalog_edition e JOIN page p ON p.revision=e.active_revision '
                'WHERE e.workspace=? AND e.id=? ORDER BY p.ordinal LIMIT 1',
                (workspace, 'source')).fetchone()
        revision, asset, ordinal, physical, text_digest = row
        location = ReadingLocation(workspace, 'source', revision,
            'physical_page' if physical is not None else 'logical_segment',
            physical if physical is not None else ordinal, text_digest, asset)
        reading = ReadingState(library.root)
        reading.remember(location)
        mark = reading.add_bookmark(location, 'My return location')
        ReaderDraftStore(library.root).save(ReaderResume(document, 'missing:exact-voice', 1, 70, 8, 2.5))
        frame = desktop_app.DesktopFrame(library)
        frame.Show()
        workers = []
        actual_run_job = frame.run_job
        def run_job(label, work, done=None, failed=None, **options):
            def observed():
                workers.append(threading.get_ident())
                return work()
            return actual_run_job(label, observed, done, failed, **options)
        frame.run_job = run_job
        try:
            def choose(label, folders, selected):
                dialogs = Choices(frame, folders, selected)
                frame.incremental_backup_actions.dialogs = dialogs
                menu = frame.GetMenuBar().GetMenu(0)
                candidates = [item for item in menu.GetMenuItems()
                              if item.GetItemLabelText() == label]
                check('registered native Library command: ' + label, len(candidates) == 1)
                event = wx.CommandEvent(wx.EVT_MENU.typeId, candidates[0].GetId())
                check('actual native menu dispatch: ' + label, frame.GetEventHandler().ProcessEvent(event))
                pump(lambda: len(dialogs.results) == 1 and not frame.working, label)
                return dialogs.results[0][1]
            receipt = choose('Create incremental backup...', [full, root], checksum)
            layer = Path(receipt['path'])
            check('actual incremental layer published', (layer / 'manifest.json').is_file())
            verified = choose('Verify incremental backup chain...', [layer, full], receipt['manifest_sha256'])
            check('actual required chain verified', verified['chain'] == [receipt['manifest_sha256'], checksum])
            restored = choose('Restore incremental backup to a new folder...', [layer, full, root], receipt['manifest_sha256'])
            target = Path(restored['restored_root'])
            recovered = LibraryApp(target)
            check('actual independent library restored', target != library.root and
                  recovered.edition_metadata(workspace, 'source').title == 'Exact source')
            resume = ReaderDraftStore(target).load()
            check('actual reader bookmark and exact missing voice retained',
                  ReadingState(target).bookmarks(workspace, 'source') == (mark,) and
                  ReadingState(target).resume(workspace, 'source') == location and
                  (resume.voice_id, resume.cursor, resume.audio_seconds) == ('missing:exact-voice', 8, 2.5))
            check('all heavy operations used existing background worker', len(workers) == 8 and
                  all(identity != ui_thread for identity in workers))
        finally:
            frame.Close()
            pump(lambda: getattr(frame, '_closing', False), 'Desktop cleanup')
            for window in tuple(wx.GetTopLevelWindows()):
                if window:
                    window.Destroy()
    app.Destroy()
    return {'status': 'PASS', 'checks': checks, 'windows_nvda': 'NOT_RUN',
            'manual_file_picker': 'NOT_RUN', 'power_loss': 'NOT_RUN',
            'mode': 'installed' if args.installed else 'source',
            'runtime': __import__('sys').version}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = {'status': 'FAILED', 'checks': [], 'reason': 'Probe did not complete.',
              'windows_nvda': 'NOT_RUN', 'manual_file_picker': 'NOT_RUN',
              'power_loss': 'NOT_RUN', 'runtime': __import__('sys').version}
    def persist():
        raw = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(raw, encoding='utf-8')
        print(raw, flush=True)
    persist()
    faulthandler.enable()
    faulthandler.dump_traceback_later(120, repeat=False, exit=True)
    try:
        result = _run_probe(args)
        persist()
        return 0
    except BaseException:
        result['reason'] = traceback.format_exc()
        persist()
        raise
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
