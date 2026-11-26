"""Bounded real native-wx evidence-page smoke for Windows and Linux.

Uses only synthetic source text and a temporary SQLite database. This checks
real controls and events; it does not test NVDA or any other screen reader.
Run directly with --native, or automatically on Windows/an existing display.
On POSIX, --xvfb PATH optionally starts an isolated disposable X server; the
launcher terminates only the process that it starts. Missing native prerequisites
produce NOT_RUN and exit 77. Assertion failures produce a nonzero failure exit.

From carr-library: python tests/ui/native_evidence_pages_smoke.py --native
"""
from argparse import ArgumentParser
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import selectors
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

NOT_RUN = 77


def not_run(reason, **details):
    print(json.dumps({'result': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN',
                      **details}, sort_keys=True), flush=True)
    return NOT_RUN


def run_native(source):
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('no native display is available; use an authorized native session or optional --xvfb')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('native wxPython is unavailable', error=str(exc))
    if not (source / 'src/carr_library/ui/evidence_panel.py').is_file():
        return not_run('assembled carr-library source is unavailable', source=str(source))
    sys.path.insert(0, str(source / 'src'))
    from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
    from carr_library.research.saved_evidence import SavedEvidenceStore
    from carr_library.ui.evidence_panel import EvidencePanel

    results = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        results.append(name)
        print('PASS:', name, flush=True)

    try:
        app = wx.App(False)
    except (Exception, SystemExit) as exc:
        return not_run('native wx application could not open its display', error=str(exc))

    def focused(control):
        # Native focus changes may arrive on a later event-loop iteration.
        deadline = monotonic() + 3
        while monotonic() < deadline:
            app.Yield()
            if wx.Window.FindFocus() == control:
                return True
            wx.MilliSleep(10)
        return False

    with TemporaryDirectory(prefix='carr-native-evidence-') as temporary:
        db = sqlite3.connect(Path(temporary) / 'synthetic.sqlite')
        frame = None
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript("CREATE TABLE workspace(id TEXT PRIMARY KEY); INSERT INTO workspace VALUES('a');")
            for name in ('topic_tree_candidate.sql', 'saved_evidence_candidate.sql'):
                db.executescript((source / 'tests' / 'reader' / name).read_text())
            identity = Identity('a', 'synthetic-edition', 'a' * 64, 'synthetic-revision')
            metadata = Metadata('Synthetic author', 'Synthetic source')
            raw = {3: 'Цитата — citácia.\nFirst exact source page.',
                   4: 'Druhý presný úryvok.\nSecond exact source page.'}
            first = make_anchor(identity, Locator('physical_page', 3), raw[3],
                                0, len(raw[3]), metadata, text_verified=True)
            second = make_anchor(identity, Locator('physical_page', 4), raw[4],
                                 0, 18, metadata, text_verified=True)
            changed = False

            def load(wanted, locator):
                return identity, locator, raw[locator.index] + (' changed' if changed else ''), True

            def attest(wanted, value):
                return wanted == identity and value == metadata

            @contextmanager
            def transaction():
                db.execute('BEGIN IMMEDIATE')
                with db:
                    yield SavedEvidenceStore(db, 'a', load, attest)

            with transaction() as service:
                service.tree.create('topic', 'Synthetic topic')
                service.save('topic', 'span', (first, second), 'Saved note')

            frame = wx.Frame(None, title='Synthetic evidence smoke', size=(700, 720))
            panel = EvidencePanel(frame, 'a', 'topic', transaction)
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(panel, 1, wx.EXPAND)
            frame.SetSizer(layout)
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            app.Yield()

            check('native wx Choice has two source parts',
                  isinstance(panel.page_choice, wx.Choice) and panel.page_choice.GetCount() == 2
                  and panel.page_choice.IsEnabled())
            check('initial quotation is exact first source slice', panel.quote.GetValue() == first.quote)
            check('source locators are outside the quotation',
                  'PDF page 3' in panel.page_choice.GetString(0)
                  and 'PDF page 4' in panel.page_choice.GetString(1))
            draft = 'Unsaved research note — зберегти дослівно.'
            panel.note.SetValue(draft)
            panel.page_choice.SetFocus()
            app.Yield()
            check('native selector accepts keyboard focus', focused(panel.page_choice))

            def select(index):
                panel.page_choice.SetSelection(index)
                event = wx.CommandEvent(wx.EVT_CHOICE.typeId, panel.page_choice.GetId())
                event.SetEventObject(panel.page_choice)
                event.SetInt(index)
                check('native choice event is handled for part ' + str(index + 1),
                      panel.page_choice.GetEventHandler().ProcessEvent(event))
                app.Yield()

            select(1)
            check('second part contains only its exact source quotation', panel.quote.GetValue() == second.quote)
            check('second part citation corresponds only to its own page',
                  'PDF page 4' in panel.citation.GetValue() and 'PDF page 3' not in panel.citation.GetValue())
            check('changing source part preserves unsaved note', panel.note.GetValue() == draft)
            select(0)
            check('returning to first part restores exact first quote', panel.quote.GetValue() == first.quote)
            check('returning to first part keeps draft', panel.note.GetValue() == draft)

            changed = True
            panel.save_note()
            app.Yield()
            check('source invalidation clears quotation and citation',
                  panel.quote.GetValue() == '' and panel.citation.GetValue() == '')
            check('source invalidation clears and disables native selector',
                  panel.page_choice.GetCount() == 0 and not panel.page_choice.IsEnabled())
            check('source invalidation preserves research draft', panel.note.GetValue() == draft)
            check('failure focuses actual native status control', focused(panel.status))
            check('status explains keyboard return to retained draft',
                  'Shift+Tab returns to the draft' in panel.status.GetValue())
            panel.show_page()
            check('cleared selector cannot revive cached source text', panel.quote.GetValue() == '')
            check('native backward focus navigation succeeds',
                  panel.status.Navigate(flags=wx.NavigationKeyEvent.IsBackward))
            app.Yield()
            check('backward navigation returns to draft control', focused(panel.note))
            print(json.dumps({'result': 'PASS', 'checks': len(results),
                              'wx': wx.version(), 'sqlite': sqlite3.sqlite_version,
                              'source_sha256': sha256((source / 'src/carr_library/ui/evidence_panel.py').read_bytes()).hexdigest(),
                              'native_platform': platform.system(),
                              'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
        finally:
            if frame is not None:
                frame.Destroy()
                app.Yield()
            db.close()
    return 0


def run_isolated(args):
    if os.name != 'posix':
        return not_run('--xvfb is supported only on POSIX; use --native on Windows')
    if not args.xvfb.is_file() or not args.runtime.is_file():
        return not_run('specified Xvfb or Python runtime is unavailable')
    # Fail clearly before Xvfb probes display numbers if this executor disallows
    # Unix-domain sockets. Do not bypass the executor's networking restrictions.
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
            pass
    except OSError as exc:
        return not_run('executor cannot create an isolated X socket', error=str(exc), errno=exc.errno)
    read_fd, write_fd = os.pipe()
    server = None
    with TemporaryDirectory(prefix='carr-evidence-xvfb-') as temporary:
        try:
            with open(Path(temporary) / 'xvfb.log', 'wb') as log:
                server = subprocess.Popen([str(args.xvfb), '-displayfd', str(write_fd),
                                           '-screen', '0', '1280x900x24', '-nolisten', 'tcp'],
                                          pass_fds=(write_fd,), stdout=log, stderr=log)
                os.close(write_fd)
                write_fd = None
                with selectors.DefaultSelector() as selector:
                    selector.register(read_fd, selectors.EVENT_READ)
                    if not selector.select(timeout=10):
                        raise TimeoutError('isolated Xvfb failed to become ready')
                    display = os.read(read_fd, 32).decode('ascii').strip()
                if not display.isdigit():
                    log.flush()
                    with (Path(temporary) / 'xvfb.log').open('rb') as stream:
                        detail = stream.read(2000).decode('utf-8', errors='replace')
                    raise RuntimeError('invalid isolated Xvfb display response: ' + detail)
                env = dict(os.environ)
                env['DISPLAY'] = ':' + display
                env.pop('WAYLAND_DISPLAY', None)
                print('Started isolated Xvfb PID', server.pid, 'display', env['DISPLAY'], flush=True)
                process = subprocess.run([str(args.runtime), str(Path(__file__).resolve()),
                                          '--native', '--source', str(args.source.resolve())],
                                         env=env, timeout=40, check=False)
                return process.returncode
        except subprocess.TimeoutExpired:
            print('FAIL: native smoke exceeded the 40-second runtime limit', flush=True)
            return 1
        except (OSError, TimeoutError, RuntimeError, UnicodeError) as exc:
            return not_run('isolated X display could not start', error=str(exc))
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                print('Stopped owned isolated Xvfb PID', server.pid, flush=True)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2],
                        help='carr-library directory; defaults relative to tests/ui/this-script')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--native', action='store_true',
                      help='run actual native controls on the current authorized desktop')
    mode.add_argument('--xvfb', type=Path,
                      help='optional POSIX Xvfb executable for an isolated display')
    parser.add_argument('--runtime', type=Path, default=Path(sys.executable),
                        help='Python executable for the optional Xvfb child; defaults to current Python')
    args = parser.parse_args()
    if args.xvfb is not None:
        return run_isolated(args)
    return run_native(args.source.resolve())


if __name__ == '__main__':
    raise SystemExit(main())
