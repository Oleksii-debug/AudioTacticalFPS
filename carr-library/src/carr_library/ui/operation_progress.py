"""Native progress presentation over the shared measured ProgressHub.

The presenter does not decide whether work committed: it observes the current
service result. Optional notification output owns a separate short-lived player.
No source text, rendered master or user recording is altered.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import threading
import time
import weakref

from ..progress import (ProgressPreferences, decode_preferences, display_message,
                        encode_preferences, finish_result, preference_snapshot, save_preferences)
from ..progress_audio import (NotificationWorker, configured_registry,
                              voice_fingerprint)

from .command_registry import CommandSpec
from .keyboard_settings import register_menu_command
from .progress_view import shared_hub

_CURRENT_PREFERENCES = object()

COMMANDS = (
    CommandSpec('progress.query', 'Query current progress'),
    CommandSpec('options.progress', 'Progress feedback settings'),
    CommandSpec('progress.mute', 'Mute or unmute extra progress feedback'),
)


def _items(menu):
    for item in menu.GetMenuItems():
        if item.IsSeparator():
            continue
        child = item.GetSubMenu()
        if child is None:
            yield item
        else:
            yield from _items(child)


_UNSET = object()
_IDLE = 'No operation is running.'
_LANGUAGES = {'en': 'English', 'uk': 'Українська', 'sk': 'Slovenčina'}


def operation_progress_for(window):
    """Find the existing main-window presenter without creating a second hub."""
    seen = set()
    while window is not None and id(window) not in seen:
        seen.add(id(window))
        value = getattr(window, 'operation_progress', None)
        if value is not None:
            return value
        parent = getattr(window, 'GetParent', None)
        window = parent() if callable(parent) else getattr(window, 'parent', None)
    return None


class _AudioHold:
    def __init__(self, presenter):
        self.presenter, self.closed = presenter, False
        self.token = object()
        presenter._holds.add(self.token)
        presenter._environment()

    @property
    def ready(self):
        return (not self.closed and not self.presenter.closed
                and self.presenter.notifications.quiescent)

    def close(self):
        if not self.closed:
            self.closed = True
            self.presenter._holds.discard(self.token)
            self.presenter._environment()


class _MediaRequest:
    def __init__(self, presenter, action, *, alive, failed, timeout):
        self.presenter, self.action = presenter, action
        self.alive, self.failed = alive, failed
        self.deadline = presenter.clock() + timeout
        self.hold, self.timer, self.closed = _AudioHold(presenter), None, False
        self.scope = presenter.library
        presenter._requests.add(self)
        self._poll()

    def _poll(self):
        self.timer = None
        if self.closed:
            return
        if (self.presenter.closed or not self.presenter.scope_current
                or self.presenter.library is not self.scope or not self.alive()):
            self.close()
            return
        if not self.hold.ready:
            if self.presenter.clock() >= self.deadline:
                self.close()
                self.failed(RuntimeError('Progress audio has not released its device. Playback was not started.'))
            else:
                self.timer = self.presenter.wx.CallLater(25, self._poll)
            return
        try:
            self.action()
        except Exception as exc:
            self.failed(exc)
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            if self.timer is not None:
                self.timer.Stop()
                self.timer = None
            self.presenter._requests.discard(self)
            self.hold.close()


class OperationProgress:
    """One native view, current-result token and bounded shared progress history."""
    def __init__(self, frame, gauge=None, label=None, *, wx_module=None, notifications=None,
                 clock=time.monotonic, start_timer=True):
        if wx_module is None:
            import wx as wx_module
        self.frame, self.wx = frame, wx_module
        self.clock, self.hub = clock, shared_hub(frame, clock=clock)
        self.library, self.reporter = frame.library, None
        self.root = Path(self.library.root)
        self.closed, self.session_quiet = False, False
        self._started, self._last_sequence, self._notice_error = None, None, ''
        self._players, self._holds, self._requests = {}, set(), set()
        self._voice_cancel = threading.Event()
        self._voice_executor = None
        self._ids, self._labels, self._prior_scope_ids = [], [], set()
        self._selected, self._explicit_selection = None, False
        self._gauge_value, self._indeterminate = 0, False
        self._busy_menus = self._busy_items = None
        self.command_items = []
        self.panel = self.operations = None
        if (gauge is None) != (label is None):
            raise ValueError('Supply both native progress controls or let the presenter create them.')
        if gauge is None:
            self._build_native()
        else:
            self.gauge, self.label = gauge, label
            self.text = label
        self.preferences, self.load_notice = self._load()
        self.notice = self.load_notice
        self.notifications = notifications or NotificationWorker(self.preferences, library_root=self.root)
        self.notifications.configure(self.preferences)
        self._applied_preferences = self.preferences
        self.gauge.SetName('Current operation progress; measured work or total unknown')
        self.label.SetName('Current operation status and measured work')
        self.gauge.SetRange(100)
        self.gauge.SetValue(0)
        self._set_text(_IDLE)
        self.timer = None
        if start_timer:
            owner = self.panel if self.panel is not None else frame
            self.timer = self.wx.Timer(owner)
            owner.Bind(self.wx.EVT_TIMER, self.tick, self.timer)
            self.timer.Start(200)
        if self.panel is not None:
            self.panel.Bind(self.wx.EVT_WINDOW_DESTROY, self._destroyed)
            frame.host.Layout()

    def _build_native(self):
        wx, frame = self.wx, self.frame
        self.panel = wx.Panel(frame.host)
        self.panel.SetName('Operation progress')
        layout = wx.BoxSizer(wx.VERTICAL)
        self.operations = wx.Choice(self.panel)
        self.operations.SetName('Operations and subtasks')
        self.operations.Bind(wx.EVT_CHOICE, self.select)
        layout.Add(self.operations, 0, wx.EXPAND | wx.ALL, 3)
        self.gauge = wx.Gauge(self.panel, range=100, style=wx.GA_HORIZONTAL)
        layout.Add(self.gauge, 0, wx.EXPAND | wx.ALL, 3)
        self.label = self.text = wx.TextCtrl(self.panel, style=wx.TE_READONLY | wx.TE_MULTILINE)
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
        quiet = wx.CheckBox(self.panel, label='Quiet now (this session)')
        quiet.SetName('Quiet progress feedback for this session')
        quiet.Bind(wx.EVT_CHECKBOX, lambda event: self.set_session_quiet(quiet.GetValue()))
        buttons.Add(quiet, 0, wx.ALL, 3)
        layout.Add(buttons, 0, wx.EXPAND)
        self.panel.SetSizer(layout)
        frame.layout.Insert(1, self.panel, 0, wx.EXPAND | wx.ALL, 3)

    def _set_text(self, value):
        if self.panel is not None:
            if self.text.GetValue() != value:
                self.text.ChangeValue(value)
        else:
            self.label.SetLabel(value)

    def _read_preferences(self):
        value, self.load_notice = self._load()
        self.notice = self.load_notice
        return value

    def _reload_root(self):
        if not self.scope_current:
            self.change_root()


    def _load(self):
        self._preferences_sha256 = 'unavailable'
        try:
            raw, self._preferences_sha256 = preference_snapshot(self.library.root)
            return (ProgressPreferences() if raw is None else decode_preferences(raw)), ''
        except (OSError, ValueError, RuntimeError) as exc:
            return ProgressPreferences(), 'Progress audio is Off: saved settings were kept and need review. ' + display_message(exc)

    @property
    def event(self):
        return self.reporter.event if self.reporter is not None else None

    @property
    def scope_current(self):
        return not self.closed and getattr(self.frame, 'library', None) is self.library

    def _matching(self, token):
        return (self.scope_current and self.reporter is not None
                and (token is None or token == self.reporter.operation_id))

    def change_root(self):
        if self.closed:
            return
        for request in tuple(self._requests):
            request.close()
        self._voice_cancel.set()
        self._voice_cancel = threading.Event()
        self.notifications.clear()
        self.notifications.set_registry(None)
        self.library = self.frame.library
        self.root = Path(self.library.root)
        bind_root = getattr(self.notifications, 'set_library_root', None)
        if callable(bind_root):
            bind_root(self.root)
        # A player or quarantined native handle may still own this hub's media
        # guard. Keep the authority and hide only the previous library's events.
        self._prior_scope_ids = {row.operation_id for row in self.hub.snapshot()}
        self.reporter = None
        self._selected, self._explicit_selection = None, False
        self._started, self._last_sequence, self._notice_error = None, None, ''
        self.preferences, self.load_notice = self._load()
        self.notice = self.load_notice
        self._applied_preferences = None
        self.gauge.SetValue(0)
        self._set_text(_IDLE)
        self._environment()


    def start(self, title, *, total=None, unit='items', phase=''):
        if self.closed:
            return None
        if not self.scope_current:
            self.change_root()
        self.notifications.clear()
        self.reporter = self.hub.start(display_message(title), total=total, unit=unit,
                                       phase=display_message(phase))
        self._selected, self._explicit_selection = self.reporter.operation_id, False
        self._started, self._last_sequence = self.clock(), None
        return self.reporter.event


    @contextmanager
    def bind(self, token):
        reporter = self.reporter if self._matching(token) else None
        if reporter is None:
            yield None
        else:
            with reporter.bind():
                yield reporter

    def update(self, completed=_UNSET, total=_UNSET, phase=None, *, token=None,
               message=None, unit=None):
        # This method is intentionally safe for an observer on the worker thread.
        if not self._matching(token) or self.event.terminal:
            return None
        values = {}
        if completed is not _UNSET:
            values['completed'] = completed
        if total is not _UNSET:
            values['total'] = total
            if total != self.event.total and self.event.total is not None:
                values['total_changed_reason'] = 'The service reported a revised measured work total.'
        if phase is not None:
            values['phase'] = display_message(phase)
        if message is not None:
            values['message'] = display_message(message)
        if unit is not None and unit != self.event.unit:
            raise ValueError('Start a new operation when its work unit changes.')
        return self.reporter.update(**values)

    def finish_result(self, result, *, cancelled=False, token=None):
        if not self._matching(token) or self.event.terminal:
            return None
        return finish_result(self.reporter, result, cancelled=cancelled)

    def fail(self, error, *, token=None):
        if self._matching(token) and not self.event.terminal:
            return self.reporter.fail(error)

    def cancel(self, message='The operation acknowledged cancellation.', *, token=None):
        if self._matching(token) and not self.event.terminal:
            return self.reporter.cancel(message)

    def pause(self, message='The operation saved its paused checkpoint.', *, token=None):
        if self._matching(token) and not self.event.terminal:
            return self.reporter.pause(message)

    def resume(self, *, token=None):
        if self._matching(token) and self.event.state == 'paused':
            return self.reporter.resume()

    def succeed(self, *, token=None):
        return self.finish_result({'state': 'complete'}, token=token)

    def request_cancel(self):
        if self._matching(None) and not self.event.terminal:
            self.reporter.update(phase='Stopping; waiting for the service result or saved checkpoint.')

    def _foreground(self):
        try:
            active = self.wx.GetActiveWindow()
            seen = set()
            while active is not None and id(active) not in seen:
                if active is self.frame:
                    return True
                seen.add(id(active))
                active = active.GetParent()
            return False
        except AttributeError:
            return bool(getattr(self.frame, 'IsActive', lambda: False)())
        except RuntimeError:
            return False

    def register_player(self, player):
        key = object()
        self._players[key] = weakref.ref(player)
        self._environment()
        def remove():
            self._players.pop(key, None)
            self._environment()
        return remove

    def _media_active(self):
        if self._holds or self.hub.media_active:
            return True
        for key, reference in tuple(self._players.items()):
            player = reference()
            if player is None:
                self._players.pop(key, None)
                continue
            try:
                state = player.state
            except Exception:
                return True  # A device with unknown status is not known silent.
            if state not in ('idle', 'stopped', 'completed', 'closed', 'paused'):
                return True
        return False

    def _environment(self):
        if not self.closed:
            # A content-start handoff always releases our output first, even
            # when periodic background notices during media were opted into.
            effective = (replace(self.preferences, quiet=True)
                         if self.session_quiet or self._holds else self.preferences)
            if effective != self._applied_preferences:
                self.notifications.configure(effective)
                self._applied_preferences = effective
            self.notifications.environment(foreground=self._foreground(),
                                           media_active=self._media_active())

    def hold_audio(self):
        return _AudioHold(self)

    def media_action(self, action, *, alive=lambda: True, failed=None, timeout=5):
        """Wait asynchronously for our cue to stop before starting content."""
        if self.closed:
            return None
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError('Content-start waiting must be bounded.')
        return _MediaRequest(self, action, alive=alive,
            failed=failed or (lambda exc: self.frame.report(str(exc))), timeout=timeout)

    def set_session_quiet(self, quiet):
        self.session_quiet = bool(quiet)
        self._environment()

    def configure(self, preferences, *, registry=None, expected_root=None,
                  expected_sha256=_CURRENT_PREFERENCES):
        if not isinstance(preferences, ProgressPreferences):
            raise ValueError('Use validated progress preferences.')
        if preferences.mode in ('speech', 'beep_speech') and not preferences.voice_id:
            raise ValueError('Choose an explicit local voice before enabling progress speech.')
        if not self.scope_current or (expected_root is not None and Path(expected_root) != self.root):
            raise ValueError('The library folder changed while progress settings were open.')
        observed = self._preferences_sha256 if expected_sha256 is _CURRENT_PREFERENCES else expected_sha256
        save_preferences(self.library.root, preferences, expected_sha256=observed)
        self._preferences_sha256 = sha256(encode_preferences(preferences)).hexdigest()
        self.preferences = preferences
        if registry is not None:
            self.notifications.set_registry(registry)
        self.notifications.clear()
        self._applied_preferences = None
        self._notice_error = self.notice = self.load_notice = ''
        self._environment()
        return preferences


    def voice_task(self, action, done, failed, *, alive):
        """Discovery/hash work is local, bounded and never runs in the wx loop."""
        if self._voice_executor is None:
            self._voice_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='progress-voice')
        scope, stop, deadline = self.library, self._voice_cancel, self.clock() + 20
        local_stop = threading.Event()
        cancelled = lambda: stop.is_set() or local_stop.is_set() or self.clock() >= deadline
        future = self._voice_executor.submit(lambda: action(cancelled))
        future.cancel_operation = local_stop.set
        def finished(result):
            def apply():
                if (self.closed or not self.scope_current or self.library is not scope
                        or stop.is_set() or local_stop.is_set() or not alive()):
                    return
                try:
                    value = result.result()
                    if cancelled():
                        raise TimeoutError('Local progress voice discovery exceeded its time limit.')
                    done(value)
                except Exception as exc:
                    failed(exc)
            self.wx.CallAfter(apply)
        future.add_done_callback(finished)
        return future

    def _events(self):
        return tuple(row for row in self.hub.snapshot()
                     if row.operation_id not in self._prior_scope_ids)

    def selected_event(self, events=None):
        events = self._events() if events is None else events
        selected = next((row for row in events if row.operation_id == self._selected), None)
        if self._explicit_selection and selected is not None:
            return selected
        if self.reporter is not None:
            current = next((row for row in events if row.operation_id == self.reporter.operation_id), None)
            if current is not None:
                return current
        active = [row for row in events if not row.terminal and row.parent_id is None]
        return max(active or list(events), key=lambda row: row.sequence, default=None)

    def select(self, event=None):
        if self.operations is not None:
            index = self.operations.GetSelection()
            if 0 <= index < len(self._ids):
                self._selected, self._explicit_selection = self._ids[index], True
                self.tick()

    def tick(self, event=None):
        if self.closed:
            return
        if not self.scope_current:
            self.change_root()
        self._environment()
        events = self._events()
        value = self.selected_event(events)
        if self.operations is not None:
            ids = [row.operation_id for row in events]
            labels = [('Subtask: ' if row.parent_id else '') + row.title + ' — ' + row.state for row in events]
            if ids != self._ids or labels != self._labels:
                self.operations.Set(labels)
                self._ids, self._labels = ids, labels
            if value is not None:
                self._selected = value.operation_id
                index = ids.index(self._selected)
                if self.operations.GetSelection() != index:
                    self.operations.SetSelection(index)
            self.operations.Enable(bool(events))
        indeterminate = value is not None and value.percent is None and value.state == 'running'
        measured = value.percent if value is not None and value.percent is not None else 0
        if indeterminate:
            self.gauge.Pulse()
        elif self._indeterminate or self._gauge_value != measured:
            self.gauge.SetValue(measured)
        self._indeterminate, self._gauge_value = indeterminate, measured
        self.gauge.SetName('Current operation progress; measured work: '
            + ('indeterminate; ' if indeterminate else '')
            + (value.state if value is not None else 'idle'))
        messages = [value.text if value is not None else _IDLE]
        if self.notice:
            messages.append(self.notice)
        if self.notifications.error:
            messages.append(self.notifications.error)
        if self.preferences.muted:
            messages.append('Extra progress feedback is muted.')
        self._set_text('\n'.join(messages))
        # A selected historical row never replays a terminal sound. Notices
        # follow only the current operation and its existing subtasks.
        current = self.reporter.operation_id if self.reporter is not None else (
            value.operation_id if value is not None else None)
        selected = {current} if current is not None else set()
        for _ in range(len(events)):
            children = {row.operation_id for row in events if row.parent_id in selected}
            if children <= selected:
                break
            selected.update(children)
        self.notifications.offer(tuple(row for row in events if row.operation_id in selected))
        # Automatic updates never focus, raise or announce a window themselves.

    def refresh(self, event=None):
        self.tick(event)

    def query(self, event=None):
        if not self.closed:
            self.tick()
            if self.panel is not None:
                self.text.SetInsertionPoint(0)
                self.text.SetFocus()
            else:
                self.read_status(event)


    def read_status(self, event=None):
        self.tick()
        value = self.event
        text = value.text if value is not None else _IDLE
        if self._started is not None and value is not None and not value.terminal:
            text += f' Elapsed {max(0, int(self.clock() - self._started))} seconds.'
        if self.notifications.error:
            text += ' ' + self.notifications.error
        self.frame.report(text)  # Explicit user query may move focus to the status text.

    def toggle_mute(self, event=None):
        self._reload_root()
        requested = replace(self.preferences, muted=not self.preferences.muted)
        if requested.muted:
            # A full disk must not prevent the requested immediate silence.
            self.notifications.configure(requested)
            self.preferences = requested
            try:
                save_preferences(self.root, requested, expected_sha256=self._preferences_sha256)
                self._preferences_sha256 = sha256(encode_preferences(requested)).hexdigest()
                self.notice = ''
            except (OSError, ValueError) as error:
                self.notice = 'Muted for this session; settings were not saved. ' + str(error)
        else:
            try:
                self.configure(requested)
            except (OSError, ValueError) as error:
                self.notice = 'Extra feedback remains muted; settings were not saved. ' + str(error)
        self.refresh()

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

    def settings(self, event=None):
        self._reload_root()
        focus = self.wx.Window.FindFocus()
        try:
            return show_progress_settings(self)
        finally:
            if focus:
                try:
                    focus.SetFocus()
                except RuntimeError:
                    pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        for request in tuple(self._requests):
            request.close()
        if self.timer is not None:
            self.timer.Stop()
        self._voice_cancel.set()
        self.notifications.close()
        if self._voice_executor is not None:
            self._voice_executor.shutdown(wait=False, cancel_futures=True)


def show_progress_settings(presenter):
    """Actual named native controls; Cancel publishes no preference changes."""
    wx, frame = presenter.wx, presenter.frame
    try:
        raw, observed = preference_snapshot(presenter.library.root)
    except (OSError, ValueError, RuntimeError) as error:
        frame.report('Saved progress settings could not be read and were kept. ' + display_message(error))
        return False
    notice = ''
    try:
        initial = ProgressPreferences() if raw is None else decode_preferences(raw)
    except (ValueError, TypeError):
        initial = ProgressPreferences()
        notice = 'Saved settings need review. Choose OK to replace them with these settings.'
    dialog = wx.Dialog(frame, title='Progress feedback', size=(620, 590),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    values = {}
    def text(label, value):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 5)
        field = wx.TextCtrl(dialog, value=str(value))
        field.SetName(label)
        layout.Add(field, 0, wx.EXPAND | wx.ALL, 5)
        return field
    layout.Add(wx.StaticText(dialog, label='Optional local progress audio'), 0, wx.ALL, 5)
    mode = wx.Choice(dialog, choices=['Off', 'Beep', 'Speech', 'Beep and speech'])
    mode.SetName('Progress audio mode')
    modes = ('off', 'beep', 'speech', 'beep_speech')
    mode.SetSelection(modes.index(initial.mode))
    layout.Add(mode, 0, wx.EXPAND | wx.ALL, 5)
    interval = text('Notification interval in seconds, 0.5 to 60', initial.interval_seconds)
    volume = text('Notification volume, 0 to 1', initial.volume)
    for key, label in (('muted', 'Mute all progress audio'), ('quiet', 'Quiet mode'),
                       ('background', 'Allow progress audio while this application is in the background'),
                       ('suppress_during_media', 'Suppress progress audio during playback or recording')):
        field = wx.CheckBox(dialog, label=label)
        field.SetName(label)
        field.SetValue(getattr(initial, key))
        values[key] = field
        layout.Add(field, 0, wx.ALL, 5)
    selected = [initial.voice_id, initial.voice_language, initial.voice_fingerprint]
    registry = [None]
    voice_label = wx.StaticText(dialog, label='Selected voice: ' + (initial.voice_id or 'none'))
    voice_label.SetName('Explicitly selected local progress voice and language')
    layout.Add(voice_label, 0, wx.EXPAND | wx.ALL, 5)
    choose = wx.Button(dialog, label='Choose local progress voice')
    choose.SetName('Choose local progress voice')
    layout.Add(choose, 0, wx.ALL, 5)
    status = wx.StaticText(dialog, label=notice or 'No voice is selected automatically. Settings apply only to progress feedback.')
    status.SetName('Progress settings status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 5)
    layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
    dialog.SetSizer(layout)
    scope, selected_root, active, futures = presenter.library, presenter.root, [True], []
    alive = lambda: (active[0] and not presenter.closed and presenter.scope_current
                     and presenter.library is scope and presenter.root == selected_root)
    def failure(exc):
        if alive():
            choose.Enable()
            status.SetLabel(display_message(exc))
    def choose_voice(event):
        choose.Disable()
        status.SetLabel('Discovering existing local voices; no downloads or provider requests.')
        def discover(cancelled):
            if cancelled():
                raise InterruptedError('Progress voice selection stopped.')
            current = configured_registry(selected_root)
            rows = [voice for voice in current.voices(cancelled=cancelled)
                    if voice.available and 'volume' in voice.controls
                    and any(language.lower().replace('_', '-').split('-')[0] in _LANGUAGES
                            for language in voice.languages)]
            return current, rows
        def loaded(value):
            if not alive():
                return
            current, rows = value
            choose.Enable()
            if not rows:
                status.SetLabel('No compatible local English, Ukrainian or Slovak voice is available.')
                return
            selector = wx.SingleChoiceDialog(dialog, 'Choose the exact local voice to use for short notices.',
                'Local progress voice', [f'{voice.name} — {", ".join(voice.languages)} — {voice.id}' for voice in rows])
            selector.SetName('Explicit local progress voice')
            try:
                if selector.ShowModal() != wx.ID_OK:
                    return
                voice = rows[selector.GetSelection()]
            finally:
                selector.Destroy()
            if not alive():
                return
            languages = tuple(language for language in _LANGUAGES
                              if language in {item.lower().replace('_', '-').split('-')[0] for item in voice.languages})
            language_selector = wx.SingleChoiceDialog(dialog, 'Choose the language of progress notices.',
                'Progress notice language', [_LANGUAGES[language] for language in languages])
            language_selector.SetName('Progress notice language')
            try:
                if language_selector.ShowModal() != wx.ID_OK:
                    return
                language = languages[language_selector.GetSelection()]
            finally:
                language_selector.Destroy()
            if not alive():
                return
            choose.Disable()
            status.SetLabel('Checking the selected voice and local runtime identity.')
            def verified(fingerprint):
                selected[:] = (voice.id, language, fingerprint)
                registry[0] = current
                choose.Enable()
                voice_label.SetLabel('Selected voice: ' + voice.id + ' — ' + _LANGUAGES[language])
                status.SetLabel('Voice checked. Choose OK to save; Cancel keeps the previous settings.')
            futures.append(presenter.voice_task(
                lambda cancelled: voice_fingerprint(current, voice.id, language, cancelled=cancelled),
                verified, failure, alive=alive))
        futures.append(presenter.voice_task(discover, loaded, failure, alive=alive))
    choose.Bind(wx.EVT_BUTTON, choose_voice)
    def save(event):
        try:
            if not alive():
                raise ValueError('The library changed while progress settings were open.')
            preferences = ProgressPreferences(mode=modes[mode.GetSelection()],
                interval_seconds=float(interval.GetValue()), volume=float(volume.GetValue()),
                **{key: field.GetValue() for key, field in values.items()},
                voice_id=selected[0], voice_language=selected[1], voice_fingerprint=selected[2])
            presenter.configure(preferences, registry=registry[0], expected_sha256=observed)
        except (ValueError, OSError, RuntimeError) as exc:
            status.SetLabel(display_message(exc))
            return
        dialog.EndModal(wx.ID_OK)
    dialog.Bind(wx.EVT_BUTTON, save, id=wx.ID_OK)
    try:
        mode.SetFocus()
        return dialog.ShowModal() == wx.ID_OK
    finally:
        active[0] = False
        for future in futures:
            future.cancel_operation()
            future.cancel()
        dialog.Destroy()


def install_operation_progress(frame):
    existing = getattr(frame, 'operation_progress', None)
    if existing is not None and not existing.closed:
        return existing
    import wx
    bar = frame.GetMenuBar()
    options = next((bar.GetMenu(index) for index in range(bar.GetMenuCount())
                    if bar.GetMenuLabel(index).replace('&', '').strip().casefold() == 'options'), None)
    if options is None:
        raise ValueError('Install keyboard Options before operation progress.')
    presenter = OperationProgress(frame, wx_module=wx)
    for spec, handler in zip(COMMANDS, (presenter.query, presenter.settings, presenter.toggle_mute)):
        caption = spec.label + ('\t' + spec.default_shortcut if spec.default_shortcut else '')
        item = options.Append(wx.ID_ANY, caption)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, spec, item)
        presenter.command_items.append(item)
    frame.operation_progress = presenter
    # Compatibility name points to the same controller, never another view.
    frame.progress_view = presenter
    frame.Layout()
    if presenter.load_notice:
        frame.report(presenter.load_notice)
    return presenter
