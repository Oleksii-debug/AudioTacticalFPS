"""Explicit reader synthesis options; no playback processing or engine discovery."""
from ..speech.runtime import SpeechSettings


def _options(pitch, punctuation, pauses):
    if not isinstance(pauses, tuple) or len(pauses) != 3:
        raise ValueError('Three validated extra reading pauses are required.')
    return SpeechSettings(pitch=pitch, punctuation=punctuation,
                          sentence_pause_ms=pauses[0], paragraph_pause_ms=pauses[1],
                          section_pause_ms=pauses[2])


def _checked(voice, pitch, punctuation, *, pauses=(0, 0, 0)):
    settings = _options(pitch, punctuation, pauses)
    capabilities = voice.capabilities if voice is not None else ()
    if settings.pitch is not None and 'pitch' not in capabilities:
        raise ValueError('The selected voice does not support the requested native pitch. '
                         'Choose a compatible voice or reset native voice options.')
    if settings.punctuation and 'punctuation' not in capabilities:
        raise ValueError('The selected voice does not support punctuation reading. '
                         'Choose a compatible voice or reset native voice options.')
    return settings


class ReaderSynthesisControls:
    """Passive reader state and a modal editor, borrowing the existing reader."""
    def __init__(self, parent, *, pitch=None, punctuation=False, pauses=(0, 0, 0), alive, busy,
                 guard, before_apply, changed, report, wx):
        self.parent, self.wx = parent, wx
        self.alive, self.busy, self.guard = alive, busy, guard
        self.before_apply, self.changed, self.report = before_apply, changed, report
        self.voice, self.generation, self.view = None, 0, None
        self.current, self.pauses = (None, False), (0, 0, 0)
        self.restore(pitch, punctuation, pauses=pauses)
        self.button = wx.Button(parent, label='Native voice options...')
        self.button.SetName('Native voice pitch and punctuation')
        self.button.SetToolTip('Native voice options and extra reading pauses for the next synthesis; '
                               'playback controls are separate.')
        self.button.Bind(wx.EVT_BUTTON, self.show)

    def restore(self, pitch, punctuation, *, pauses=None):
        # Passive recovery must preserve an unavailable request, never select a
        # substitute voice or turn it into a default synthesis without consent.
        selected = self.pauses if pauses is None else pauses
        _options(pitch, punctuation, selected)
        value = (pitch, punctuation)
        if value != self.current or selected != self.pauses:
            self.current, self.pauses = value, selected
            self.generation += 1

    def snapshot(self):
        return self.current

    def pause_snapshot(self):
        return self.pauses

    def settings(self, rate, volume, voice):
        if voice is None:
            raise ValueError('Choose an installed local voice first.')
        _checked(voice, *self.current, pauses=self.pauses)
        return SpeechSettings(rate=rate, volume=volume,
                              pitch=self.current[0], punctuation=self.current[1],
                              sentence_pause_ms=self.pauses[0], paragraph_pause_ms=self.pauses[1],
                              section_pause_ms=self.pauses[2])

    def explanation(self, voice):
        try:
            _checked(voice, *self.current, pauses=self.pauses)
        except ValueError as error:
            return str(error)
        pitch, punctuation = self.current
        value = ('Native pitch: ' + ('engine default' if pitch is None else str(pitch))
                 + '; punctuation reading: ' + ('on' if punctuation else 'off') + '.')
        if any(self.pauses):
            value += (' Extra sentence / paragraph / section pauses in milliseconds: '
                      + ' / '.join(str(item) for item in self.pauses) + '.')
        return value

    def sync(self, voice, *, enabled):
        if not self.alive():
            return False
        if voice is not self.voice:
            self.voice = voice
            self.generation += 1
        self.button.Enable(bool(enabled))
        self.button.SetToolTip(self.explanation(voice))
        try:
            _checked(voice, *self.current, pauses=self.pauses)
            return voice is not None
        except ValueError:
            return False

    def _ready(self):
        if not self.alive() or self.busy():
            raise ValueError('Finish the current reader operation before changing native voice options.')
        self.guard()

    def show(self, event=None):
        try:
            self._ready()
            generation, voice, original = self.generation, self.voice, self.current
            original_pauses = self.pauses

            def authorize():
                self._ready()
                if (self.generation != generation or self.voice is not voice
                        or self.current != original or self.pauses != original_pauses):
                    raise ValueError('The reader voice or options changed. Open native voice options again.')

            view = _NativeOptionsDialog(self.parent, voice, original, pauses=original_pauses,
                                        authorize=authorize, wx=self.wx)
            self.view = view
            result = view.show()
            if result is None:
                return False
            authorize()
            _checked(voice, *result, pauses=view.pause_result)
            if result != original or view.pause_result != original_pauses:
                # A failed native Stop must leave the prior request and its
                # resource ownership intact. The reader invalidates audio first.
                self.before_apply()
                authorize()
                self.restore(*result, pauses=view.pause_result)
                self.changed()
            return True
        except Exception as error:
            if self.alive():
                self.report(str(error), focus=True)
            return False


