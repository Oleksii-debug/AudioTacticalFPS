"""Installed Windows wx flow with the independent controlled protocol worker.

This verifies native controls, review boundaries and a real isolated Python
child. The fixture is not an ASR model. Native file selectors are not exercised;
the synthetic profile and source are supplied explicitly by this probe.
"""
import argparse
from hashlib import sha256
import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', action='store_true', required=True)
    parser.add_argument('--timeout', type=float, default=120)
    options = parser.parse_args(argv)
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows wx is required.',
                          'actual_asr_inference': 'NOT_RUN', 'nvda': 'NOT_RUN'}))
        return 77
    import wx
    from carr_library.application import LibraryApp
    import carr_library.transcription.service as service_module
    import carr_library.ui.transcription as ui
    import carr_library.ui.desktop_app as desktop

    if not 1 <= options.timeout <= 300:
        raise ValueError('Use a bounded native acceptance timeout.')
    installed = Path(metadata.distribution('universal-research-library').locate_file('carr_library')).resolve()
    for module in (service_module, ui, desktop):
        assert Path(module.__file__).resolve().is_relative_to(installed), 'Application loaded outside the installed wheel.'
    helper_path = Path(__file__).resolve().parents[1] / 'transcription' / 'conftest.py'
    spec = importlib.util.spec_from_file_location('controlled_transcription_native_fixture', helper_path)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    checks, failures, dialogs = [], [], []
    deadline = monotonic() + options.timeout
    frame = None
    previous_show = ui.show_transcription

    def abort(reason):
        failures.append(str(reason))
        for dialog in dialogs:
            if dialog.alive:
                dialog.close()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    application = ProbeApp(False)
    event_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(event_loop)
    watchdog = wx.CallLater(int(options.timeout * 1000), abort, 'Native transcription deadline exceeded.')

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def pump(predicate):
        while monotonic() < deadline:
            application.Yield()
            if failures:
                raise AssertionError('; '.join(failures))
            if predicate():
                return
            wx.MilliSleep(10)
        raise TimeoutError('Native transcription deadline exceeded.')

    def click(button):
        assert button.IsEnabled(), button.GetName() + ' is disabled'
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.ProcessWindowEvent(event)

    try:
        with TemporaryDirectory(prefix='carr-native-transcription-', ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            class Factory:
                def mktemp(self, name):
                    path = root / name
                    path.mkdir()
                    return path
            template = helpers.protocol_runtime_template.__wrapped__(Factory())
            case = root / 'Синтетичний протокол Ľudská'
            case.mkdir()
            fixture = helpers.setup.__wrapped__(case, template)
            create = next(fixture)
            try:
                service, workspace, profile, source = create('valid')
                library = service.app
                topic = library.create_topic(workspace, 'Native transcription test')
                imported = library.add_text(workspace, 'Immutable source', 'Original source — Україна.')
                protected = library.assets.resolve(imported['asset'])
                original, source_bytes = protected.read_bytes(), source.read_bytes()
                frame = desktop.DesktopFrame(library)
                application.SetTopWindow(frame)
                frame._open_workspace(workspace, topic)
                frame.Show()
                frame.Raise()
                pump(lambda: frame.active == (workspace, topic) and not frame.working)
                check('installed Main registry owns the transcription command',
                      frame.keyboard_commands.available('transcription.open'))

                def exercise(dialog):
                    dialogs.append(dialog)
                    try:
                        check('real modal dialog has named review and operation controls',
                              isinstance(dialog.dialog, wx.Dialog) and dialog.dialog.IsModal()
                              and dialog.dialog.GetName() == 'Local transcription'
                              and dialog.review.GetName() == 'Request review and derivative results'
                              and not dialog.review.IsEditable())
                        check('Run is disabled before a prepared request', not dialog.buttons['run'].IsEnabled())
                        dialog.profile.SetValue(str(profile))
                        dialog.language.SetValue('uk')
                        dialog.disk.SetValue('32')
                        dialog.deadline.SetValue('30')
                        dialog.sources = (source,)
                        dialog.source_list.Set([source.name])
                        for control in dialog.formats.values():
                            control.SetValue(True)
                        dialog.changed()
                        click(dialog.buttons['prepare'])
                        pump(lambda: not dialog.actions.busy)
                        prepared = dialog.actions.prepared
                        check('native Prepare creates a review without an output receipt',
                              prepared is not None and dialog.receipt is None
                              and not (service.jobs_root / prepared.id / 'receipt.json').exists())
                        check('review displays the exact request and explicit selected source',
                              prepared.request_sha256 in dialog.review.GetValue()
                              and source.name in dialog.review.GetValue())
                        dialog.language.SetValue('en')
                        check('editing a language withdraws the previous authorization',
                              dialog.actions.prepared is None and not dialog.buttons['run'].IsEnabled())
                        dialog.language.SetValue('uk')
                        click(dialog.buttons['prepare'])
                        pump(lambda: not dialog.actions.busy)
                        check('a fresh native preview restores explicit Run', dialog.buttons['run'].IsEnabled())
                        click(dialog.buttons['run'])
                        pump(lambda: not dialog.actions.busy)
                        receipt = dialog.receipt
                        check('native Run completes the actual isolated controlled child',
                              receipt is not None and receipt.quotation_authority is False)
                        check('receipt retains all four requested derivative formats',
                              len(receipt.outputs) == 4
                              and {Path(name).suffix for name in receipt.outputs} == {'.txt', '.srt', '.vtt', '.json'})
                        check('native result exposes an independent receipt token',
                              dialog.receipt_token.GetValue() == receipt.receipt_sha256)
                        click(dialog.buttons['read'])
                        pump(lambda: not dialog.actions.busy)
                        check('native read verifies the derivative before displaying Unicode text',
                              'Контрольний текст' in dialog.review.GetValue()
                              and 'not quotation evidence' in dialog.review.GetValue())
                        check('result focus belongs to the named native read-only review',
                              wx.Window.FindFocus() is dialog.review and not dialog.review.IsEditable())
                        check('original audio and catalog source bytes remain unchanged',
                              source.read_bytes() == source_bytes and protected.read_bytes() == original)
                        retained = service.verify(receipt.id, expected_receipt_sha256=receipt.receipt_sha256)
                        check('fresh backend verifies the displayed receipt independently',
                              retained.receipt_sha256 == receipt.receipt_sha256)
                        frame.library = LibraryApp(root / 'replacement-library')
                        check('changing the parent library withdraws the dialog capability',
                              not dialog.actions.current())
                        frame.library = library
                        click(dialog.buttons['close'])
                        check('native Close withdraws authorization and hides the modal',
                              dialog.actions.closed and not dialog.dialog.IsShown())
                    except BaseException:
                        abort(traceback.format_exc())

                ui.show_transcription = lambda parent: previous_show(parent, on_ready=exercise)
                event = wx.CommandEvent(wx.EVT_MENU.typeId, frame._transcription_item.GetId())
                event.SetEventObject(frame)
                wx.CallAfter(frame.ProcessWindowEvent, event)
                pump(lambda: len(dialogs) == 1 and not dialogs[0].alive)
                check('one real Main menu event opens one transcription dialog', len(dialogs) == 1)
                fresh = ui.TranscriptionActions(frame)
                check('a new session retains no reusable request authorization', fresh.prepared is None)
                fresh.close()
            finally:
                for dialog in dialogs:
                    if dialog.alive:
                        dialog.close()
                    dialog.executor.shutdown(wait=True, cancel_futures=True)
                fixture.close()
        print(json.dumps({'status': 'PASS', 'checks': len(checks), 'platform': 'Windows',
                          'native_controls': 'PASS', 'isolated_controlled_protocol': 'PASS',
                          'file_selector': 'INJECTED_SYNTHETIC_PATHS', 'actual_asr_inference': 'NOT_RUN',
                          'recognition_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
        return 0
    except BaseException as exc:
        print(json.dumps({'status': 'FAIL', 'checks_passed': len(checks), 'error': str(exc),
                          'actual_asr_inference': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
        return 1
    finally:
        ui.show_transcription = previous_show
        watchdog.Stop()
        if frame is not None:
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.Yield()
        del activation
        application.Destroy()


if __name__ == '__main__':
    raise SystemExit(main())
