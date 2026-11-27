"""Actual Windows SAPI/FFmpeg/native-control acceptance. No manual NVDA claim."""
from dataclasses import asdict
from pathlib import Path
import json
import os
import runpy
import sys
import struct
import wave
from tempfile import TemporaryDirectory


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance script requires actual Windows execution.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech import VoiceRegistry, SpeechSettings
    from carr_library.studio.render import ProductionService
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.studio_dialog import show_studio
    from carr_library.ui.assistant_dialog import show_assistant, show_provider_settings
    from carr_library.ui.engine_settings import show_engine_settings
    from carr_library.ui.translation_dialog import show_translation
    app = wx.App(False)
    result = {'status': 'RUNNING', 'platform': sys.platform, 'manual_nvda': 'NOT_RUN', 'checks': []}
    with TemporaryDirectory(prefix='carr-native-v3-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Native acceptance')
        registry = VoiceRegistry.discover(include_flite=False)
        sapi = tuple(voice for voice in registry.voices() if voice.engine.startswith('SAPI 5'))
        if not sapi:
            raise RuntimeError('Native SAPI discovery failed: ' + '; '.join(registry.notices))
        voice = sapi[0]
        spoken = registry.synthesize('This is an actual offline native voice acceptance check.', voice.id,
                                      root / 'spoken.wav', settings=SpeechSettings(volume=65))
        assert spoken.duration_seconds > .2 and spoken.frames > 0
        peak = 0
        with wave.open(str(spoken.path), 'rb') as speech:
            assert speech.getsampwidth() == 2
            while raw := speech.readframes(65536):
                peak = max(peak, max(abs(value[0]) for value in struct.iter_unpack('<h', raw)))
        assert peak > 100, 'The installed speech engine emitted silent PCM.'
        result['checks'].append('actual installed SAPI voice to measured PCM')
        result['voices'] = [{'id': item.id, 'name': item.name, 'language': item.language} for item in sapi]
        result['speech_peak_pcm16'] = peak
        service = ProductionService(library.root / 'state' / 'studio', voices=registry)
        script = ('[Scene: First chapter]\n[Music: suspense; tempo=80]\n'
                  'Narrator [center]: A quiet archive.\nAlice [left]: I found the letter.\n'
                  '[Pause: 2]\nBob [right]: Preserve the original.\n'
                  '[Scene: Second chapter]\nNarrator: The work is saved.')
        project = service.create(script, voice_ids=tuple(item.id for item in sapi[:3]))
        job = service.submit(project, root, stems=True, format='flac')
        report = service.run(job)
        assert project.original_script == script
        assert report['status'] == 'complete' and report['result']['duration'] > 3
        assert len(report['result']['chapters']) == 2 and not report['result']['loudness']['silent']
        resumed = ProductionService(service.root, voices=registry).run(job)
        assert resumed['artifacts'] == report['artifacts']
        result['checks'] += ['actual local three-role production with score and pause',
                             'WAV/FLAC, chapter and stem publication', 'completed-job restart and checksum verification']
        result['audio'] = {'duration': report['result']['duration'], 'artifacts': len(report['artifacts']),
                           'loudness': report['result']['loudness'],
                           'distinct_voice_tokens': len({role.voice_id for role in project.roles})}
        frame = DesktopFrame(library)
        frame.Show()
        app.Yield()
        labels = [frame.menu_bar.GetMenuLabel(i).replace('&', '') for i in range(frame.menu_bar.GetMenuCount())]
        assert all(name in labels for name in ('File', 'Edit', 'Library', 'Research', 'Audio Studio', 'Tools'))
        shortcuts = []
        for index in range(frame.menu_bar.GetMenuCount()):
            for item in frame.menu_bar.GetMenu(index).GetMenuItems():
                label = item.GetItemLabel()
                if '\t' in label:
                    shortcuts.append(label.split('\t', 1)[1].casefold())
        assert len(shortcuts) == len(set(shortcuts)), 'Global menu shortcut collision'
        result['checks'].append('native menu integration and unique global shortcuts')

        def inspect_dialog(open_dialog, required_names, close_id=None):
            inspected = []
            def visit():
                dialogs = [window for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog) and window.IsShown()]
                if not dialogs:
                    wx.CallLater(100, visit)
                    return
                dialog = dialogs[-1]
                names = []
                def children(parent):
                    for child in parent.GetChildren():
                        names.append(child.GetName())
                        children(child)
                children(dialog)
                inspected.append(all(any(required in name for name in names) for required in required_names))
                if close_id is not None:
                    dialog.EndModal(close_id)
                else:
                    dialog.Close()
            wx.CallLater(500, visit)
            open_dialog(frame)
            assert inspected == [True], 'Native dialog lacked a required accessible control name'
            app.Yield()
        inspect_dialog(show_studio, ('Script', 'Production and save status', 'Default voice',
                                     'Audio Studio current progress and phase', 'Query Audio Studio progress'))
        inspect_dialog(show_assistant, ('Library scope', 'Goal', 'Plan preview'))
        inspect_dialog(show_provider_settings, ('Provider accounts', 'Connection configuration status'), wx.ID_CLOSE)
        inspect_dialog(show_engine_settings, ('Local engine configuration',), wx.ID_CLOSE)
        inspect_dialog(show_translation, ('Google API account and edition', 'Selected text', 'Preview, supported languages'), wx.ID_CLOSE)
        result['checks'].append('native studio/assistant/provider/engine dialogs with named keyboard controls')

        def studio_modal_destruction(parent):
            from threading import Event
            from time import monotonic, sleep
            import traceback
            from carr_library.ui import studio_dialog as studio_ui

            owner = DesktopFrame(LibraryApp(root / ('studio-modal-parent' if parent else 'studio-modal-owner')))
            owner.Show()
            original_dialog, original_service = wx.Dialog, studio_ui.ProductionService
            observed = {'destroy_calls': 0, 'destroy_events': [], 'returned': False}
            state, errors, timers = {}, [], []
            closed = Event()
            deadline = monotonic() + 30
            stopped = False

            class ObservedDialog(original_dialog):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    state['dialog'] = self
                    identity = self.GetId()
                    def destroyed(event):
                        if event.GetId() == identity:
                            observed['destroy_events'].append(True)
                        event.Skip()
                    self.Bind(wx.EVT_WINDOW_DESTROY, destroyed)

                def Destroy(self):
                    observed['destroy_calls'] += 1
                    return super().Destroy()

            def observed_service(*args, **kwargs):
                actual = original_service(*args, **kwargs)
                actual_close = actual.close
                def close():
                    actual_close()
                    closed.set()
                actual.close = close
                return actual

            def fail(message):
                errors.append(message)
                probe = state.get('dialog')
                if probe and not probe.IsBeingDeleted() and probe.IsModal():
                    probe.EndModal(wx.ID_CANCEL)

            def request_destroy():
                if stopped:
                    return
                try:
                    probe = state.get('dialog')
                    assert monotonic() < deadline, 'Studio modal did not open.'
                    if probe is None or not probe.IsModal():
                        timers.append(wx.CallLater(25, request_destroy))
                        return
                    (owner if parent else probe).Destroy()
                except BaseException:
                    fail(traceback.format_exc())

            try:
                while owner.working and monotonic() < deadline:
                    app.Yield()
                    sleep(.01)
                assert not owner.working, 'Studio modal owner did not finish initialization.'
                wx.Dialog, studio_ui.ProductionService = ObservedDialog, observed_service
                timers.append(wx.CallLater(25, request_destroy))
                timers.append(wx.CallLater(30000, fail, 'Native Studio modal destruction timed out.'))
                studio_ui.show_studio(owner)
                observed['returned'] = True
                assert observed['destroy_calls'] == (0 if parent else 1), dict(observed)
                deletion_loop = wx.GUIEventLoop()
                def settled():
                    if observed['destroy_events'] and closed.is_set():
                        deletion_loop.Exit()
                    elif monotonic() >= deadline:
                        errors.append('Studio native destruction or owned service cleanup did not finish.')
                        deletion_loop.Exit()
                    else:
                        timers.append(wx.CallLater(25, settled))
                wx.CallAfter(settled)
                deletion_loop.Run()
                assert not errors, '\n'.join(errors)
                assert observed['destroy_events'] == [True] and not state['dialog'], dict(observed)
                assert closed.is_set(), 'The actual Studio service did not close before fixture cleanup.'
                observed['owned_service_closed_before_fixture_cleanup'] = True
                return dict(observed)
            finally:
                stopped = True
                wx.Dialog, studio_ui.ProductionService = original_dialog, original_service
                for timer in timers:
                    timer.Stop()
                owner.cancel_event.set()
                owner.executor.shutdown(wait=True, cancel_futures=True)
                probe = state.get('dialog')
                if probe and not probe.IsBeingDeleted():
                    probe.Destroy()
                if owner and not owner.IsBeingDeleted():
                    owner.Destroy()
                app.Yield()
                print('MODAL-STUDIO-LIFETIME:', json.dumps(observed), flush=True)

        result['studio_modal_owner'] = studio_modal_destruction(False)
        result['studio_modal_parent'] = studio_modal_destruction(True)
        result['checks'].append('native Studio modal destruction and owned cleanup before fixture release')
        operations_probe = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tests' / 'ui' / 'native_operations_smoke.py'))
        result['operations'] = operations_probe['run_native_operations'](frame, root / 'native-operations')
        result['checks'].append('native Tasks policy save, responsive close and safe destruction')
        frame.Close()
        app.Yield()
    result['status'] = 'PASS'
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
