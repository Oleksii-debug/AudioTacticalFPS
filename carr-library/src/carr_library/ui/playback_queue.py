"""Native retained playback queue over the existing WavePlayer and Save All."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import math

from ..audio.jobs import checked_directory
from ..audio.player import WavePlayer
from ..audio.playlist import prepare_playlist, retain_playlist
from ..audio.playlist_session import PlaylistSession
from ..security.admission import admit_new_operation, operation_context
from .save_all import SaveCoordinator, SaveResult, coordinator_for, defer_close


class PlaybackQueueDialog:
    def __init__(self, frame, *, initial_paths=(), on_ready=None):
        if (getattr(frame, '_closing', False) or getattr(frame, 'working', False)
                or getattr(frame, 'busy', False)):
            raise ValueError('Finish the current operation before opening the playback queue.')
        if on_ready is not None and not callable(on_ready):
            raise ValueError('A callable playback queue observer is required.')
        if type(initial_paths) not in (tuple, list) or len(initial_paths) > 256:
            raise ValueError('Choose at most 256 explicit Studio recordings.')
        self.frame, self.library = frame, frame.library
        self.root = checked_directory(self.library.root)
        info = self.root.stat()
        self.root_identity = info.st_dev, info.st_ino
        self.trust_controller = getattr(self.library, 'trust_controller', None)
        self.initial_paths = tuple(initial_paths)
        self.on_ready = on_ready
        self.guard()
        import wx
        self.wx = wx
        self.closed = self.destroyed = self.showing = False
        self.closing = self.closing_worker = self.stopping = self.settling = False
        self.future = self.media_request = self.prepared = None
        self.executor = self.player = self.session = self.timer = None
        self.unregister_player = self.unregister_save = lambda: None
        self.cancelled, self.serial = Event(), 0
        self.dialog = wx.Dialog(frame, title='Saved playback queue',
            size=(800, 600), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            self.dialog.SetName('Retained playback queue')
            self._build()
            self.player = WavePlayer()
            self.session = PlaylistSession(self.root, self.player)
            from .operation_progress import operation_progress_for
            self.progress = operation_progress_for(frame)
            if self.progress is not None:
                self.unregister_player = self.progress.register_player(self.player)
            self.coordinator = coordinator_for(frame) or SaveCoordinator()
            self.dialog._save_coordinator = self.coordinator
            self.unregister_save = self.coordinator.register(
                'playback-queue.' + str(id(self)), 'Saved playback queue',
                dirty=lambda: not self.closed and self.session.dirty,
                save=self.save_draft, focus=self.queue.SetFocus)
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='retained-playback')
            self.timer = wx.Timer(self.dialog)
            self.dialog.Bind(wx.EVT_TIMER, self.tick, self.timer)
            self.timer.Start(100)
            self._display()
            self._say('Saved order and position are loaded passively. Review saved recordings, '
                      'then choose Play. Adding files retains independent copies; Save keeps this queue.')
            self._enable()
        except BaseException:
            if self.timer is not None:
                self.timer.Stop()
            if self.player is not None:
                self.player.close()
            self.unregister_player()
            self.unregister_save()
            if self.executor is not None:
                self.executor.shutdown(wait=False, cancel_futures=True)
            self.dialog.Destroy()
            raise

    def guard(self):
        path = checked_directory(self.library.root)
        info = path.stat()
        if (self.frame.library is not self.library or getattr(self.frame, '_closing', False)
                or path != self.root or (info.st_dev, info.st_ino) != self.root_identity
                or getattr(self.library, 'trust_controller', None) is not self.trust_controller):
            raise ValueError('The library changed or is closing. Reopen the saved playback queue.')

    def _build(self):
        wx = self.wx
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self.dialog, label=
            'Independent retained PCM recordings — original files remain unchanged'), 0, wx.ALL, 5)
        self.queue = wx.ListBox(self.dialog, choices=[])
        self.queue.SetName('Saved playback queue order and selection')
        self.queue.Bind(wx.EVT_LISTBOX, self.select)
        layout.Add(self.queue, 1, wx.EXPAND | wx.ALL, 5)
        cursor = wx.BoxSizer(wx.HORIZONTAL)
        cursor.Add(wx.StaticText(self.dialog, label='Selected recording position, seconds'), 0, wx.ALL, 4)
        self.seconds = wx.TextCtrl(self.dialog, value='0')
        self.seconds.SetName('Retained playback position in seconds')
        cursor.Add(self.seconds, 1, wx.EXPAND | wx.ALL, 4)
        layout.Add(cursor, 0, wx.EXPAND | wx.ALL, 2)
        self.status = wx.TextCtrl(self.dialog, value='', style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Saved queue status and cursor accuracy')
        self.status.SetMinSize((-1, 100))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 5)
        self.controls = {}
        row = wx.WrapSizer(wx.HORIZONTAL)
        for key, label, handler in (
                ('review', '&Review / reload saved recordings', self.review),
                ('add', '&Add local WAV or RF64...', self.add),
                ('studio', 'Retain selected Studio recordings', self.add_studio),
                ('remove', '&Remove selected', self.remove),
                ('up', 'Move up', lambda event: self.move(-1)),
                ('down', 'Move down', lambda event: self.move(1)),
                ('clear', 'Clear queue', self.clear),
                ('seek', '&Set saved position', self.seek),
                ('back', 'Back 10 seconds', lambda event: self.skip(-1)),
                ('forward', 'Forward 30 seconds', lambda event: self.skip(1)),
                ('play', '&Play retained queue', self.play),
                ('pause', 'P&ause', self.pause),
                ('resume', 'Resume paused audio', self.resume),
                ('stop', '&Stop / cancel', self.stop),
                ('save', '&Save queue', self.save),
                ('save_all', 'Save &all open drafts', self.save_all),
                ('close', '&Close queue', self.close)):
            button = wx.Button(self.dialog, id=wx.ID_CLOSE if key == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, handler)
            self.controls[key] = button
            row.Add(button, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 3)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CLOSE)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)

    def _say(self, message):
        if not self.closed:
            self.status.ChangeValue(str(message))

    def _error(self, error):
        self._say(str(error))
        self._enable()

    def _stopped(self):
        self.guard()
        return self.cancelled.is_set() or self.closed

    def _idle(self):
        return not self.closed and self.future is None and not self.closing and not self.stopping

    def _enable(self):
        if self.closed:
            return
        idle = self._idle() and self.prepared is None
        ready = idle and self.session is not None and self.session.reviewed
        selected = ready and self.session.value.index is not None
        active = self.session is not None and self.session.admission is not None
        for key in ('review', 'clear', 'save_all'):
            self.controls[key].Enable(idle)
        self.controls['add'].Enable(ready)
        self.controls['studio'].Enable(ready and bool(self.initial_paths))
        for key in ('remove', 'up', 'down', 'seek', 'back', 'forward'):
            self.controls[key].Enable(selected)
        self.controls['play'].Enable(selected and not active)
        self.controls['pause'].Enable(idle and active and self.player.native is not None)
        self.controls['resume'].Enable(idle and active and self.player.native is not None)
        self.controls['save'].Enable(ready)
        self.controls['stop'].Enable(not self.closing)
        self.controls['close'].Enable(not self.closing)
        self.queue.Enable(ready)
        self.seconds.Enable(selected)

    def _display(self):
        value = self.session.value
        for key, label, seconds in (('back', 'Back', value.skips[0]), ('forward', 'Forward', value.skips[1])):
            text = label + ' ' + format(seconds, 'g') + ' seconds'
            self.controls[key].SetLabel(text)
            self.controls[key].SetName(text)
        self.queue.SetItems([str(i + 1) + '. ' + row.label + ' (' +
            format(row.frames / row.sample_rate, '.3f') + ' s)' for i, row in enumerate(value.items)])
        if value.index is not None:
            self.queue.SetSelection(value.index)
            self.seconds.ChangeValue(format(value.frame / value.items[value.index].sample_rate, '.6f'))
        else:
            self.seconds.ChangeValue('0')

    def _work(self, action, done, *, dispose=None, control=False, failed=None, committed=False):
        if self.closed or self.future is not None:
            return False
        def authorized():
            if control:
                return action()  # Cleanup must remain available after authority is revoked.
            with operation_context(self.trust_controller):
                self.guard()
                admit_new_operation(self.trust_controller, 'data.read')
                return action()
        future = self.executor.submit(authorized)
        self.future = future
        self._enable()
        def receive(completed):
            def apply():
                if self.future is not completed:
                    return
                self.future = None
                result = None
                try:
                    result = completed.result()
                    if not control:
                        self.guard()
                        if not committed and (self.closing or self.stopping or self.cancelled.is_set()):
                            if dispose is not None:
                                dispose(result)
                            result = None
                            return
                    done(result)
                    result = None
                except Exception as exc:
                    if result is not None and dispose is not None:
                        dispose(result)
                    (failed or self._error)(exc)
                finally:
                    self._enable()
                    if self.closing and not self.closing_worker and self.future is None:
                        self._begin_close()
                    elif self.stopping and not self.settling and self.future is None:
                        self._begin_stop()
            self.wx.CallAfter(apply)
        future.add_done_callback(receive)
        return True

    def _cancel_media(self):
        self.serial += 1
        if self.media_request is not None:
            self.media_request.close()
            self.media_request = None
        prepared, self.prepared = self.prepared, None
        if prepared is not None and self.session.admission is not prepared:
            prepared.close()

    def _discard_ok(self):
        if not self.session.dirty:
            return True
        return self.wx.MessageBox('Discard the unsaved queue changes and reload the saved queue?',
            'Unsaved playback queue', self.wx.YES_NO | self.wx.NO_DEFAULT | self.wx.ICON_QUESTION,
            self.dialog) == self.wx.YES

    def review(self, event=None):
        if not self._idle() or not self._discard_ok():
            return
        try:
            self.guard()
            self._cancel_media()
            self.cancelled.clear()
            self._say('Reviewing the exact independent retained recordings...')
            def run():
                self.session.stop()
                return self.session.review(cancelled=self._stopped)
            def done(result):
                self.session.adopt(result)
                self._display()
                self._say('Saved recordings verified. Selection and cursor are restored; Play stays a separate action.')
            self._work(run, done)
        except Exception as exc:
            self._error(exc)

    def add_paths(self, paths):
        if not self._idle() or not self.session.reviewed:
            return False
        try:
            self.guard()
            self._cancel_media()
            selected = tuple(paths)
            self.cancelled.clear()
            self._say('Verifying and retaining independent copies of the selected recordings...')
            def run():
                self.session.stop()
                reviewed = prepare_playlist(selected, cancelled=self._stopped)
                return retain_playlist(self.root, reviewed, cancelled=self._stopped)
            def done(rows):
                self.session.append(rows)
                self._display()
                self._say('Retained recordings added to this draft. Save the queue to keep its order and selection.')
            return self._work(run, done)
        except Exception as exc:
            self._error(exc)
            return False

    def add(self, event=None):
        if not self._idle():
            return
        wx = self.wx
        dialog = wx.FileDialog(self.dialog, 'Add explicit local PCM recordings',
            wildcard='PCM WAV or RF64 (*.wav;*.rf64)|*.wav;*.rf64',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.add_paths(tuple(dialog.GetPaths()))
        finally:
            dialog.Destroy()

    def add_studio(self, event=None):
        if self.initial_paths:
            self.add_paths(self.initial_paths)

    def _edit(self, action):
        if not self._idle():
            return
        try:
            self.guard()
            self._cancel_media()
            action()
            self._display()
            self._say('Queue draft changed. Playback is stopped; Save retains this selection.')
            self._enable()
        except Exception as exc:
            self._error(exc)

    def select(self, event=None):
        index = self.queue.GetSelection()
        if index >= 0:
            self._edit(lambda: self.session.select(index))

    def remove(self, event=None):
        index = self.queue.GetSelection()
        self._edit(lambda: self.session.remove(index))

    def move(self, direction):
        index = self.queue.GetSelection()
        self._edit(lambda: self.session.move(index, direction))

    def clear(self, event=None):
        self._edit(self.session.clear)

    def seek(self, event=None):
        def apply():
            index = self.session.value.index
            seconds = float(self.seconds.GetValue())
            if index is None or not math.isfinite(seconds):
                raise ValueError('Select a recording and a finite position within it.')
            row = self.session.value.items[index]
            if not 0 <= seconds <= row.frames / row.sample_rate:
                raise ValueError('Choose a position within the selected recording.')
            self.session.select(index, frame=min(row.frames, round(seconds * row.sample_rate)))
        self._edit(apply)

    def skip(self, direction):
        def apply():
            self.session.capture_position()
            value = self.session.value
            if value.index is None:
                raise ValueError('Select a recording before moving the saved cursor.')
            row = value.items[value.index]
            delta = -value.skips[0] if direction < 0 else value.skips[1]
            frame = min(row.frames, max(0, value.frame + round(delta * row.sample_rate)))
            self.session.select(value.index, frame=frame)
        self._edit(apply)

    def play(self, event=None):
        if not self._idle() or self.session.admission is not None:
            return
        try:
            self.guard()
            self._cancel_media()
            self.cancelled.clear()
            serial = self.serial
            def alive():
                return (not self.closed and not self.closing and not self.stopping
                        and not self.cancelled.is_set() and self.serial == serial)
            def prepared(value):
                self.prepared = value
                def begin():
                    if not alive():
                        self._cancel_media()
                        return
                    try:
                        self.guard()
                        with operation_context(self.trust_controller):
                            admit_new_operation(self.trust_controller, 'data.read')
                            self.session.start(value)
                        self.prepared = None
                        self._say('Retained queue playback started. Save or Stop retains an observed native cursor; '
                                  'an external player cannot report an exact cursor.')
                        self._enable()
                    except Exception as exc:
                        # start() retains the same owner if native cleanup failed.
                        if self.session.admission is not value:
                            value.close()
                        self.prepared = None
                        self._error(exc)
                if self.progress is None:
                    begin()
                else:
                    def refused(error):
                        self._cancel_media()
                        self._error(error)
                    self.media_request = self.progress.media_action(begin, alive=alive, failed=refused)
                self._enable()
            self._say('Verifying all retained bytes before the explicit Play action...')
            self._work(lambda: self.session.prepare_play(cancelled=self._stopped),
                       prepared, dispose=lambda value: value.close())
        except Exception as exc:
            self._error(exc)

    def pause(self, event=None):
        if self._idle():
            try:
                self.session.pause()
                self._display()
                self._say('Paused. The observed position remains an unsaved queue draft.')
            except Exception as exc:
                self._error(exc)

    def resume(self, event=None):
        if self._idle():
            try:
                self.guard()
                with operation_context(self.trust_controller):
                    admit_new_operation(self.trust_controller, 'data.read')
                    self.session.resume()
                self._say('Resumed the current explicitly admitted playback.')
            except Exception as exc:
                self._error(exc)

    def tick(self, event=None):
        if not self._idle():
            return
        if self.prepared is not None:
            try:
                self.guard()
                # The shared progress request can close on a changed scope
                # without invoking either action or failure. Its UI owner must
                # still release the unstarted review's held input readers.
                if getattr(self.media_request, 'closed', False):
                    self._cancel_media()
                    self._say('Playback was not started; the retained queue draft is unchanged.')
                    self._enable()
            except Exception as exc:
                self._cancel_media()
                self._error(exc)
            return
        if self.session.admission is None:
            return
        try:
            self.guard()
            previous = self.session.value.index
            self.session.poll()
            if previous != self.session.value.index:
                self._display()
            self._enable()
        except Exception as exc:
            self._error(exc)
            self.stop()

    def stop(self, event=None):
        if self.closed:
            return
        self.cancelled.set()
        self._cancel_media()
        self.stopping = True
        self._say('Stopping audio and waiting for the owned review or copy to settle...')
        self._enable()
        if self.future is None and not self.settling:
            self._begin_stop()

    def _begin_stop(self):
        self.settling = True
        def done(_):
            self.settling = self.stopping = False
            self._display()
            self._say('Stopped. The observed cursor is retained in this draft; Save keeps it across reopening.')
        def failed(error):
            self.settling = self.stopping = False
            self._error(error)
        self._work(self.session.stop, done, control=True, failed=failed)

    def save_draft(self, completed, *, closing=False):
        if (self.closed or self.future is not None or self.stopping or self.prepared is not None
                or self.closing and not closing):
            completed(SaveResult(False, 'The queue has an active operation; its draft is retained.'))
            return
        if not self.session.reviewed:
            completed(SaveResult(False, 'Review the retained saved recordings before saving.'))
            return
        try:
            self.guard()
            self._cancel_media()
            self.cancelled.clear()
            def run():
                value, token = self.session.snapshot()
                return self.session.store.save(value, expected_token=token, cancelled=self._stopped)
            def saved(receipt):
                self.session.acknowledge(receipt)
                self._display()
                exact = receipt.confirmed and not self.session.dirty
                message = receipt.warning or ('Queue and cursor saved.' if exact else
                    'The snapshot was saved; newer queue changes remain unsaved.')
                self._say(message)
                completed(SaveResult(exact, message))
            def failed(error):
                self._error(error)
                completed(SaveResult(False, str(error)))
            # A completed publication still needs its receipt when Cancel arrives
            # after commit; it must not be reported as an unwritten cancellation.
            self._work(run, saved, failed=failed, committed=True)
        except Exception as exc:
            self._error(exc)
            completed(SaveResult(False, str(exc)))

    def save(self, event=None):
        self.save_draft(lambda result: self._say(result.message))

    def save_all(self, event=None):
        if not self.coordinator.save_all(lambda summary: self._say(summary.message()),
                                         cancelled=lambda: self.closed):
            self._say('Save All is already waiting for actual save completions.')

    def close(self, event=None):
        if self.closed:
            return
        if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
            event.Veto()
        if defer_close(self, self.coordinator, close=self.close, report=self._say,
                       alive=lambda: not self.closed, post=self.wx.CallAfter, event=event):
            return
        self.closing = True
        self.cancelled.set()
        self._cancel_media()
        self._enable()
        if self.future is None and not self.closing_worker:
            self._begin_close()

    def _close_failed(self, error):
        self.closing = self.closing_worker = self.stopping = self.settling = False
        self._error(error)

    def _begin_close(self):
        self.closing_worker = True
        def stopped(_):
            self.stopping = self.settling = False
            self._ask_close()
        self._work(self.session.stop, stopped, control=True, failed=self._close_failed)

    def _ask_close(self):
        if self.session.dirty:
            wx = self.wx
            dialog = wx.MessageDialog(self.dialog, 'Save the current queue and observed cursor before closing?',
                'Unsaved playback queue', wx.YES_NO | wx.CANCEL | wx.CANCEL_DEFAULT | wx.ICON_QUESTION)
            try:
                answer = dialog.ShowModal()
            finally:
                dialog.Destroy()
            if answer == wx.ID_CANCEL:
                self.closing = self.closing_worker = False
                self._enable()
                return
            if answer == wx.ID_YES:
                def saved(result):
                    if result.saved and not self.session.dirty:
                        self._close_owner()
                    else:
                        self._close_failed(result.message)
                self.save_draft(saved, closing=True)
                return
        self._close_owner()

    def _close_owner(self):
        self._work(self.session.close, lambda _: self._finish_close(), control=True, failed=self._close_failed)

    def _finish_close(self):
        self.unregister_player()
        self.unregister_save()
        self.timer.Stop()
        self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CLOSE)
        elif not self.showing:
            self._destroy()

    def _destroy(self):
        if not self.destroyed:
            self.dialog.Destroy()
            self.destroyed = True

    def show(self):
        self.showing = True
        try:
            self.guard()
            if self.on_ready is not None:
                self.wx.CallAfter(self.on_ready, self)
            self.dialog.ShowModal()
        finally:
            self.showing = False
            if not self.closed:
                self.close()
            if self.closed:
                self._destroy()


def show_playback_queue(frame, *, initial_paths=(), on_ready=None):
    return PlaybackQueueDialog(frame, initial_paths=initial_paths, on_ready=on_ready).show()


def install_playback_queue_actions(frame):
    existing = getattr(frame, 'playback_queue_action', None)
    if existing is not None:
        return existing
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    index = frame.menu_bar.FindMenu('Library')
    if index < 0:
        raise ValueError('Install the Library menu before the saved playback queue.')
    def open_queue(event=None):
        try:
            return show_playback_queue(frame)
        except Exception as exc:
            frame.report('The saved playback queue could not open: ' + str(exc))
    item = frame.menu_bar.GetMenu(index).Append(wx.ID_ANY, 'Saved playback queue...')
    frame.Bind(wx.EVT_MENU, open_queue, item)
    register_menu_command(frame, CommandSpec('audio.saved_queue', 'Saved playback queue', 'main'), item)
    frame.playback_queue_action = open_queue
    return open_queue
