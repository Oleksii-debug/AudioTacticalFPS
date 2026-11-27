"""Actual installed wx/Node custom-style workflow with disposable local sources.

Native buttons and worker completion are exercised. The OS file picker is a
recorded, explicit synthetic selection; NVDA and manual keyboard are NOT_RUN.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.'}), flush=True)
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.academic_integration import read_citation_docx
    from carr_library.ui.academic_library_dialog import AcademicController
    from carr_library.ui import citation_formatting as ui
    from carr_library.ui.desktop_app import DesktopFrame

    checks, failures = [], []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            return False

    gui = ProbeApp(False)

    def wait_for(predicate, label, timeout=20):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError(label)
            gui.Yield()
            wx.MilliSleep(10)

    def click(button):
        check('native button enabled: ' + button.GetLabel(), button.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native button event handled: ' + button.GetLabel(),
              button.GetEventHandler().ProcessEvent(event))

    source = Path(ui.__file__).resolve()
    check('formatter comes from installed package', 'site-packages' in tuple(p.casefold() for p in source.parts))
    check('actual native Windows wx controls', wx.Platform == '__WXMSW__')
    with TemporaryDirectory(prefix='resonance-native-custom-csl-') as temporary:
        root = Path(temporary)
        frame = dialog = None
        picker_factory = wx.FileDialog
        picked = []
        try:
            library = LibraryApp(root / 'private library')
            workspace = library.create_workspace('Synthetic academic workspace')
            imported = library.add_text(workspace, 'Original source', 'Exact source wording remains unchanged.')
            original = library.assets.resolve(imported['asset']).read_bytes()
            academic = AcademicController(library, workspace)
            reference = academic.create({'title': 'Native institutional methods', 'authors': ('Research Group',),
                'year': '2026', 'notes': 'Private research note', 'edition_id': imported['edition']})
            style = root / 'кафедра.csl'
            style.write_bytes(b'<style xmlns="http://purl.org/net/xbiblio/csl" class="in-text" version="1.0">'
                b'<info><title>Native institutional style</title><id>urn:test:native</id></info>'
                b'<citation><layout><text variable="title"/></layout></citation>'
                b'<bibliography><layout><text variable="title" prefix="Institution: "/></layout></bibliography></style>')
            style_hash = sha256(style.read_bytes()).hexdigest()
            destination = root / 'institutional.docx'
            selections = iter((style, destination))

            class SelectedFile:
                def __init__(self, parent, title, **kwargs):
                    self.path = next(selections)
                    picked.append((title, self.path))
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(self.path)

            frame = DesktopFrame(library)
            frame.Show()
            gui.SetTopWindow(frame)
            wait_for(lambda: not frame.working and not frame.busy, 'Initial desktop did not become ready.')
            frame._open_workspace(workspace)
            wait_for(lambda: frame.active == (workspace, None) and not frame.working and not frame.busy,
                     'Chosen workspace did not become ready.')
            initial_book = frame.book
            CitationDialog, _ = ui._native_types()
            controller = ui.CitationFormattingController(library, workspace)
            dialog = CitationDialog(frame, controller)
            wx.FileDialog = SelectedFile

            def scenario():
                try:
                    wait_for(lambda: dialog.snapshot is not None and not dialog.pending,
                             'Actual academic references did not load.')
                    check('custom style actions use named native controls',
                          isinstance(dialog.choose_style, wx.Button) and isinstance(dialog.local_style, wx.TextCtrl)
                          and bool(dialog.choose_style.GetName()) and bool(dialog.local_style.GetName()))
                    check('installed style remains the initial choice', dialog.style_path is None and dialog.style.IsEnabled())
                    dialog.choose_style.SetFocus()
                    gui.Yield()
                    check('native focus reaches custom-style action', wx.Window.FindFocus() is dialog.choose_style)
                    click(dialog.choose_style)
                    check('explicit file choice captures the selected local path', dialog.style_path == style
                          and str(style) == dialog.local_style.GetValue() and picked[0][1] == style)
                    check('custom selection disables installed style and invalidates export',
                          not dialog.style.IsEnabled() and dialog.result is None and not dialog.export_button.IsEnabled())
                    click(dialog.format_button)
                    check('pending native formatting disables both style changes', dialog.pending
                          and not dialog.choose_style.IsEnabled() and not dialog.installed_style.IsEnabled())
                    wait_for(lambda: not dialog.pending, 'Actual local CSL worker did not finish.')
                    check('real Node result uses exact custom style bytes', dialog.result is not None
                          and dialog.result.style.sha256 == style_hash)
                    check('native preview displays real institutional formatting',
                          'Institution: Native institutional methods' in dialog.preview.GetValue())
                    check('private note is excluded from native citation preview',
                          'Private research note' not in dialog.preview.GetValue())
                    click(dialog.export_button)
                    wait_for(lambda: not dialog.pending, 'Actual citation DOCX export did not finish.')
                    retained = read_citation_docx(destination)
                    check('DOCX retains style identity and actual result', retained['style']['sha256'] == style_hash
                          and retained['bibliography'][0]['text'] == 'Institution: Native institutional methods')
                    check('DOCX retains current reference identity', retained['references'][0]['id'] == reference.id)
                    click(dialog.installed_style)
                    check('return to installed style invalidates the previous result', dialog.style_path is None
                          and dialog.style.IsEnabled() and dialog.result is None and not dialog.export_button.IsEnabled())
                    check('custom workflow preserves academic records and retained source bytes',
                          academic.get(reference.id) == reference and library.assets.resolve(imported['asset']).read_bytes() == original)
                    dialog.close()
                except BaseException:
                    failures.append(traceback.format_exc())
                    dialog.close()

            wx.CallAfter(scenario)
            dialog.ShowModal()
            if failures:
                raise RuntimeError('\n'.join(failures))
            check('closing formatter preserves current desktop scope and notebook',
                  frame.active == (workspace, None) and frame.book is initial_book)
            print(json.dumps({'result': 'PASS', 'checks': len(checks), 'platform': platform.system(),
                'wx': wx.version(), 'module_sha256': sha256(source.read_bytes()).hexdigest(),
                'native_file_picker': 'SYNTHETIC_EXPLICIT_SELECTION', 'nvda': 'NOT_RUN',
                'manual_keyboard': 'NOT_RUN', 'network': 'NOT_USED'}, sort_keys=True), flush=True)
        finally:
            wx.FileDialog = picker_factory
            if dialog is not None:
                dialog.shutdown()
                dialog.executor.shutdown(wait=True, cancel_futures=True)
                dialog.Destroy()
            if frame is not None:
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
            gui.Yield()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
