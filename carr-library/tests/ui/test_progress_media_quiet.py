"""Progress notification/media boundaries; no native device or listening claims."""

from types import SimpleNamespace
import threading
import time

import pytest

from carr_library.speech.player import WavePlayer
from carr_library.ui.operation_progress import (
    OperationProgress, ProgressPreferences, WindowsProgressSound,
)


def eventually(predicate, message):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, message
        threading.Event().wait(.005)


class MemoryBackend:
    SND_MEMORY, SND_NODEFAULT, SND_NOSTOP = 4, 2, 16

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.calls, self.threads = [], []

    def PlaySound(self, raw, flags):
        # A global stop could interrupt unrelated application/screen-reader sound.
        assert isinstance(raw, bytes), 'Do not stop unrelated PlaySound output'
        self.calls.append((raw, flags))
        self.threads.append(threading.get_ident())
        self.entered.set()
        assert self.release.wait(5), 'Test did not release the owned finite cue'


class BarrierVoice:
    """A cooperative provider held between synthesis and its owned playback."""
    available = True

    def __init__(self):
        self.entered, self.release, self.finished = (
            threading.Event(), threading.Event(), threading.Event()
        )
        self.calls, self.heard, self.threads = [], [], []
        self.busy, self.uncertain = False, False

    @property
    def quiescent(self):
        return not self.busy and not self.uncertain

    def speak_progress(self, text, *, volume, cancelled):
        self.busy = True
        self.calls.append((text, volume))
        self.threads.append(threading.get_ident())
        self.entered.set()
        try:
            assert self.release.wait(5), 'Test did not release speech synthesis'
            if not cancelled():
                self.heard.append(text)
        finally:
            self.busy = False
            self.finished.set()


class Gauge:
    def __init__(self):
        self.values, self.names = [], []

    def SetValue(self, value): self.values.append(value)
    def SetName(self, value): self.names.append(value)
    def SetRange(self, value): pass
    def Pulse(self): pass
    def SetFocus(self): pytest.fail('Progress stole keyboard focus')


class Label:
    def SetLabel(self, value): self.label = value
    def SetName(self, value): self.name = value
    def SetFocus(self): pytest.fail('Progress stole keyboard focus')


class Timer:
    def __init__(self, frame): self.running = False
    def Start(self, interval): self.running = True
    def Stop(self): self.running = False
    def IsRunning(self): return self.running


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.reports, self.notices = [], []
        self.status = SimpleNamespace(SetValue=self.notices.append)

    def IsActive(self): return True
    def Bind(self, *args): pass
    def report(self, value): self.reports.append(value)


@pytest.fixture
def media(tmp_path):
    backend, frame = MemoryBackend(), Frame(tmp_path)
    sound, queue, voices = WindowsProgressSound(backend=backend), [], []
    wx = SimpleNamespace(Timer=Timer, EVT_TIMER='timer', CallAfter=queue.append)
    controller = OperationProgress(frame, Gauge(), Label(), wx_module=wx, sound=sound)
    controller.configure(ProgressPreferences(mode='beep', mute_during_audio=False))

    def use_voice(voice=None):
        voice = voice or BarrierVoice()
        voices.append(voice)
        controller.set_speech_provider(voice, root=frame.library.root)
        return voice

    yield SimpleNamespace(controller=controller, sound=sound, backend=backend,
                          frame=frame, queue=queue, use_voice=use_voice)
    controller.close()
    backend.release.set()
    for voice in voices:
        voice.uncertain = False
        voice.release.set()
    eventually(lambda: sound.quiescent, 'Notification worker did not terminate')


def test_cancel_discards_blocked_speech_and_never_queues_another_announcement(media):
    voice = media.use_voice()
    assert media.sound.speech_available
    assert media.sound.speak('First announcement', volume=.25)
    assert voice.entered.wait(1)
    assert not media.sound.speak('Must not be queued', volume=.25)
    assert not media.sound.play(30, volume=.25)

    media.sound.cancel()
    assert not media.sound.quiescent
    voice.release.set()
    eventually(lambda: media.sound.quiescent, 'Cancelled speech did not settle')
    assert voice.heard == []
    assert voice.calls == [('First announcement', .25)]
    assert media.backend.calls == []

    assert media.sound.speak('Explicit new announcement', volume=.25)
    eventually(lambda: media.sound.quiescent, 'Fresh speech did not finish')
    assert voice.heard == ['Explicit new announcement']


