"""Explicit synthetic fixtures for pure/native-bridge contract tests."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace

from carr_library.audio.playback import PlaybackStatus
from carr_library.reader.live_speech import SpeechSettings
from carr_library.reader.live_playback import PlaybackSettings
from carr_library.reader.reading_state import ReadingLocation
from carr_library.assistant.interactive_tools import _EFFECTS, RECEIPT_SCHEMA, TARGET_SCHEMA


SOURCE = 'Prefix 😀.\r\nSelected source sentence. Another sentence.'
FIRST, LAST = SOURCE.index('Selected'), SOURCE.index(' Another')
LOCATION = dict(workspace='workspace', edition='edition', revision='revision',
    kind='logical_segment', index=1, text_sha256=sha256(SOURCE.encode()).hexdigest(),
    asset_sha256='a' * 64)
TARGET = dict(schema=TARGET_SCHEMA, session_id='1' * 32, location=LOCATION, source_chars=len(SOURCE))
SETTINGS = dict(voice='sapi5:synthetic-test', language='en', rate=0, pitch=0, volume=.8,
                playback_tempo=1, playback_pitch=0)
PCM = dict(sha256='b' * 64, frames=24000, sample_rate=24000, start=FIRST, end=LAST)
SCOPE = dict(workspace_id='workspace', max_seconds=10)


def step(tool='reader.control', action='prepare'):
    args = dict(target=deepcopy(TARGET), action=action)
    if tool == 'reader.control' and action in ('select', 'prepare', 'read'):
        args.update(start=FIRST, end=LAST)
        if action != 'select':
            args['settings'] = dict(SETTINGS)
    if tool == 'player.control':
        args['pcm'] = dict(PCM)
        if action == 'seek':
            args['seconds'] = .5
    return SimpleNamespace(tool=tool, arguments=args)


def result(command=None, operation_id='2' * 32):
    command = command or step()
    action = command.arguments['action']
    pcm = None if action in ('stop', 'select') else dict(PCM)
    playback = None
    if action not in ('prepare', 'select', 'stop'):
        playback = dict(state='paused' if action == 'pause' else 'playing',
            position_frames=12000 if action == 'seek' else 0, total_frames=24000, sample_rate=24000,
            position_accuracy='driver_reported')
    return dict(artifacts=[], interactive_receipt=dict(schema=RECEIPT_SCHEMA,
        operation_id=operation_id, tool=command.tool, action=action,
        target=deepcopy(command.arguments['target']), effect=_EFFECTS[(command.tool, action)],
        observation=dict(selection=[FIRST, LAST], pcm=pcm, playback=playback,
                         settings=deepcopy(command.arguments.get('settings')))))


class Control:
    def __init__(self, value='', selection=0):
        self.value, self.selection = value, selection
        self.range = (FIRST, LAST)
    def GetValue(self):
        return self.value
    def ChangeValue(self, value):
        self.value = value
    def GetSelection(self):
        return self.range
    def GetRange(self, first, last):
        return self.value[first:last]
    def GetLastPosition(self):
        return len(self.value)
    def SetSelection(self, *value):
        if len(value) == 1:
            self.selection = value[0]
        else:
            self.range = value


class Reader:
    def __init__(self, root, main_id):
        self.root, self.main_id = root, main_id
        self.source, self.location = SOURCE, ReadingLocation(**LOCATION)
        self._assistant_session_id = TARGET['session_id']
        self._assistant_error_sequence, self._assistant_last_error = 0, ''
        self.closed = self.close_pending = self.destroyed = self.cleanup_pending = self.playing = False
        self.future = self.session = self.media_request = None
        self.text, self.mode, self.seek_seconds = Control(SOURCE), Control(), Control('0.5')
        self.modes = ['selection', 'cursor']
        self.player = SimpleNamespace(native=object(), path=None)
        self.player.snapshot = PlaybackStatus()
        self.player.status = lambda: self.player.snapshot
        self.settings, self.playback = dict(SETTINGS), PlaybackSettings()
        self.actions, self.delay, self.failure = [], False, None
    def check_thread(self):
        assert threading.get_ident() == self.main_id, 'A native control escaped its UI thread.'
    def guard(self):
        self.check_thread()
    def _settings(self):
        self.check_thread()
        return SpeechSettings(**{key: self.settings[key] for key in ('voice', 'language', 'rate', 'pitch', 'volume')})
    def _playback_settings(self):
        self.check_thread()
        return PlaybackSettings(self.settings['playback_tempo'], self.settings['playback_pitch'])
    def _select(self, selected):
        self.check_thread()
        self.text.range = tuple(selected)
        self.actions.append(('select', tuple(selected)))
    def read(self, *, play=True):
        self.check_thread()
        self.actions.append(('read', play))
        if self.failure:
            self._assistant_error_sequence += 1
            self._assistant_last_error = self.failure
            return
        if self.delay:
            self.future = object()
            return
        self.prepare()
        if play:
            self.play()
    def prepare(self):
        self.check_thread()
        first, last = self.text.range
        excerpt = SimpleNamespace(sha256=PCM['sha256'], frames=24000, sample_rate=24000,
                                  selection=SimpleNamespace(start=first, end=last))
        self.session = SimpleNamespace(current=excerpt)
        self.future = None
    def play(self):
        self.check_thread()
        self.actions.append(('play',))
        self.playing = True
        self.player.path = self.root / 'synthetic-not-opened.wav'
        self.player.snapshot = PlaybackStatus(state='playing', total_frames=24000, sample_rate=24000,
                                               position_accuracy='driver_reported')
    def pause(self):
        self.check_thread()
        before = self.player.snapshot.state
        self.actions.append(('pause' if before != 'paused' else 'resume',))
        self.player.snapshot = replace(self.player.snapshot, state='playing' if before == 'paused' else 'paused')
    def seek(self):
        self.check_thread()
        seconds = float(self.seek_seconds.GetValue())
        self.actions.append(('seek', seconds))
        self.player.snapshot = replace(self.player.snapshot, position_frames=round(seconds * 24000))
    def stop(self):
        self.check_thread()
        self.actions.append(('stop',))
        self.future = self.session = None
        self.playing = self.cleanup_pending = False
        self.player.snapshot = PlaybackStatus()


class Harness:
    def __init__(self, tmp_path):
        from carr_library.ui.assistant_interactive import NativeReaderBridge
        self.main_id, self.events = threading.get_ident(), queue.Queue()
        self.root = Path(tmp_path)
        self.root.mkdir(exist_ok=True)
        self.reader = Reader(self.root, self.main_id)
        self.frame = SimpleNamespace(library=SimpleNamespace(root=self.root),
                                     active=('workspace', None), _closing=False)
        self.bridge = NativeReaderBridge(self.frame, self.reader, dispatch=self.events.put,
                         is_main_thread=lambda: threading.get_ident() == self.main_id)
    def run(self, action, *, poll=None):
        outcome = []
        def work():
            try:
                outcome.append((True, action()))
            except BaseException as exc:
                outcome.append((False, exc))
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while thread.is_alive():
            if time.monotonic() >= deadline:
                raise AssertionError('The tested interactive worker did not settle.')
            if poll is not None:
                poll()
            try:
                callback = self.events.get(timeout=.02)
            except queue.Empty:
                continue
            callback()
        thread.join(timeout=1)
        if not outcome[0][0]:
            raise outcome[0][1]
        return outcome[0][1]
    def execute(self, command, *, operation_id='2' * 32, cancelled=lambda: False, poll=None):
        return self.run(lambda: self.bridge.execute_step(command, SCOPE,
            operation_id=operation_id, cancelled=cancelled, progress=lambda *_: None), poll=poll)
