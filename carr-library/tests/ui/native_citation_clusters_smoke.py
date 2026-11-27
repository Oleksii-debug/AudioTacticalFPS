"""Actual installed Windows controls and offline citation/DOCX workflow.

The synthetic chooser supplies an explicit temporary filename. It does not
qualify the operating-system file-picker dialog, NVDA or visible Word edits.
"""
import argparse
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    options = parser.parse_args()
    result = {'schema': 'resonance-native-citation-order/1', 'status': 'FAIL',
              'nvda': 'NOT_RUN', 'native_file_picker': 'NOT_RUN',
              'word_application': 'NOT_RUN', 'quotation_approval': False, 'checks': []}
    output = Path(options.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    def check(value, message):
        if not value:
            raise AssertionError(message)
        result['checks'].append(message)
        print('PASS: ' + message, flush=True)
    try:
        check(os.name == 'nt', 'the probe executes on native Windows')
        import wx
        from carr_library.application import LibraryApp
        from carr_library.ui.academic_library_dialog import AcademicController
        from carr_library.ui.citation_clusters_dialog import create_citation_clusters_dialog
        from carr_library.citations.documents import read_citation_docx
        source = Path(__file__).resolve().parents[2] / 'src'
        result['source_sha256'] = {}
        for name in ('citations.cluster_workbench', 'ui.citation_clusters_dialog', 'ui.citation_styles_dialog'):
            module = importlib.import_module('carr_library.' + name)
            installed = Path(module.__file__).resolve()
            expected = source / 'carr_library' / (name.replace('.', '/') + '.py')
            check(not installed.is_relative_to(source.resolve()), 'installed package owns ' + name)
            digest = sha256(installed.read_bytes()).hexdigest()
            check(digest == sha256(expected.read_bytes()).hexdigest(), 'installed bytes match checkout: ' + name)
            result['source_sha256'][name] = digest
        app = wx.App(False)
        def pump(condition, seconds=25):
            deadline = time.monotonic() + seconds
            while not condition():
                if time.monotonic() >= deadline:
                    raise AssertionError('The native citation operation did not finish within its probe allowance.')
                app.Yield()
                time.sleep(.01)
            app.Yield()
        def click(control):
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
            event.SetEventObject(control)
            check(control.GetEventHandler().ProcessEvent(event), 'native event reaches ' + control.GetLabel())
            app.Yield()
        with tempfile.TemporaryDirectory(prefix='citation-order-native-') as folder:
            root = Path(folder).resolve()
            library = LibraryApp(root / 'private')
            workspace = library.create_workspace('Native citation order acceptance')
            controller = AcademicController(library, workspace)
            first = controller.create(dict(title='Synthetic alpha', authors=('Smith, Jane',),
                year='2020', pages='11-99', notes='PrivateNotesNeverExported'))
            second = controller.create(dict(title='Synthetic beta', authors=('Jones, Alex',), year='2022'))
            dialog = create_citation_clusters_dialog(None, controller, name_mode='family-given')
            try:
                dialog.SetSize((760, 540))
                dialog.Show()
                dialog.Raise()
                pump(lambda: dialog.selection is not None and dialog.pending is None)
                check(len(dialog.references) == 2, 'native editor loads the exact current scoped references')
                check(dialog.groups.GetSize().height >= 100 and dialog.items.GetSize().height >= 70,
                      'citation and item lists retain usable height on a short display')
                first_index = next(i for i, row in enumerate(dialog.references) if row['id'] == first.id)
                second_index = next(i for i, row in enumerate(dialog.references) if row['id'] == second.id)
                dialog.reference.SetSelection(first_index)
                dialog.locator.ChangeValue('12')
                dialog.prefix.ChangeValue('see ')
                dialog.suppress.SetValue(True)
                click(dialog.new_group)
                check(dialog.draft.clusters[0]['items'][0]['locator'] == '12',
                      'actual Add citation captures the explicit locator')
                dialog.reference.SetSelection(second_index)
                dialog.locator.ChangeValue('')
                dialog.prefix.ChangeValue('')
                dialog.suppress.SetValue(False)
                click(dialog.add_item)
                check(len(dialog.draft.clusters[0]['items']) == 2, 'native group retains multiple distinct references')
                dialog.reference.SetSelection(first_index)
                dialog.locator.ChangeValue('13')
                click(dialog.new_group)
                ids = tuple(group['id'] for group in dialog.draft.clusters)
                click(dialog.up)
                check(tuple(group['id'] for group in dialog.draft.clusters) == tuple(reversed(ids)),
                      'moving a citation preserves its stable occurrence identity')
                before = dialog.draft.captured
                dialog.locator.SetValue('14')
                app.Yield()
                check(dialog.fields_dirty and not dialog.generate.IsEnabled(),
                      'unapplied locator edits disable formatting and export')
                dialog.on_preview()
                check(dialog.pending is None and dialog.draft.captured == before,
                      'a queued format command cannot silently ignore unapplied field edits')
                click(dialog.apply_item)
                check(dialog.draft.clusters[0]['items'][0]['locator'] == '14',
                      'Apply edits commits the selected reference item')
                dialog.groups.SetFocus()
                app.Yield()
                simulator = wx.UIActionSimulator()
                check(simulator.KeyDown(wx.WXK_F6) and simulator.KeyUp(wx.WXK_F6),
                      'native keyboard sends the F6 formatting accelerator')
                pump(lambda: dialog.preview is not None and dialog.pending is None)
                check(len(dialog.preview.result.citations) == 2, 'actual local citeproc renders the complete ordered sequence')
                check(wx.Window.FindFocus() is dialog.output, 'completed formatting focuses the readable result')
                check('14' in dialog.output.GetValue() and 'PrivateNotes' not in dialog.output.GetValue(),
                      'the native preview preserves locators and excludes private research notes')
                destination = root / 'ordered.docx'
                original_picker = wx.FileDialog
                class SelectedFile:
                    def __init__(self, *args, **kwargs): pass
                    def ShowModal(self): return wx.ID_OK
                    def GetPath(self): return str(destination)
                    def Destroy(self): pass
                try:
                    wx.FileDialog = SelectedFile
                    click(dialog.save)
                finally:
                    wx.FileDialog = original_picker
                pump(lambda: dialog.pending is None)
                check(destination.is_file(), 'the native Save handler publishes the real new Word document')
                stored = read_citation_docx(destination)
                check(tuple(stored['citation_ids']) == tuple(reversed(ids)),
                      'saved Word snapshot retains the displayed occurrence order')
                check(stored['quotation_verified'] is False and stored['visible_edits_verified'] is False,
                      'saved citations grant no quotation or Word-edit approval')
                original = destination.read_bytes()
                dialog.tabs.SetSelection(0)
                click(dialog.remove_group)
                check(len(dialog.draft.clusters) == 1, 'native Remove changes only the working citation draft')
                try:
                    wx.FileDialog = SelectedFile
                    click(dialog.load_order)
                finally:
                    wx.FileDialog = original_picker
                pump(lambda: dialog.pending is None)
                check(len(dialog.draft.clusters) == 2 and destination.read_bytes() == original,
                      'saved citation order reloads without changing its original document')
                dialog.note.SetFocus()
                app.Yield()
                check(dialog.edit_page.GetViewStart()[1] > 0,
                      'keyboard focus scrolls deep citation fields into the short display')
                started, release = threading.Event(), threading.Event()
                actual_preview = dialog.workbench.preview
                def delayed(*args, **kwargs):
                    started.set()
                    if not release.wait(5):
                        raise AssertionError('The close-race fixture was not released.')
                    return actual_preview(*args, **kwargs)
                dialog.workbench.preview = delayed
                dialog.on_preview()
                pump(started.is_set)
                future = dialog.pending
                previous_status = dialog.status.GetValue()
                dialog.on_close()
                release.set()
                pump(future.done)
                check(dialog.cancelled.is_set() and dialog.status.GetValue() == previous_status,
                      'closing cancels the original worker token and ignores its late result')
                check(destination.read_bytes() == original, 'closing unfinished formatting preserves the saved document')
            finally:
                dialog.Destroy()
                app.Yield()
        result['status'] = 'PASS'
        return_code = 0
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        result['traceback'] = traceback.format_exc()
        print(result['traceback'], flush=True)
        return_code = 1
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return return_code


if __name__ == '__main__':
    raise SystemExit(main())
