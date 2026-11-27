"""Measured notification PCM and event-gated asynchronous feedback acceptance."""
from dataclasses import replace
from io import BytesIO
import math
import struct
from threading import Event, Thread, get_ident
from time import monotonic
import wave

import pytest

from carr_library.progress import ProgressHub
from carr_library.progress_feedback import (
    FeedbackPlanner, ProgressFeedback, Tone, WindowsToneSink, progress_frequency, tone_wave,
)
from carr_library.progress_preferences import ProgressPreferences


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds=2):
        self.now += seconds


def pcm(tone):
    with wave.open(BytesIO(tone_wave(tone)), 'rb') as recording:
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getcomptype() == 'NONE'
        rate, frames = recording.getframerate(), recording.getnframes()
        samples = struct.unpack('<' + 'h' * frames, recording.readframes(frames))
    return rate, samples


def measured_frequency(rate, samples):
    # Interpolate rising zero crossings away from the fade envelope. This
    # measures the produced PCM, not merely the frequency stored in a Tone.
    margin = round(rate * 0.01)
    crossings = []
    for index in range(margin, len(samples) - margin - 1):
        left, right = samples[index:index + 2]
        if left <= 0 < right:
            crossings.append(index + (-left / (right - left)))
    assert len(crossings) > 2
    return rate * (len(crossings) - 1) / (crossings[-1] - crossings[0])


@pytest.mark.parametrize('percent', [0, 25, 50, 75, 100])
def test_actual_pcm_pitch_duration_channels_and_no_clipping(percent):
    tone = Tone(progress_frequency(percent), duration_ms=200, volume=65)
    rate, samples = pcm(tone)
    assert len(samples) == round(rate * tone.duration_ms / 1000)
    assert abs(measured_frequency(rate, samples) - tone.frequency) < 2
    assert 0 < max(abs(sample) for sample in samples) <= 12000 * 0.65
    assert samples[0] == samples[-1] == 0


def test_pcm_volume_is_local_to_the_notification_and_mute_is_silent():
    _, quiet = pcm(Tone(660, duration_ms=200, volume=25))
    _, loud = pcm(Tone(660, duration_ms=200, volume=75))
    _, muted = pcm(Tone(660, duration_ms=200, volume=0))
    rms = lambda values: math.sqrt(sum(value * value for value in values) / len(values))
    assert abs(rms(loud) / rms(quiet) - 3) < 0.01
    assert not any(muted)


@pytest.mark.parametrize('wrong', [-1, 101, True, None, 25.0, '25'])
def test_progress_pitch_rejects_unmeasured_or_out_of_range_percentages(wrong):
    with pytest.raises(ValueError):
        progress_frequency(wrong)


def test_known_progress_is_throttled_and_pitch_rises_from_actual_quarters():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Known audio progress', total=100)
    planner = FeedbackPlanner(clock=clock)
    preferences = ProgressPreferences(mode='beep', interval_seconds=2)
    cues = [planner.plan(task.snapshot(), preferences)]
    for percent in (25, 50, 75):
        clock.advance(1)
        task.update(completed=percent)
        assert planner.plan(task.snapshot(), preferences) is None
        clock.advance(1)
        cue = planner.plan(task.snapshot(), preferences)
        assert cue is not None and cue.kind == 'progress'
        cues.append(cue)
        assert planner.plan(task.snapshot(), preferences) is None
    clock.advance()
    task.update(completed=100)
    task.succeed(durable=True)
    cues.append(planner.plan(task.snapshot(), preferences))
    assert cues[-1].kind == 'completed'
    assert [cue.tones[0].frequency for cue in cues] == [220, 440, 660, 880, 1100]
    assert planner.plan(task.snapshot(), preferences) is None


def test_indeterminate_work_repeats_a_distinct_busy_cue_at_the_configured_interval():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Unknown render time')
    planner = FeedbackPlanner(clock=clock)
    preferences = ProgressPreferences(mode='beep', interval_seconds=2)
    cue = planner.plan(task.snapshot(), preferences)
    assert cue.kind == 'busy' and len(cue.tones) == 2
    assert task.snapshot().percent is None
    clock.advance(1.9)
    assert planner.plan(task.snapshot(), preferences) is None
    clock.advance(0.1)
    repeated = planner.plan(task.snapshot(), preferences)
    assert repeated.kind == 'busy' and repeated.tones == cue.tones


