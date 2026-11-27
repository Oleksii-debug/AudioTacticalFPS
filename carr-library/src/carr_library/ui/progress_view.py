"""Native observers for the existing shared progress and notification contract.

The host keeps its executor and service results. This module owns only controls,
preferences and the lifetime of their optional notification output. Widget and
focus patterns reuse the earlier native progress implementations; no tracker,
queue, sound generator or completion protocol is duplicated here.
"""
from dataclasses import replace
from pathlib import Path

from ..progress import (ProgressHub, ProgressPreferences, finish_result,
                        load_preferences, save_preferences)
from ..progress_audio import NotificationWorker
from .command_registry import CommandSpec
from .keyboard_settings import register_menu_command


COMMANDS = (
    CommandSpec('progress.query', 'Query current progress'),
    CommandSpec('options.progress', 'Progress feedback settings'),
    CommandSpec('progress.mute', 'Mute or unmute extra progress feedback'),
)
MODES = (('Off', 'off'), ('Beep', 'beep'), ('Speech', 'speech'), ('Beep and speech', 'beep_speech'))


def shared_hub(frame):
    hub = getattr(frame, 'progress_hub', None)
    if hub is None:
        frame.progress_hub = hub = ProgressHub()
    return hub


def fail_operation(reporter, error):
    """Preserve a committed terminal result and the original service exception."""
    from ..audio.jobs import RenderCancelled
    try:
        if reporter.event.terminal:
            return
        if isinstance(error, (InterruptedError, RenderCancelled, KeyboardInterrupt)):
            reporter.cancel('The operation was stopped.')
        else:
            reporter.fail('The operation did not complete.')
    except Exception:
        pass  # A detached observer cannot replace a business/submission error.


def run_observed(reporter, operation):
    """Bind exactly one host reporter to the existing background operation."""
    try:
        with reporter.bind():
            result = operation()
    except BaseException as error:
        fail_operation(reporter, error)
        raise
    try:
        if not reporter.event.terminal:
            # Existing services return only after their own commit/verification.
            # A late cancel request does not reverse that accepted result.
            finish_result(reporter, result)
    except Exception:
        try:
            if not reporter.event.terminal:
                reporter.pause('The operation returned; progress reporting is unavailable. Review its result.')
        except Exception:
            pass  # A reporting error cannot cause completed work to retry.
    return result


def _items(menu):
    for item in menu.GetMenuItems():
        if item.IsSeparator():
            continue
        child = item.GetSubMenu()
        if child is None:
            yield item
        else:
            yield from _items(child)


