"""Native wx reference-review smoke using real local imports and SQLite saves.

Installed-package command (no source-path bootstrap):
    python -I -B -X utf8 tests/ui/native_academic_smoke.py

Needs a native display (or externally managed Xvfb on Linux). Missing wx/display
returns NOT_RUN / exit 77. A 20-second wx watchdog bounds the interaction. All
sources are synthetic and temporary; this does not test NVDA or contact services.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import threading
import traceback


def run_native():
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'A native display is required.',
                          'windows_nvda': 'NOT_RUN'}), flush=True)
        return 77
    try:
        import wx
    except (ImportError, OSError) as exc:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native wxPython is unavailable.',
                          'error': str(exc), 'windows_nvda': 'NOT_RUN'}), flush=True)
        return 77

    from carr_library.application import LibraryApp
    from carr_library.catalog.reference_exchange import preview_references
    from carr_library.ui import academic_actions, catalog_metadata_dialog

    failures, checks, active = [], [], [None]
    report = {}
    gui_thread = threading.get_ident()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    gui = ProbeApp(False)

    def abort(message):
        failures.append(message)
        dialog = active[0]
        if dialog is not None and dialog.IsModal():
            dialog.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    def guarded(callback):
        try:
            callback()
        except BaseException:
            abort(traceback.format_exc())

    def later(callback, milliseconds=80):
        return wx.CallLater(milliseconds, lambda: guarded(callback))

    def click(button):
        assert isinstance(button, wx.Button) and button.IsShown() and button.IsEnabled()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event), 'Native button event was not handled.'

    def named(window, name, kind):
        pending, found = [window], []
        while pending:
            item = pending.pop()
            if isinstance(item, kind) and item.GetName() == name:
                found.append(item)
            pending.extend(item.GetChildren())
        assert len(found) == 1, f'Expected one named {kind.__name__}: {name}'
        return found[0]

    with TemporaryDirectory(prefix='carr-native-academic-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'private library')
        workspace = library.create_workspace('Native academic smoke')
        topic = library.create_topic(workspace, 'Research outline')
        child = library.create_topic(workspace, 'Selected evidence', parent=topic)
        source = root / 'synthetic.txt'
        source.write_text('The railway connected cities. Український точний текст.\n', encoding='utf-8')
        imported = library.import_file(workspace, source, title='Original title',
            authors=('Original Author',), language='en', edition='First', volume='III')
        assert imported['state'] == 'complete', imported
        hit, = library.search(workspace, 'railway')
        evidence_id = library.save_hit(workspace, child, hit.ref, hit.revision,
                                      'Separate historical research note.')
        library.set_evidence_tags(workspace, child, evidence_id, ('transport',))
        original_evidence = library.evidence(workspace, child)
        original_bytes = library.assets.resolve(imported['asset']).read_bytes()
        current = library.edition_metadata(workspace, imported['edition'])
        hint, = preview_references(json.dumps([{
            'id': 'external-id-never-used-as-source', 'type': 'book',
            'title': 'Reviewed title — Україна',
            'author': [{'literal': 'Olena Коваль'}, {'literal': 'Ján Novák'}],
            'language': 'uk', 'DOI': '10.0000/synthetic',
            'URL': 'https://invalid.example/not-opened',
        }], ensure_ascii=False).encode('utf-8'), format='csl-json')
        assert not hint.metadata_verified and hint.editions == () and hint.volumes == ()
        assert 'DOI' in hint.unsupported_fields
        checks.append('real TXT import, saved historical evidence and local reference parser')

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='academic-smoke')
        save_threads, saved_values = [], []
        frame = wx.Frame(None, title='Academic workflow smoke', size=(900, 800))
        gui.SetTopWindow(frame)
        frame.Show()
        frame.Raise()

        def save(**values):
            save_threads.append(threading.get_ident())
            saved_values.append(values)
            return library.update_metadata(workspace, imported['edition'],
                                           expected_metadata=current, **values)

        def check_originals():
            assert library.evidence(workspace, child) == original_evidence
            assert library.assets.resolve(imported['asset']).read_bytes() == original_bytes
            assert source.read_bytes() == original_bytes

        def run_review():
            dialog = academic_actions.ReferenceMetadataDialog(frame, current, hint,
                                                              save=save, executor=executor)
            active[0] = dialog

            def apply_fields():
                preview = named(dialog,
                    'Reference review: current values, suggestions and unsupported fields', wx.TextCtrl)
                assert not preview.IsEditable() and wx.Window.FindFocus() is preview
                assert 'Fields not stored in this catalog: ' in preview.GetValue()
                assert 'DOI' in preview.GetValue() and 'not verified' in preview.GetValue()
                checks.append('read-only review starts with native keyboard focus')
                assert dialog.fields['title'].GetValue() == current.title
                assert dialog.fields['authors'].GetValue() == '\n'.join(current.authors)
                assert library.edition_metadata(workspace, imported['edition']) == current
                click(dialog.save_button)
                assert not dialog.pending and not saved_values
                assert 'No metadata changes selected' in dialog.status.GetValue()
                checks.append('unchanged Save does not publish a revision')
                for key in ('title', 'authors', 'language'):
                    click(named(dialog, f'Use reference {key}', wx.Button))
                assert dialog.fields['title'].GetValue() == hint.titles[0]
                assert dialog.fields['authors'].GetValue() == '\n'.join(hint.authors)
                assert dialog.fields['language'].GetValue() == 'uk'
                assert dialog.fields['edition'].GetValue() == 'First'
                assert dialog.fields['volume'].GetValue() == 'III'
                assert library.edition_metadata(workspace, imported['edition']) == current
                assert not saved_values
                checks.append('named reference events change only chosen form fields before Save')
                click(dialog.save_button)
                assert dialog.pending
                assert not dialog.save_button.IsEnabled() and not dialog.cancel_button.IsEnabled()
                assert all(not control.IsEnabled() for control in dialog.fields.values())
                assert all(not control.IsEnabled() for control in dialog.reference_controls)
                checks.append('asynchronous Save disables conflicting controls')

            try:
                later(apply_fields)
                result = dialog.ShowModal()
                if failures:
                    return
                assert result == wx.ID_OK
            finally:
                active[0] = None
                dialog.Destroy()
            assert len(saved_values) == 1 and len(save_threads) == 1
            assert save_threads[0] != gui_thread
            after = library.edition_metadata(workspace, imported['edition'])
            assert (after.title, after.authors, after.language) == (hint.titles[0], hint.authors, 'uk')
            assert (after.edition, after.volume) == ('First', 'III')
            check_originals()
            checks.append('real executor save keeps unspecified metadata and historical quotations')
            later(lambda: run_cancel(after))

        def run_cancel(after):
            def unexpected_save(**values):
                raise AssertionError('Cancel must not invoke metadata save.')

            dialog = academic_actions.ReferenceMetadataDialog(frame, after, hint,
                save=unexpected_save, executor=executor)
            active[0] = dialog

            def discard():
                dialog.fields['title'].SetValue('Unsaved cancelled title')
                dialog.fields['edition'].SetValue('Unsaved cancelled edition')
                assert library.edition_metadata(workspace, imported['edition']) == after
                click(dialog.cancel_button)

            try:
                later(discard)
                result = dialog.ShowModal()
                if failures:
                    return
                assert result == wx.ID_CANCEL
            finally:
                active[0] = None
                dialog.Destroy()
            assert library.edition_metadata(workspace, imported['edition']) == after
            check_originals()
            checks.append('native Cancel discards edits without publishing metadata')
            document = library.export_outline(workspace, topic)
            assert 'Research outline' in document and 'Selected evidence' in document
            assert 'Original title' in document and 'Reviewed title' not in document
            assert json.dumps(hit.quote, ensure_ascii=False) in document
            assert '**Research note**' in document and 'Separate historical research note.' in document
            assert '**Research tags**' in document and 'transport' in document
            checks.append('real application outline wrapper preserves saved citation history')
            report.update(status='PASS', native_platform=platform.system(), wx_version=wx.version(),
                checks=checks, windows_native='PASS' if sys.platform == 'win32' else 'NOT_RUN',
                windows_nvda='NOT_RUN',
                academic_actions_sha256=sha256(Path(academic_actions.__file__).read_bytes()).hexdigest(),
                metadata_dialog_sha256=sha256(Path(catalog_metadata_dialog.__file__).read_bytes()).hexdigest())
            gui.ExitMainLoop()

        timeout = wx.CallLater(20000, lambda: abort('Native academic smoke exceeded 20 seconds.'))
        later(run_review)
        try:
            gui.MainLoop()
        finally:
            timeout.Stop()
            executor.shutdown(wait=True, cancel_futures=True)
            if frame:
                frame.Destroy()
                gui.ProcessPendingEvents()

    if failures or report.get('status') != 'PASS':
        print(json.dumps({'status': 'FAIL', 'errors': failures or ['Probe did not complete.'],
                          'windows_nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


def main():
    runtime_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        runtime_handle = activate_bundled_runtime()
        return run_native()
    except Exception:
        print(json.dumps({'status': 'FAIL', 'errors': [traceback.format_exc()],
                          'windows_nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
        return 1
    finally:
        if runtime_handle is not None:
            runtime_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
