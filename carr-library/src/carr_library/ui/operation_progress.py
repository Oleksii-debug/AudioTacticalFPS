"""Native progress presentation over the shared measured ProgressHub.

The presenter does not decide whether work committed: it observes the current
service result. Optional notification output owns a separate short-lived player.
No source text, rendered master or user recording is altered.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import threading
import time
import weakref

from ..progress import (ProgressHub, ProgressPreferences, display_message,
                        finish_result, load_preferences, save_preferences)
from ..progress_audio import (NotificationWorker, configured_registry,
                              voice_fingerprint)

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
    def __init__(self, presenter, *, recording=False):
        self.presenter, self.closed, self.recording = presenter, False, recording
        self.token = object()
        presenter._holds.add(self.token)
        if recording:
            presenter._recording_holds.add(self.token)
        try:
            presenter._environment()
        except BaseException:
            presenter._holds.discard(self.token)
            presenter._recording_holds.discard(self.token)
            raise

    @property
    def ready(self):
        return (not self.closed and not self.presenter.closed
                and self.presenter.notifications.quiescent
                and (not self.recording or self.presenter._players_quiescent()))

    def close(self):
        if not self.closed:
            self.closed = True
            def release():
                self.presenter._holds.discard(self.token)
                self.presenter._recording_holds.discard(self.token)
                self.presenter._environment()
            wx = self.presenter.wx
            is_main = getattr(wx, 'IsMainThread',
                              lambda: threading.current_thread() is threading.main_thread())
            if is_main():
                release()
            else:
                # Native cleanup callbacks run off the wx thread. Retain the
                # quiet token until the existing event loop observes quiescence.
                try:
                    wx.CallAfter(release)
                except RuntimeError:
                    pass  # A destroyed host cannot safely resume notifications.



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
        if self.presenter._recording_holds:
            self.close()
            self.failed(RuntimeError('Stop the microphone recording and confirm cleanup before starting playback.'))
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
    def __init__(self, frame, gauge, label, *, wx_module, notifications=None,
                 clock=time.monotonic, start_timer=True):
        self.frame, self.gauge, self.label, self.wx = frame, gauge, label, wx_module
        self.clock, self.hub = clock, ProgressHub(clock=clock)
        self.library, self.reporter = frame.library, None
        self.closed, self.session_quiet = False, False
        self._started, self._last_sequence, self._notice_error = None, None, ''
        self._players, self._holds, self._requests = {}, set(), set()
        self._recording_holds = set()
        self._voice_cancel = threading.Event()
        self._voice_executor = None
        self.preferences, self.load_notice = self._load()
        self.notifications = notifications or NotificationWorker(self.preferences)
        self.notifications.configure(self.preferences)
        self._applied_preferences = self.preferences
        self.gauge.SetName('Current operation progress; measured work or total unknown')
        self.label.SetName('Current operation status and measured work')
        self.gauge.SetRange(100)
        self.gauge.SetValue(0)
        self.label.SetLabel(_IDLE)
        self.timer = None
        if start_timer:
            self.timer = self.wx.Timer(frame)
            frame.Bind(self.wx.EVT_TIMER, self.tick, self.timer)
            self.timer.Start(250)

    def _load(self):
        try:
            from ..legacy_ui_preferences import migrate_progress
            notice = migrate_progress(self.library.root)
            return load_preferences(self.library.root), notice
        except (OSError, ValueError, RuntimeError) as exc:
            return ProgressPreferences(), 'Progress audio is Off: saved settings need review. ' + display_message(exc)

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
        self.hub, self.reporter = ProgressHub(clock=self.clock), None
        self._started, self._last_sequence, self._notice_error = None, None, ''
        self.preferences, self.load_notice = self._load()
        self._applied_preferences = None
        self.gauge.SetValue(0)
        self.label.SetLabel(_IDLE)
        self._environment()

    def start(self, title, *, total=None, unit='items', phase=''):
        if self.closed:
            return None
        if not self.scope_current:
            self.change_root()  # Called from the native start action, never a worker.
        self.notifications.clear()
        self.reporter = self.hub.start(display_message(title), total=total, unit=unit,
                                       phase=display_message(phase))
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
        except (AttributeError, RuntimeError):
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

    def _players_quiescent(self):
        """Recording needs known stopped output, not an inferred idle timer."""
        for key, reference in tuple(self._players.items()):
            player = reference()
            if player is None:
                self._players.pop(key, None)
                continue
            try:
                if player.state not in ('idle', 'stopped', 'completed', 'closed'):
                    return False
            except Exception:
                return False
        return True

    def hold_audio(self, *, recording=False):
        if type(recording) is not bool:
            raise ValueError('The recording quiet reservation must be explicit.')
        if self.closed:
            raise RuntimeError('The progress host has closed.')
        if recording and (self._recording_holds or self._requests):
            raise RuntimeError('Another recording or playback handoff is active. Stop it before recording.')
        return _AudioHold(self, recording=recording)

    def media_action(self, action, *, alive=lambda: True, failed=None, timeout=5):
        """Wait asynchronously for our cue to stop before starting content."""
        if self.closed:
            return None
        if self._recording_holds:
            (failed or (lambda exc: self.frame.report(str(exc))))(
                RuntimeError('Stop the microphone recording and confirm cleanup before starting playback.'))
            return None
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError('Content-start waiting must be bounded.')
        return _MediaRequest(self, action, alive=alive,
            failed=failed or (lambda exc: self.frame.report(str(exc))), timeout=timeout)

    def set_session_quiet(self, quiet):
        self.session_quiet = bool(quiet)
        self._environment()

    def configure(self, preferences, *, registry=None):
        if not isinstance(preferences, ProgressPreferences):
            raise ValueError('Use validated progress preferences.')
        if preferences.mode in ('speech', 'beep_speech') and not preferences.voice_id:
            raise ValueError('Choose an explicit local voice before enabling progress speech.')
        if not self.scope_current:
            raise ValueError('The library changed while progress settings were open.')
        save_preferences(self.library.root, preferences)
        self.preferences = preferences
        if registry is not None:
            self.notifications.set_registry(registry)
        self.notifications.clear()
        self._applied_preferences = None
        self._notice_error = ''
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

    def tick(self, event=None):
        if self.closed:
            return
        if not self.scope_current:
            self.change_root()
        self._environment()
        value = self.event
        if value is not None:
            if value.percent is None and value.state == 'running':
                self.gauge.Pulse()
            elif value.percent is not None:
                self.gauge.SetValue(value.percent)
            else:
                self.gauge.SetValue(0)
            if value.sequence != self._last_sequence:
                self.label.SetLabel(value.text)
                self._last_sequence = value.sequence
            selected = {value.operation_id}
            snapshot = self.hub.snapshot()
            for _ in range(len(snapshot)):
                children = {row.operation_id for row in snapshot if row.parent_id in selected}
                if children <= selected:
                    break
                selected.update(children)
            self.notifications.offer(tuple(row for row in snapshot if row.operation_id in selected))
        error = self.notifications.error
        if error and error != self._notice_error:
            self._notice_error = error
            self.label.SetLabel((value.text + ' ' if value is not None else '') + error)

    def read_status(self, event=None):
        self.tick()
        value = self.event
        text = value.text if value is not None else _IDLE
        if self._started is not None and value is not None and not value.terminal:
            text += f' Elapsed {max(0, int(self.clock() - self._started))} seconds.'
        if self.notifications.error:
            text += ' ' + self.notifications.error
        self.frame.report(text)  # Explicit user query may move focus to the status text.

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
    wx, frame, initial = presenter.wx, presenter.frame, presenter.preferences
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
    interval = text('Notification interval in seconds, 0.5 to 120', initial.interval_seconds)
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
    status = wx.StaticText(dialog, label='No voice is selected automatically. Settings apply only to progress feedback.')
    status.SetName('Progress settings status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 5)
    layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
    dialog.SetSizer(layout)
    scope, active, futures = presenter.library, [True], []
    alive = lambda: active[0] and not presenter.closed and presenter.library is scope
    def failure(exc):
        if alive():
            choose.Enable()
            status.SetLabel(display_message(exc))
    def choose_voice(event):
        choose.Disable()
        status.SetLabel('Discovering existing local voices; no downloads or provider requests.')
        def discover(cancelled):
            current = configured_registry()
            rows = [voice for voice in current.voices(cancelled=cancelled)
                    if voice.available and 'volume' in voice.controls
                    and any(language.lower().replace('_', '-').split('-')[0] in _LANGUAGES
                            for language in voice.languages)]
            return current, rows
        def loaded(value):
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
            presenter.configure(preferences, registry=registry[0])
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
    import wx
    panel = wx.Panel(frame.host)
    layout = wx.BoxSizer(wx.VERTICAL)
    label = wx.StaticText(panel, label=_IDLE)
    gauge = wx.Gauge(panel, range=100)
    layout.Add(label, 0, wx.EXPAND | wx.ALL, 3)
    layout.Add(gauge, 0, wx.EXPAND | wx.ALL, 3)
    actions = wx.BoxSizer(wx.HORIZONTAL)
    query = wx.Button(panel, label='Read current progress')
    query.SetName('Read current operation progress')
    quiet = wx.CheckBox(panel, label='Quiet now (this session)')
    quiet.SetName('Quiet progress feedback for this session')
    actions.Add(query, 0, wx.ALL, 3)
    actions.Add(quiet, 0, wx.ALL, 3)
    layout.Add(actions, 0, wx.ALL, 1)
    panel.SetSizer(layout)
    frame.layout.Insert(1, panel, 0, wx.EXPAND | wx.ALL, 3)
    presenter = OperationProgress(frame, gauge, label, wx_module=wx)
    frame.operation_progress = presenter
    query.Bind(wx.EVT_BUTTON, presenter.read_status)
    quiet.Bind(wx.EVT_CHECKBOX, lambda event: presenter.set_session_quiet(quiet.GetValue()))
    help_index = frame.menu_bar.FindMenu('Help')
    if help_index < 0:
        help_index = frame.menu_bar.GetMenuCount() - 1
    menu = frame.menu_bar.GetMenu(help_index)
    item = menu.Append(wx.ID_ANY, 'Current operation progress\tCtrl+Alt+P')
    frame.Bind(wx.EVT_MENU, presenter.read_status, item)
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec('progress.current', 'Current operation progress',
                                             'main', 'Ctrl+Alt+P'), item)
    settings = menu.Append(wx.ID_ANY, 'Progress feedback settings...')
    frame.Bind(wx.EVT_MENU, lambda event: show_progress_settings(presenter), settings)
    register_menu_command(frame, CommandSpec('progress.settings', 'Progress feedback settings', 'main'), settings)
    frame.Layout()
    if presenter.load_notice:
        frame.report(presenter.load_notice)
    return presenter