class NativeProgressView:
    """One bounded, focus-preserving native view of a shared ProgressHub."""
    def __init__(self, frame, *, notifications=None):
        import wx
        self.wx, self.frame, self.hub = wx, frame, shared_hub(frame)
        self.closed, self.notice = False, ''
        self.root = Path(frame.library.root)
        self.preferences = self._read_preferences()
        self.notifications = notifications or NotificationWorker(self.preferences)
        if notifications is not None:
            notifications.configure(self.preferences)
        self._ids, self._labels = [], []
        self._selected, self._explicit_selection = None, False
        self._gauge_value, self._indeterminate = 0, False
        self._busy_menus = self._busy_items = None
        self.command_items = []
        self.panel = wx.Panel(frame.host)
        self.panel.SetName('Operation progress')
        layout = wx.BoxSizer(wx.VERTICAL)
        self.operations = wx.Choice(self.panel)
        self.operations.SetName('Operations and subtasks')
        self.operations.Bind(wx.EVT_CHOICE, self.select)
        layout.Add(self.operations, 0, wx.EXPAND | wx.ALL, 3)
        self.gauge = wx.Gauge(self.panel, range=100, style=wx.GA_HORIZONTAL)
        self.gauge.SetName('Current operation progress')
        self.gauge.SetValue(0)
        layout.Add(self.gauge, 0, wx.EXPAND | wx.ALL, 3)
        self.text = wx.TextCtrl(self.panel, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.text.SetName('Current operation progress, state and phase')
        self.text.SetMinSize((-1, 58))
        layout.Add(self.text, 0, wx.EXPAND | wx.ALL, 3)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        for title, action in (('Query current progress', self.query),
                              ('Progress feedback settings...', self.settings),
                              ('Mute or unmute progress feedback', self.toggle_mute)):
            control = wx.Button(self.panel, label=title)
            control.SetName(title)
            control.Bind(wx.EVT_BUTTON, action)
            buttons.Add(control, 0, wx.ALL, 3)
        layout.Add(buttons, 0, wx.EXPAND)
        self.panel.SetSizer(layout)
        frame.layout.Insert(1, self.panel, 0, wx.EXPAND | wx.ALL, 3)
        self.timer = wx.Timer(self.panel)
        self.panel.Bind(wx.EVT_TIMER, self.refresh, self.timer)
        self.panel.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.timer.Start(200)
        self.refresh()
        frame.host.Layout()

    def _read_preferences(self):
        try:
            return load_preferences(self.root)
        except (OSError, ValueError) as error:
            self.notice = ('Extra progress feedback is Off because its saved settings could not be read. '
                           'The existing settings were kept. ' + str(error))
            return ProgressPreferences()

    def _reload_root(self):
        root = Path(self.frame.library.root)
        if root != self.root:
            self.root, self.notice = root, ''
            self.preferences = self._read_preferences()
            self.notifications.configure(self.preferences)

    def selected_event(self, events=None):
        events = self.hub.snapshot() if events is None else events
        selected = next((row for row in events if row.operation_id == self._selected), None)
        if self._explicit_selection and selected is not None:
            return selected
        active = [row for row in events if not row.terminal and row.parent_id is None]
        return max(active or list(events), key=lambda row: row.sequence, default=None)

    def select(self, event=None):
        index = self.operations.GetSelection()
        if 0 <= index < len(self._ids):
            self._selected, self._explicit_selection = self._ids[index], True
            self.refresh()

    def refresh(self, event=None):
        if self.closed:
            return
        self._reload_root()
        events = self.hub.snapshot()
        self.notifications.environment(foreground=bool(self.frame.IsActive()), media_active=self.hub.media_active)
        self.notifications.offer(events)
        selected = self.selected_event(events)
        ids = [row.operation_id for row in events]
        labels = [('Subtask: ' if row.parent_id else '') + row.title + ' — ' + row.state for row in events]
        if ids != self._ids or labels != self._labels:
            self.operations.Set(labels)
            self._ids, self._labels = ids, labels
        if selected is not None:
            self._selected = selected.operation_id
            index = ids.index(self._selected)
            if self.operations.GetSelection() != index:
                self.operations.SetSelection(index)
        self.operations.Enable(bool(events))
        description = selected.text if selected is not None else 'No operation has started. Extra progress sound is Off by default.'
        indeterminate = selected is not None and selected.percent is None and selected.state == 'running'
        value = selected.percent if selected is not None and selected.percent is not None else 0
        if indeterminate:
            self.gauge.Pulse()
        elif self._indeterminate or self._gauge_value != value:
            self.gauge.SetValue(value)
        self._indeterminate, self._gauge_value = indeterminate, value
        name = ('Current operation progress: ' + ('indeterminate; ' if indeterminate else '')
                + (selected.state if selected is not None else 'idle'))
        if self.gauge.GetName() != name:
            self.gauge.SetName(name)
        messages = [description]
        if self.notice:
            messages.append(self.notice)
        if self.notifications.error:
            messages.append(self.notifications.error)
        if self.preferences.muted:
            messages.append('Extra progress feedback is muted.')
        text = '\n'.join(messages)
        if self.text.GetValue() != text:
            self.text.ChangeValue(text)
        # Timed updates never focus, raise or announce a window themselves.

    def query(self, event=None):
        if not self.closed:
            self.refresh()
            self.text.SetInsertionPoint(0)
            self.text.SetFocus()  # Only this explicit command moves focus.

    def configure(self, preferences, *, expected_root=None):
        if self.closed or Path(self.frame.library.root) != self.root or (
                expected_root is not None and expected_root != self.root):
            raise ValueError('The library folder changed. Reopen progress settings.')
        save_preferences(self.root, preferences)
        self.notifications.configure(preferences)
        self.preferences, self.notice = preferences, ''
        self.refresh()

    def toggle_mute(self, event=None):
        self._reload_root()
        requested = replace(self.preferences, muted=not self.preferences.muted)
        if requested.muted:
            # A full disk must not prevent the requested immediate silence.
            self.notifications.configure(requested)
            self.preferences = requested
            try:
                save_preferences(self.root, requested)
                self.notice = ''
            except (OSError, ValueError) as error:
                self.notice = 'Muted for this session; settings were not saved. ' + str(error)
        else:
            try:
                self.configure(requested)
            except (OSError, ValueError) as error:
                self.notice = 'Extra feedback remains muted; settings were not saved. ' + str(error)
        self.refresh()

    def settings(self, event=None):
        self._reload_root()
        focus = self.wx.Window.FindFocus()
        try:
            ProgressSettingsDialog(self.frame, self).show()
        finally:
            if focus:
                try:
                    focus.SetFocus()
                except RuntimeError:
                    pass

    def set_busy(self, busy):
        """Keep query/settings/mute available, preserving every other menu state."""
        bar = self.frame.GetMenuBar()
        if busy:
            if self._busy_menus is not None:
                return
            allowed = {item.GetId() for item in self.command_items}
            self._busy_menus = [(bar.GetMenu(index), bar.IsEnabledTop(index)) for index in range(bar.GetMenuCount())]
            self._busy_items = []
            for index in range(bar.GetMenuCount()):
                leaves = tuple(_items(bar.GetMenu(index)))
                available = any(item.GetId() in allowed for item in leaves)
                bar.EnableTop(index, available)
                if available:
                    for item in leaves:
                        self._busy_items.append((item, item.IsEnabled()))
                        item.Enable(item.GetId() in allowed)
        elif self._busy_menus is not None:
            for item, enabled in self._busy_items:
                try:
                    item.Enable(enabled)
                except RuntimeError:
                    pass
            for index in range(bar.GetMenuCount()):
                menu = bar.GetMenu(index)
                enabled = next((value for old, value in self._busy_menus if old == menu), True)
                bar.EnableTop(index, enabled)
            self._busy_menus = self._busy_items = None

    def _destroyed(self, event):
        if event.GetEventObject() is self.panel:
            self.close()
        event.Skip()

    def close(self):
        if not self.closed:
            self.closed = True
            self.timer.Stop()
            self.notifications.close()  # Never join a device/speech thread here.


class ProgressSettingsDialog:
    def __init__(self, parent, view):
        import wx
        self.wx, self.view, self.root = wx, view, view.root
        self.dialog = wx.Dialog(parent, title='Progress feedback settings', size=(660, 620))
        layout = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(self.dialog, label=('Text and the native progress bar are always available. Extra sound is Off by default. '
            'NVDA can announce native progress itself; enable extra sound only when wanted. '
            'Speech uses an installed English local voice for this English interface.'))
        intro.Wrap(610)
        layout.Add(intro, 0, wx.EXPAND | wx.ALL, 8)
        self.mode = wx.Choice(self.dialog, choices=[label for label, _ in MODES])
        self.mode.SetName('Extra progress feedback mode')
        self.mode.SetSelection(next(i for i, (_, mode) in enumerate(MODES) if mode == view.preferences.mode))
        self.interval = wx.SpinCtrlDouble(self.dialog, min=.5, max=60, inc=.5, initial=view.preferences.interval_seconds)
        self.interval.SetName('Minimum progress feedback interval in seconds')
        self.volume = wx.Slider(self.dialog, value=round(view.preferences.volume * 100), minValue=0, maxValue=100,
                                style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        self.volume.SetName('Progress feedback volume percent')
        for label, control in (('Extra feedback', self.mode), ('Interval in seconds', self.interval), ('Volume percent', self.volume)):
            layout.Add(wx.StaticText(self.dialog, label=label), 0, wx.LEFT | wx.TOP, 8)
            layout.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        self.flags = {}
        for key, label in (('muted', 'Mute extra progress sound and speech'), ('quiet', 'Quiet mode'),
                           ('background', 'Allow feedback while this window is in the background'),
                           ('suppress_during_media', 'Suppress feedback during app audio playback or recording')):
            control = wx.CheckBox(self.dialog, label=label)
            control.SetName(label)
            control.SetValue(getattr(view.preferences, key))
            self.flags[key] = control
            layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Progress settings status')
        self.status.ChangeValue(view.notice or 'Changes apply only when you choose Save.')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        save = wx.Button(self.dialog, wx.ID_OK, '&Save')
        save.Bind(wx.EVT_BUTTON, self.save)
        footer.Add(save, 0, wx.ALL, 4)
        footer.Add(wx.Button(self.dialog, wx.ID_CANCEL, 'Cancel'), 0, wx.ALL, 4)
        layout.Add(footer, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.mode.SetFocus()

    def save(self, event=None):
        try:
            index = self.mode.GetSelection()
            if not 0 <= index < len(MODES):
                raise ValueError('Choose a progress feedback mode.')
            preferences = ProgressPreferences(mode=MODES[index][1], interval_seconds=self.interval.GetValue(),
                volume=self.volume.GetValue() / 100, **{key: control.GetValue() for key, control in self.flags.items()})
            self.view.configure(preferences, expected_root=self.root)
        except (ValueError, OSError) as error:
            self.status.ChangeValue('Progress settings were not saved. ' + str(error))
            self.status.SetFocus()
            return
        self.dialog.EndModal(self.wx.ID_OK)

    def show(self):
        try:
            return self.dialog.ShowModal() == self.wx.ID_OK
        finally:
            self.dialog.Destroy()


def install_progress_view(frame):
    existing = getattr(frame, 'progress_view', None)
    if existing is not None:
        return existing
    import wx
    bar = frame.GetMenuBar()
    options = next((bar.GetMenu(index) for index in range(bar.GetMenuCount())
                    if bar.GetMenuLabel(index).replace('&', '').strip().casefold() == 'options'), None)
    if options is None:
        raise ValueError('Install keyboard Options before operation progress.')
    view = NativeProgressView(frame)
    for spec, handler in zip(COMMANDS, (view.query, view.settings, view.toggle_mute)):
        caption = spec.label + ('\t' + spec.default_shortcut if spec.default_shortcut else '')
        item = options.Append(wx.ID_ANY, caption)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, spec, item)
        view.command_items.append(item)
    frame.progress_view = view
    return view
