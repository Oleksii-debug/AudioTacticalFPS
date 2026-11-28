"""Actual installed wx language evidence and explicit registry-refresh workflow.

No speech synthesis, model download or pronunciation/NVDA acceptance is inferred.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows required', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    runtime_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.language_matrix import LanguageMatrixController, _native_dialog_type
    import carr_library.language_qualification.catalog as catalog_module
    assert 'site-packages' in tuple(part.casefold() for part in Path(catalog_module.__file__).parts)
    failures = []
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            return False
    gui = ProbeApp(False)
    def pump(predicate, timeout=90):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError('Native language evidence operation exceeded its budget.')
            gui.Yield()
            sleep(.01)
    frame = dialog = None
    with TemporaryDirectory(prefix='resonance-language-native-') as temporary:
        try:
            library = LibraryApp(Path(temporary) / 'library')
            library.create_workspace('Synthetic language evidence')
            frame = DesktopFrame(library)
            gui.SetTopWindow(frame)
            frame.Show()
            assert 'audio.language_evidence' in frame.keyboard_commands.bindings
            controller = LanguageMatrixController()
            dialog = _native_dialog_type()(frame, controller)
            dialog.Show()
            gui.Yield()
            assert controller.inventory is None and dialog.languages.GetCount() == 135
            assert 'Canonical selected entries: 135' in dialog.summary.GetValue()
            assert dialog.regions.GetName() == 'Region filter'
            assert dialog.status_filter.GetName() == 'Evidence status filter'
            dialog.search.SetFocus()
            dialog.search.ChangeValue('Українська')
            dialog.filter()
            gui.Yield()
            assert len(dialog.rows) == 1 and dialog.rows[0].language.label_uk == 'Українська'
            assert 'Capability evidence: 22 categories' in dialog.details.GetValue()
            assert wx.Window.FindFocus() is dialog.search
            dialog.status_filter.SetSelection(dialog.status_ids.index('verified') + 1)
            dialog.filter()
            assert dialog.languages.GetCount() == 0
            dialog.status_filter.SetSelection(0)
            dialog.search.ChangeValue('')
            dialog.filter()
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, dialog.refresh_button.GetId())
            dialog.refresh_button.GetEventHandler().ProcessEvent(event)
            pump(lambda: not dialog.pending and not frame.working)
            assert controller.inventory is not None, dialog.status.GetValue()
            assert controller.inventory.voices, 'This Windows fixture must enumerate a real installed voice.'
            assert controller.matrix.summary()['qualified_entries'] == 0
            assert controller.matrix.summary()['entries_with_declared_voice_matches'] == 0
            assert 'No automatic language bindings' in dialog.inventory.GetValue()
            print(json.dumps({'status': 'PASS', 'catalog_entries': 135, 'capabilities_per_entry': 22,
                'installed_voices_enumerated': len(controller.inventory.voices),
                'qualified_languages': 0, 'registry': 'existing Audio Studio adapters',
                'native_controls': 'PASS', 'explicit_background_refresh': 'PASS',
                'speech_synthesis': 'NOT_RUN', 'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
        finally:
            if dialog is not None:
                dialog.shutdown()
                dialog.Destroy()
            if frame is not None:
                frame.cancel_event.set()
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
            gui.Yield()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({'status': 'FAIL', 'error': traceback.format_exc(), 'nvda': 'NOT_RUN'}), flush=True)
        raise SystemExit(1)