def test_close_cancels_active_speech_and_prevents_late_output(media):
    voice = media.use_voice()
    assert media.sound.speak('Cancelled by close', volume=.25)
    assert voice.entered.wait(1)
    media.sound.close()
    assert not media.sound.speak('Closed worker', volume=.25)
    assert not media.sound.play(100, volume=.25, state='succeeded')
    voice.release.set()
    eventually(lambda: media.sound.quiescent, 'Closed worker did not settle')
    assert voice.heard == []


def test_combined_mode_drains_beep_before_speech_on_the_same_worker(media):
    voice = media.use_voice()
    media.backend.release.clear()
    assert media.sound.speak('Half complete', volume=.2, beep=True, percent=50,
                             state='running')
    assert media.backend.entered.wait(1)
    assert not voice.entered.is_set()
    assert not media.sound.speak('Do not queue another cue', volume=.2)
    media.backend.release.set()
    assert voice.entered.wait(1)
    assert media.backend.threads == voice.threads
    assert voice.threads[0] != threading.get_ident()
    voice.release.set()
    eventually(lambda: media.sound.quiescent, 'Combined notification did not settle')
    assert voice.heard == ['Half complete']
    assert voice.calls == [('Half complete', .2)]
    assert len(media.backend.calls) == 1


def test_cancelling_combined_mode_during_beep_prevents_late_voice(media):
    voice = media.use_voice()
    media.backend.release.clear()
    assert media.sound.speak('Must never be spoken', volume=.2, beep=True,
                             percent=50, state='running')
    assert media.backend.entered.wait(1)
    media.sound.cancel()
    assert not media.sound.quiescent
    media.backend.release.set()
    eventually(lambda: media.sound.quiescent, 'Cancelled combined cue did not settle')
    assert not voice.entered.is_set()
    assert voice.calls == [] and voice.heard == []
    assert len(media.backend.calls) == 1


def test_recording_hold_waits_for_owned_beep_without_global_audio_stop(media):
    media.backend.release.clear()
    media.controller.start('Importing files', 2)
    assert media.backend.entered.wait(1)
    before = time.monotonic()
    hold = media.controller.hold_audio(recording=True)
    assert time.monotonic() - before < .5
    try:
        assert media.sound.muted
        assert not hold.ready
        assert not media.sound.play(50, volume=.25)
        media.backend.release.set()
        eventually(lambda: hold.ready, 'Recording did not wait for the finite cue')
        assert len(media.backend.calls) == 1
        assert media.backend.calls[0][1] == 22
    finally:
        hold.close()


def test_recording_hold_cancels_speech_even_when_playback_mute_is_disabled(media):
    voice = media.use_voice()
    assert media.sound.speak('Do not enter a recording', volume=.25)
    assert voice.entered.wait(1)
    hold = media.controller.hold_audio(recording=True)
    try:
        assert media.sound.muted
        assert not hold.ready
        voice.release.set()
        eventually(lambda: hold.ready, 'Cancelled notification is not quiet')
        assert voice.heard == []
        assert media.frame.reports == []
    finally:
        hold.close()
    assert not media.sound.muted


def test_finished_speech_worker_cannot_claim_unconfirmed_provider_silence(media):
    voice = media.use_voice()
    voice.uncertain = True
    voice.release.set()
    assert media.sound.speak('Provider stop was not confirmed', volume=.25)
    assert voice.finished.wait(1)
    hold = media.controller.hold_audio(recording=True)
    try:
        assert not media.sound.quiescent
        assert not hold.ready
        voice.uncertain = False
        eventually(lambda: hold.ready, 'Confirmed silence did not clear the gate')
    finally:
        hold.close()


@pytest.mark.parametrize('state', ['recording', 'monitoring'])
def test_observed_capture_state_always_mutes_without_optional_playback_setting(media, state):
    class Recorder:
        pass

    recorder = Recorder()
    recorder.state = state
    media.controller.register_player(recorder)
    assert media.sound.muted
    recorder.state = 'stopped'
    media.controller.refresh_audio_state()
    assert not media.sound.muted
    media.controller.unregister_player(recorder)


