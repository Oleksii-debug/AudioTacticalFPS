"""Installed native citation regeneration with real Node, DOCX and wx controls.

Only file-picker results are controlled. Space activates real native buttons;
focus, asynchronous completion, stale source rejection and no-clobber behavior
are measured. Native file pickers, manual accessibility and NVDA are separate.
Run with the installed interpreter: python -I -B scripts/windows_citation_regeneration_smoke.py.
"""
from collections import deque
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'A native Windows session is required.',
                          'native_keyboard': 'NOT_RUN', 'native_file_pickers': 'NOT_RUN',
                          'nvda': 'NOT_RUN'}), flush=True)
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.academic_integration import (
        CitationRequest, export_citation_docx, read_citation_docx, render_citations,
    )
    from carr_library.academic_integration import documents, regeneration
    from carr_library.ui import citation_formatting as ui
    from carr_library.ui.academic_library_dialog import AcademicController

    checks, failures = [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    def pump(predicate, description, timeout=30):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)

    def focus(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle(),
             'The native control did not acquire foreground keyboard focus.', timeout=10)
        check('native keyboard focus: ' + control.GetName(), wx.Window.FindFocus() is control)

    def press(control):
        focus(control)
        check('native Space key-down accepted: ' + control.GetName(), simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            sleep(.04)
        finally:
            check('native Space key-up accepted: ' + control.GetName(), simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()

    for module in (ui, documents, regeneration):
        check('installed module: ' + module.__name__,
              'site-packages' in [part.casefold() for part in Path(module.__file__).resolve().parts])
    check('real native Windows controls', wx.Platform == '__WXMSW__')
    actual_picker = wx.FileDialog
    frame = dialog = None
    with TemporaryDirectory(prefix='carr-citation-regeneration-') as temporary:
        root = Path(temporary)
        try:
            library = LibraryApp(root / 'private library')
            workspace = library.create_workspace('Native citation regeneration')
            AcademicController(library, workspace).create({'title': 'Separate native library reference'})
            refs = [{'id': 'saved-source', 'type': 'book', 'title': 'Saved snapshot — Україна',
                     'author': [{'literal': 'Synthetic Research Group'}],
                     'issued': {'date-parts': [[2026]]}}]
            formatted = render_citations(refs, style='harvard', citations=[CitationRequest('saved-source')])
            source = export_citation_docx(formatted, root / 'selected.docx', title='Native retained citation snapshot')
            original = source.read_bytes()
            expected = read_citation_docx(source)
            target = root / 'regenerated.docx'
            occupied = root / 'occupied.docx'
            occupied.write_bytes(b'Another existing document')
            stale_target = root / 'stale-must-not-exist.docx'
            choices = deque((source, target, occupied, stale_target))

            class SelectedPath:
                def __init__(self, *args, **kwargs):
                    self.path = choices.popleft()
                    check('native flow requests a bounded DOCX picker', '*.docx' in kwargs.get('wildcard', ''))
                    check('regeneration never requests implicit overwrite approval',
                          not (kwargs.get('style', 0) & wx.FD_OVERWRITE_PROMPT))
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def ShowModal(self):
                    return wx.ID_OK
                def GetPath(self):
                    return str(self.path)

            wx.FileDialog = SelectedPath
            frame = wx.Frame(None, title='Native citation regeneration acceptance')
            frame.library, frame.active = library, (workspace, None)
            frame.working = frame.busy = frame._closing = False
            frame._workspace = lambda: workspace
            frame.Show()
            gui.SetTopWindow(frame)
            def scenario():
                nonlocal dialog
                try:
                    check('native event loop is active', gui.GetMainLoop() is not None)
                    controller = ui.CitationFormattingController(library, workspace)
                    CitationDialog, _ = ui._native_types()
                    dialog = CitationDialog(frame, controller)
                    dialog.Show()
                    dialog.Raise()
                    pump(lambda: dialog.snapshot is not None and not dialog.pending, 'The citation window did not load its library.')
                    completed = []
                    modal_deadline = monotonic() + 15

                    def exercise():
                        child = None
                        try:
                            child = next((window for window in wx.GetTopLevelWindows()
                                          if window.GetName() == 'Regenerate saved citation document'), None)
                            if child is None:
                                if monotonic() >= modal_deadline:
                                    raise TimeoutError('Native Space did not open the regeneration dialog.')
                                wx.CallLater(50, exercise)
                                return
                            check('regeneration opens as a real native modal dialog', child.IsModal())
                            check('new DOCX is unavailable before source review', not child.save_button.IsEnabled())
                            check('embedded snapshot preview is native read-only multiline text',
                                  bool(child.preview.GetWindowStyleFlag() & wx.TE_READONLY)
                                  and bool(child.preview.GetWindowStyleFlag() & wx.TE_MULTILINE))
                            press(child.open_button)
                            pump(lambda: child.document is not None and not child.pending, 'The saved source did not finish review.')
                            check('review explains saved locator assertions and Word edits',
                                  ui.REGENERATION_NOTICE in child.preview.GetValue())
                            check('review shows the exact selected source digest',
                                  sha256(original).hexdigest() in child.preview.GetValue())
                            press(child.save_button)
                            pump(lambda: not child.pending, 'Actual Node regeneration did not finish.')
                            check('actual regenerated DOCX preserves the complete embedded snapshot',
                                  target.exists() and read_citation_docx(target) == expected)
                            check('selected original document is unchanged', source.read_bytes() == original)
                            check('completion is readable and focused',
                                  'Saved a new citation document:' in child.status.GetValue()
                                  and wx.Window.FindFocus() is child.status)
                            press(child.save_button)
                            pump(lambda: not child.pending, 'Existing-file rejection did not finish.')
                            check('existing destination is preserved', occupied.read_bytes() == b'Another existing document')
                            check('existing-file error is readable and focused',
                                  'already exists' in child.status.GetValue() and wx.Window.FindFocus() is child.status)
                            source.write_bytes(b'Newer saved source after native review')
                            press(child.save_button)
                            pump(lambda: not child.pending, 'Stale source rejection did not finish.')
                            check('changed reviewed source never publishes a new document',
                                  not stale_target.exists() and 'Open and review' in child.status.GetValue())
                            check('the native scenario consumed only the explicit selected paths', not choices)
                            completed.append(True)
                        except Exception:
                            failures.append(traceback.format_exc())
                        finally:
                            if child is not None:
                                child.close()

                    wx.CallLater(250, exercise)
                    press(dialog.regenerate_button)
                    pump(lambda: completed or failures, 'The regeneration entry did not complete the native scenario.', timeout=45)
                    if failures:
                        raise RuntimeError('\n'.join(failures))
                    check('closing regeneration restores focus to its entry button',
                          wx.Window.FindFocus() is dialog.regenerate_button)
                except Exception:
                    failures.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()

            wx.CallAfter(scenario)
            gui.MainLoop()
            if failures:
                raise RuntimeError('\n'.join(failures))
        finally:
            wx.FileDialog = actual_picker
            if dialog is not None:
                dialog.shutdown()
                dialog.Destroy()
            if frame is not None:
                frame.Destroy()
            gui.Yield()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'native_keyboard': 'PASS', 'native_file_pickers': 'NOT_RUN',
                      'nvda': 'NOT_RUN', 'manual_accessibility': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
