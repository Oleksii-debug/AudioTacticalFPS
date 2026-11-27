"""Installed native wx ASR controls with a purpose-written protocol fixture.

This performs no speech inference and supplies no private worker or model.
--fixture-python must name a full reviewed Python installation: the product's
embedded interpreter is not an ordinary venv builder. NVDA remains NOT_RUN.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture-python', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'NOT_RUN', 'checks': [], 'inference': 'synthetic-protocol-only', 'nvda': 'NOT_RUN'}
    if os.name != 'nt':
        report['reason'] = 'Native Windows is required.'
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        return 77
    try:
        exercise(args, report)
        report['status'] = 'PASS'
        return 0
    except BaseException:
        report.update(status='FAIL', error=traceback.format_exc())
        raise
    finally:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)


def exercise(args, report):
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup
    from carr_library.transcription import TranscriptionService
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import transcription_dialog
    failures = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        report['checks'].append(label)
        print('PASS:', label, flush=True)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            return False

    gui = App(False)
    check('real native Windows wx', wx.Platform == '__WXMSW__')
    check('transcription UI comes from installed wheel',
          'site-packages' in [part.casefold() for part in Path(transcription_dialog.__file__).parts])

    def pump(predicate, timeout=480):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() > deadline:
                raise TimeoutError('Native transcription action did not finish.')
            gui.Yield()
            sleep(.01)

    def click(control):
        check('enabled native button: ' + control.GetLabel(), control.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        check('native button dispatch: ' + control.GetLabel(), control.GetEventHandler().ProcessEvent(event))

    def button(window, label):
        pending = [window]
        while pending:
            control = pending.pop()
            if isinstance(control, wx.Button) and control.GetLabel().replace('&', '') == label:
                return control
            pending.extend(control.GetChildren())
        raise AssertionError('Missing native button: ' + label)

    with TemporaryDirectory(prefix='resonance-asr-native-') as temporary:
        root = Path(temporary)
        builder = Path(__file__).resolve().parents[1] / 'tests/transcription/fixture_verified_runtime.py'
        code = ('import importlib.util,json,sys;from pathlib import Path;'
                's=importlib.util.spec_from_file_location("native_asr_fixture",sys.argv[1]);'
                'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                'r=m.make_runtime(Path(sys.argv[2]));'
                'print(json.dumps({k:str(getattr(r,k)) for k in ("python","worker","model")}))')
        built = subprocess.run([str(args.fixture_python), '-I', '-X', 'utf8', '-c', code,
            str(builder), str(root / 'synthetic-runtime')], capture_output=True, text=True,
            encoding='utf-8', timeout=120, check=True)
        runtime = json.loads(built.stdout)
        source = root / 'Синтетичний протокол.wav'
        source.write_bytes(b'Purpose-written protocol input. No acoustic inference.')
        original = source.read_bytes()
        library = LibraryApp(root / 'library')
        frame = DesktopFrame(library)
        dialog = None
        try:
            gui.SetTopWindow(frame)
            frame.Show()
            dialog = transcription_dialog.build_transcription_dialog(frame)
            dialog.Show()
            check('Tools includes local transcription', any(
                item.GetItemLabelText() == 'Local transcription…'
                for index in range(frame.menu_bar.GetMenuCount())
                for item in frame.menu_bar.GetMenu(index).GetMenuItems()))
            check('reading editor allows Tab navigation',
                  not bool(dialog.editor.GetWindowStyleFlag() & wx.TE_PROCESS_TAB))
            dialog.pages.SetSelection(1)
            for control, key in ((dialog.python_path, 'python'), (dialog.worker_path, 'worker'),
                                 (dialog.model_path, 'model')):
                control.SetValue(runtime[key])
            dialog.runtime_name.SetValue('Synthetic native protocol fixture')
            click(button(dialog, 'Inspect files'))
            pump(lambda: not dialog.pending and not frame.working)
            check('inspection returns exact file identity', dialog.inspection is not None)
            check('inspection leaves execution trust unchecked', dialog.trust.GetValue() is False)
            dialog.trust.SetValue(True)
            click(button(dialog, 'Register trusted runtime'))
            pump(lambda: not dialog.pending and not frame.working)
            check('explicit registration saves one unqualified runtime',
                  len(dialog.profiles) == 1 and dialog.profiles[0]['qualification'] == 'unqualified')
            dialog.pages.SetSelection(0)
            dialog.sources = (source,)
            dialog._show_sources()
            dialog.headers.SetValue(True)
            for control in dialog.formats.values():
                control.SetValue(True)
            click(dialog.start_button)
            pump(lambda: not dialog.pending and not frame.working)
            job = dialog.current_job
            check('actual fixture process completes one durable result',
                  job is not None and job['status'] == 'complete' and job['done'] == job['total'] == 1)
            service = dialog.controller.service
            check('original input remains unchanged', source.read_bytes() == original)
            check('retained original exists independently',
                  (service._job_root(job['id']) / job['sources'][0]['retained_path']).read_bytes() == original)
            dialog.pages.SetSelection(2)
            click(button(dialog, 'Load transcript'))
            pump(lambda: not dialog.pending and not frame.working)
            check('native reading copy exposes derivative provenance',
                  dialog.document is not None and 'Unverified' in dialog.provenance.GetValue())
            original_text = service.read_transcript(job['id'])['text']
            edited = original_text + '\nExplicit native reading edit.'
            dialog.editor.SetValue(edited)
            click(button(dialog, 'Save edits'))
            pump(lambda: not dialog.pending and not frame.working)
            check('native Save retains recognition and separate edited text',
                  service.read_transcript(job['id'])['text'] == original_text
                  and service.read_transcript(job['id'], edited=True)['text'] == edited)
            dialog.export_to(root / 'exports')
            pump(lambda: not dialog.pending and not frame.working)
            export_manifests = tuple((root / 'exports').glob('*-manifest.json'))
            check('native export action commits its manifest', len(export_manifests) == 1)
            exported = json.loads(export_manifests[0].read_text(encoding='utf-8'))
            check('all requested exports and separate edit are published',
                  len(exported['files']) == 5 and exported['status'] == 'complete')
            checksum = library.backup(root / 'backup')
            restored = restore_backup(root / 'backup', root / 'restored', checksum)
            reloaded = TranscriptionService(restored / 'state/transcription')
            check('typed backup restores results and revokes runtime trust',
                  reloaded.read_transcript(job['id'], edited=True)['text'] == edited
                  and reloaded.list_runtimes()[0]['trusted'] is False)
            check('completed progress and UI state agree',
                  dialog.current_job['status'] == 'complete' and not dialog.pending)
        finally:
            if dialog is not None:
                dialog.closed = True
                dialog.timer.Stop()
                dialog.Destroy()
            frame.Close(force=True)
            gui.Yield()
            if native_handle is not None:
                native_handle.close()
    if failures:
        raise RuntimeError('\n'.join(failures))


if __name__ == '__main__':
    raise SystemExit(main())
