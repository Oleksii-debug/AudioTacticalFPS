"""Installed WinMM playback and native Studio control acceptance.

Uses public synthetic silent RIFF/RF64 files through the real Windows device
and unmodified Studio/player. Only the local file chooser selection is supplied
by the probe. Command-event delivery is not physical keyboard or NVDA evidence.
A host without a usable output device returns 77/UNAVAILABLE, never PASS.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import struct
import sys
from tempfile import TemporaryDirectory
from threading import Timer
from time import monotonic, sleep
import wave


def _recording(path, *, seconds, rf64=False):
    rate, channels = 44100, 1
    frames = round(seconds * rate)
    data = b'\0\0' * frames
    if rf64:
        path.write_bytes(
            b'RF64' + struct.pack('<I', 0xffffffff) + b'WAVE' +
            b'ds64' + struct.pack('<IQQQI', 28, len(data) + 72, len(data), frames, 0) +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, channels, rate, rate * 2, 2, 16) +
            b'data' + struct.pack('<I', 0xffffffff) + data)
    else:
        with wave.open(str(path), 'wb') as output:
            output.setparams((channels, 2, rate, frames, 'NONE', 'PCM'))
            output.writeframes(data)
    return sha256(path.read_bytes()).hexdigest()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.path.lexists(args.output):
        raise FileExistsError('Choose a new playback acceptance report path.')
    manual = dict(physical_listening='NOT_RUN', physical_keyboard='NOT_RUN',
                  manual_file_picker='NOT_RUN', nvda='NOT_RUN')
    final = dict(schema='carr-native-playback-smoke/1', status='NOT_RUN', **manual)

    def report(code):
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(final, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(final, ensure_ascii=False), flush=True)
        return code

    if sys.platform != 'win32':
        final['reason'] = 'Native Windows execution is required.'
        return report(77)
    if not __debug__:
        raise ValueError('Run this acceptance probe without Python optimization.')
    bundle = args.bundle.resolve(strict=True)
    import carr_library
    installed_root = Path(carr_library.__file__).resolve().parent
    if not installed_root.is_relative_to(bundle / '.venv'):
        raise ValueError('Run the probe with this bundle\'s actual installed application.')
    manifest_path = bundle / 'bundle-manifest.json'
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    source_root = Path(__file__).resolve().parents[1] / 'src/carr_library'
    modules = (
        'audio/playback.py', 'audio/player.py', 'audio/ffmpeg.py',
        'audio/jobs.py', 'audio/model_manifest.py', 'ui/audio_studio.py',
        'ui/desktop_app.py', 'progress.py', 'ui/progress_view.py',
    )
    hashes = {}
    for name in modules:
        raw = (source_root / name).read_bytes()
        if (installed_root / name).read_bytes() != raw:
            raise ValueError('Installed playback source differs from the acceptance checkout: ' + name)
        hashes[name] = sha256(raw).hexdigest()
    final.update(source_commit=manifest['source_commit'], source_hashes=hashes,
                 sealed_manifest_sha256=sha256(manifest_raw).hexdigest())

    def unchanged():
        for name, digest in hashes.items():
            assert sha256((source_root / name).read_bytes()).hexdigest() == digest
            assert sha256((installed_root / name).read_bytes()).hexdigest() == digest
        assert manifest_path.read_bytes() == manifest_raw, 'Sealed bundle manifest changed.'

    network_attempts = []
    def offline(*args, **kwargs):
        network_attempts.append(True)
        raise OSError('Network is disabled during local playback acceptance.')
    socket.getaddrinfo = socket.create_connection = offline
    socket.socket.connect = socket.socket.connect_ex = offline

    def force_failure():
        try:
            print(json.dumps(dict(final, status='FAIL', forced_process_exit=True,
                                  reason='Playback acceptance exceeded its bounded cleanup deadline.')),
                  flush=True)
        finally:
            os._exit(1)

    watchdog = Timer(70, force_failure)
    watchdog.daemon = True
    watchdog.start()
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime(bundle_root=bundle)
        assert native_handle is not None, 'The sealed native runtime must be activated.'
        from carr_library.audio.playback import native_playback_probe, playback_capabilities
        capability = playback_capabilities()
        final['capability'] = capability
        api_result = native_playback_probe()
        final['native_api'] = api_result
        if api_result['status'] != 'PASS':
            final.update(status='UNAVAILABLE' if api_result['status'] == 'UNAVAILABLE' else 'FAIL',
                         reason=api_result.get('reason', 'Native API acceptance did not pass.'))
            unchanged()
            return report(77 if final['status'] == 'UNAVAILABLE' else 1)

        import wx
        from carr_library.application import LibraryApp
        from carr_library.ui.desktop_app import DesktopFrame
        from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
        application = wx.App(False)
        checks, native_events = [], []
        with TemporaryDirectory(prefix='carr-native-playback-') as temporary:
            root = Path(temporary)
            first, second = root / 'public-riff.wav', root / 'public-rf64.wav'
            originals = {
                first: _recording(first, seconds=2.4),
                second: _recording(second, seconds=1.2, rf64=True),
            }
            library = LibraryApp(root / 'library')
            frame = DesktopFrame(library)
            controller = StudioController(private_root=library.root)
            controller.new('Public synthetic playback acceptance.\r\n  No private recording.\n',
                           title='Synthetic PCM playback acceptance')
            project = controller.save(root / 'project.json')
            project_hash = sha256(project.read_bytes()).hexdigest()
            catalog = library.root / 'library.sqlite'
            catalog_hash = sha256(catalog.read_bytes()).hexdigest()
            # Prepared PCM is the fixture input. This probe makes no synthesis,
            # renderer, speech-quality or voice-selection acceptance claim.
            controller.result = {
                'output': str(first),
                'chapters': [{'start_frame': 0, 'title': 'Opening'},
                             {'start_frame': 44100, 'title': 'Second section'}],
            }
            studio = AudioStudioDialog(frame, controller)
            native_file_dialog = wx.FileDialog
            selected_files, destroyed_choices = [], []
            deadline = monotonic() + 45

            def pump_until(predicate, label, timeout=5):
                end = min(deadline, monotonic() + timeout)
                while monotonic() < end:
                    application.Yield()
                    status = studio.playback.status()
                    assert status.state not in ('error', 'unavailable'), status.error
                    if predicate(status):
                        return status
                    sleep(.01)
                raise TimeoutError('Native playback did not reach: ' + label)

            def button(name):
                control = wx.FindWindowByName(name, studio.dialog)
                assert isinstance(control, wx.Button), 'Missing native button: ' + name
                assert control.IsEnabled(), 'Disabled native button: ' + name
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
                event.SetEventObject(control)
                control.GetEventHandler().ProcessEvent(event)
                native_events.append(name)

            class Selection:
                def __init__(self, parent, message, **options):
                    assert parent is studio.dialog
                    assert message == 'Add local WAV recordings to the playlist'
                    assert options['style'] & wx.FD_MULTIPLE
                    assert options['style'] & wx.FD_FILE_MUST_EXIST
                def ShowModal(self):
                    return wx.ID_OK
                def GetPaths(self):
                    selected_files.append(str(second))
                    return [str(second)]
                def Destroy(self):
                    destroyed_choices.append(True)

            try:
                frame.Show()
                studio.dialog.Show()
                studio.notebook.SetSelection(2)
                application.Yield()
                assert studio.notebook.GetPageCount() == 4
                assert studio.playback_device.GetCount() >= 2, 'No named output device is available.'
                studio.playback_device.SetSelection(1)
                device = studio.playback_device_ids[1]
                assert type(device) is int
                button('Play rendered master')
                active = pump_until(lambda value: value.state == 'playing', 'actual device playback')
                assert active.device_id == device and active.device_name
                assert studio.progress_view._media_active
                button('Pause / resume playback')
                pump_until(lambda value: value.state == 'paused', 'pause')
                assert studio.progress_view._media_active
                studio.seek_seconds.ChangeValue('0.4')
                button('Go to playback position')
                pump_until(lambda value: value.state == 'paused' and
                           abs(value.position_seconds - .4) < .01, 'seek preserving pause')
                button('Pause / resume playback')
                pump_until(lambda value: value.state == 'playing', 'resume')
                button('Stop playback')
                pump_until(lambda value: value.state == 'stopped', 'stop and native cleanup')
                assert not studio.progress_view._media_active
                checks.extend(['actual WinMM named device', 'native Play/Pause/Seek/Resume/Stop controls',
                               'shared tone suppression while playing and paused; release after Stop'])

                studio.notebook.SetSelection(3)
                wx.FileDialog = Selection
                try:
                    button('Add WAV recordings')
                finally:
                    wx.FileDialog = native_file_dialog
                assert selected_files == [str(second)] and destroyed_choices == [True]
                assert studio.playback.playlist == (first, second)
                assert studio.playlist_choice.GetCount() == 2
                studio.playlist_choice.SetSelection(0)
                button('Play selected recording')
                pump_until(lambda value: value.state == 'playing', 'playlist first recording')
                studio.loop_start.ChangeValue('0.2')
                studio.loop_end.ChangeValue('0.55')
                button('Repeat A–B range')
                assert studio.playback.ab_loop == (.2, .55)
                loop_positions, resets = [], [0]

                def two_repeats(value):
                    assert value.device_id == device
                    assert .2 - 1 / 44100 <= value.position_seconds <= .55 + 1 / 44100
                    if loop_positions and value.position_seconds + .05 < loop_positions[-1]:
                        resets[0] += 1
                    loop_positions.append(value.position_seconds)
                    return resets[0] >= 2

                pump_until(two_repeats, 'two actual A–B range repeats', timeout=6)
                button('Pause / resume playback')
                pump_until(lambda value: value.state == 'paused', 'pause within repeated recording')
                button('Clear A–B repeat')
                pump_until(lambda value: value.state == 'paused', 'clear repeat preserving pause')
                assert studio.playback.ab_loop is None
                assert studio.segment_choice.GetCount() == 2
                studio.segment_choice.SetSelection(1)
                button('Go to selected segment')
                pump_until(lambda value: value.state == 'paused' and
                           abs(value.position_seconds - 1) < .01, 'chapter seek preserving pause')
                studio.notebook.SetSelection(2)
                studio.seek_seconds.ChangeValue('2.2')
                button('Go to playback position')
                pump_until(lambda value: value.state == 'paused' and
                           abs(value.position_seconds - 2.2) < .01, 'seek near first recording end')
                button('Pause / resume playback')
                pump_until(lambda value: studio.playback.playlist_index == 1 and
                           value.path == str(second) and value.state == 'playing', 'real RF64 queue advance')
                assert studio.playback.status().device_id == device
                pump_until(lambda value: value.state == 'completed' and
                           not studio.progress_view._media_active, 'natural RF64 completion and tone release')
                assert studio.playlist_choice.GetSelection() == 1
                checks.extend(['native A–B endpoints and repeated device playback',
                               'chapter seek preserves pause', 'RIFF to RF64 playlist advance',
                               'natural completion releases native device and tone suppression'])

                studio.notebook.SetSelection(3)
                studio.playlist_choice.SetSelection(0)
                button('Play selected recording')
                pump_until(lambda value: value.state == 'playing', 'play before sleep timer')
                studio.sleep_minutes.ChangeValue('0.001')
                button('Apply sleep timer')
                pump_until(lambda value: value.state == 'stopped' and
                           studio.playback.sleep_remaining_seconds is None, 'sleep timer stop')
                assert not studio.progress_view._media_active
                button('Read playback controls')
                assert 'Stopped' in studio.status.GetValue()
                checks.append('actual Studio sleep timer and readable playback state')

                button('Close Audio Studio')
                assert studio.closed and not studio.dialog.IsShown()
                assert studio.playback.status().state == 'closed'
                assert not studio.progress_view._media_active
                assert not studio.timer.IsRunning() and not studio.playback_timer.IsRunning()
                assert not controller.dirty and not studio.editing_dirty
                assert all(sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())
                assert sha256(project.read_bytes()).hexdigest() == project_hash
                assert sha256(catalog.read_bytes()).hexdigest() == catalog_hash
                assert not network_attempts
                checks.append('normal Close releases owned worker and timers; original PCM/project/catalog unchanged')
                final.update(status='PASS', checks=checks, native_command_events=native_events,
                             device_id=device, device_name=active.device_name, observed_repeat_resets=resets[0],
                             fixture_kind='public synthetic silent RIFF/RF64 prepared PCM; no renderer substitution',
                             fixture_sha256={path.name: digest for path, digest in originals.items()},
                             source_originals_unchanged=True, project_unchanged=True, library_catalog_unchanged=True,
                             file_picker_selection='controlled public synthetic local file',
                             network='connections denied in this acceptance process', synthesis='NOT_RUN')
            finally:
                wx.FileDialog = native_file_dialog
                # The watchdog remains active through actual device and executor
                # shutdown; it affects only this owned acceptance process.
                try:
                    studio.playback.close()
                finally:
                    studio.closed = True
                    studio.cancelled.set()
                    studio.timer.Stop()
                    studio.playback_timer.Stop()
                    studio.progress_view.close()
                    studio.executor.shutdown(wait=True, cancel_futures=True)
                    studio.dialog.Destroy()
                    frame._closing = True
                    frame.executor.shutdown(wait=True, cancel_futures=True)
                    frame.progress_view.close()
                    frame.Destroy()
                    application.Yield()
        unchanged()
        return report(0)
    except Exception as exc:
        final.update(status='FAIL', reason=type(exc).__name__ + ': ' + str(exc)[:2000])
        return report(1)
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            watchdog.cancel()


if __name__ == '__main__':
    raise SystemExit(main())

