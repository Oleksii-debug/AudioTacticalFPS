"""Parent-owned native progress; background updates never move keyboard focus.

The host connects real producers using start/update/finish and explicitly
reports playback/recording state. Installing this panel does not claim that
every producer or every audio owner is already connected.
"""
from dataclasses import replace
from pathlib import Path

from ..progress import (FeedbackPreferences, MODES, ProgressFeedback, ProgressHub,
                        ProgressPreferenceStore, TERMINAL)


class ProgressPanel:
    """Native controls are touched only by the UI timer and explicit UI actions."""
    def __init__(self, parent, *, preferences=None, current=lambda: True,
                 on_tick=None, feedback_factory=ProgressFeedback, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        self.wx = wx = wx_module
        self.alive = True
        self.current = current
        self.on_tick = on_tick
        self.feedback_factory = feedback_factory
        self.hub = ProgressHub()
        self.feedback = feedback_factory(preferences or FeedbackPreferences())
        self.window = wx.Panel(parent)
        self.window.SetName('Operation progress')
        layout = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self.window, label='Operation progress')
        layout.Add(label, 0, wx.ALL, 3)
        self.gauge = wx.Gauge(self.window, range=100)
        self.gauge.SetName('Current operation progress percentage')
        layout.Add(self.gauge, 0, wx.EXPAND | wx.ALL, 3)
        self.text = wx.TextCtrl(self.window, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.text.SetName('Current operations and progress details')
        self.text.SetMinSize((-1, 74))
        layout.Add(self.text, 0, wx.EXPAND | wx.ALL, 3)
        self.window.SetSizer(layout)
        self.notice = ''
        self._displayed = None
        self.timer = wx.Timer(self.window)
        self.window.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.window.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.timer.Start(150)
        self._render()

    def _bound(self):
        if not self.alive:
            return False
        try:
            return bool(self.current())
        except Exception:
            return False

    def start(self, task_id, label, phase='Starting', *, total=None):
        hub = self.hub
        return hub.start(task_id, label, phase, total=total) if self._bound() else None

    def update(self, handle, **values):
        hub = self.hub
        return hub.update(handle, **values) if self._bound() else None

    def pause(self, handle):
        hub = self.hub
        return hub.pause(handle) if self._bound() else None

    def resume(self, handle):
        hub = self.hub
        return hub.resume(handle) if self._bound() else None

    def finish(self, handle, *, state='completed', verified=False):
        hub = self.hub
        return hub.finish(handle, state=state, verified=verified) if self._bound() else None

    def set_media_state(self, *, playback=False, recording=False):
        feedback = self.feedback
        if self._bound():
            feedback.set_media_state(playback=playback, recording=recording)

    def set_background(self, background):
        if self.alive:
            self.feedback.set_background(background)

    def rebind(self, preferences, *, current, notice=''):
        """UI-thread reload: withdraw old handles, audio and queued completions."""
        if not self.alive:
            return
        self.hub.close()
        self.feedback.close()
        self.hub = ProgressHub()
        self.feedback = self.feedback_factory(preferences)
        self.current, self.notice = current, notice
        self._displayed = None
        self._render()

    def _tick(self, event=None):
        if not self.alive:
            return
        if self.on_tick is not None:
            self.on_tick()
        if not self._bound():
            return
        for item in self.hub.drain():
            self.feedback.submit(item)
        self._render()

    def _render(self):
        if not self.alive:
            return
        events = self.hub.snapshot()
        active = tuple(item for item in events if item.state not in TERMINAL)
        selected = active[-1] if active else (events[-1] if events else None)
        if selected is None:
            self.gauge.SetValue(0)
        elif selected.percent is not None:
            self.gauge.SetValue(selected.percent)
        elif selected.state in ('running', 'resumed'):
            self.gauge.Pulse()
        else:
            # A paused/failed/cancelled unknown task must not keep animating.
            self.gauge.SetValue(0)
        message = ('\n'.join(item.describe() for item in events) if events else 'No operations are active.')
        if len(active) > 1:
            message = f'{len(active)} active operations. The gauge shows {selected.label}.\n' + message
        notes = '\n'.join(note for note in (self.notice, self.feedback.notice) if note)
        if notes:
            message += '\n' + notes
        if message != self._displayed:
            self.text.ChangeValue(message)
            self._displayed = message

    def query_current(self, event=None):
        """Only this explicit user action requests focus or immediate feedback."""
        if not self._bound():
            return ''
        self._render()
        self.text.SetFocus()
        events = self.hub.snapshot()
        if events:
            self.feedback.submit(events[-1], manual=True)
        return self._displayed

    def dispose(self):
        if not self.alive:
            return
        self.alive = False
        self.hub.close()
        self.feedback.close()
        self.timer.Stop()

    def _destroyed(self, event):
        if event.GetEventObject() is self.window:
            self.dispose()
        event.Skip()


class FeedbackDialog:
    """Native preference controls; no global shortcut or system audio changes."""
    def __init__(self, parent, preferences, *, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        self.wx = wx = wx_module
        self.window = wx.Dialog(parent, title='Operation progress feedback')
        layout = wx.BoxSizer(wx.VERTICAL)
        self.mode = self._field(layout, 'Feedback mode', wx.Choice(self.window, choices=list(MODES)))
        self.mode.SetSelection(MODES.index(preferences.mode))
        self.interval = self._field(layout, 'Minimum interval in seconds (1–120)',
            wx.SpinCtrl(self.window, min=1, max=120, initial=int(preferences.interval)))
        self.volume = self._field(layout, 'Notification volume (0–100)',
            wx.SpinCtrl(self.window, min=0, max=100, initial=preferences.volume))
        self.muted = wx.CheckBox(self.window, label='Mute operation audio feedback')
        self.muted.SetName('Mute operation audio feedback')
        self.muted.SetValue(preferences.muted)
        layout.Add(self.muted, 0, wx.ALL, 5)
        self.background = wx.CheckBox(self.window, label='Allow feedback while this app is in the background')
        self.background.SetName('Allow feedback while this app is in the background')
        self.background.SetValue(preferences.background)
        layout.Add(self.background, 0, wx.ALL, 5)
        message = ('Off is the default. Notifications pause during connected playback or recording. '
                   'Beep and speech require available Windows audio; speech checks an installed SAPI voice. '
                   'Progress text remains available in every mode.')
        help_text = wx.StaticText(self.window, label=message)
        help_text.Wrap(460)
        layout.Add(help_text, 0, wx.EXPAND | wx.ALL, 5)
        layout.Add(self.window.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 5)
        self.window.SetSizerAndFit(layout)

    def _field(self, layout, label, control):
        layout.Add(self.wx.StaticText(self.window, label=label), 0, self.wx.ALL, 5)
        control.SetName(label)
        layout.Add(control, 0, self.wx.EXPAND | self.wx.ALL, 5)
        return control

    def show(self):
        try:
            self.mode.SetFocus()
            if self.window.ShowModal() != self.wx.ID_OK:
                return None
            index = self.mode.GetSelection()
            if not 0 <= index < len(MODES):
                raise ValueError('Choose a feedback mode.')
            return FeedbackPreferences(MODES[index], self.interval.GetValue(), self.volume.GetValue(),
                                       self.muted.GetValue(), self.background.GetValue())
        finally:
            self.window.Destroy()


class ProgressActions:
    def __init__(self, frame, *, panel_factory=ProgressPanel, dialog_factory=FeedbackDialog,
                 wx_module=None):
        if wx_module is None:
            import wx as wx_module
        self.wx, self.frame, self.dialog_factory = wx_module, frame, dialog_factory
        self.preferences = FeedbackPreferences()
        self.store = None
        self._binding = None
        self.notice = ''
        self.panel = panel_factory(frame.host, preferences=self.preferences, current=self._current,
                                   on_tick=self._poll, wx_module=wx_module)
        self.reload()

    def _identity(self):
        library = self.frame.library
        return library, str(Path(library.root).absolute())

    def _current(self):
        if self._binding is None or getattr(self.frame, '_closing', False):
            return False
        try:
            library, root = self._identity()
            return library is self._binding[0] and root == self._binding[1]
        except Exception:
            return False

    def reload(self):
        """Call after replacing frame.library; the timer also catches drift."""
        if not self.panel.alive:
            return ''
        self.panel.hub.close()
        self.panel.feedback.close()
        self._binding = None
        self.store = None
        self.preferences, self.notice = FeedbackPreferences(), ''
        try:
            identity = self._identity()
            store = ProgressPreferenceStore(identity[1])
            preferences = store.load()
            self.store, self.preferences = store, preferences
            self._binding = identity
        except Exception:
            # Invalid files are preserved. Do not silently overwrite them with
            # defaults merely because the application opened a data folder.
            self.notice = 'Operation audio is off because its saved preferences could not be read.'
            try:
                self._binding = self._identity()
            except Exception:
                pass
        self.panel.rebind(self.preferences, current=self._current, notice=self.notice)
        return self.notice

    def _poll(self):
        if not self.panel.alive:
            return
        if getattr(self.frame, '_closing', False):
            self.panel.dispose()
            return
        if not self._current():
            self.reload()
        if self.panel.alive:
            # Native focus is inspected on the UI thread only. A child dialog
            # still belongs to this foreground app even when frame.IsActive is false.
            active = self.frame.IsActive()
            if not active and hasattr(self.wx, 'GetActiveWindow'):
                window = self.wx.GetActiveWindow()
                seen = set()
                while window is not None and id(window) not in seen:
                    if window is self.frame:
                        active = True
                        break
                    seen.add(id(window))
                    window = window.GetParent()
            self.panel.set_background(not active)

    def query_current(self, event=None):
        if not self._current():
            self.reload()
        return self.panel.query_current(event)

    def _save(self, preferences, binding):
        if not self._current() or self._binding is not binding or self.store is None:
            raise ValueError('The library folder changed or its preferences are unavailable. Reopen this action.')
        self.store.save(preferences)
        self.preferences = preferences
        self.panel.feedback.configure(preferences)
        self.panel.notice = f'Operation feedback: {preferences.mode}' + ('; muted.' if preferences.muted else '.')
        self.panel._render()

    def configure(self, event=None):
        if not self._current():
            self.reload()
        if not self.panel.alive:
            return
        binding = self._binding
        try:
            preferences = self.dialog_factory(self.frame, self.preferences, wx_module=self.wx).show()
            if preferences is not None:
                self._save(preferences, binding)
        except Exception:
            if self.panel.alive:
                self.panel.notice = 'Feedback preferences were not saved. Check the active data folder and its settings file.'
                self.panel._render()

    def mute(self, event=None):
        if not self._current():
            self.reload()
        try:
            self._save(replace(self.preferences, muted=not self.preferences.muted), self._binding)
        except Exception:
            if self.panel.alive:
                self.panel.notice = 'Feedback mute was not saved. Check the active data folder and its settings file.'
                self.panel._render()


def install_progress_actions(frame):
    import wx
    actions = ProgressActions(frame, wx_module=wx)
    # Locate the real status item, rather than assuming a fixed panel index.
    index = next((index for index, item in enumerate(frame.layout.GetChildren())
                  if item.GetWindow() is frame.status), None)
    if index is None:
        frame.layout.Add(actions.panel.window, 0, wx.EXPAND | wx.ALL, 3)
    else:
        frame.layout.Insert(index + 1, actions.panel.window, 0, wx.EXPAND | wx.ALL, 3)
    menu = None
    for index in range(frame.menu_bar.GetMenuCount()):
        label = frame.menu_bar.GetMenuLabel(index).replace('&', '').strip()
        if label.casefold() == 'options':
            menu = frame.menu_bar.GetMenu(index)
            break
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Options')
    for label, handler in (('Query current operation progress', actions.query_current),
                           ('Operation feedback preferences...', actions.configure),
                           ('Toggle operation feedback mute', actions.mute)):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.progress_actions = actions
    frame.host.Layout()
    return actions
