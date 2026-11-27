"""Synthetic PCM and a controllable owner; no physical-device claim."""
from dataclasses import replace
from pathlib import Path
import struct
import wave

from carr_library.application import LibraryApp
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.playback import PlaybackStatus
from carr_library.audio.playlist import (PlaylistStore, SavedPlaylist, SavedPlaylistItem,
                                        prepare_playlist, retain_playlist)


def pcm(path, *, value=123, frames=8000, rate=8000, rf64=False):
    path = Path(path)
    payload = struct.pack('<h', value) * frames
    if rf64:
        path.write_bytes(b'RF64' + struct.pack('<I', 0xffffffff) + b'WAVE' +
            b'ds64' + struct.pack('<IQQQI', 28, len(payload) + 72, len(payload), frames, 0) +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) +
            b'data' + struct.pack('<I', 0xffffffff) + payload)
    else:
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            stream.writeframes(payload)
    return path


def retained(tmp_path, *, count=2, save=True, rf64=False):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Retained playback')
    sources = tuple(pcm(tmp_path / ('source-' + str(i) + '.wav'), value=i + 1, rf64=rf64)
                    for i in range(count))
    rows = retain_playlist(app.root, prepare_playlist(sources)) if sources else ()
    value = SavedPlaylist(tuple(SavedPlaylistItem.reviewed(app.root, row) for row in rows),
                          0 if rows else None)
    store = PlaylistStore(app.root)
    receipt = store.save(value, expected_token=None) if save else None
    return app, sources, rows, value, receipt


class Player:
    def __init__(self):
        self.native = object()
        self.playlist, self.playlist_index, self.path = (), None, None
        self.current = PlaybackStatus()
        self.calls = []
        self.stop_failure = self.close_failure = self.play_failure = False
        self.next_poll = None

    def status(self):
        return self.current

    def set_playlist(self, paths):
        self.stop()
        self.playlist, self.playlist_index, self.path = tuple(paths), None, None
        self.calls.append(('set', self.playlist))
        self.current = PlaybackStatus()

    def play_index(self, index, *, start_seconds=None, device_id=None):
        path = self.playlist[index]
        info = wav_info(path)
        self.path, self.playlist_index = path, index
        self.current = PlaybackStatus(state='playing', path=str(path),
            position_frames=round((start_seconds or 0) * info.rate), total_frames=info.frames,
            sample_rate=info.rate, channels=info.channels, device_id=device_id,
            position_accuracy='driver_reported')
        self.calls.append(('play', index, start_seconds, device_id))
        if self.play_failure:
            raise RuntimeError('Native start failed after owning input.')
        return True

    def pause(self):
        self.current = replace(self.current, state='paused')
        return True

    def resume(self):
        self.current = replace(self.current, state='playing')
        return True

    def poll(self):
        if self.next_poll is not None:
            action, self.next_poll = self.next_poll, None
            return action()
        return None

    def stop(self):
        if self.stop_failure:
            raise RuntimeError('Native stop did not settle.')
        self.calls.append(('stop',))
        self.current = replace(self.current, state='stopped', position_frames=0,
                               position_accuracy='not_started')

    def close(self):
        if self.close_failure:
            raise RuntimeError('Native close did not settle.')
        self.stop()
        self.calls.append(('close',))
        self.current = replace(self.current, state='closed')
