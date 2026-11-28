"""Native live-reading controls; background polling never changes keyboard focus."""
from __future__ import annotations

import time

from ..reader.live_reading import LiveReader, SourceSequence, native_text_range
from ..reader.reading_units import reading_unit_range


def reading_range(control, raw, mode, *, cancelled=lambda: False):
    def read(name, *args):
        if cancelled():
            raise InterruptedError('Reading selection was cancelled.')
        return getattr(control, name)(*args)

    if mode == "page":
        return 0, len(raw)
    if mode in ("cursor", "word", "sentence", "paragraph"):
        position = read('GetInsertionPoint')
        prefix = read('GetRange', 0, position)
        start, _ = native_text_range(raw, read('GetValue'), prefix, prefix)
        return ((start, len(raw)) if mode == 'cursor' else
                reading_unit_range(raw, start, mode, cancelled=cancelled))
    if mode == "selection":
        start, end = read('GetSelection')
        if start == end:
            raise ValueError("Select source text before choosing Read selection aloud.")
        first, last = native_text_range(raw, read('GetValue'), read('GetRange', 0, start), read('GetRange', 0, end))
        if first == last:
            raise ValueError("The selected range contains no complete source characters.")
        return first, last
    raise ValueError("Choose the current text, cursor, selection, word, sentence or paragraph.")


def native_cursor_position(control, raw, offset):
    """Restore a Python source offset through observed native prefixes."""
    if type(offset) is not int or not 0 <= offset <= len(raw):
        raise ValueError('The saved cursor is outside the reading text.')
    displayed = control.GetValue()
    last = control.GetLastPosition()

    def mapped(position):
        try:
            prefix = control.GetRange(0, position)
            # Normalization maps both the prefix ending in CR and the complete
            # CRLF to one source newline. Never restore inside the native pair:
            # a following exact-text selection would otherwise include its LF.
            if prefix.endswith('\r') and position < last:
                try:
                    following = control.GetRange(position, position + 1)
                except UnicodeError:
                    following = ''
                if following == '\n':
                    position += 1
                    prefix += following
            return native_text_range(raw, displayed, prefix, prefix)[0], position
        except (UnicodeError, ValueError):
            # A native UTF-16 position can split a surrogate pair. Probe its
            # complete boundary for comparison, then verify the final result.
            if position >= last:
                raise
            prefix = control.GetRange(0, position + 1)
            return native_text_range(raw, displayed, prefix, prefix)[0], position + 1

    low, high = 0, last
    while low < high:
        middle = (low + high) // 2
        actual, _position = mapped(middle)
        if actual < offset:
            low = middle + 1
        else:
            high = middle
    actual, position = mapped(low)
    if actual != offset:
        raise ValueError('The saved cursor is not a complete native text boundary.')
    return position


def _top_host(window):
    current = window
    for _ in range(32):
        if getattr(current, 'live_media_feedback', None) is not None:
            return current
        parent = current.GetParent() if hasattr(current, "GetParent") else None
        if parent is None:
            break
        current = parent
    return current


class _MediaActivity:
    """A plain activity flag that can outlive destroyed native controls."""
    def __init__(self, previous, core):
        self.previous, self.core = previous, core

    def __call__(self):
        previous = self.previous() if callable(self.previous) else self.previous
        core = self.core
        return bool(previous or (core is not None and
            (not getattr(core, 'quiescent', True) or core.busy and core.status().state != "discovering")))

    @property
    def silence_uncertain(self):
        return bool(self.core is not None and getattr(self.core, 'silence_uncertain', False))

    def finished(self, future=None):
        self.core = None


