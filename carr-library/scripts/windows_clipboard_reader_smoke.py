"""Actual Windows clipboard and native command dispatch; no automatic speech.

Uses purpose-written public fixture text on an ephemeral CI desktop. Reports
only checks and module identity, never the clipboard contents.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time


def _checkpoint(path, value):
    """Persist only bounded diagnostic metadata, never clipboard values."""
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
    if len(raw) > 64 * 1024:
        raise RuntimeError('Clipboard diagnostic receipt exceeds its bound.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.partial')
    with temporary.open('wb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _phase(args, report, name):
    report['phase'] = name
    _checkpoint(args.report, report)


def _bounded_sha256(path):
    with path.open('rb') as source:
        raw = source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError('Clipboard probe module exceeds its source-proof bound.')
    return sha256(raw).hexdigest()


def _module_evidence(checkout, package_root, modules, *, source_mode, distribution=None):
    """Bind each actually loaded module to its exact checkout and wheel record."""
    checkout, package_root = checkout.resolve(strict=True), package_root.resolve(strict=True)
    expected = checkout / 'src' / 'carr_library'
    if source_mode:
        if package_root != expected.resolve(strict=True):
            raise RuntimeError('Clipboard source package differs from the exact checkout.')
    elif package_root.is_relative_to(checkout) or distribution is None:
        raise RuntimeError('The installed clipboard probe imported the source checkout.')
    result = {}
    for relative, module in modules:
        origin = Path(module.__file__).resolve(strict=True)
        if origin != (package_root / relative).resolve(strict=True):
            raise RuntimeError('A loaded clipboard dependency differs from its package root.')
        if not source_mode:
            member = 'carr_library/' + relative
            recorded = [Path(distribution.locate_file(item)).resolve(strict=True)
                        for item in distribution.files or ()
                        if str(item).replace('\\', '/') == member]
            if recorded != [origin]:
                raise RuntimeError('A loaded clipboard dependency is absent from its wheel RECORD.')
        checksum = _bounded_sha256(origin)
        if checksum != _bounded_sha256(expected / relative):
            raise RuntimeError('Loaded clipboard dependency bytes differ from the exact checkout.')
        result[relative] = {'path': str(origin), 'sha256': checksum}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = dict(status='FAIL', phase='starting', checks=[],
        scope='actual Windows clipboard and menu handoff; reader rendering is a separate native gate',
        clipboard_content='NOT_RECORDED', automatic_speech=False, watcher=False,
        nvda='NOT_RUN', installed=args.expect_installed,
        source_commit=os.environ.get('GITHUB_SHA'), python=sys.version)
    _checkpoint(args.report, report)
    faulthandler.enable()
    faulthandler.dump_traceback_later(90, exit=True)
    try:
        result = _run_probe(args, report)
        report.update(result)
        report['phase'] = 'completed'
        _checkpoint(args.report, report)
        return 0
    except BaseException as exc:
        report['status'] = 'FAIL'
        report['failure'] = {'type': type(exc).__name__, 'message': str(exc)[:2000]}
        _checkpoint(args.report, report)
        raise
    finally:
        faulthandler.cancel_dump_traceback_later()


def _run_probe(args, report):
    _phase(args, report, 'source-and-platform-admission')
    if os.name != 'nt':
        raise RuntimeError('This probe requires actual Windows clipboard controls.')
    checkout = Path(__file__).resolve().parents[1]
    if args.source:
        if args.expect_installed:
            raise ValueError('Select one exact source or installed origin.')
        source = args.source.resolve(strict=True)
        if source != checkout:
            raise RuntimeError('The clipboard source override differs from this exact checkout.')
        sys.path.insert(0, str(source / 'src'))
        expected = source / 'src' / 'carr_library'
    else:
        if not args.expect_installed:
            raise ValueError('An explicit source or installed origin is required.')
        expected = None
    _phase(args, report, 'import-native-controls-and-reader')
    import wx
    import carr_library
    from carr_library.ui import clipboard_reader as ui, speech_dialog
    from carr_library.speech import _io as speech_io, clipboard_reader as native_clipboard
    report['wx_version'] = wx.version()
    _phase(args, report, 'verify-imported-module-origin')
    origin = Path(ui.__file__).resolve(strict=True)
    package_root = Path(carr_library.__file__).resolve(strict=True).parent
    distribution = (None if args.source else
                    importlib.metadata.distribution('universal-research-library'))
    report['modules'] = _module_evidence(checkout, package_root,
        (('ui/clipboard_reader.py', ui), ('ui/speech_dialog.py', speech_dialog),
         ('speech/_io.py', speech_io), ('speech/clipboard_reader.py', native_clipboard)),
        source_mode=bool(args.source), distribution=distribution)
    report['module_sha256'] = report['modules']['ui/clipboard_reader.py']['sha256']
    checks = report['checks']
    def check(condition, name):
        _phase(args, report, name)
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        _checkpoint(args.report, report)
        print('PASS: ' + name, flush=True)

    _phase(args, report, 'create-native-clipboard-owner')
    app = wx.App(False)
    frame = wx.Frame(None, title='Purpose-written clipboard verification')
    menu = wx.Menu()
    bar = wx.MenuBar()
    bar.Append(wx.Menu(), '&Library')
    bar.Append(menu, '&Voice')
    frame.SetMenuBar(bar)
    reports, opened = [], []
    frame.report = reports.append
    original_reader = ui.show_speech_reader
    original_clipboard = None
    had_text = False
    _phase(args, report, 'preserve-existing-clipboard')
    if wx.TheClipboard.Open():
        try:
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
                previous = wx.CustomDataObject(wx.DataFormat(wx.DF_UNICODETEXT))
                had_text = wx.TheClipboard.GetData(previous)
                if had_text:
                    original_clipboard = bytes(previous.GetData())
        finally:
            wx.TheClipboard.Close()
    def set_unicode_bytes(raw):
        if not wx.TheClipboard.Open():
            raise RuntimeError('The actual Windows clipboard is unavailable.')
        try:
            value = wx.CustomDataObject(wx.DataFormat(wx.DF_UNICODETEXT))
            value.SetData(raw)
            if not wx.TheClipboard.SetData(value):
                raise RuntimeError('Windows rejected the purpose-written fixture.')
        finally:
            wx.TheClipboard.Close()
    def set_text(text):
        set_unicode_bytes(text.encode('utf-16-le') + b'\0\0')
    def raw_snapshot():
        if not wx.TheClipboard.Open():
            raise RuntimeError('The actual Windows clipboard is unavailable.')
        try:
            value = wx.CustomDataObject(wx.DataFormat(wx.DF_UNICODETEXT))
            if not wx.TheClipboard.GetData(value):
                raise RuntimeError('The raw native Unicode fixture could not be verified.')
            return bytes(value.GetData())
        finally:
            wx.TheClipboard.Close()
    def snapshot():
        return ui._read_clipboard_text(wx)

    try:
        _phase(args, report, 'write-purpose-written-clipboard-fixture')
        fixture = '  Український текст 😀.\r\nSecond line.\t '
        set_text(fixture)
        raw_before = raw_snapshot()
        report['fixture_cf_unicode_sha256'] = sha256(raw_before).hexdigest()
        item = ui.install_clipboard_reader(frame)
        check(snapshot() == fixture, 'native Unicode read preserves exact fixture whitespace')
        check(not opened and not reports, 'installing the native menu starts no reader or audio')
        def handed_off(parent, **options):
            check(parent is frame, 'native command retains its window owner')
            check(not wx.TheClipboard.IsOpened(), 'clipboard ownership ends before reader handoff')
            opened.append(options)
        ui.show_speech_reader = handed_off
        _phase(args, report, 'dispatch-native-menu-command')
        frame.Show()
        event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
        frame.ProcessEvent(event)
        check(opened == [dict(text=fixture, title='Clipboard reading copy', recovery_slot='clipboard')],
              'actual native menu dispatch uses the independent recoverable reading slot')
        check(snapshot() == fixture and raw_snapshot() == raw_before,
              'reading leaves actual clipboard bytes unchanged')
        set_text('A later purpose-written clipboard value.')
        for _ in range(5):
            app.Yield()
            time.sleep(.02)
        check(len(opened) == 1, 'later clipboard changes do not start a watcher or another reader')
        set_text(' \r\n\t')
        frame.ProcessEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))
        check(len(opened) == 1 and len(reports) == 1 and 'nonempty' in reports[0],
              'empty native clipboard text cannot enter the reader')
        frame._closing = True
        frame.ProcessEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))
        check(len(opened) == 1 and len(reports) == 2 and 'closing' in reports[-1],
              'closing owner rejects native command admission')
        result = dict(status='PASS', checks=checks,
            scope='actual Windows clipboard and menu handoff; reader rendering is a separate native gate',
            clipboard_content='NOT_RECORDED', automatic_speech=False, watcher=False,
            nvda='NOT_RUN', module_sha256=sha256(origin.read_bytes()).hexdigest(),
            installed=args.expect_installed, source_commit=os.environ.get('GITHUB_SHA'))
    finally:
        report['cleanup_started'] = True
        try:
            _checkpoint(args.report, report)
        finally:
            ui.show_speech_reader = original_reader
            frame.Destroy()
            app.Yield()
            if had_text:
                set_unicode_bytes(original_clipboard)
            elif wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.Clear()
                finally:
                    wx.TheClipboard.Close()
        report['cleanup_complete'] = True
        _checkpoint(args.report, report)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
