"""Native, focus-preserving operation progress and optional local feedback.

Producers report through the shared ProgressHub contract. This view does not
invent work units, durations or percentages and never sends audio to a renderer.
"""
from dataclasses import replace
from pathlib import Path
import sys

from ..progress import ProgressHub
from ..progress_preferences import ProgressPreferences, LegacyProgressPreferences, LibraryProgressPreferenceStore
from ..progress_feedback import ProgressFeedback, WindowsToneSink
from .command_registry import CommandSpec
from .keyboard_settings import register_menu_command


_TERMINAL = {'succeeded', 'failed', 'cancelled', 'unavailable'}


def _state(snapshot):
    return getattr(snapshot.state, 'value', snapshot.state)


def _items(menu):
    for item in menu.GetMenuItems():
        if item.IsSeparator():
            continue
        yield item
        nested = item.GetSubMenu()
        if nested is not None:
            yield from _items(nested)


class NativeProgressPanel:
    """One native observer; querying is explicit and works during a busy job."""

    def __init__(self, frame, *, accessible_name=None,
                 details_name='Current operation progress and phase',
                 query_name='Query current progress'):
        import wx

        self.wx, self.frame = wx, frame
        self._fixed_gauge_name = accessible_name
        self.hub = ProgressHub()
        self.active_task = None
        self.closed = False
        self._last_revision = None
        self._reporting_failure = None
        self._busy_menus = self._busy_items = None
        self.query_item = None
        self.notice = ''
        self.tone_sink = None
        if sys.platform == 'win32':
            try:
                self.tone_sink = WindowsToneSink()
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                self.notice = 'Progress tones are unavailable: ' + str(error)
        self.speech_provider = getattr(frame, 'progress_speech_provider', None)
        if not callable(getattr(self.speech_provider, 'speak_progress', None)):
            self.speech_provider = None
        from ..speech.progress_speech import snapshot_speech_provider
        self.speech_provider = snapshot_speech_provider(self.speech_provider)
        self.store = LibraryProgressPreferenceStore(frame.library.root)
        self.preferences = self._read_preferences()
        self.feedback = ProgressFeedback(self._effective_preferences(self.preferences),
                                         tone_sink=self.tone_sink, speech_provider=self.speech_provider)
        from .progress_media import ProgressMedia
        self.media = ProgressMedia(self)
        frame.live_media_feedback = self.media.live_feedback

        self.panel = wx.Panel(frame.host)
        self._native_id = self.panel.GetId()
        self.panel.SetName('Operation progress')
        layout = wx.BoxSizer(wx.VERTICAL)
        self.gauge = wx.Gauge(self.panel, range=100, style=wx.GA_HORIZONTAL)
        self.gauge.SetName(accessible_name or 'Current operation progress')
        self.gauge.SetValue(0)
        self._gauge_value, self._gauge_indeterminate = 0, False
        self._gauge_name = accessible_name or 'Current operation progress'
        layout.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)
        self.text = wx.TextCtrl(self.panel, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.text.SetName(details_name)
        self.text.SetMinSize((-1, 64))
        layout.Add(self.text, 0, wx.EXPAND | wx.ALL, 4)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.query_button = wx.Button(self.panel, label='&Query current progress')
        self.query_button.SetName(query_name)
        self.query_button.Bind(wx.EVT_BUTTON, self.query)
        buttons.Add(self.query_button, 0, wx.ALL, 3)
        self.settings_button = wx.Button(self.panel, label='Progress feedback settings...')
        self.settings_button.SetName('Progress feedback settings')
        self.settings_button.Bind(wx.EVT_BUTTON, self.settings)
        buttons.Add(self.settings_button, 0, wx.ALL, 3)
        self.mute = wx.CheckBox(self.panel, label='Mute extra progress feedback')
        self.mute.SetName('Mute extra progress feedback while work continues')
        self.mute.SetValue(self.preferences.muted)
        self.mute.Bind(wx.EVT_CHECKBOX, self.toggle_mute)
        buttons.Add(self.mute, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
        layout.Add(buttons, 0, wx.EXPAND)
        self.panel.SetSizer(layout)
        frame.layout.Insert(1, self.panel, 0, wx.EXPAND | wx.ALL, 3)
        self.timer = wx.Timer(self.panel)
        self.panel.Bind(wx.EVT_TIMER, self.refresh, self.timer)
        self.panel.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.timer.Start(200)
        self._retained_providers = []
        self.refresh()
        frame.host.Layout()

    @property
    def legacy(self):
        if not hasattr(self, '_legacy'):
            from .operation_progress import ProgressCompatibility
            self._legacy = ProgressCompatibility(self)
        return self._legacy

    def _read_preferences(self):
        try:
            return self.store.load()
        except (ValueError, OSError) as error:
            self.notice = ('Progress feedback settings could not be loaded. Feedback is off; '
                           'the existing file was kept. Open Progress feedback settings to repair it. '
                           + str(error))
            return ProgressPreferences()

    def _effective_preferences(self, preferences):
        mode = preferences.mode
        has_tone = self.tone_sink is not None
        has_speech = self.speech_provider is not None
        if mode == 'speech' and not has_speech:
            self.notice = 'Spoken progress is unavailable until an approved local voice is connected.'
            mode = 'off'
        elif mode == 'beep_speech' and not has_speech:
            self.notice = 'Spoken progress is unavailable; only supported progress tones can play.'
            mode = 'beep' if has_tone else 'off'
        if mode == 'beep' and not has_tone:
            self.notice = 'Progress tones are unavailable on this computer.'
            mode = 'off'
        elif mode == 'beep_speech' and not has_tone:
            self.notice = 'Progress tones are unavailable; the connected local voice can announce progress.'
            mode = 'speech' if has_speech else 'off'
        return replace(preferences, mode=mode)

    def supported_modes(self):
        modes = [('Off', 'off')]
        if self.tone_sink is not None:
            modes.append(('Beep', 'beep'))
        if self.speech_provider is not None:
            modes.append(('Speech', 'speech'))
            if self.tone_sink is not None:
                modes.append(('Beep and speech', 'beep_speech'))
        return tuple(modes)

    def set_speech_provider(self, provider, *, root=None):
        """Authorize this session's local voice without enabling its sound."""
        if self.closed:
            raise ValueError('The progress view is closed.')
        from ..durable_state import local_path
        from ..speech.progress_speech import snapshot_speech_provider
        if provider is not None:
            current = local_path(self.frame.library.root, exists=True)
            selected = root if root is not None else getattr(provider, 'library_root', None)
            if (current != local_path(self.store.library_root, exists=True)
                    or selected is not None and local_path(selected, exists=True) != current):
                raise ValueError('The library folder changed. Select its progress voice again.')
            provider_root = getattr(provider, 'library_root', None)
            if provider_root is not None and local_path(provider_root, exists=True) != current:
                raise ValueError('This progress voice belongs to a different library.')
        provider = snapshot_speech_provider(provider)
        previous = self.feedback.set_speech_provider(provider)
        self.speech_provider = provider
        if previous is not None and previous is not provider:
            close = getattr(previous, 'close', None)
            if callable(close):
                try:
                    closed = close()
                except (Exception, KeyboardInterrupt):
                    closed = False
                if closed is False:
                    if previous not in self._retained_providers:
                        self._retained_providers.append(previous)
                    self.notice = 'The previous progress voice has not confirmed cleanup; its owner is retained for retry.'
        self.feedback.set_preferences(self._effective_preferences(self.preferences))
        self.refresh_audio_state()

    def hold_audio(self, *, recording=False):
        return self.media.hold_audio(recording=recording)

    def register_player(self, player):
        return self.media.register_player(player)

    def unregister_player(self, player, *, silence_confirmed=True):
        self.media.unregister_player(player, silence_confirmed=silence_confirmed)

    def refresh_audio_state(self):
        self.media.refresh()

    def begin(self, title, *, total=None, unit='units', measured=False):
        if self.closed:
            raise ValueError('The progress view is closed.')
        if type(measured) is not bool:
            raise ValueError('Measured progress selection must be boolean.')
        start = self.hub.start_measured if measured else self.hub.start
        task = start(title, total=total, unit=unit)
        self.active_task = task
        self._last_revision = None
        self._reporting_failure = None
        self.refresh()
        return task

    def request_cancel(self):
        if self.closed:
            return
        if self.active_task is not None and _state(self.active_task.snapshot()) not in _TERMINAL:
            self.active_task.request_cancel()
            self.refresh()

    def finish(self, task, state, message=''):
        if task is None:
            return
        message = ''.join(character if (ord(character) >= 32 and ord(character) != 127)
                          or character == '\n' else ' ' for character in str(message))[:1000]
        if state == 'succeeded':
            task.succeed(durable=True, message=message)
        elif state == 'cancelled':
            task.cancelled(message)
        elif state == 'failed':
            task.fail(message)
        elif state == 'paused':
            task.pause(message)
        else:
            raise ValueError('Unknown terminal operation state.')
        self.refresh()

    def _flag(self, name):
        value = getattr(self.frame, name, False)
        try:
            return bool(value() if callable(value) else value)
        except Exception:
            # A failing audio-state adapter must not cause unexpected sound.
            return True

    def reporting_unavailable(self, task, actual_state, warning):
        """Keep the real operation result separate from failed measurement."""
        if task is not None and not task.snapshot().terminal:
            message = ''.join(character if (ord(character) >= 32 and ord(character) != 127)
                              or character == '\n' else ' ' for character in str(warning))[:1000]
            task.unavailable(message)
        self.notice = str(warning)
        if task is self.active_task:
            self._reporting_failure = task.operation_id, actual_state, str(warning)
            self.feedback.suspend()
        self.refresh()

    def refresh(self, event=None):
        if self.closed:
            return
        media = getattr(self, 'media', None)
        force_quiet, media_busy = media.flags() if media is not None else (
            self._flag('progress_recording_active'), self._flag('progress_media_busy'))
        snapshot = self.active_task.snapshot() if self.active_task is not None else None
        if snapshot is None:
            description = 'No operation is running. Query current progress remains available while work runs.'
            percent, state, indeterminate = 0, 'idle', False
        else:
            state = _state(snapshot)
            description = snapshot.describe()
            indeterminate = snapshot.percent is None and state in {'running', 'cancelling'}
            percent = snapshot.percent if snapshot.percent is not None else 0
            reporting_failed = (self._reporting_failure is not None
                                and self._reporting_failure[0] == snapshot.operation_id)
            if reporting_failed:
                description = self._reporting_failure[2]
                state, indeterminate = 'measurement unavailable', False
            identity = snapshot.operation_id, snapshot.revision
            if identity != self._last_revision:
                self._last_revision = identity
            # Recording can never opt into notification sound. Other media
            # respects the user's quiet-during-media preference.
            if reporting_failed or force_quiet:
                self.feedback.suspend(snapshot)
            else:
                try:
                    self.feedback.update(snapshot, active=bool(self.frame.IsActive()),
                                         media_busy=media_busy)
                except Exception as error:
                    self.notice = 'Extra progress feedback is unavailable. ' + str(error)
        if indeterminate:
            self.gauge.Pulse()
        elif self._gauge_indeterminate or self._gauge_value != percent:
            self.gauge.SetValue(percent)
        self._gauge_indeterminate, self._gauge_value = indeterminate, percent
        gauge_name = self._fixed_gauge_name or ('Current operation progress: ' +
                      ('indeterminate; ' if indeterminate else '') + state)
        if gauge_name != self._gauge_name:
            self.gauge.SetName(gauge_name)
            self._gauge_name = gauge_name
        if self.notice and self.notice not in description:
            description += '\n' + self.notice
        feedback_error = getattr(self.feedback, 'last_error', '')
        if isinstance(feedback_error, str) and feedback_error:
            description += '\n' + feedback_error
        if self.text.GetValue() != description:
            self.text.ChangeValue(description)
        # No SetFocus, window raising or forced announcements on timed updates.

    def query(self, event=None):
        if self.closed:
            return
        self.refresh()
        self.text.SetInsertionPoint(0)
        self.text.SetFocus()

    def set_busy(self, busy):
        """Preserve native command enablement while keeping progress query live."""
        if self.closed:
            return
        bar = self.frame.GetMenuBar()
        if busy:
            if self._busy_menus is not None:
                return
            self._busy_menus = [(bar.GetMenu(index), bar.IsEnabledTop(index))
                                for index in range(bar.GetMenuCount())]
            self._busy_items = []
            for index in range(bar.GetMenuCount()):
                menu = bar.GetMenu(index)
                leaves = tuple(_items(menu))
                owns_query = self.query_item is not None and any(
                    item.GetId() == self.query_item.GetId() for item in leaves)
                bar.EnableTop(index, owns_query)
                if owns_query:
                    for item in leaves:
                        self._busy_items.append((item, item.IsEnabled()))
                        item.Enable(item.GetId() == self.query_item.GetId())
            self.settings_button.Disable()
        else:
            if self._busy_menus is None:
                return
            for item, enabled in self._busy_items:
                try:
                    item.Enable(enabled)
                except RuntimeError:
                    pass  # A removed module must not break operation completion.
            for index in range(bar.GetMenuCount()):
                menu = bar.GetMenu(index)
                previous = next((enabled for old, enabled in self._busy_menus if old == menu), True)
                bar.EnableTop(index, previous)
            self._busy_menus = self._busy_items = None
            self.settings_button.Enable()

    def save_preferences(self, preferences):
        if self.closed:
            raise ValueError('The progress view is closed.')
        self.store.save(preferences)
        self.preferences = preferences
        self.notice = self.store.warning
        self.feedback.set_preferences(self._effective_preferences(preferences))
        self.mute.SetValue(preferences.muted)
        self.refresh()

    def toggle_mute(self, event=None):
        """The native mute control remains available during long operations."""
        if self.closed:
            return
        requested = replace(self.preferences, muted=self.mute.GetValue())
        if requested.muted:
            # Silence is an immediate user action. A full disk must not make
            # an ongoing operation keep producing unwanted extra feedback.
            self.feedback.suspend()
            self.feedback.set_preferences(self._effective_preferences(requested))
            self.preferences = requested
            self.mute.SetValue(True)
            try:
                self.store.save(requested)
            except (ValueError, OSError) as error:
                self.notice = 'Muted for this session; the preference could not be saved. ' + str(error)
            else:
                self.notice = ''
            self.refresh()
            return
        try:
            self.save_preferences(requested)
        except (ValueError, OSError) as error:
            self.mute.SetValue(self.preferences.muted)
            self.notice = 'Progress feedback remains muted; the preference could not be saved. ' + str(error)
            self.refresh()

    def settings(self, event=None):
        if self.closed or getattr(self.frame, 'working', False):
            return
        focus = self.wx.Window.FindFocus()
        try:
            ProgressSettingsDialog(self.frame, self).show()
        finally:
            if focus:
                try:
                    focus.SetFocus()
                except RuntimeError:
                    pass

    def change_root(self, root):
        if self.closed:
            raise ValueError('The progress view is closed.')
        if getattr(self.frame, 'working', False):
            raise ValueError('Wait for the current operation before changing progress settings folders.')
        self.feedback.suspend(self.active_task.snapshot() if self.active_task is not None else None)
        self.set_speech_provider(None)
        self.store = LibraryProgressPreferenceStore(root)
        self.notice = ''
        self.preferences = self._read_preferences()
        self.feedback.set_preferences(self._effective_preferences(self.preferences))
        self.mute.SetValue(self.preferences.muted)
        self.hub = ProgressHub()
        self.active_task = None
        self._last_revision = None
        self._reporting_failure = None
        self.refresh()

    def _destroyed(self, event):
        try:
            if event.GetId() == self._native_id:
                self.close()
        finally:
            event.Skip()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.timer.Stop()
            except RuntimeError:
                pass
            self.feedback.close()
            self.media.close()
        retained, seen = [], set()
        for provider in (*self._retained_providers, self.speech_provider):
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, 'close', None)
            if not callable(close):
                continue
            try:
                closed = close()
            except (Exception, KeyboardInterrupt):
                closed = False
            if closed is False:
                retained.append(provider)
        self._retained_providers = retained
        if retained:
            self.notice = 'Progress voice cleanup is pending; its owners are retained for retry.'
        return not retained


class ProgressSettingsDialog:
    """A native, local-only preference draft; cancellation leaves it unchanged."""

    def __init__(self, parent, panel):
        import wx

        self.wx, self.panel = wx, panel
        self.closed = False
        self.original_store = panel.store
        from .keyboard_settings import _owned_keyboard_dialog
        self.dialog = _owned_keyboard_dialog(parent, title='Progress feedback settings', size=(660, 600),
                                             detach=self.close)
        layout = wx.BoxSizer(wx.VERTICAL)
        introduction = wx.StaticText(self.dialog, label=(
            'Progress is always available as text and a native bar. Extra sound is off by default. '
            'NVDA can provide its own progress announcements; choose extra sound only when you want it.'))
        introduction.Wrap(610)
        layout.Add(introduction, 0, wx.EXPAND | wx.ALL, 8)
        self.modes = panel.supported_modes()
        layout.Add(wx.StaticText(self.dialog, label='Extra progress feedback'), 0, wx.LEFT | wx.TOP, 8)
        self.mode = wx.Choice(self.dialog, choices=[label for label, _ in self.modes])
        self.mode.SetName('Extra progress feedback mode')
        chosen = next((index for index, (_, mode) in enumerate(self.modes)
                       if mode == panel.preferences.mode), 0)
        self.mode.SetSelection(chosen)
        layout.Add(self.mode, 0, wx.EXPAND | wx.ALL, 8)
        if panel.speech_provider is None:
            layout.Add(wx.StaticText(self.dialog, label='Speech becomes available when an approved local voice is connected.'),
                       0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        layout.Add(wx.StaticText(self.dialog, label='Minimum interval between progress sounds, in seconds'),
                   0, wx.LEFT | wx.TOP, 8)
        self.interval = wx.SpinCtrl(self.dialog, min=1, max=120 if panel.store.legacy else 60,
                                    initial=int(panel.preferences.interval_seconds))
        self.interval.SetName('Minimum progress feedback interval in seconds')
        layout.Add(self.interval, 0, wx.ALL | wx.EXPAND, 8)
        layout.Add(wx.StaticText(self.dialog, label='Progress feedback volume, percent'), 0, wx.LEFT | wx.TOP, 8)
        self.volume = wx.Slider(self.dialog, value=round(panel.preferences.volume), minValue=0, maxValue=100,
                                style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        self.volume.SetName('Progress feedback volume percent')
        layout.Add(self.volume, 0, wx.ALL | wx.EXPAND, 8)
        self.muted = wx.CheckBox(self.dialog, label='Mute extra progress sound and speech')
        self.muted.SetValue(panel.preferences.muted)
        self.background = wx.CheckBox(self.dialog, label='Allow extra feedback when this window is in the background')
        self.background.SetValue(panel.preferences.allow_background)
        self.quiet = wx.CheckBox(self.dialog, label='Keep extra feedback quiet during audio playback')
        self.quiet.SetValue(panel.preferences.quiet_during_media)
        for control in (self.muted, self.background, self.quiet):
            control.SetName(control.GetLabel())
            layout.Add(control, 0, wx.ALL | wx.EXPAND, 8)
        layout.Add(wx.StaticText(self.dialog, label='Extra feedback is always suppressed during recording.'),
                   0, wx.LEFT | wx.RIGHT, 8)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Progress settings status')
        self.status.SetMinSize((-1, 65))
        precision_notice = (' The earlier volume has fractional percent precision. Saving this dialog applies '
                            'the displayed whole percent; Cancel keeps the exact saved value.'
                            if panel.store.legacy and panel.preferences.volume != round(panel.preferences.volume) else '')
        self.status.ChangeValue((panel.notice or 'Changes apply only when you choose Save.') + precision_notice)
        layout.Add(self.status, 0, wx.ALL | wx.EXPAND, 8)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        save = wx.Button(self.dialog, wx.ID_OK, '&Save')
        save.Bind(wx.EVT_BUTTON, self.save)
        footer.Add(save, 0, wx.ALL, 4)
        footer.Add(wx.Button(self.dialog, wx.ID_CANCEL, 'Cancel'), 0, wx.ALL, 4)
        layout.Add(footer, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.mode.SetFocus()

    def close(self):
        self.closed = True

    def _alive(self):
        return not self.closed and not self.panel.closed and bool(self.dialog)

    def save(self, event=None):
        if not self._alive():
            return
        if self.original_store is not self.panel.store:
            self.status.ChangeValue('The library folder changed. Close and reopen progress settings.')
            self.status.SetFocus()
            return
        try:
            selection = self.mode.GetSelection()
            if not 0 <= selection < len(self.modes):
                raise ValueError('Choose an available progress feedback mode.')
            policy_type = LegacyProgressPreferences if self.panel.store.legacy else ProgressPreferences
            preferences = policy_type(
                mode=self.modes[selection][1],
                interval_seconds=float(self.interval.GetValue()), volume=self.volume.GetValue(),
                muted=self.muted.GetValue(), allow_background=self.background.GetValue(),
                quiet_during_media=self.quiet.GetValue())
            self.panel.save_preferences(preferences)
        except (ValueError, OSError) as error:
            if not self._alive():
                return
            self.status.ChangeValue('Progress settings were not saved. ' + str(error))
            self.status.SetFocus()
            return
        if self._alive():
            self.dialog.EndModal(self.wx.ID_OK)

    def show(self):
        try:
            return self.dialog.ShowModal() == self.wx.ID_OK and self._alive()
        finally:
            self.close()
            if self.dialog:
                self.dialog.Destroy()


def install_progress_panel(frame):
    existing = getattr(frame, 'progress_panel', None)
    if existing is not None:
        frame.operation_progress = existing.legacy
        return existing
    import wx
    panel = NativeProgressPanel(frame)
    frame.progress_panel = panel
    frame.operation_progress = panel.legacy
    bar = frame.GetMenuBar()
    options = next((bar.GetMenu(index) for index in range(bar.GetMenuCount())
                    if bar.GetMenuLabel(index).replace('&', '').strip().casefold() == 'options'), None)
    if options is None:
        raise ValueError('Install the shared keyboard Options menu before operation progress.')
    query = options.Append(wx.ID_ANY, 'Query current &progress\tCtrl+Shift+Q')
    settings = options.Append(wx.ID_ANY, 'Progress feedback settings...')
    panel.query_item = query
    frame.Bind(wx.EVT_MENU, panel.query, query)
    frame.Bind(wx.EVT_MENU, panel.settings, settings)
    manager = frame.keyboard_commands
    previous_notice = manager.load_notice
    register_menu_command(frame, CommandSpec('progress.query', 'Query current progress',
                                            default_shortcut='Ctrl+Shift+Q'), query,
                          fallback_unassigned=True)
    if manager.load_notice and manager.load_notice != previous_notice:
        panel.notice = panel.notice + '\n' + manager.load_notice if panel.notice else manager.load_notice
    register_menu_command(frame, CommandSpec('options.progress', 'Progress feedback settings'), settings)
    panel.refresh()
    return panel