@pytest.mark.parametrize(('ending', 'kind'), [
    ('pause', 'paused'), ('cancel', 'cancelled'), ('fail', 'failed'), ('request', 'cancelling'),
])
def test_non_success_states_have_distinct_cues_and_never_success_pitch(ending, kind):
    clock = Clock()
    task = ProgressHub(clock=clock).start('State cue', total=100)
    task.advance(25)
    planner = FeedbackPlanner(clock=clock)
    preferences = ProgressPreferences(mode='beep')
    planner.plan(task.snapshot(), preferences)
    clock.advance(0.3)
    if ending == 'pause':
        task.pause()
    elif ending == 'cancel':
        task.cancelled()
    elif ending == 'request':
        task.request_cancel()
    else:
        task.fail('Synthetic failure')
    cue = planner.plan(task.snapshot(), preferences)
    assert cue.kind == kind and cue.tones
    assert all(tone.frequency != progress_frequency(100) for tone in cue.tones)


def test_resume_is_announced_as_resume_after_a_reported_pause():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Pause and resume', total=10)
    task.pause()
    planner = FeedbackPlanner(clock=clock)
    preferences = ProgressPreferences(mode='beep')
    assert planner.plan(task.snapshot(), preferences).kind == 'paused'
    clock.advance(0.3)
    task.resume()
    assert planner.plan(task.snapshot(), preferences).kind == 'resumed'


@pytest.mark.parametrize(('preferences', 'context'), [
    (ProgressPreferences(), {}),
    (ProgressPreferences(mode='beep', muted=True), {}),
    (ProgressPreferences(mode='beep', volume=0), {}),
    (ProgressPreferences(mode='beep'), {'active': False}),
    (ProgressPreferences(mode='beep'), {'media_busy': True}),
])
def test_off_mute_zero_volume_background_and_quiet_media_emit_nothing(preferences, context):
    task = ProgressHub().start('Suppressed operation')
    assert FeedbackPlanner().plan(task.snapshot(), preferences, **context) is None


def test_suppressed_completed_cue_is_not_replayed_when_user_unmutes():
    task = ProgressHub().start('Already completed')
    task.succeed(durable=True)
    planner = FeedbackPlanner()
    preferences = ProgressPreferences(mode='beep', muted=True)
    assert planner.plan(task.snapshot(), preferences) is None
    assert planner.plan(task.snapshot(), replace(preferences, muted=False)) is None


def test_explicit_background_and_media_permissions_are_honored():
    task = ProgressHub().start('Explicit background mode')
    preferences = ProgressPreferences(mode='beep', allow_background=True, quiet_during_media=False)
    assert FeedbackPlanner().plan(task.snapshot(), preferences, active=False, media_busy=True) is not None


def test_windows_sink_receives_only_in_memory_pcm_and_respects_preexisting_cancel():
    class Backend:
        SND_MEMORY, SND_NODEFAULT, SND_SYNC = 1, 2, 4

        def __init__(self):
            self.calls = []

        def PlaySound(self, payload, flags):
            self.calls.append((payload, flags))
    backend = Backend()
    sink = WindowsToneSink(backend=backend)
    tone = Tone(660, volume=25)
    sink.play(tone, cancelled=lambda: True)
    assert backend.calls == []
    sink.play(tone)
    assert len(backend.calls) == 1 and backend.calls[0][1] == 7
    assert backend.calls[0][0] == tone_wave(tone)
    assert isinstance(backend.calls[0][0], bytes)


def test_feedback_returns_before_playback_and_coalesces_to_the_latest_pending_cue():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Coalesced producer', total=100)
    entered, release, second_finished, update_returned = Event(), Event(), Event(), Event()
    calls = []

    class Sink:
        def play(self, tone, *, cancelled):
            if not calls:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('Probe did not release the notification worker.')
            if not cancelled():
                calls.append(tone.frequency)
            if len(calls) == 2:
                second_finished.set()
    feedback = ProgressFeedback(ProgressPreferences(mode='beep', interval_seconds=1),
                                tone_sink=Sink(), clock=clock)
    caller = Thread(target=lambda: (feedback.update(task.snapshot()), update_returned.set()))
    try:
        caller.start()
        assert entered.wait(1)
        assert update_returned.wait(1)  # Playback is still held by release.
        for count in range(1, 21):
            clock.advance()
            task.update(completed=count)
            assert feedback.update(task.snapshot()) is not None
        release.set()
        assert second_finished.wait(2)
        feedback.close()
        caller.join(timeout=2)
        assert not caller.is_alive()
        assert calls == [progress_frequency(0), progress_frequency(20)]
    finally:
        release.set()
        feedback.close()
        caller.join(timeout=2)