class _NativeOptionsDialog:
    def __init__(self, parent, voice, options, *, pauses=(0, 0, 0), authorize, wx):
        self.wx, self.voice, self.authorize = wx, voice, authorize
        self.closed, self.destroyed, self.result = False, False, None
        self.pause_result = pauses
        self.window = wx.Dialog(parent, title='Native voice options',
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.owner_id = self.window.GetId()
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self.window, label=(
            'These options change the next speech synthesis. They do not change playback pitch.')),
            0, wx.ALL, 8)
        self.pitch_enabled = wx.CheckBox(self.window, label='Use native voice pitch')
        self.pitch_enabled.SetName('Use native voice pitch')
        self.pitch_enabled.SetValue(options[0] is not None)
        layout.Add(self.pitch_enabled, 0, wx.ALL, 8)
        self.pitch = wx.SpinCtrl(self.window, min=0, max=99,
                                 initial=50 if options[0] is None else options[0])
        self.pitch.SetName('Native voice pitch, zero to ninety-nine')
        layout.Add(self.pitch, 0, wx.EXPAND | wx.ALL, 8)
        self.punctuation = wx.CheckBox(self.window, label='Read punctuation aloud')
        self.punctuation.SetName('Read punctuation aloud')
        self.punctuation.SetValue(options[1])
        layout.Add(self.punctuation, 0, wx.ALL, 8)
        layout.Add(wx.StaticText(self.window, label=(
            'Extra pauses: 1000 milliseconds = 1 second. Zero keeps the engine pause.\n'
            'Sentences follow punctuation/newlines; paragraphs follow blank lines.\n'
            'Sections require a form-feed marker. The most specific boundary wins.')),
            0, wx.ALL, 8)
        self.pause_fields = []
        for name, value in zip(('sentence', 'paragraph', 'section'), pauses):
            label = 'Extra ' + name + ' pause (milliseconds, 0 to 30000)'
            layout.Add(wx.StaticText(self.window, label=label), 0, wx.ALL, 4)
            control = wx.SpinCtrl(self.window, min=0, max=30000, initial=value)
            control.SetName('Extra ' + name + ' pause in milliseconds')
            layout.Add(control, 0, wx.EXPAND | wx.ALL, 4)
            control.Bind(wx.EVT_SPINCTRL, self.refresh)
            self.pause_fields.append(control)
        self.status = wx.TextCtrl(self.window, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                  size=(500, 85))
        self.status.SetName('Native voice options status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.reset = wx.Button(self.window, label='Reset native voice options')
        self.reset.SetName('Reset native voice options')
        self.apply = wx.Button(self.window, label='Apply native voice options')
        self.apply.SetName('Apply native voice options')
        self.cancel = wx.Button(self.window, wx.ID_CANCEL, 'Cancel native voice options')
        self.cancel.SetName('Cancel native voice options')
        for button in (self.reset, self.apply, self.cancel):
            row.Add(button, 0, wx.ALL, 6)
        layout.Add(row, 0, wx.ALIGN_RIGHT)
        self.window.SetSizerAndFit(layout)
        self.window.SetEscapeId(wx.ID_CANCEL)
        self.pitch_enabled.Bind(wx.EVT_CHECKBOX, self.refresh)
        self.punctuation.Bind(wx.EVT_CHECKBOX, self.refresh)
        self.pitch.Bind(wx.EVT_SPINCTRL, self.refresh)
        self.reset.Bind(wx.EVT_BUTTON, self.reset_options)
        self.apply.Bind(wx.EVT_BUTTON, self.accept)
        self.cancel.Bind(wx.EVT_BUTTON, self.close)
        self.window.Bind(wx.EVT_CLOSE, self.close)
        self.window.Bind(wx.EVT_WINDOW_DESTROY, self.owner_destroyed)
        self.refresh()

    def _read(self):
        use_pitch = self.pitch_enabled.GetValue()
        if type(use_pitch) is not bool:
            raise ValueError('Native pitch selection must be a boolean.')
        return _checked(self.voice, self.pitch.GetValue() if use_pitch else None,
                        self.punctuation.GetValue(),
                        pauses=tuple(control.GetValue() for control in self.pause_fields))

    def refresh(self, event=None):
        if self.closed:
            return
        try:
            self.authorize()
            capabilities = self.voice.capabilities if self.voice is not None else ()
            self.pitch_enabled.Enable('pitch' in capabilities)
            self.pitch.Enable('pitch' in capabilities and self.pitch_enabled.GetValue())
            self.punctuation.Enable('punctuation' in capabilities)
            self._read()
            self.apply.Enable(True)
            available = ', '.join(name for name in ('pitch', 'punctuation') if name in capabilities)
            self.status.ChangeValue(
                ('Selected voice supports: ' + available + '. ' if available else
                 'The selected voice exposes neither native pitch nor punctuation reading. ')
                + 'Apply keeps rate and volume unchanged. Cancel leaves the reader unchanged.')
        except Exception as error:
            self.apply.Enable(False)
            self.status.ChangeValue(str(error))

    def reset_options(self, event=None):
        if self.closed:
            return
        try:
            self.authorize()
            self.pitch_enabled.SetValue(False)
            self.punctuation.SetValue(False)
            for control in self.pause_fields:
                control.SetValue(0)
            self.refresh()
        except Exception as error:
            self.status.ChangeValue(str(error))

    def accept(self, event=None):
        if self.closed:
            return
        try:
            self.authorize()
            value = self._read()
            self.result = (value.pitch, value.punctuation)
            self.pause_result = (value.sentence_pause_ms, value.paragraph_pause_ms, value.section_pause_ms)
            self.closed = True
            self.window.EndModal(self.wx.ID_OK)
        except Exception as error:
            self.status.ChangeValue(str(error))

    def close(self, event=None):
        if self.closed:
            return
        self.closed, self.result = True, None
        if self.window.IsModal():
            self.window.EndModal(self.wx.ID_CANCEL)
        else:
            self.window.Destroy()

    def owner_destroyed(self, event):
        if event.GetId() == self.owner_id:
            self.destroyed, self.closed, self.result = True, True, None
        event.Skip()

    def show(self):
        try:
            self.window.ShowModal()
            return self.result
        finally:
            self.closed = True
            if not self.destroyed:
                self.window.Destroy()
