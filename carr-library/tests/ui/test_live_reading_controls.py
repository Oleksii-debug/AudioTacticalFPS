"""Native-control boundary checks without claiming a physical audio device."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from carr_library.reader.live_reading import LiveSection, LiveStatus
from carr_library.speech.runtime import Voice
from carr_library.ui.live_reading import LiveReadingControls, _MediaActivity, native_cursor_position, reading_range


class NativeText:
    """Windows-like UTF-16 positions over a normalized multiline display."""
    def __init__(self, text):
        self.text = text.replace('\r\n', '\n')
        self.selection, self.cursor = (0, 0), 0

    def GetValue(self):
        return self.text

    def GetRange(self, start, end):
        return self.text.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')

    def GetLastPosition(self):
        return len(self.text.encode('utf-16-le')) // 2

    def GetInsertionPoint(self):
        return self.cursor

    def GetSelection(self):
        return self.selection


class Control:
    def __init__(self, value='', selection=-1):
        self.value, self.selection, self.focus, self.enabled = value, selection, 0, True
        self.destroyed = False

    def alive(self):
        assert not self.destroyed, 'Late timer touched a destroyed native control.'

    def ChangeValue(self, value):
        self.alive()
        self.value = value

    def SetFocus(self):
        self.alive()
        self.focus += 1

    def SetInsertionPoint(self, position):
        self.alive()

    def Enable(self, enabled=True):
        self.alive()
        self.enabled = enabled

    def GetSelection(self):
        self.alive()
        return self.selection

    def SetSelection(self, selection):
        self.alive()
        self.selection = selection

    def Set(self, items):
        self.alive()
        self.items = items
        self.selection = 0 if items else -1  # Force tests to require explicit deselection.

    def GetValue(self):
        self.alive()
        return self.value

    def SetValue(self, value):
        self.alive()
        self.value = value

    def GetStringSelection(self):
        self.alive()
        return self.value


class Core:
    def __init__(self):
        self.busy, self.view, self.acks, self.calls = False, LiveStatus(), [], []
        self.completion = None

    def status(self):
        return self.view

    def acknowledge(self, section):
        self.acks.append(section)

    def start(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.busy = True

    def stop(self, reason=None):
        self.calls.append(('stop', reason))

    def close(self):
        return self.completion


def controls():
    owner = object.__new__(LiveReadingControls)
    owner.closed = False
    owner.wx = SimpleNamespace(NOT_FOUND=-1)
    owner.core = Core()
    owner.source = SimpleNamespace(supports_resume=True)
    owner.ready = lambda: True
    owner._sequence, owner._voices, owner._section = -1, (), None
    owner.read_buttons = [Control() for _ in range(3)]
    owner.discover_button, owner.resume_button, owner.pause_button, owner.stop_button = [Control() for _ in range(4)]
    owner.voice, owner.status = Control(), Control()
    owner.rate, owner.volume, owner.continuous = Control(0), Control(100), Control(True)
    owner._host = SimpleNamespace()
    owner._previous_media = False
    owner._media_indicator = _MediaActivity(False, owner.core)
    owner._host.progress_media_busy = owner._media_indicator
    owner._feedback_wait_since = None
    owner._bridge = None
    owner._release_media = None
    owner.on_stop = lambda: None
    owner.seek_button = Control()
    owner.shown = []
    owner.show_section = owner.shown.append
    owner.timer = SimpleNamespace(Stop=lambda: None)
    return owner


def test_native_utf16_cursor_selection_maps_exact_original_crlf_text():
    raw = 'Є 🚂\r\nrepeat\r\nrepeat Ω'
    control = NativeText(raw)
    prefix = 'Є 🚂\nrepeat\n'
    position = len(prefix.encode('utf-16-le')) // 2
    control.cursor = position
    control.selection = position, position + 6
    start, end = reading_range(control, raw, 'selection')
    assert raw[start:end] == 'repeat' and start == raw.rindex('repeat')
    assert reading_range(control, raw, 'cursor') == (start, len(raw))
    control.selection = position, position
    with pytest.raises(ValueError, match='Select source text'):
        reading_range(control, raw, 'selection')


def test_saved_python_cursor_roundtrips_every_complete_native_boundary():
    raw = 'Є 🚂\r\nrepeat\r\nrepeat Ω 🚀.'
    control = NativeText(raw)
    for offset in range(len(raw) + 1):
        if 0 < offset < len(raw) and raw[offset - 1:offset + 1] == '\r\n':
            with pytest.raises(ValueError, match='boundary'):
                native_cursor_position(control, raw, offset)
            continue
        control.cursor = native_cursor_position(control, raw, offset)
        assert reading_range(control, raw, 'cursor') == (offset, len(raw))


def test_native_crlf_presentation_does_not_create_authored_reading_revision():
    from carr_library.speech.reading import ReadingDocument
    from carr_library.ui.speech_dialog import capture_document_text
    original = 'Exact 🚂\r\nOriginal.\r\n'
    document = ReadingDocument(original)
    displayed = original.replace('\r\n', '\n')
    for _ in range(3):
        capture_document_text(document, displayed, displayed_value=displayed, displayed_text=original)
    assert document.revision == 0 and document.text == document.source_text == original
    capture_document_text(document, displayed + 'An explicit edit.',
                          displayed_value=displayed, displayed_text=original)
    assert document.revision == 1 and document.source_text == original
    assert document.undo() and document.text == original


def test_discovery_does_not_preselect_voice_or_steal_focus():
    owner = controls()
    voice = Voice('test:explicit', 'Explicit voice', 'synthetic', 'uk', ('rate', 'volume'))
    owner.core.view = LiveStatus(sequence=1, voices=(voice,))
    owner.tick()
    assert owner.voice.selection == -1
    assert owner.status.focus == owner.voice.focus == 0
    with pytest.raises(ValueError, match='explicitly'):
        owner._voice_settings()
    owner.voice.SetSelection(0)
    assert owner._voice_settings() == ('test:explicit', 1., 1.)


def test_approximate_backend_position_is_labelled_without_word_alignment_claim():
    owner = controls()
    owner.core.busy = True
    owner.core.view = LiveStatus(sequence=2, state='paused', duration_seconds=2,
        audio_seconds=.5, position_is_approximate=True,
        message='Paused at the estimated audio position.')
    owner.tick()
    assert 'Estimated audio 0.5 of 2.0 seconds.' in owner.status.value
    assert 'measured' not in owner.status.value and owner.status.focus == 0


def test_selection_never_enables_continuous_reading_even_when_checkbox_is_on():
    owner = controls()
    voice = Voice('test:explicit', 'Explicit voice', 'synthetic', 'uk', ('rate', 'volume'))
    owner._voices, owner.voice.selection = (voice,), 0
    raw = 'Є 🚂\r\nRead this only. Next.'
    owner.text = NativeText(raw)
    beginning = len('Є 🚂\n'.encode('utf-16-le')) // 2
    owner.text.selection = (beginning, beginning + 4)
    owner.current = lambda: (1, raw)
    owner.start('selection')
    assert len(owner.core.calls) == 1
    _args, options = owner.core.calls[0]
    assert options['continuous'] is False
    assert raw[options['start']:options['end']] == 'Read'


def test_closed_timer_and_actions_never_touch_native_controls():
    owner = controls()
    owner.closed = True
    for value in vars(owner).values():
        if isinstance(value, Control):
            value.destroyed = True
    owner.tick()
    owner.start('page')
    owner.resume()
    owner.pause()
    owner.stop()
    owner.focus_status()
    assert owner.core.calls == []


def test_stopped_pending_section_cannot_reopen_old_source_after_user_navigation():
    owner = controls()
    section = LiveSection('Text', 'Source', 1, 2)
    owner.core.view = LiveStatus(sequence=2, state='stopped', section=section)
    owner.tick()
    assert owner.shown == owner.core.acks == []
    assert owner.status.focus == 0


def test_playback_waits_for_progress_cue_quiescence_without_blocking_native_loop():
    owner = controls()
    feedback = SimpleNamespace(quiescent=False, suspended=0)
    def suspend():
        feedback.suspended += 1
    feedback.suspend = suspend
    feedback.quiet_during_media = True
    owner._bridge = feedback
    owner._quiet_begin()
    assert feedback.suspended == 1
    section = LiveSection('Text', 'Source', 1, 2)
    owner.core.busy = True
    owner.core.view = LiveStatus(sequence=2, state='loading', section=section)
    owner.tick()
    assert owner.shown == [section] and owner.core.acks == []
    feedback.quiescent = True
    owner.tick()  # Same worker sequence: an acknowledged display must still wake it.
    assert owner.core.acks == [section]
    assert owner.status.focus == 0


def test_closing_retains_quiet_media_flag_until_player_worker_actually_finishes():
    owner = controls()
    owner.core.busy = True
    owner.core.view = LiveStatus(state='playing')
    future = Future()
    owner.core.completion = future
    indicator = owner._host.progress_media_busy
    owner.close()
    assert owner.closed and indicator() is True
    owner.status.destroyed = True
    future.set_result(None)
    assert indicator() is False
    assert owner.status.focus == 0


def test_close_is_nonblocking_and_retains_bridge_lease_until_cleanup_future():
    owner = controls()
    owner.core.busy = True
    owner.core.view = LiveStatus(state='playing')
    pending = Future()
    owner.core.completion = pending
    releases = []
    owner._release_media = lambda: releases.append('released')
    owner.close()
    owner.close()
    assert owner.closed and not pending.done() and not releases
    for value in vars(owner).values():
        if isinstance(value, Control):
            value.destroyed = True
    pending.set_exception(RuntimeError('The native device failed during bounded cleanup.'))
    assert releases == ['released'] and owner._media_indicator() is False


def test_uncertain_native_silence_retains_lease_after_worker_completion_even_if_optional_muting_is_off():
    owner = controls()
    owner.core.quiescent, owner.core.silence_uncertain = False, True
    owner.core.completion = Future()
    owner.core.completion.set_result(None)
    releases = []
    owner._release_media = lambda: releases.append('released')
    owner._bridge = SimpleNamespace(quiet_during_media=False, quiescent=False)
    assert owner._quiet_ready() is False
    indicator = owner._media_indicator
    owner.close()
    assert indicator() is True and indicator.silence_uncertain is True and not releases


def test_display_rejection_stops_playback_and_preserves_specific_error():
    owner = controls()
    owner.core.view = LiveStatus(sequence=3, state='loading', section=LiveSection('Text', 'Source', 2, 2))
    def reject(_section):
        raise ValueError('Source selection was revoked.')
    owner.show_section = reject
    owner.tick()
    assert owner.core.acks == []
    assert owner.core.calls == [('stop', 'Source selection was revoked.')]
    assert 'revoked' in owner.status.value and owner.status.focus == 0


def test_initial_source_acceptance_keeps_async_voice_inventory_alive(monkeypatch):
    import carr_library.ui.reading_desk as source_ui
    owner = controls()
    owner.core.busy = True
    owner.core.view = LiveStatus(sequence=1, state='discovering')
    dialog = object.__new__(source_ui.ReadingDialog)
    dialog.closed = False
    dialog.live = owner
    accepted = []
    dialog.controller = SimpleNamespace(check=lambda: None, accept=accepted.append, save_warning='')
    dialog.text, dialog.location, dialog.status = Control(), Control(), Control()
    monkeypatch.setattr(source_ui, 'location_text', lambda page: 'Verified source location')
    navigation = SimpleNamespace(page=SimpleNamespace(raw_text='Є source.\r\n'))
    dialog._page(navigation)
    assert accepted == [navigation] and dialog.text.value == 'Є source.\r\n'
    assert owner.core.calls == [], 'Initial source acceptance cancelled asynchronous discovery.'
    voice = Voice('test:explicit', 'Explicit voice', 'synthetic', 'uk', ('rate', 'volume'))
    owner.core.busy = False
    owner.core.view = LiveStatus(sequence=2, state='idle', voices=(voice,))
    owner.tick()
    assert owner._voices == (voice,) and owner.voice.selection == -1
    owner.core.busy = True
    owner.core.view = LiveStatus(sequence=3, state='playing')
    dialog._page(navigation)
    assert owner.core.calls == [('stop', None)], 'Navigation must still stop actual speech.'
    accepted_before = tuple(accepted)
    dialog.closed = True
    dialog._page(SimpleNamespace(page=SimpleNamespace(raw_text='Rejected late source.')))
    dialog._stop_live()
    assert tuple(accepted) == accepted_before and dialog.text.value == 'Є source.\r\n'
    assert owner.core.calls == [('stop', None)], 'Detached source callbacks touched the live owner.'
