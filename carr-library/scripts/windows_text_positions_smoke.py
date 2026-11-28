"""Actual Windows TextCtrl coordinate proof for the shared EPUB position mapper.

This checks native source/installed controls and exact Unicode text, not audio
playback, speech quality, screen-reader behavior, or distribution readiness.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import monotonic
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('Actual native Windows is required; no skipped acceptance.')
    if not sys.flags.isolated or not sys.flags.dont_write_bytecode or not __debug__:
        raise RuntimeError('Use isolated Python, no bytecode writes and enabled assertions.')
    checkout = Path(__file__).resolve().parents[1]
    expected = checkout / 'src' / 'carr_library' / 'ui' / 'ebook_model.py'
    if args.source is not None:
        source = args.source.resolve(strict=True)
        if source != checkout:
            raise ValueError('The explicitly selected source differs from this checkout.')
        sys.path.insert(0, str(source / 'src'))
    import wx
    from carr_library.ui import ebook_model
    actual = Path(ebook_model.__file__).resolve(strict=True)
    mode = 'source' if args.source is not None else 'installed'
    checks, errors = [], []
    before = sha256(actual.read_bytes()).hexdigest()

    def check(name, value):
        if not value:
            raise AssertionError(name)
        if name in checks:
            raise AssertionError('Duplicate native evidence name: ' + name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    check('exact selected source bytes', actual.read_bytes() == expected.read_bytes())
    check('requested module origin', actual == expected.resolve(strict=True) if mode == 'source'
          else actual.is_relative_to(Path(sys.prefix).resolve(strict=True))
          and not actual.is_relative_to(checkout))
    check('actual native MSW widgets', wx.Platform == '__WXMSW__')

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False

    gui = ProbeApp(False)
    frame = wx.Frame(None, title='Exact Unicode native position acceptance', size=(760, 320))
    control = None
    frame.Show()
    gui.SetTopWindow(frame)
    fragment = 'Exact α 😀 source & words.'
    raw = ('Source title 😀\n' + 'Wrapped α e\u0301 source words. ' * 24
           + fragment + '\nSecond line 😀. End.')
    started = monotonic()
    rows = []

    def run():
        nonlocal control
        try:
            for name, width, extra_style in (
                    ('wrapped-narrow', 180, 0),
                    ('wrapped-wide', 620, 0),
                    ('unwrapped', 180, wx.TE_DONTWRAP)):
                control = wx.TextCtrl(frame, value=raw, pos=(8, 8), size=(width, 230),
                    style=wx.TE_READONLY | wx.TE_MULTILINE | extra_style)
                control.SetName('Exact source Unicode position proof')
                gui.Yield()
                check(name + ': exact complete native value', control.GetValue() == raw)
                logical = raw.count('\n') + 1
                physical = control.GetNumberOfLines()
                check(name + ': actual line representation',
                      physical > logical if not extra_style else physical == logical)
                for offset in range(len(raw) + 1):
                    if monotonic() - started > 45:
                        raise TimeoutError('Native Unicode boundary proof exceeded45 seconds.')
                    position = ebook_model.native_position(control, raw, offset)
                    if (not 0 <= position <= control.GetLastPosition()
                            or control.GetRange(0, position) != raw[:offset]):
                        raise AssertionError('A native Unicode source boundary differs.')
                check(name + ': every source boundary proved', True)
                start = raw.index(fragment)
                first = ebook_model.native_position(control, raw, start)
                last = ebook_model.native_position(control, raw, start + len(fragment))
                control.SetSelection(first, last)
                check(name + ': exact selected Unicode fragment',
                      control.GetStringSelection() == fragment
                      and control.GetRange(*control.GetSelection()) == fragment)
                rows.append({'name': name, 'logical_lines': logical, 'physical_lines': physical,
                             'source_characters': len(raw), 'native_last': control.GetLastPosition(),
                             'all_boundaries': len(raw) + 1, 'selected_native': list(control.GetSelection())})
                control.Destroy()
                control = None
                gui.Yield()
            control = wx.TextCtrl(frame, value='Changed source.', style=wx.TE_MULTILINE | wx.TE_READONLY)
            try:
                ebook_model.native_position(control, 'Original source.', 1)
            except ValueError as error:
                check('changed native value refuses source positioning', 'changed the extracted text' in str(error))
            else:
                raise AssertionError('Changed native text was accepted.')
            for offset in (-1, 1000):
                try:
                    ebook_model.native_position(control, 'Changed source.', offset)
                except ValueError as error:
                    check('invalid offset refused: ' + str(offset), 'Invalid extracted-text position' in str(error))
                else:
                    raise AssertionError('An invalid source offset was accepted.')
        except BaseException:
            errors.append(traceback.format_exc())
        finally:
            if control is not None:
                control.Destroy()
                control = None
            frame.Destroy()
            gui.ExitMainLoop()

    wx.CallAfter(run)
    gui.MainLoop()
    gui.Yield()
    check('native windows retired after real event loop', not list(wx.GetTopLevelWindows()))
    check('module bytes unchanged', sha256(actual.read_bytes()).hexdigest() == before
          and actual.read_bytes() == expected.read_bytes())
    result = {'schema': 'carr-native-text-positions/1', 'status': 'FAIL' if errors else 'PASS',
              'mode': mode, 'origin': str(actual), 'module_sha256': before,
              'checks': checks, 'profiles': rows, 'wx': wx.version(),
              'errors': [value[:8192] for value in errors],
              'audio': 'NOT_RUN', 'manual_nvda': 'NOT_RUN', 'release': 'NOT_RUN'}
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=True, indent=2)
        stream.write('\n')
    print(json.dumps(result, ensure_ascii=True, sort_keys=True), flush=True)
    if errors:
        raise RuntimeError('Actual native text positioning failed; see its bounded report.')
    if len(checks) != 20 or len(rows) != 3:
        raise AssertionError('The complete native position inventory is required.')


if __name__ == '__main__':
    main()