class LiveReadingControls:
    def __init__(self, dialog, layout, text, source, *, current, show_section,
                 ready=lambda: True, command_buttons=None, host=None, core_factory=LiveReader,
                 on_stop=lambda: None):
        import wx
        self.wx, self.dialog, self.text = wx, dialog, text
        self.current, self.show_section, self.ready = current, show_section, ready
        self.source = source
        self.on_stop = on_stop
        self.core = core_factory(source, require_display_ack=True)
        self.closed = False
        self._start_generation = 0
        self._sequence = -1
        self._voices = ()
        self._section = None
        self._range_listeners = set()
        self.command_buttons = command_buttons if command_buttons is not None else {}
        self._host = _top_host(host or dialog)
        self._previous_media = getattr(self._host, "progress_media_busy", False)

        self._media_indicator = _MediaActivity(self._previous_media, self.core)
        self._bridge = getattr(self._host, 'live_media_feedback', None)
        self._release_media = (self._bridge.hold(self._media_indicator)
                               if self._bridge is not None and hasattr(self._bridge, 'hold') else None)
        if self._release_media is None:
            self._host.progress_media_busy = self._media_indicator
        self._feedback_wait_since = None

        voices = wx.WrapSizer(wx.HORIZONTAL)
        voices.Add(wx.StaticText(dialog, label="Local reading voice"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.voice = wx.Choice(dialog, choices=[])
        self.voice.SetName("Local reading voice; choose explicitly")
        voices.Add(self.voice, 1, wx.EXPAND | wx.ALL, 4)
        self.discover_button = self._button(voices, "Discover local voices", self.discover, "reader.speech_voices")
        self.output_button = self._button(voices, "Playback output...", self.choose_output, "reader.output_device")
        self._button(voices, "Independent playback settings...", self.playback_settings, "reader.playback_settings")
        self._button(voices, "Tray and session hotkeys...", self.session_controls, "reader.session_controls")
        self._button(voices, "Assistant for this source...", self.assistant_tools, "reader.assistant_tools")
        voices.Add(wx.StaticText(dialog, label="Synthesis rate (-10 to 10)"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.rate = wx.SpinCtrl(dialog, min=-10, max=10, initial=0)
        self.rate.SetName("Speech rate for the next reading operation")
        voices.Add(self.rate, 0, wx.ALL, 4)
        voices.Add(wx.StaticText(dialog, label="Voice volume"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.volume = wx.SpinCtrl(dialog, min=0, max=100, initial=100)
        self.volume.SetName("Voice volume for the next reading operation")
        voices.Add(self.volume, 0, wx.ALL, 4)
        layout.Add(voices, 0, wx.EXPAND | wx.ALL, 3)
        self.voice.Bind(wx.EVT_CHOICE, self.capabilities)

        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.read_buttons = [
            self._button(buttons, "Read current text aloud", lambda event: self.start("page"), "reader.speech_all"),
            self._button(buttons, "Read aloud from cursor", lambda event: self.start("cursor"), "reader.speech_cursor"),
            self._button(buttons, "Read selection aloud", lambda event: self.start("selection"), "reader.speech_selection"),
            self._button(buttons, "Read current word aloud", lambda event: self.start("word"), "reader.speech_word"),
            self._button(buttons, "Read current sentence aloud", lambda event: self.start("sentence"), "reader.speech_sentence"),
            self._button(buttons, "Read current paragraph aloud", lambda event: self.start("paragraph"), "reader.speech_paragraph"),
        ]
        for button in self.read_buttons[-3:]:
            button.SetToolTip('Use the text at the insertion cursor. Words follow letter/number boundaries, '
                             'sentences follow punctuation, and paragraphs are separated by blank lines. '
                             'At a separator, read the next unit; at the end, read the last unit.')
        self.pause_button = self._button(buttons, "Pause or resume voice", self.pause, "reader.speech_pause")
        self.stop_button = self._button(buttons, "Stop voice reading", self.stop, "reader.speech_stop")
        self.seek_button = self._button(buttons, "Seek within current speech chunk", self.seek, "reader.speech_seek")
        self._button(buttons, 'A–B text repeat and sleep...', self.repeat_and_sleep, 'reader.repeat_sleep')
        self.resume_button = self._button(buttons, "Resume saved voice position", self.resume, "reader.speech_resume")
        self._button(buttons, "Voice reading status", self.focus_status, "reader.speech_status")
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 3)
        self.continuous = wx.CheckBox(dialog, label="Continue automatically to the next source location")
        self.continuous.SetName("Continue automatically to the next source location; off by default")
        self.continuous.SetValue(False)
        self.continuous.Enable(source.supports_resume)
        layout.Add(self.continuous, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName("Live voice reading status, exact chunk range and audio position")
        self.status.SetMinSize((-1, 75))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 6)
        self.status.ChangeValue(self.core.status().message)
        self.pause_button.Disable()
        self.stop_button.Disable()
        self.resume_button.Enable(source.supports_resume)
        self.timer = wx.Timer(dialog)
        dialog.Bind(wx.EVT_TIMER, self.tick, self.timer)
        self.timer.Start(150)

    def _button(self, sizer, label, action, command):
        button = self.wx.Button(self.dialog, label=label)
        button.SetName(label)
        button.Bind(self.wx.EVT_BUTTON, action)
        sizer.Add(button, 0, self.wx.ALL, 3)
        self.command_buttons[command] = button, action
        return button

    def _report(self, error):
        if not self.closed:
            self.status.ChangeValue(str(error))

    def discover(self, event=None):
        if self.closed:
            return
        try:
            self.core.discover_voices()
        except Exception as exc:
            self._report(exc)

    def session_controls(self, event=None):
        if not self.closed:
            from .reader_background import show_session_controls
            show_session_controls(self, event)

    def assistant_tools(self, event=None):
        if self.closed:
            return
        try:
            from .assistant_dialog import show_assistant
            show_assistant(self._host, reader_controls=self)
        except Exception as error:
            self._report(error)

    def choose_output(self, event=None):
        if self.closed:
            return
        core = self.core
        generation = getattr(self, '_start_generation', 0)

        def guard():
            if (self.closed or self.core is not core or not self.dialog
                    or generation != getattr(self, '_start_generation', 0)
                    or core.busy or not self.ready()):
                raise ValueError('The reader changed or is active. Stop reading before choosing its output.')

        try:
            from .output_devices import choose_output_device
            guard()
            choice = choose_output_device(self.dialog, current=core.output_device, guard=guard)
            if choice is None:
                return
            guard()
            core.set_output_device(choice.device)
            label = choice.device.name if choice.device is not None else 'the Windows system default'
            self._report(f'Playback output selected: {label}. Start reading when ready.')
        except Exception as exc:
            self._report(exc)

    def _voice_settings(self):
        index = self.voice.GetSelection()
        if type(index) is not int or not 0 <= index < len(self._voices):
            raise ValueError("Choose an installed local voice explicitly before reading aloud.")
        return self._voices[index].id, 2 ** (self.rate.GetValue() / 10), self.volume.GetValue() / 100

    def capabilities(self, event=None):
        if self.closed:
            return
        index = self.voice.GetSelection()
        voice = self._voices[index] if 0 <= index < len(self._voices) else None
        enabled = voice is not None and not self.core.busy
        rate = voice is not None and 'rate' in voice.capabilities
        volume = voice is not None and 'volume' in voice.capabilities
        self.rate.Enable(enabled and rate)
        self.volume.Enable(enabled and volume)
        if voice is not None and not rate:
            self.rate.SetValue(0)
        if voice is not None and not volume:
            self.volume.SetValue(100)

    def _quiet_begin(self):
        self._feedback_wait_since = time.monotonic()
        bridge = self._bridge
        if bridge is not None and bridge.quiet_during_media:
            bridge.suspend()

    def _quiet_ready(self):
        bridge = self._bridge
        if bridge is None or bridge.quiescent:
            return True
        if self._feedback_wait_since is not None and time.monotonic() - self._feedback_wait_since >= 5:
            self.core.stop("Playback did not start because audio silence is not confirmed. Check the playback device before trying again.")
        return False

    def start(self, mode):
        if self.closed:
            return
        generation = getattr(self, '_start_generation', 0)
        stopping = lambda: self.closed or getattr(self, '_start_generation', 0) != generation
        try:
            if not self.ready() or self.core.busy:
                raise ValueError("Wait for the current source operation before starting voice reading.")
            if stopping():
                return
            ordinal, raw = self.current()
            if stopping():
                return
            start, end = reading_range(self.text, raw, mode, cancelled=stopping)
            if stopping():
                return
            if not raw[start:end].strip():
                raise ValueError("The chosen reading range has no spoken text.")
            voice, rate, volume = self._voice_settings()
            if stopping():
                return
            continuous = self.continuous.GetValue() if mode in ('page', 'cursor') else False
            self._quiet_begin()
            if stopping():
                return
            self.core.start(ordinal, voice, start=start, end=end, rate=rate, volume=volume,
                            continuous=continuous)
        except Exception as exc:
            self._report(exc)

    def resume(self, event=None):
        if self.closed:
            return
        try:
            if not self.ready() or self.core.busy:
                raise ValueError("Wait for the current source operation before resuming voice reading.")
            ordinal, _raw = self.current()
            voice, rate, volume = self._voice_settings()
            self._quiet_begin()
            self.core.start(ordinal, voice, rate=rate, volume=volume, resume=True,
                            continuous=self.continuous.GetValue())
        except Exception as exc:
            self._report(exc)

    def pause(self, event=None):
        if self.closed:
            return
        try:
            self.core.toggle_pause()
        except Exception as exc:
            self._report(exc)

    def stop(self, event=None):
        if not self.closed:
            self._start_generation = getattr(self, '_start_generation', 0) + 1
            self.core.stop()
            self.on_stop()

    def seek(self, event=None):
        if self.closed:
            return
        try:
            view = self.core.status()
            if view.state not in ('playing', 'paused') or not view.duration_seconds:
                raise ValueError('Read text before seeking within its current audio chunk.')
            prompt = self.wx.TextEntryDialog(self.dialog,
                f'Seconds from 0 to {view.duration_seconds:.2f} within this speech chunk. '
                'This does not identify a source word.', 'Seek current speech chunk',
                str(round(view.audio_seconds, 2)))
            try:
                answer = prompt.ShowModal()
                if self.closed or not self.dialog or not prompt or answer != self.wx.ID_OK:
                    return
                seconds = float(prompt.GetValue())
            finally:
                if prompt:
                    prompt.Destroy()
            if not self.closed and self.dialog:
                self.core.seek(seconds, expected=view)
        except Exception as exc:
            self._report(exc)

    def repeat_and_sleep(self, event=None):
        """Configure exact source ranges without borrowing a disposable WAV."""
        if self.closed:
            return
        from .live_range_controls import show_live_range_controls
        core = self.core
        def guard():
            if self.closed or not self.dialog or self.core is not core:
                raise ValueError('This live reading owner is no longer available.')
            self.source.guard()
        try:
            guard()
            if not self.ready():
                raise ValueError('Wait for the current source operation before configuring repeat.')
            view = core.status()
            if core.busy:
                if view.section is None or view.state in ('discovering', 'stopping'):
                    raise ValueError('Wait until the current source range is checked.')
                ordinal, raw = view.section.ordinal, view.section.text
            else:
                ordinal, raw = self.current()
            voice_settings = self._voice_settings()
            native_first, native_last = self.text.GetSelection()
            initial = (reading_range(self.text, raw, 'selection')
                       if native_first != native_last else (0, len(raw)))
            def current():
                guard()
                if not self.ready() or core.busy:
                    raise ValueError('Stop reading and wait for its owned cleanup before changing the A–B range.')
                current_ordinal, current_text = self.current()
                if ((current_ordinal, current_text) != (ordinal, raw)
                        or self._voice_settings() != voice_settings):
                    raise ValueError('The source, reading copy or selected voice changed. Reopen A–B controls.')
                return current_text
            def start_repeat(first, last):
                generation = getattr(self, '_start_generation', 0)
                current_text = current()
                if (type(first) is not int or type(last) is not int
                        or not 0 <= first < last <= len(current_text) or not current_text[first:last].strip()):
                    raise ValueError('Choose a nonempty spoken A–B range in this exact source.')
                voice, rate, volume = voice_settings
                if getattr(self, '_start_generation', 0) != generation:
                    raise InterruptedError('The pending A–B start was cancelled.')
                self._quiet_begin()
                guard()
                if getattr(self, '_start_generation', 0) != generation:
                    raise InterruptedError('The pending A–B start was cancelled.')
                core.start(ordinal, voice, start=first, end=last, rate=rate,
                           volume=volume, continuous=False, repeat=True)
            def selected_range():
                return reading_range(self.text, current(), 'selection')
            def subscribe(callback):
                self._range_listeners.add(callback)
                return lambda: self._range_listeners.discard(callback)
            show_live_range_controls(self.dialog, length=len(raw), initial=initial,
                start_repeat=start_repeat, selected_range=selected_range,
                set_sleep=core.set_sleep, cancel_sleep=core.cancel_sleep,
                stop=self.stop, status=core.navigation_status, subscribe=subscribe,
                guard=guard, alive=lambda: not self.closed and self.core is core,
                wx_module=self.wx)
        except Exception as exc:
            self._report(exc)

    def playback_settings(self, event=None):
        if self.closed:
            return
        from .playback_settings import show_playback_settings
        import os
        core = self.core
        def guard():
            if self.closed or not self.dialog or core is not self.core:
                raise ValueError('This reading owner is no longer available.')
            self.source.guard()
        def prepare(selected, completed, failed, *, active):
            guard()
            core.set_listening_settings(selected.speed, selected.pitch_semitones)
            if active():
                completed('Speed and pitch selected for the next explicit reading operation. '
                          'The current source and synthesis settings are unchanged.')
            return None
        def levels(selected):
            guard()
            core.set_playback_levels(selected.volume, muted=selected.muted, balance=selected.balance)
        try:
            guard()
            show_playback_settings(self.dialog, settings=core.listening_settings,
                levels=core.playback_levels, prepare=prepare, apply_levels=levels,
                mix_available=os.name == 'nt', alive=lambda: not self.closed and core is self.core,
                stop=self.stop, wx_module=self.wx,
                preparation_label='Use speed and pitch for next reading',
                notice='Stop reading before changing speed or pitch. '
                       'Volume, mute and balance are handled by the current reading worker. '
                       'Saved source positions stay on the original speech timeline.')
        except (Exception, KeyboardInterrupt) as exc:
            if not self.closed:
                self._report(exc)

    def focus_status(self, event=None):
        if not self.closed:
            self.status.SetFocus()

    def tick(self, event=None):
        if self.closed:
            return
        view = self.core.status()
        busy = self.core.busy
        for callback in tuple(getattr(self, '_range_listeners', ())):
            try:
                callback()
            except Exception as exc:
                self._report(exc)
        available = self.ready() and not busy
        for button in self.read_buttons:
            button.Enable(available)
        self.discover_button.Enable(not busy)
        self.output_button.Enable(available)
        self.resume_button.Enable(available and self.source.supports_resume)
        self.pause_button.Enable(busy and view.state in ("loading", "rendering", "preparing_playback", "playing", "paused"))
        self.stop_button.Enable(busy or not self.ready())
        self.seek_button.Enable(busy and view.state in ('playing', 'paused'))
        self.capabilities()
        if view.sequence == self._sequence:
            if (view.section is not None and view.section is self._section
                    and view.state not in ("stopping", "stopped", "error", "closed")
                    and self._quiet_ready()):
                self.core.acknowledge(view.section)
            return
        self._sequence = view.sequence
        try:
            if view.voices != self._voices:
                index = self.voice.GetSelection()
                selected = self._voices[index].id if 0 <= index < len(self._voices) else None
                self._voices = view.voices
                self.voice.Set([f"{item.name}; {item.language or 'language not declared'}; {item.engine}"
                                for item in self._voices])
                chosen = next((i for i, item in enumerate(self._voices) if item.id == selected), self.wx.NOT_FOUND)
                self.voice.SetSelection(chosen)
                self.capabilities()
            if view.section is not None and view.state not in ("stopping", "stopped", "error", "closed"):
                if view.section is not self._section:
                    self.show_section(view.section)
                    self._section = view.section
                if self._quiet_ready():
                    self.core.acknowledge(view.section)
            text = view.message
            if view.section is not None:
                text += (f"\n{'Reading-copy' if view.section.derivative else 'Source'} chunk characters "
                         f"{view.chunk_start} to {view.chunk_end} of {len(view.section.text)}; "
                         f"reading location {view.section.ordinal} of {view.section.total}.")
            if view.duration_seconds:
                label = 'Estimated audio' if view.position_is_approximate else 'Audio'
                text += f" {label} {view.audio_seconds:.1f} of {view.duration_seconds:.1f} seconds."
            if view.warning:
                text += "\n" + view.warning
            self.status.ChangeValue(text)
        except Exception as exc:
            self.core.stop(str(exc))
            self._report(exc)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for callback in tuple(getattr(self, '_range_listeners', ())):
            self._range_listeners.discard(callback)
        from .reader_background import close_session_controls
        close_session_controls(self)
        try:
            self.timer.Stop()
        except (OSError, RuntimeError):
            # A detached native timer does not prevent worker cancellation.
            pass
        completion = self.core.close()
        # The last worker owns the player and its WAV until cleanup completes.
        # Its callback touches only a plain flag, never a native window.
        activity, release = self._media_indicator, self._release_media
        def finished(future=None):
            if activity.core is not None and not getattr(activity.core, 'quiescent', True):
                return  # Uncertain native sound retains its plain mute lease.
            activity.finished()
            if release is not None:
                release()
        if completion is not None and not completion.done():
            completion.add_done_callback(finished)
        else:
            finished()
        if (completion is None or completion.done()) and getattr(self._host, "progress_media_busy", None) is self._media_indicator:
            self._host.progress_media_busy = self._previous_media


def attach_source_controls(reader, layout, *, core_factory=LiveReader):
    source = SourceSequence(reader.controller.desk, reader.controller.guard)

    def current():
        page = reader.controller.current
        if page is None:
            raise ValueError("Open a source location before reading aloud.")
        reader.cancelled.clear()
        return page.ordinal, page.raw_text

    def show(section):
        if reader.closed:
            raise InterruptedError("The reading window is closed.")
        if reader.controller.current is None or reader.controller.current.location != section.location:
            from .reading_desk import _Navigation
            reader._page(_Navigation(section.page, None), focus=False, from_live=True)

    return LiveReadingControls(reader.dialog, layout, reader.text, source, current=current,
                               show_section=show, ready=lambda: not reader.closed and not reader.busy,
                               command_buttons=reader.command_buttons, host=reader.parent,
                               core_factory=core_factory)


def bind_live_shortcuts(dialog, controls, accelerators=()):
    """Extend the canonical modal shortcuts without replacing navigation keys."""
    wx = controls.wx
    mapping = (
        ('reader.speech_all', 0, wx.WXK_F6),
        ('reader.speech_cursor', 0, wx.WXK_F9),
        ('reader.speech_selection', wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_F6),
        ('reader.speech_word', wx.ACCEL_CTRL, wx.WXK_F9),
        ('reader.speech_sentence', wx.ACCEL_SHIFT, wx.WXK_F9),
        ('reader.speech_paragraph', wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_F9),
        ('reader.speech_pause', 0, wx.WXK_F7),
        ('reader.speech_stop', 0, wx.WXK_F8),
        ('reader.speech_resume', wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_F8),
        ('reader.speech_status', wx.ACCEL_CTRL | wx.ACCEL_ALT, wx.WXK_F8),
    )
    entries, identities = list(accelerators), []
    for command, flags, key in mapping:
        button, action = controls.command_buttons[command]
        identity = wx.NewIdRef()
        identities.append(identity)
        def invoke(event, target=button, handler=action):
            if not controls.closed and target.IsEnabled():
                handler(event)
        dialog.Bind(wx.EVT_MENU, invoke, id=int(identity))
        entries.append((flags, key, int(identity)))
    controls._shortcut_ids = identities
    dialog.SetAcceleratorTable(wx.AcceleratorTable(entries))