def test_optional_playback_mute_cancels_speech_using_actual_wave_player_state(media):
    media.controller.configure(ProgressPreferences(mode='beep', mute_during_audio=True))
    voice = media.use_voice()
    assert media.sound.speak('Cancel when content begins', volume=.25)
    assert voice.entered.wait(1)

    player = WavePlayer()
    process = SimpleNamespace(returncode=None)
    process.poll = lambda: process.returncode
    player._state, player._process = 'playing', process
    media.controller.register_player(player)
    assert media.sound.muted
    voice.release.set()
    eventually(lambda: media.sound.quiescent, 'Playback did not cancel notification')
    assert voice.heard == []

    process.returncode = 0
    media.controller.refresh_audio_state()
    assert player.state == 'stopped'
    assert not media.sound.muted
    media.controller.unregister_player(player)


def test_unconfirmed_player_close_stays_latched_until_each_player_is_confirmed(media):
    first, second = WavePlayer(), WavePlayer()

    def unknown_device_state():
        raise RuntimeError('Device stop and close were not confirmed')

    for player in (first, second):
        player._state = 'playing'
        player._process = SimpleNamespace(poll=unknown_device_state)
        media.controller.register_player(player)
        media.controller.unregister_player(player, silence_confirmed=False)
    hold = media.controller.hold_audio(recording=True)
    try:
        assert media.sound.muted
        assert media.sound.quiescent
        assert not hold.ready
        media.controller.unregister_player(first, silence_confirmed=True)
        assert not hold.ready
        media.controller.unregister_player(second, silence_confirmed=True)
        assert hold.ready
    finally:
        hold.close()
    assert not media.sound.muted


def test_root_switch_cancels_old_selected_voice_without_reactivating_it(media, tmp_path):
    voice = media.use_voice()
    assert media.sound.speak('Belongs to old library', volume=.25)
    assert voice.entered.wait(1)
    other = tmp_path / 'another-library'
    other.mkdir()
    media.frame.library = SimpleNamespace(root=other)
    media.controller.reload_preferences()
    assert media.controller.preferences.mode == 'off'
    assert media.sound.muted
    assert not media.sound.speech_available
    assert not media.sound.speak('Must require new selection', volume=.25)
    voice.release.set()
    eventually(lambda: media.sound.quiescent, 'Old library voice did not settle')
    assert voice.heard == []
    assert not (other / 'state' / 'ui' / 'progress.json').exists()


def test_provider_activation_rejects_a_stale_library_root(media, tmp_path):
    old_root = media.frame.library.root
    other = tmp_path / 'new-library'
    other.mkdir()
    media.frame.library = SimpleNamespace(root=other)
    media.controller.reload_preferences()
    with pytest.raises(ValueError):
        media.controller.set_speech_provider(BarrierVoice(), root=old_root)
    assert not media.sound.speech_available


def test_independent_recording_holds_release_without_unmuting_each_other(media):
    first = media.controller.hold_audio(recording=True)
    second = media.controller.hold_audio(recording=True)
    assert first.ready and second.ready
    first.close()
    first.close()  # Cleanup is safe after an earlier explicit release.
    assert media.sound.muted
    second.close()
    assert not media.sound.muted


@pytest.mark.parametrize('observed', ['unrecognized transport state', None, 'device-query-failed'])
def test_unknown_player_state_hard_mutes_even_when_optional_playback_mute_is_disabled(media, observed):
    state = [observed]

    class Player:
        @property
        def state(self):
            if state[0] == 'device-query-failed':
                raise RuntimeError('Native device query failed')
            return state[0]

    player = Player()
    media.controller.register_player(player)
    assert media.sound.muted
    state[0] = 'stopped'
    media.controller.refresh_audio_state()
    assert not media.sound.muted
    media.controller.unregister_player(player)


def test_provider_origin_cannot_be_reauthorized_by_passing_a_different_root_argument(media, tmp_path):
    other = tmp_path / 'provider-origin'
    other.mkdir()
    voice = BarrierVoice()
    voice.library_root = other
    with pytest.raises(ValueError):
        media.controller.set_speech_provider(voice, root=media.frame.library.root)
    assert not media.sound.speech_available
