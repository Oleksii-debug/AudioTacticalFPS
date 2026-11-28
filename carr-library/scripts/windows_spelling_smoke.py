"""Actual installed Windows dictionary and native spelling-review controls.

No dictionary is downloaded, installed or modified. English en-US must already
be supported. This tests OS checking and native review/edit/undo, not dictionary
quality for every language, human NVDA, or full speech synthesis.
"""
import argparse
import faulthandler
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
import sys
import sysconfig
from time import monotonic
import traceback


def _binding(args):
    checkout = Path(__file__).resolve().parents[1] / 'src'
    if args.source is not None:
        root = (args.source.resolve(strict=True) / 'src').resolve(strict=True)
        sys.path.insert(0, str(root))
    else:
        root = (args.installed_root if args.installed_root is not None
                else Path(sysconfig.get_path('purelib'))).resolve(strict=True)
    origins = {}
    for name in ('carr_library', 'carr_library.speech.spelling',
                 'carr_library.speech.spelling_worker', 'carr_library.speech.reading',
                 'carr_library.speech.reading_tools', 'carr_library.ui.reading_tools',
                 'carr_library.ui.reader_spelling'):
        module = import_module(name)
        relative = (Path('carr_library/__init__.py') if name == 'carr_library'
                    else Path(*name.split('.')).with_suffix('.py'))
        path = Path(module.__file__).resolve(strict=True)
        if path != (root / relative).resolve(strict=True):
            raise AssertionError(f'{name} is not from the explicitly selected package root')
        if args.source is None and path.is_relative_to(checkout.resolve()):
            raise AssertionError('Installed native proof imported checkout source')
        actual = path.read_bytes()
        reference = checkout / relative
        if reference.is_file() and reference.read_bytes() != actual:
            raise AssertionError(f'Native module differs from the current checkout: {name}')
        origins[name] = {'path': str(path), 'sha256': sha256(actual).hexdigest()}
    return {'source_override': str(args.source) if args.source else None,
            'package_root': str(root), 'modules': origins}