def test_mute_invalidates_the_queued_cue_and_cooperatively_cancels_in_flight_feedback():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Quiet transition', total=100)
    entered, release, finished = Event(), Event(), Event()
    heard = []

    class Sink:
        def play(self, tone, *, cancelled):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Notification test release failed.')
            if not cancelled():
                heard.append(tone)
            finished.set()
    preferences = ProgressPreferences(mode='beep', interval_seconds=1)
    feedback = ProgressFeedback(preferences, tone_sink=Sink(), clock=clock)
    try:
        feedback.update(task.snapshot())
        assert entered.wait(1)
        clock.advance()
        task.update(completed=25)
        feedback.update(task.snapshot())
        feedback.set_preferences(replace(preferences, muted=True))
        release.set()
        assert finished.wait(2)
        feedback.close()
        assert heard == []
    finally:
        release.set()
        feedback.close()


def test_speech_requires_an_explicit_provider_and_runs_on_the_notification_worker():
    task = ProgressHub().start('Approved spoken status', total=4)
    task.advance(1)
    unavailable = ProgressFeedback(ProgressPreferences(mode='speech'))
    try:
        assert 'speech' not in unavailable.available_modes
        assert unavailable.update(task.snapshot()) is None
        assert unavailable.last_error
        with pytest.raises(ValueError):
            unavailable.set_preferences(ProgressPreferences(mode='speech'))
    finally:
        unavailable.close()
    completed = Event()
    calls = []
    caller = get_ident()

    class ApprovedProvider:
        def speak_progress(self, text, *, volume, cancelled):
            calls.append((text, volume, get_ident(), cancelled()))
            completed.set()
    feedback = ProgressFeedback(ProgressPreferences(mode='speech', volume=42),
                                speech_provider=ApprovedProvider())
    try:
        feedback.update(task.snapshot())
        assert completed.wait(2)
        assert calls == [(task.snapshot().describe(), 42, calls[0][2], False)]
        assert calls[0][2] != caller
        assert '25 percent' in calls[0][0]
    finally:
        feedback.close()


def test_recording_suspension_cancels_pending_feedback_without_changing_user_preferences():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Recording handoff', total=100)
    entered, release, delivered = Event(), Event(), Event()
    heard = []

    class Sink:
        def play(self, tone, *, cancelled):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Recording handoff fixture did not release playback.')
            if not cancelled():
                heard.append(tone.frequency)
                delivered.set()
    preferences = ProgressPreferences(mode='beep', interval_seconds=1, quiet_during_media=False)
    feedback = ProgressFeedback(preferences, tone_sink=Sink(), clock=clock)
    try:
        feedback.update(task.snapshot())
        assert entered.wait(1) and not feedback.quiescent
        clock.advance()
        task.advance(25)
        feedback.update(task.snapshot())
        feedback.suspend()
        assert feedback.preferences == preferences
        assert not feedback.quiescent  # The held OS/provider call has not returned.
        release.set()
        deadline = monotonic() + 2
        while not feedback.quiescent and monotonic() < deadline:
            Event().wait(0.01)
        assert feedback.quiescent and heard == []
        clock.advance()
        task.advance(25)
        feedback.update(task.snapshot())
        assert delivered.wait(2)
        assert heard == [progress_frequency(50)]
    finally:
        release.set()
        feedback.close()


def test_retained_provider_cancel_callback_keeps_its_original_generation():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Provider callback generations', total=100)
    first, second = Event(), Event()
    callbacks = []

    class Provider:
        def speak_progress(self, text, *, volume, cancelled):
            callbacks.append(cancelled)
            (first if len(callbacks) == 1 else second).set()
    preferences = ProgressPreferences(mode='speech', interval_seconds=1)
    feedback = ProgressFeedback(preferences, speech_provider=Provider(), clock=clock)
    try:
        feedback.update(task.snapshot())
        assert first.wait(1)
        assert not callbacks[0]()
        feedback.set_preferences(replace(preferences, volume=31))
        clock.advance()
        task.advance(25)
        feedback.update(task.snapshot())
        assert second.wait(1) and len(callbacks) == 2
        assert callbacks[0]()  # A retained old callback cannot follow the new cue.
        assert not callbacks[1]()
    finally:
        feedback.close()
    assert all(cancelled() for cancelled in callbacks)
