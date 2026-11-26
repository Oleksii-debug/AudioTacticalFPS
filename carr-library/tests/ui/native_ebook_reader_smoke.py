"""Real wx controls/events for the EPUB reader, with synthetic section data.

Run directly: python tests/ui/native_ebook_reader_smoke.py --native
This checks native focus, section selection and separate image descriptions.
It uses a small asynchronous synthetic loader, not an EPUB parser or the main
frame's job controller. Real-source import/reader tests cover those services.
No screenshot, external viewer, user document or screen-reader claim is made.
Missing display/wx produces NOT_RUN and exit 77; assertions fail with exit 1.
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from time import monotonic
import traceback


def run_native(source):
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'No native display.', 'nvda': 'NOT_RUN'}))
        return 77
    try:
        import wx
    except (ImportError, OSError):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native wxPython unavailable.', 'nvda': 'NOT_RUN'}))
        return 77
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(source / 'src'))
    from carr_library.citations.anchors import Identity
    from carr_library.reader.ebook import EbookBook, EbookChapter, EbookSection
    from carr_library.ui import ebook_reader

    checks, failures, timers, loads = [], [], [], []
    controls = {}
    completed = False
    gui = wx.App(False)
    frame = wx.Frame(None, title='Synthetic EPUB reader control probe')
    frame.Show()
    gui.SetTopWindow(frame)
    book = EbookBook('workspace', 'edition', 'revision', 'Synthetic research book',
                     'Synthetic author', 'First edition', 2)
    identity = Identity(book.workspace, book.edition_id, 'a' * 64, book.revision)
    chapters = (EbookChapter(1, 'First chapter — Україна', 'OPS/first.xhtml', True),
                EbookChapter(2, 'Appendix — štúdia', 'OPS/appendix.xhtml', False))
    texts = ('First source section. Український текст.', 'Second source section. Slovenská štúdia.')
    description = 'Authored diagram description — джерела.'
    sections = tuple(EbookSection(identity, book.title, chapter.title, chapter.resource,
        chapter.position, len(chapters), chapter.linear, texts[index], chapters,
        ('layout_not_verified',), ((1, description),) if index == 0 else ())
        for index, chapter in enumerate(chapters))

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def dialog():
        found = [window for window in wx.GetTopLevelWindows()
                 if isinstance(window, wx.Dialog) and window.GetTitle() == 'Read EPUB book']
        if len(found) != 1:
            raise AssertionError('Expected one native EPUB reader dialog.')
        return found[0]

    def close_dialog():
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.GetTitle() == 'Read EPUB book':
                if window.IsModal():
                    window.EndModal(wx.ID_CANCEL)

    def guarded(callback):
        try:
            callback()
        except BaseException:
            failures.append(traceback.format_exc())
            close_dialog()

    def later(callback, delay=25):
        timers.append(wx.CallLater(delay, lambda: guarded(callback)))

    deadline = monotonic() + 12

    def wait_for(predicate, callback):
        if monotonic() >= deadline:
            raise AssertionError('Native reader state did not arrive before the deadline.')
        if predicate():
            callback()
        else:
            later(lambda: wait_for(predicate, callback))

    def click(button):
        check('native navigation button is enabled', button.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native navigation event is handled', button.GetEventHandler().ProcessEvent(event))

    def select(index):
        choice = controls['choice']
        choice.SetSelection(index)
        event = wx.CommandEvent(wx.EVT_CHOICE.typeId, choice.GetId())
        event.SetEventObject(choice)
        event.SetInt(index)
        check('native section choice event is handled', choice.GetEventHandler().ProcessEvent(event))

    def initially_ready():
        window = dialog()
        for key, name in (
            ('choice', 'EPUB section'), ('status', 'Section information'),
            ('text', 'Extracted section text — unverified'),
            ('alternatives', 'Authored image descriptions — separate from quotation text')):
            matches = [child for child in window.GetChildren() if child.GetName() == name]
            check('one named native ' + key + ' control', len(matches) == 1)
            controls[key] = matches[0]
        for key, label in (('previous', 'Previous section'), ('next', 'Next section')):
            controls[key] = next(child for child in window.GetChildren()
                if isinstance(child, wx.Button) and child.GetLabel().replace('&', '') == label)
        check('native choice contains both spine sections',
              isinstance(controls['choice'], wx.Choice) and controls['choice'].GetCount() == 2)
        check('first source text is exact', controls['text'].GetValue() == texts[0])
        check('image description stays outside source text',
              description in controls['alternatives'].GetValue()
              and description not in controls['text'].GetValue())
        check('first-section bounds are enforced',
              not controls['previous'].IsEnabled() and controls['next'].IsEnabled())
        check('unverified and unknown-page status is explicit',
              'unverified' in controls['status'].GetValue()
              and 'Printed pages are unknown' in controls['status'].GetValue())
        wait_for(lambda: wx.Window.FindFocus() == controls['status'], advance)

    def advance():
        check('first section focuses readable status', wx.Window.FindFocus() == controls['status'])
        click(controls['next'])
        check('loading clears previous section and alternatives',
              controls['text'].GetValue() == '' and controls['alternatives'].GetValue() == '')
        wait_for(lambda: controls['text'].GetValue() == texts[1]
                 and wx.Window.FindFocus() == controls['status'], second_ready)

    def second_ready():
        check('next loaded exactly the second section', loads == [2])
        check('native selector tracks the second section', controls['choice'].GetSelection() == 1)
        check('supplementary section label is visible',
              'supplementary' in controls['choice'].GetString(1)
              and 'supplementary' in controls['status'].GetValue())
        check('last-section bounds are enforced',
              controls['previous'].IsEnabled() and not controls['next'].IsEnabled())
        check('new section clears previous image alternatives',
              description not in controls['alternatives'].GetValue())
        check('next section focuses readable status', wx.Window.FindFocus() == controls['status'])
        select(0)
        wait_for(lambda: controls['text'].GetValue() == texts[0]
                 and wx.Window.FindFocus() == controls['status'], returned)

    def returned():
        check('choice returns to the exact first section', loads == [2, 1])
        check('return restores corresponding image descriptions',
              description in controls['alternatives'].GetValue())
        select(wx.NOT_FOUND)
        wait_for(lambda: 'unavailable' in controls['status'].GetValue()
                 and wx.Window.FindFocus() == controls['status'], rejected)

    def rejected():
        nonlocal completed
        check('invalid selection cannot revive stale source text',
              controls['text'].GetValue() == '' and controls['alternatives'].GetValue() == '')
        check('invalid selection disables all section navigation',
              not controls['choice'].IsEnabled() and not controls['previous'].IsEnabled()
              and not controls['next'].IsEnabled())
        check('invalid selection does not call source loader', loads == [2, 1])
        check('failure focuses the native status control', wx.Window.FindFocus() == controls['status'])
        completed = True
        close_dialog()

    with ThreadPoolExecutor(max_workers=1) as executor:
        def load(position):
            loads.append(position)
            return sections[position - 1]

        def run_job(title, operation, done, failed):
            future = executor.submit(operation)

            def finished(future):
                def apply():
                    try:
                        result = future.result()
                    except Exception as exc:
                        failed(exc)
                    else:
                        done(result)
                wx.CallAfter(lambda: guarded(apply))
            future.add_done_callback(finished)

        def timed_out():
            raise AssertionError('Native EPUB smoke exceeded 15 seconds.')

        later(timed_out, 15000)
        later(initially_ready, 100)
        try:
            ebook_reader.show_ebook_reader(frame, book, lambda: True, load, run_job, sections[0])
        except BaseException:
            failures.append(traceback.format_exc())
        finally:
            for timer in timers:
                timer.Stop()
            frame.Destroy()
            gui.ProcessPendingEvents()
    if not completed and not failures:
        failures.append('Native reader closed before all checks completed.')
    print(json.dumps({'status': 'FAIL' if failures else 'PASS', 'checks': len(checks),
                      'errors': failures, 'platform': platform.system(), 'wx': wx.version(),
                      'source_sha256': sha256(Path(ebook_reader.__file__).read_bytes()).hexdigest(),
                      'nvda': 'NOT_RUN', 'source_loader': 'synthetic asynchronous section records'},
                     ensure_ascii=False), flush=True)
    return int(bool(failures))


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', help='Run on the current native display.')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    return run_native(parser.parse_args().source.resolve())


if __name__ == '__main__':
    raise SystemExit(main())