def _run(args, report):
    if sys.platform != 'win32':
        raise RuntimeError('This qualification requires Windows and its installed spelling dictionaries.')
    report['binding'] = _binding(args)
    from carr_library.native_runtime import activate_bundled_runtime
    activate_bundled_runtime()
    import wx
    from concurrent.futures import ThreadPoolExecutor
    from carr_library.speech.reading import ReadingDocument
    from carr_library.speech.reading_tools import ReadingSnapshot
    from carr_library.ui.reading_tools import (ReaderEditHost, reading_selection,
                                                select_reading_range)
    from carr_library.ui.reader_spelling import ReaderSpellingDialog

    app = wx.App(False)
    parent = wx.Frame(None, title='Spelling native qualification', size=(800, 400))
    editor = wx.TextCtrl(parent, style=wx.TE_MULTILINE)
    editor.SetName('Qualification reading copy')
    source = '😀 This sentnce contains a mispelled word.'
    document = ReadingDocument(source)
    editor.ChangeValue(source)
    box = wx.BoxSizer(wx.VERTICAL)
    box.Add(editor, 1, wx.EXPAND)
    parent.SetSizer(box)
    parent.Show()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='spelling-probe-reader')
    state = {'busy': False, 'closed': False, 'phase': 'languages', 'failed': None}
    futures = []
    checks = []
    deadline = monotonic() + args.timeout

    def guard():
        if state['closed']:
            raise ValueError('Qualification reader is closed.')

    def capture():
        guard()
        document.set_text(editor.GetValue())
        return ReadingSnapshot.capture(document)

    def verify(snapshot):
        guard()
        if editor.GetValue() != snapshot.text:
            raise ValueError('The native reading text changed.')
        snapshot.check(document)

    def apply(snapshot, start, end, replacement, *, label):
        verify(snapshot)
        result = document.replace(start, end, replacement, label=label)
        editor.ChangeValue(document.text)
        select_reading_range(editor, start, start + len(replacement))
        return result

    def submit(work, done, *, on_error=None):
        if state['busy'] or state['closed']:
            return False
        state['busy'] = True
        future = executor.submit(work)
        futures.append(future)

        def completed():
            state['busy'] = False
            if state['closed']:
                return
            try:
                value = future.result()
            except Exception as exc:
                on_error(exc)
            else:
                done(value)
        future.add_done_callback(lambda _: wx.CallAfter(completed))
        return True

    host = ReaderEditHost(capture=capture, selection=lambda: reading_selection(editor),
        select=lambda a, b: select_reading_range(editor, a, b), verify=verify, apply=apply,
        submit=submit, guard=guard, report=lambda *a, **k: None, alive=lambda: not state['closed'],
        busy=lambda: state['busy'], cancelled=lambda: state['closed'],
        voice=lambda: ('', '', 'No voice is involved in this qualification'))
    review = ReaderSpellingDialog(parent, host)
    review.show()
    timer = wx.Timer(parent)

    def event(control, kind):
        native = wx.CommandEvent(kind.typeId, control.GetId())
        native.SetEventObject(control)
        assert control.GetEventHandler().ProcessEvent(native), 'Native event had no handler'

    def finish():
        timer.Stop()
        state['closed'] = True
        review.controller.close()
        if not review.closed:
            review.closed = True
            review.dialog.Destroy()
        parent.Destroy()
        app.ExitMainLoop()

    def tick(_):
        try:
            if monotonic() > deadline:
                raise AssertionError('Native spelling deadline exceeded in phase ' + state['phase']
                                     + ': ' + review.status.GetValue())
            if state['busy']:
                return
            if state['phase'] == 'languages':
                languages = review.controller.languages
                if 'en-US' not in languages:
                    raise AssertionError('The exact en-US Windows spelling dictionary is unavailable: '
                                         + review.status.GetValue())
                report['installed_languages'] = list(languages)
                assert review.language.GetSelection() == wx.NOT_FOUND
                checks.append('installed inventory has no implicit language selection')
                review.language.SetSelection(languages.index('en-US'))
                event(review.language, wx.EVT_CHOICE)
                event(review.check, wx.EVT_BUTTON)
                assert state['busy'] and document.text == source
                checks.append('native Check submits to the existing single reader worker')
                state['phase'] = 'checked'
            elif state['phase'] == 'checked':
                result = review.controller.result
                assert result is not None, review.status.GetValue()
                assert result.language == 'en-US'
                assert result.text_sha256 == document.source_sha256
                assert result.issues and review.issues.GetCount() == len(result.issues)
                candidates = [(index, suggestion) for index, issue in enumerate(result.issues)
                    for suggestion, value in enumerate(issue.replacements)
                    if issue.word in ('sentnce', 'mispelled') and value != issue.word]
                assert candidates, 'Actual OS dictionary returned no reviewable correction'
                index, suggestion = candidates[0]
                issue = result.issues[index]
                assert source[issue.start:issue.end] == issue.word
                assert issue.start > 1 and issue.word in ('sentnce', 'mispelled')
                checks.append('actual OS error spans map exactly after a non-BMP character')
                review.issues.SetSelection(index)
                event(review.issues, wx.EVT_LISTBOX)
                review.suggestions.SetSelection(suggestion)
                event(review.suggestions, wx.EVT_CHOICE)
                event(review.preview_button, wx.EVT_BUTTON)
                preview = review.controller.session.preview
                assert preview is not None and document.text == source
                assert reading_selection(editor) == (preview.start, preview.end)
                assert 'PROPOSED READING FRAGMENT' in review.preview_text.GetValue()
                checks.append('native Preview selects exact text without editing')
                expected = source[:preview.start] + preview.replacement + source[preview.end:]
                event(review.apply_button, wx.EVT_BUTTON)
                assert document.text == editor.GetValue() == expected
                assert document.source_text == source
                assert document.source_sha256 == sha256(source.encode()).hexdigest()
                assert review.controller.result is None
                checks.append('native Apply changes only the reviewed reading-copy span')
                assert document.undo() and document.text == source
                editor.ChangeValue(document.text)
                checks.append('existing ReadingDocument undo restores the exact original copy')
                event(review.close_button, wx.EVT_BUTTON)
                state['phase'] = 'closed'
            elif state['phase'] == 'closed':
                assert review.controller.session.closed
                checks.append('native Close invalidates unaccepted spelling work')
                finish()
        except BaseException:
            state['failed'] = traceback.format_exc()
            finish()

    parent.Bind(wx.EVT_TIMER, tick, timer)
    timer.Start(25)
    try:
        app.MainLoop()
    finally:
        state['closed'] = True
        review.controller.close()
        executor.shutdown(wait=True, cancel_futures=True)
    if state['failed']:
        raise AssertionError(state['failed'])
    assert len(checks) == 7
    report.update(result='PASS', checks=checks, count=len(checks), dictionary='en-US',
                  actual_os_check=True, synthetic_transport=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--source', type=Path, help='Explicit source-only qualification root containing src.')
    parser.add_argument('--installed-root', type=Path,
                        help='Explicit packaged import root for the sealed bundle; never adds sys.path.')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if args.source is not None and args.installed_root is not None:
        parser.error('Choose source or installed package provenance, never both.')
    if not 10 <= args.timeout <= 120:
        parser.error('Timeout must be between 10 and 120 seconds.')
    report = {'result': 'FAIL', 'checks': [], 'count': 0, 'nvda': 'NOT_RUN',
              'per_language_quality': 'NOT_RUN', 'dictionary_installation': 'NOT_REQUESTED',
              'python': sys.version, 'platform': sys.platform}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    faulthandler.dump_traceback_later(150, exit=True)
    try:
        _run(args, report)
    except BaseException:
        report['error'] = traceback.format_exc()
    finally:
        faulthandler.cancel_dump_traceback_later()
        if args.report:
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
