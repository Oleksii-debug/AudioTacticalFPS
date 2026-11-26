"""Native EPUB-reader smoke against the installed package, with a display.

From carr-library: python -I tests/ui/probe_ebook_native.py
Only the authorized contents/chapter service callbacks are synthetic. All wx
controls, bindings, modal event dispatch, text ranges and focus are real. This
probe does not establish Windows/NVDA acceptance or source extraction accuracy.
"""
from hashlib import sha256
import json
from pathlib import Path
import platform
import traceback

import wx

from carr_library.ui import ebook_dialog, ebook_model


def main():
    failures = []
    report = {}
    state = {'current': True}
    loads = []
    long_tail = 'Native end marker: Ωmega 😀 COMPLETE TAIL.\n'
    texts = ('Opening 😀 text.\nSecond 😀 Alpha and alpha.\nEnd.',
             'Supplementary chapter.\n' +
             'Long chapter body without a printed-page reference. 😀\n' * 1600 + long_tail)
    assert len(texts[1]) > 65536
    chapters = [dict(ordinal=i, title=f'Chapter title {i}', member=f'chapter-{i}.xhtml',
                     linear=i == 1, characters=len(raw),
                     warnings=['images_not_transcribed'] if i == 2 else [],
                     image_descriptions=['Author-supplied chart description.'] if i == 2 else [])
                for i, raw in enumerate(texts, 1)]
    contents = dict(title='Synthetic EPUB reader probe', revision='native-probe-revision',
                    chapters=chapters,
                    toc=[dict(title='Part one', ordinal=None, offset=None, depth=0, heading_only=True),
                         dict(title='Exact target', ordinal=1, offset=texts[0].index('Alpha'), depth=1),
                         dict(title='Supplement', ordinal=2, offset=0, depth=0)],
                    metadata_hints={}, warnings=['logical_locator_not_page', 'extracted_text_unverified'])

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            close_dialog()
            return False

    gui = ProbeApp(False)
    parent = wx.Frame(None, title='EPUB reader probe parent')
    note = wx.TextCtrl(parent, value='Unsaved research note', style=wx.TE_MULTILINE)
    note.MarkDirty()
    original_note = note.GetValue(), note.IsModified()
    parent.Show()

    def current_dialog():
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog)
                     and window.GetTitle() == 'Electronic book — extracted text'), None)

    def close_dialog():
        dialog = current_dialog()
        if dialog is not None and dialog.IsModal():
            dialog.EndModal(wx.ID_CANCEL)

    def guarded(callback):
        try:
            callback()
        except BaseException:
            failures.append(traceback.format_exc())
            close_dialog()

    def later(callback):
        wx.CallLater(80, lambda: guarded(callback))

    def named(name):
        dialog = current_dialog()
        assert dialog is not None and dialog.IsModal(), 'Reader modal is unavailable.'
        matches = [control for control in dialog.GetChildren() if control.GetName() == name]
        assert len(matches) == 1, f'Expected one labelled control: {name}'
        return matches[0]

    def button(label):
        return next(control for control in current_dialog().GetChildren()
                    if isinstance(control, wx.Button) and control.GetLabel().replace('&', '') == label)

    def event(control, kind):
        assert control.IsEnabled()
        command = wx.CommandEvent(kind.typeId, control.GetId())
        command.SetEventObject(control)
        assert control.GetEventHandler().ProcessEvent(command), 'Native event was not handled.'

    def click(label):
        event(button(label), wx.EVT_BUTTON)

    def read_chapter(ordinal):
        assert state['current'], 'A stale scope reached the chapter service.'
        loads.append(ordinal)
        return dict(chapters[ordinal - 1], revision=contents['revision'], raw_text=texts[ordinal - 1])

    def initial():
        assert named(ebook_model.NOTICE).GetValue() == texts[0]
        status = named('Chapter and navigation status')
        assert wx.Window.FindFocus() is status, 'Initial focus is not on readable chapter information.'
        assert 'Chapter 1 (unpaginated)' in status.GetValue()
        assert not button('Previous chapter').IsEnabled()
        assert button('Next chapter').IsEnabled()
        assert ebook_model.NOTICE in named('EPUB extraction report').GetValue()
        selector = named('Book contents (levels and chapter destinations)')
        assert selector.GetCount() == 3
        selector.SetSelection(0)
        event(selector, wx.EVT_CHOICE)
        assert named(ebook_model.NOTICE).GetValue() == texts[0]
        assert 'no destination' in status.GetValue()
        selector.SetSelection(1)
        event(selector, wx.EVT_CHOICE)
        reader = named(ebook_model.NOTICE)
        start, end = reader.GetSelection()
        assert start == end
        assert reader.GetRange(0, start) == texts[0][:texts[0].index('Alpha')]
        named('Find in this chapter (literal text, ignores case)').ChangeValue('alpha')
        click('Find from start')
        assert reader.GetStringSelection() == 'Alpha'
        later(found)

    def found():
        reader = named(ebook_model.NOTICE)
        assert wx.Window.FindFocus() is reader, 'Find did not focus the selected text.'
        click('Find next (F3)')
        assert reader.GetStringSelection() == 'alpha'
        click('Find next (F3)')
        assert reader.GetStringSelection() == 'Alpha'
        assert 'wrapped' in named('Chapter and navigation status').GetValue()
        click('Next chapter')
        assert reader.GetValue() == texts[1]
        assert reader.GetValue().endswith(long_tail), 'The native control lost the long chapter tail.'
        status = named('Chapter and navigation status').GetValue()
        assert 'author-supplied and unverified' in status
        assert 'Author-supplied chart description.' in status
        assert 'Author-supplied chart description.' not in reader.GetValue()
        assert not button('Next chapter').IsEnabled()
        named('Find in this chapter (literal text, ignores case)').ChangeValue('COMPLETE TAIL')
        click('Find from start')
        assert reader.GetStringSelection() == 'COMPLETE TAIL'
        start, end = reader.GetSelection()
        expected = texts[1].index('COMPLETE TAIL')
        assert expected > 65536
        assert reader.GetRange(0, start) == texts[1][:expected]
        assert reader.GetValue() == texts[1], 'Finding text changed the long chapter.'
        click('Previous chapter')
        assert reader.GetValue() == texts[0]
        later(stale)

    def stale():
        before = list(loads)
        state['current'] = False
        click('Next chapter')
        assert loads == before, 'Stale navigation reached the loader.'
        assert named(ebook_model.NOTICE).GetValue() == ''
        assert named('EPUB extraction report').GetValue() == ''
        assert not named('Chapter (unpaginated)').IsEnabled()
        assert not button('Next chapter').IsEnabled()
        assert 'changed' in named('Chapter and navigation status').GetValue()
        later(finished)

    def finished():
        assert wx.Window.FindFocus() is named('Chapter and navigation status')
        report.update(status='PASS', platform=platform.system(), wx_version=wx.version(),
            checks=['native modal and labelled text controls', 'initial chapter-information focus',
                    'grouping and linked contents entries', 'chapter navigation and button bounds',
                    'multiline and non-BMP exact caret mapping', 'literal case-insensitive find and wrap',
                    'complete chapter beyond 64 KiB and exact tail search',
                    'native find selection and focus', 'separate unverified image descriptions',
                    'stale callback blocked and source text cleared', 'unchanged unsaved parent note'],
            chapter_loads=loads, source_services='synthetic callbacks', windows_nvda='NOT_RUN',
            ebook_dialog_sha256=sha256(Path(ebook_dialog.__file__).read_bytes()).hexdigest(),
            ebook_model_sha256=sha256(Path(ebook_model.__file__).read_bytes()).hexdigest())
        close_dialog()

    def timed_out():
        failures.append('Native EPUB reader probe exceeded 20 seconds.')
        close_dialog()

    timeout = wx.CallLater(20000, timed_out)
    later(initial)
    try:
        ebook_dialog.show_ebook(parent, lambda: contents, read_chapter,
                               still_current=lambda: state['current'])
        assert (note.GetValue(), note.IsModified()) == original_note, 'The reader changed the unsaved note.'
    except BaseException:
        failures.append(traceback.format_exc())
    finally:
        timeout.Stop()
        parent.Destroy()
        gui.ProcessPendingEvents()
    if failures:
        print(json.dumps(dict(status='FAIL', errors=failures, windows_nvda='NOT_RUN'), ensure_ascii=False))
        return 1
    if report.get('status') != 'PASS':
        print(json.dumps(dict(status='FAIL', errors=['Probe did not finish.'], windows_nvda='NOT_RUN')))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
