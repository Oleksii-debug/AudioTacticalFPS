"""Real wx events + shared executor + synthetic supplied worker; no NVDA claim."""
from pathlib import Path
import json
import runpy
import sys
from tempfile import TemporaryDirectory
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]


def assert_product_origins(*, source=False):
    expected = (ROOT / 'src' if source else Path(sys.prefix)).resolve()
    origins = {}
    for name, module in tuple(sys.modules.items()):
        if name == 'carr_library' or name.startswith('carr_library.'):
            filename = getattr(module, '__file__', None)
            if filename is not None:
                path = Path(filename).resolve()
                assert path.is_relative_to(expected), f'{name} imported outside the selected distribution: {path}'
                origins[name] = str(path)
    assert 'carr_library' in origins, 'The product distribution was not imported.'
    return origins


def run_native_transcription(frame, directory, *, source=False):
    """Exercise an existing host; return measured native evidence, never model quality."""
    import pytest
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.audio import transcription as asr
    from carr_library.audio.models import AudioBudget
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    from carr_library.ui import speech_dialog
    from carr_library.ui.transcription_dialog import (TranscriptionController,
                                                      create_transcription_dialog)
    assert_product_origins(source=source)

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fixture = runpy.run_path(str(ROOT / 'tests' / 'audio' / 'test_supplied_transcription.py'))
    errors, state = [], {'ui_ticks': 0, 'reader_openings': 0}
    started = time.monotonic()
    with pytest.MonkeyPatch.context() as monkeypatch:
        manifest = fixture['installation'].__wrapped__(directory, monkeypatch)
        source = directory / 'синтетичний-запис.wav'
        source.write_text('Синтетичний запис', encoding='utf-8')
        slow = directory / 'pause-and-cancel.wav'
        slow.write_text('sleep', encoding='utf-8')
        budget = AudioBudget(timeout=15, reserve_disk_bytes=0, max_output_bytes=4*1024**2)
        def factory(host, **kwargs):
            return TranscriptionController(host, service_factory=lambda root, runtime=None:
                asr.TranscriptionService(root, runtime, budget=budget), **kwargs)
        dialog = create_transcription_dialog(frame, controller_factory=factory)
        controller = dialog.controller

        def click(button):
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            assert button.GetEventHandler().ProcessEvent(event)

        def fail():
            errors.append(traceback.format_exc())
            frame.cancel_event.set()
            # The fail path leaves worker cleanup to the host executor before
            # the temporary directory is removed.
            if dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

        def poll(callback, *, active=False):
            try:
                if time.monotonic() - started > 60:
                    raise AssertionError('Native transcription exceeded its deadline.')
                state['ui_ticks'] += 1
                ready = controller._active_job is not None if active else not controller.busy
                if not ready:
                    wx.CallLater(25, poll, callback, active=active)
                else:
                    callback()
            except BaseException:
                fail()

        def reader_handoff(parent, **kwargs):
            assert parent is frame and kwargs['text'] == ''
            assert kwargs.get('source_loader') is None
            kwargs['source_guard']()
            store = ReaderDraftStore(frame.library.root, slot=kwargs['recovery_slot'])
            saved = store.load()
            assert saved.document._source_path is None
            if state['reader_openings'] == 0:
                assert saved.document.text == 'Синтетичний запис\n'
                saved.document.set_text('Edited reading copy, retained separately.')
                store.save(ReaderResume(saved.document, cursor=3))
            else:
                assert saved.document.text == 'Edited reading copy, retained separately.'
                assert saved.document.can_undo
            state['reader_openings'] += 1

        monkeypatch.setattr(speech_dialog, 'show_speech_reader', reader_handoff)

        def cancelled():
            assert controller.record['status'] == 'cancelled', controller.status
            assert not controller.busy and not frame.working
            assert dialog.start_button.IsEnabled()
            assert not dialog.pause_button.IsEnabled()
            dialog.status.SetFocus()
            wx.YieldIfNeeded()
            assert wx.Window.FindFocus() is dialog.status
            assert state['ui_ticks'] >= 4
            state['completed'] = True
            dialog.close()

        def request_cancel():
            click(dialog.cancel_button)
            wx.CallLater(25, poll, cancelled)

        def paused():
            assert controller.record['status'] == 'paused', controller.status
            assert dialog.start_button.IsEnabled()
            click(dialog.start_button)
            assert controller.busy
            wx.CallLater(25, poll, request_cancel, active=True)

        def request_pause():
            click(dialog.pause_button)
            wx.CallLater(25, poll, paused)

        def edited_again():
            assert state['reader_openings'] == 2, controller.status
            original = Path(state['complete_output']) / 'transcript.txt'
            assert original.read_text(encoding='utf-8') == 'Синтетичний запис\n'
            controller.set_files([slow])
            click(dialog.start_button)
            assert controller.busy and frame.working
            dialog.close()
            assert not controller.closed and 'before closing' in controller.status
            wx.CallLater(25, poll, request_pause, active=True)

        def edited():
            assert state['reader_openings'] == 1, controller.status
            click(dialog.edit_button)
            wx.CallLater(25, poll, edited_again)

        def completed():
            assert controller.record['status'] == 'complete', controller.status
            assert len(controller.outputs) == 1 and dialog.results.GetCount() == 1
            assert set(controller.record['request']['formats']) == {'txt', 'srt', 'vtt', 'json'}
            assert set(controller.outputs[0]['formats']) == {'txt', 'srt', 'vtt', 'json'}
            assert dialog.edit_button.IsEnabled()
            assert not dialog.language.IsEnabled()
            state['complete_output'] = controller.outputs[0]['directory']
            click(dialog.edit_button)
            wx.CallLater(25, poll, edited)

        def verified():
            assert 'inventories verified' in dialog.dependencies.GetValue(), controller.status
            controller.set_files([source])
            dialog.language.ChangeValue('uk')
            click(dialog.start_button)
            assert controller.busy and frame.working
            assert not dialog.choose_files_button.IsEnabled()
            assert dialog.cancel_button.IsEnabled()
            wx.CallLater(25, poll, completed)

        def begin():
            try:
                assert dialog.manifest.GetName() == 'Local runtime manifest'
                assert dialog.jobs.GetName() == 'Saved transcription jobs'
                assert dialog.results.GetName() == 'Completed transcription results'
                assert dialog.status.GetName() == 'Transcription status (Ctrl+Alt+G)'
                assert dialog.pages.GetPageCount() == 3
                assert dialog.profile_view.GetName() == 'Local transcription workstation'
                assert dialog.profile_view.pages.GetPageCount() == 3
                assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                initial_size = dialog.GetSize()
                dialog.SetSize(dialog.GetMinSize())
                dialog.Layout()
                assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                for page in (dialog.pages.GetPage(0), dialog.pages.GetPage(1),
                             *(dialog.profile_view.pages.GetPage(i) for i in range(3))):
                    assert isinstance(page, wx.ScrolledWindow)
                    assert page.GetScrollPixelsPerUnit()[1] > 0
                dialog.SetSize(initial_size)
                dialog.Layout()
                assert not dialog.pause_button.IsEnabled()
                controller.set_manifest(manifest)
                click(dialog.verify)
                assert controller.busy and frame.working
                wx.CallLater(25, poll, verified)
            except BaseException:
                fail()

        wx.CallAfter(begin)
        try:
            dialog.ShowModal()
        finally:
            controller.dispose()
            # A real host provides this executor; wait for any failure cleanup
            # before undoing the exact synthetic worker pin.
            frame.executor.submit(lambda: None).result(timeout=20)
            dialog.Destroy()
        if errors:
            raise AssertionError('\n'.join(errors))
        assert state.get('completed'), 'Native transcription did not complete its workflow.'
        detached = create_transcription_dialog(frame, controller_factory=factory)
        detached_controller = detached.controller
        detached.files.Destroy()
        wx.YieldIfNeeded()
        assert not detached_controller.closed, 'A child-control destruction detached the whole dialog.'
        before_status = detached_controller.status
        detached_id = detached.GetId()
        detached_controller._token = object()
        late_reports = []
        def late_report():
            late_reports.append(True)
            detached_controller.report('Late progress after a destruction request.', focus=True)
        # Top-level Destroy queues native deletion for idle time. The modal
        # loop above has exited, so run a bounded real loop instead of assuming
        # one Yield delivers EVT_WINDOW_DESTROY.
        # https://docs.wxpython.org/wx.Window.html#wx.Window.Destroy
        destroyed = []
        deletion_loop = wx.GUIEventLoop()
        def observed_destruction(event):
            # SIP may invalidate the original Python wrapper before the native
            # base destructor sends this event. Match its captured native ID.
            if event.GetId() == detached_id:
                destroyed.append(True)
                wx.CallAfter(deletion_loop.Exit)
            event.Skip()
        detached.Bind(wx.EVT_WINDOW_DESTROY, observed_destruction)
        watchdog = wx.CallLater(5000, deletion_loop.Exit)
        try:
            wx.CallAfter(late_report)
            detached.Destroy()
            # Detach immediately, before delayed callbacks/native deletion.
            assert detached_controller.closed and detached_controller._token is None
            deletion_loop.Run()
        finally:
            watchdog.Stop()
        assert destroyed == [True], 'The native dialog destruction event was not observed.'
        assert not detached, 'The destroyed dialog still has a live native window.'
        assert detached_controller.closed and detached_controller._token is None
        # Pending Python callbacks must be harmless before/after the later
        # native destruction event. Do not inspect deleted wx child controls.
        wx.GetApp().ProcessPendingEvents()
        wx.YieldIfNeeded()
        assert late_reports == [True] and detached_controller.status == before_status

        native_parent = wx.Dialog(frame, title='Native transcription parent deletion')
        try:
            native_child = create_transcription_dialog(native_parent,
                controller_factory=lambda _parent, **kwargs: factory(frame, **kwargs))
            native_controller = native_child.controller
            native_controller._token = object()
            child_id, child_events = native_child.GetId(), []
            child_status = native_controller.status

            def child_destroyed(event):
                if event.GetId() == child_id:
                    child_events.append(True)
                event.Skip()

            def forbidden_python_destroy():
                raise AssertionError('Native parent deletion must bypass the Python Destroy override.')

            native_child.Bind(wx.EVT_WINDOW_DESTROY, child_destroyed)
            native_child.Destroy = forbidden_python_destroy
            assert native_parent.DestroyChildren()
            assert child_events == [True] and not native_child
            assert native_controller.closed and native_controller._token is None
            assert native_controller._cancelled()
            parent_late_reports = []
            def late_parent_report():
                parent_late_reports.append(True)
                native_controller.report('Late transcription report after native parent deletion.', focus=True)
            wx.CallAfter(late_parent_report)
            wx.GetApp().ProcessPendingEvents()
            assert parent_late_reports == [True] and native_controller.status == child_status
        finally:
            native_parent.Destroy()
            wx.YieldIfNeeded()
    return {'status': 'PASS', 'native_controls_and_async_completion': 'PASS',
            'synthetic_worker_all_formats': 'PASS', 'pause_cancel_resume': 'PASS',
            'derived_reader_handoff_and_reopen': 'PASS', 'native_parent_destruction': 'PASS', 'nvda': 'NOT_RUN',
            'real_model_quality': 'NOT_RUN'}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='store_true', help='Explicitly test checkout source instead of the installed distribution')
    args = parser.parse_args(argv)
    if args.source:
        sys.path.insert(0, str(ROOT / 'src'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    application = wx.App(False)
    with TemporaryDirectory(prefix='resonance-native-asr-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Native transcription acceptance')
        frame = DesktopFrame(library)
        frame.Show()
        try:
            result = run_native_transcription(frame, root / 'fixture', source=args.source)
            result['product_module_origins'] = assert_product_origins(source=args.source)
        finally:
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.ProcessPendingEvents()
        print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
