"""Actual Windows clipboard and native command dispatch; no automatic speech.

Uses purpose-written public fixture text on an ephemeral CI desktop. Reports
only checks and module identity, never the clipboard contents.
"""
import argparse
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This probe requires actual Windows clipboard controls.')
    checkout = Path(__file__).resolve().parents[1]
    if args.source:
        if args.expect_installed:
            raise ValueError('Select one exact source or installed origin.')
        source = args.source.resolve(strict=True)
        sys.path.insert(0, str(source / 'src'))
        expected = source / 'src' / 'carr_library'
    else:
        if not args.expect_installed:
            raise ValueError('An explicit source or installed origin is required.')
        expected = None
    import wx
    from carr_library.ui import clipboard_reader as ui
    origin = Path(ui.__file__).resolve(strict=True)
    if expected is not None:
        if origin != (expected / 'ui' / 'clipboard_reader.py').resolve(strict=True):
            raise RuntimeError('Clipboard source origin differs from the exact checkout.')
    else:
        if origin.is_relative_to(checkout):
            raise RuntimeError('The installed clipboard probe imported the source checkout.')
        dist = importlib.metadata.distribution('universal-research-library')
        recorded = [Path(dist.locate_file(item)).resolve(strict=True) for item in dist.files or ()
                    if str(item).replace('\\', '/') == 'carr_library/ui/clipboard_reader.py']
        if recorded != [origin]:
            raise RuntimeError('The installed clipboard module is absent from its wheel RECORD.')
    checks = []
    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

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
    if wx.TheClipboard.Open():
        try:
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
                previous = wx.TextDataObject()
                had_text = wx.TheClipboard.GetData(previous)
                if had_text:
                    original_clipboard = previous.GetText()
        finally:
            wx.TheClipboard.Close()
    def set_text(text):
        if not wx.TheClipboard.Open():
            raise RuntimeError('The actual Windows clipboard is unavailable.')
        try:
            if not wx.TheClipboard.SetData(wx.TextDataObject(text)):
                raise RuntimeError('Windows rejected the purpose-written fixture.')
        finally:
            wx.TheClipboard.Close()
    def snapshot():
        return ui._read_clipboard_text(wx)

    try:
        fixture = '  Український текст 😀.\r\nSecond line.\t '
        set_text(fixture)
        item = ui.install_clipboard_reader(frame)
        check(snapshot() == fixture, 'native Unicode read preserves exact fixture whitespace')
        check(not opened and not reports, 'installing the native menu starts no reader or audio')
        def handed_off(parent, **options):
            check(parent is frame, 'native command retains its window owner')
            check(not wx.TheClipboard.IsOpened(), 'clipboard ownership ends before reader handoff')
            opened.append(options)
        ui.show_speech_reader = handed_off
        frame.Show()
        event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
        frame.ProcessEvent(event)
        check(opened == [dict(text=fixture, title='Clipboard reading copy', recovery_slot='clipboard')],
              'actual native menu dispatch uses the independent recoverable reading slot')
        check(snapshot() == fixture, 'reading leaves actual clipboard bytes unchanged')
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
        ui.show_speech_reader = original_reader
        frame.Destroy()
        app.Yield()
        if had_text:
            set_text(original_clipboard)
        elif wx.TheClipboard.Open():
            try:
                wx.TheClipboard.Clear()
            finally:
                wx.TheClipboard.Close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
