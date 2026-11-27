"""Passive queue drafts and input ownership around the existing WavePlayer."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import time

from .jobs import file_identity, verified_reader
from .playlist import (PlaylistStore, PlaylistItem, SavedPlaylist, SavedPlaylistItem,
                       MAX_REVIEW_SECONDS, _check, _hash_stream, _independent)


class PlayAdmission:
    def __init__(self, root, value, items, readers):
        self.root, self.value, self.items, self.readers = root, value, items, readers

    def close(self):
        readers, self.readers = self.readers, None
        if readers is not None:
            readers.close()


class PlaylistSession:
    """No player opens media or a device while loading passive saved state."""
    def __init__(self, root, player):
        self.store, self.player = PlaylistStore(root), player
        saved, self.token = self.store.load()
        self.value = saved or SavedPlaylist()
        self.baseline = self.value
        self.items, self.admission = (), None
        self.reviewed = not self.value.items
        self.confirmation_pending = False
        self.closed = False

    @property
    def dirty(self):
        return self.value != self.baseline or self.confirmation_pending

    def _open(self):
        if self.closed:
            raise ValueError('The playback queue is closed.')
        self.store.guard()

    def _ready(self):
        self._open()
        if not self.reviewed:
            raise ValueError('Review the retained saved recordings before editing or playback.')

    def review(self, *, cancelled=lambda: False):
        self._open()
        value, token = self.store.load()
        value = value or SavedPlaylist()
        return value, token, value.review(self.store.root, cancelled=cancelled)

    def adopt(self, result):
        self._open()
        value, token, items = result
        if type(value) is not SavedPlaylist or type(items) is not tuple:
            raise ValueError('Use the exact completed saved-queue review.')
        if tuple(SavedPlaylistItem.reviewed(self.store.root, row) for row in items) != value.items:
            raise ValueError('Reviewed media no longer matches the passive queue.')
        if self.store.load() != (value if token is not None else None, token):
            raise ValueError('The saved queue changed during review; reload it.')
        self.stop()
        self.player.set_playlist(tuple(row.path for row in items))
        self.value, self.baseline, self.token = value, value, token
        self.items, self.reviewed, self.confirmation_pending = items, True, False

    def append(self, items):
        self._ready()
        rows = tuple(SavedPlaylistItem.reviewed(self.store.root, row) for row in items)
        value = replace(self.value, items=self.value.items + rows)
        if value.index is None and value.items:
            value = replace(value, index=0)
        self.stop()
        value = replace(self.value, items=self.value.items + rows)
        if value.index is None and value.items:
            value = replace(value, index=0)
        joined = self.items + tuple(items)
        self.player.set_playlist(tuple(row.path for row in joined))
        self.items, self.value = joined, value

    def select(self, index, *, frame=0):
        self._ready()
        value = replace(self.value, index=index, frame=frame, accuracy='manual')
        self.stop()
        self.value = value

    def remove(self, index):
        self._ready()
        if type(index) is not int or not 0 <= index < len(self.items):
            raise ValueError('Select a retained recording to remove.')
        self.stop()
        items = self.items[:index] + self.items[index + 1:]
        selected, frame = self.value.index, self.value.frame
        if not items:
            selected, frame = None, 0
        elif selected == index:
            selected, frame = min(index, len(items) - 1), 0
        elif selected is not None and selected > index:
            selected -= 1
        self.player.set_playlist(tuple(row.path for row in items))
        self.items = items
        self.value = replace(self.value, items=self.value.items[:index] + self.value.items[index + 1:],
                             index=selected, frame=frame, accuracy='manual')

    def move(self, index, offset):
        self._ready()
        if type(index) is not int or type(offset) is not int or offset not in (-1, 1):
            raise ValueError('Move one selected recording up or down.')
        target = index + offset
        if not 0 <= index < len(self.items) or not 0 <= target < len(self.items):
            raise ValueError('The selected recording is already at that end.')
        self.stop()
        order = list(range(len(self.items)))
        order[index], order[target] = order[target], order[index]
        selected = order.index(self.value.index) if self.value.index is not None else None
        items = tuple(self.items[i] for i in order)
        self.player.set_playlist(tuple(row.path for row in items))
        self.items = items
        self.value = replace(self.value, items=tuple(self.value.items[i] for i in order), index=selected)
        return target

    def clear(self):
        self._open()
        self.stop()
        self.player.set_playlist(())
        self.items, self.reviewed = (), True
        self.value = SavedPlaylist(skips=self.value.skips)

    def prepare_play(self, *, cancelled=lambda: False):
        self._ready()
        value, items = self.value, self.items
        if value.index is None or not items:
            raise ValueError('Select a retained recording before Play.')
        deadline = time.monotonic() + MAX_REVIEW_SECONDS
        readers = ExitStack()
        try:
            for saved, item in zip(value.items, items):
                _check(cancelled, deadline)
                if SavedPlaylistItem.reviewed(self.store.root, item) != saved:
                    raise ValueError('The retained queue changed before playback.')
                stream = readers.enter_context(verified_reader(item.path, expected_identity=item.identity))
                if _hash_stream(stream, saved.size_bytes, cancelled, deadline) != saved.sha256:
                    raise ValueError('Retained playback bytes changed before Play.')
            self._ready()
            if self.value != value or self.items != items:
                raise ValueError('The playlist changed during playback preparation.')
            return PlayAdmission(self.store.root, value, items, readers)
        except BaseException:
            readers.close()
            raise

    def start(self, admitted, *, device_id=None):
        self._ready()
        if (type(admitted) is not PlayAdmission or admitted.readers is None
                or admitted.root != self.store.root or admitted.value != self.value
                or admitted.items != self.items or self.admission is not None):
            raise ValueError('Use the exact current playback review.')
        if any(file_identity(_independent(row.path)) != row.identity for row in self.items):
            raise ValueError('Retained PCM changed before the explicit playback action.')
        self.player.set_playlist(tuple(row.path for row in self.items))
        self.admission = admitted
        try:
            return self.player.play_index(self.value.index,
                start_seconds=self.value.frame / self.value.items[self.value.index].sample_rate,
                device_id=device_id)
        except BaseException:
            self.stop()
            raise

    def capture_position(self):
        if self.admission is None:
            return self.value
        status = self.player.status()
        index = self.player.playlist_index
        if status.position_accuracy not in ('driver_reported', 'completed_buffers'):
            return self.value  # No elapsed-clock substitute for an unavailable cursor.
        if type(index) is not int or not 0 <= index < len(self.items):
            raise ValueError('The current player selection is outside its retained queue.')
        item = self.items[index]
        if (Path(status.path) != item.path or self.player.path != item.path
                or status.sample_rate != item.sample_rate or status.total_frames != item.frames
                or type(status.position_frames) is not int or not 0 <= status.position_frames <= item.frames):
            raise ValueError('The observed playhead differs from the retained PCM identity.')
        self.value = replace(self.value, index=index, frame=status.position_frames,
                             accuracy=status.position_accuracy)
        return self.value

    def poll(self):
        if self.admission is None:
            return None
        self.capture_position()
        event = self.player.poll()
        self.capture_position()
        status = self.player.status()
        if status.state in ('completed', 'error'):
            self.stop()
        return event

    def pause(self):
        self._ready()
        result = self.player.pause()
        self.capture_position()
        return result

    def resume(self):
        self._ready()
        if self.admission is None:
            raise ValueError('Choose Play to authorize playback of this retained queue.')
        return self.player.resume()

    def stop(self):
        # Cleanup stays available after the UI/library authority has been revoked.
        observation_error = None
        try:
            self.capture_position()
        except BaseException as exc:
            observation_error = exc
        self.player.stop()
        admitted, self.admission = self.admission, None
        if admitted is not None:
            admitted.close()
        if observation_error is not None:
            raise observation_error

    def snapshot(self):
        self._ready()
        self.stop()  # Settle the existing PCM owner before freezing its cursor.
        return self.value, self.token

    def acknowledge(self, receipt):
        self.token = receipt.token
        self.confirmation_pending = not receipt.confirmed
        if receipt.confirmed:
            self.baseline = receipt.saved

    def close(self):
        observation_error = None
        try:
            self.capture_position()
        except BaseException as exc:
            observation_error = exc
        self.player.close()
        admitted, self.admission = self.admission, None
        self.closed = True
        if admitted is not None:
            admitted.close()
        if observation_error is not None:
            raise observation_error
