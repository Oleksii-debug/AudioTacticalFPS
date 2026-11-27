"""Native live-reading controls; background polling never changes keyboard focus."""
from __future__ import annotations

import time

from ..reader.live_reading import CopySequence, LiveReader, SourceSequence, native_text_range


def reading_range(control, raw, mode):
    if mode == "page":
        return 0, len(raw)
    if mode == "cursor":
        position = control.GetInsertionPoint()
        prefix = control.GetRange(0, position)
        start, _ = native_text_range(raw, control.GetValue(), prefix, prefix)
        return start, len(raw)
    if mode == "selection":
        start, end = control.GetSelection()
        if start == end:
            raise ValueError("Select source text before choosing Read selection aloud.")
        first, last = native_text_range(raw, control.GetValue(), control.GetRange(0, start), control.GetRange(0, end))
        if first == last:
            raise ValueError("The selected range contains no complete source characters.")
        return first, last
    raise ValueError("Choose the current source, cursor or selected text.")


def _top_host(window):
    current = window
    for _ in range(32):
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
        return bool(previous or (core is not None and core.busy
                                 and core.status().state != "discovering"))

    def finished(self, future=None):
        self.core = None


class LiveReadingControls:
    def __init__(self, dialog, layout, text, source, *, current, show_section,
                 ready=lambda: True, command_buttons=None, host=None, core_factory=LiveReader):
        import wx
        self.wx, self.dialog, self.text = wx, dialog, text
        self.current, self.show_section, self.ready = current, show_section, ready
        self.source = source
        self.core = core_factory(source, require_display_ack=True)
        self.closed = False
        self._sequence = -1
        self._voices = ()
        self._section = None
        self.command_buttons = command_buttons if command_buttons is not None else {}
        self._host = host or _top_host(dialog)
        self._previous_media = getattr(self._host, "progress_media_busy", False)

        self._media_indicator = _MediaActivity(self._previous_media, self.core)
        self._host.progress_media_busy = self._media_indicator
        self._feedback_wait_since = None

        voices = wx.WrapSizer(wx.HORIZONTAL)
        voices.Add(wx.StaticText(dialog, label="Local reading voice"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.voice = wx.Choice(dialog, choices=[])
        self.voice.SetName("Local reading voice; choose explicitly")
        voices.Add(self.voice, 1, wx.EXPAND | wx.ALL, 4)
        self.discover_button = self._button(voices, "Discover local voices", self.discover, "reader.speech_voices")
        voices.Add(wx.StaticText(dialog, label="Speech rate"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.rate = wx.Choice(dialog, choices=["0.5", "0.75", "1.0", "1.25", "1.5", "2.0"])
        self.rate.SetName("Speech rate for the next reading operation")
        self.rate.SetSelection(2)
        voices.Add(self.rate, 0, wx.ALL, 4)
        voices.Add(wx.StaticText(dialog, label="Voice volume"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.volume = wx.Choice(dialog, choices=["25%", "50%", "75%", "100%"])
        self.volume.SetName("Voice volume for the next reading operation")
        self.volume.SetSelection(3)
        voices.Add(self.volume, 0, wx.ALL, 4)
        layout.Add(voices, 0, wx.EXPAND | wx.ALL, 3)

        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.read_buttons = [
            self._button(buttons, "Read current text aloud", lambda event: self.start("page"), "reader.speech_all"),
            self._button(buttons, "Read aloud from cursor", lambda event: self.start("cursor"), "reader.speech_cursor"),
            self._button(buttons, "Read selection aloud", lambda event: self.start("selection"), "reader.speech_selection"),
        ]
        self.pause_button = self._button(buttons, "Pause or resume voice", self.pause, "reader.speech_pause")
        self.stop_button = self._button(buttons, "Stop voice reading", self.stop, "reader.speech_stop")
        self.resume_button = self._button(buttons, "Resume saved voice position", self.resume, "reader.speech_resume")
        self._button(buttons, "Voice reading status", self.focus_status, "reader.speech_status")
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 3)
        self.continuous = wx.CheckBox(dialog, label="Continue automatically to the next source location")
        self.continuous.SetName("Continue automatically to the next source location; off by default")
        self.continuous.SetValue(False)
        self.continuous.Enable(source.supports_resume)
        layout.Add(self.continuous, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName("Live voice reading status, exact chunk range and measured audio position")
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

    def _voice_settings(self):
        index = self.voice.GetSelection()
        if type(index) is not int or not 0 <= index < len(self._voices):
            raise ValueError("Choose an installed local voice explicitly before reading aloud.")
        return self._voices[index].id, float(self.rate.GetStringSelection()), float(self.volume.GetStringSelection().rstrip("%")) / 100

    def _quiet_begin(self):
        self._feedback_wait_since = time.monotonic()
        panel = getattr(self._host, "progress_panel", None)
        if panel is not None and panel.preferences.quiet_during_media:
            panel.feedback.suspend()

    def _quiet_ready(self):
        panel = getattr(self._host, "progress_panel", None)
        if (panel is None or not panel.preferences.quiet_during_media
                or panel.feedback.quiescent):
            return True
        if self._feedback_wait_since is not None and time.monotonic() - self._feedback_wait_since >= 5:
            self.core.stop("Playback did not start because a progress notification has not stopped. Try again.")
        return False

    def start(self, mode):
        if self.closed:
            return
        try:
            if not self.ready():
                raise ValueError("Wait for the current source operation before starting voice reading.")
            ordinal, raw = self.current()
            start, end = reading_range(self.text, raw, mode)
            if not raw[start:end].strip():
                raise ValueError("The chosen reading range has no spoken text.")
            voice, rate, volume = self._voice_settings()
            self._quiet_begin()
            self.core.start(ordinal, voice, start=start, end=end, rate=rate, volume=volume,
                            continuous=self.continuous.GetValue() if mode != "selection" else False)
        except Exception as exc:
            self._report(exc)

    def resume(self, event=None):
        if self.closed:
            return
        try:
            if not self.ready():
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
            self.core.stop()

    def focus_status(self, event=None):
        if not self.closed:
            self.status.SetFocus()

    def tick(self, event=None):
        if self.closed:
            return
        view = self.core.status()
        busy = self.core.busy
        available = self.ready() and not busy
        for button in self.read_buttons:
            button.Enable(available)
        self.discover_button.Enable(not busy)
        self.resume_button.Enable(available and self.source.supports_resume)
        self.pause_button.Enable(busy and view.state in ("loading", "rendering", "playing", "paused"))
        self.stop_button.Enable(busy)
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
                text += f" Audio {view.audio_seconds:.1f} of {view.duration_seconds:.1f} seconds."
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
        self.timer.Stop()
        completion = self.core.close()
        # The last worker owns the player and its WAV until cleanup completes.
        # Its callback touches only a plain flag, never a native window.
        if completion is not None and not completion.done():
            completion.add_done_callback(self._media_indicator.finished)
        else:
            self._media_indicator.finished()
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


def open_live_copy(parent, root, project, *, guard, validate_source):
    """Read the frozen derivative using the same controls; never save source offsets."""
    import wx
    from .reading_shortcuts import ReaderKeyboardAdapter
    source = CopySequence(root, project, guard=guard, validate_source=validate_source)

    class CopyDialog:
        def __init__(self):
            self.closed = self.busy = False
            self.private_root = root
            self.command_buttons = {}
            self.dialog = wx.Dialog(parent, title="Read reading copy aloud — " + project.title,
                                    size=(960, 760), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            layout = wx.BoxSizer(wx.VERTICAL)
            provenance = wx.TextCtrl(self.dialog, value=project.provenance,
                                     style=wx.TE_READONLY | wx.TE_MULTILINE)
            provenance.SetName("Reading-copy source status; edited text is not quotation evidence")
            layout.Add(provenance, 0, wx.EXPAND | wx.ALL, 6)
            text = wx.TextCtrl(self.dialog, value=project.current_text, style=wx.TE_READONLY | wx.TE_MULTILINE)
            text.SetName("Frozen reading-copy text for voice reading")
            layout.Add(text, 1, wx.EXPAND | wx.ALL, 6)
            self.controls = LiveReadingControls(self.dialog, layout, text, source,
                current=lambda: (1, project.current_text), show_section=lambda section: guard(),
                command_buttons=self.command_buttons, host=_top_host(parent))
            close = wx.Button(self.dialog, wx.ID_CANCEL, "Close voice reader")
            close.Bind(wx.EVT_BUTTON, self.close)
            layout.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
            self.dialog.SetSizer(layout)
            self.dialog.SetEscapeId(wx.ID_CANCEL)
            self.dialog.Bind(wx.EVT_CLOSE, self.close)
            self.keyboard = ReaderKeyboardAdapter(self, getattr(_top_host(parent), "keyboard_commands", None))

        def close(self, event=None):
            if not self.closed:
                self.closed = True
                self.controls.close()
                self.keyboard.close()
                if self.dialog.IsModal():
                    self.dialog.EndModal(wx.ID_CANCEL)

    dialog = CopyDialog()
    try:
        dialog.controls.discover()
        dialog.dialog.ShowModal()
    finally:
        dialog.close()
        dialog.dialog.Destroy()
