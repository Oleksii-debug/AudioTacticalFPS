"""Accessible playlist controls borrowing the existing playback host and worker.

The session holds only passive queue state and GUI admission tokens. It never
creates a player, executor, discovery service, native timer or process owner.
"""
from pathlib import Path
import threading

from ..speech.playlist import PlaylistStore, SavedPlaylist, prepare_playlist, retain_playlist


class PlaylistSession:
    def __init__(self, host, library, *, submit, retain, guard, busy=lambda: False):
        self.host, self.library = host, library
        self.root = Path(library.root)
        self.submit, self.retain, self.guard, self.busy = submit, retain, guard, busy
        self.store = PlaylistStore(self.root)
        self.saved, self.token = self.store.load()
        self.pending_saved = self.saved
        self.pending = None
        self.closed = False
        self.epoch = 0
        self.view = None
        self.notice = ('Saved queue awaits explicit media review. No audio or device was opened.'
                       if self.pending_saved is not None else
                       'Add local PCM WAV files, review their retained copies, then play explicitly.')

    @property
    def player(self):
        return self.host.controller.player

    def check(self, *, idle=False):
        if self.closed or self.host.closed or not self.host.controller.alive():
            raise ValueError('This playback owner is closed.')
        self.guard()
        self.store.guard()
        if idle and (self.pending is not None or self.busy()):
            raise ValueError('Wait for the current Studio operation or cancel its review.')

    def active(self, view):
        return not self.closed and self.view is view and view.alive and not self.host.closed

    def announce(self, message=None):
        if message is not None:
            self.notice = str(message)
        view = self.view
        if view is not None and self.active(view):
            try:
                self.check()
                view.refresh()
            except (Exception, KeyboardInterrupt):
                return  # Feedback cannot reverse a completed saved document.

    def detach(self, view):
        if self.view is view:
            self.view = None
            if self.pending is not None:
                self.pending.set()
            self.epoch += 1

    def close(self):
        self.closed = True
        self.epoch += 1
        if self.pending is not None:
            self.pending.set()
        self.view = None

    def cancel_review(self):
        if self.pending is not None:
            self.pending.set()
            self.announce('Cancellation requested. Wait for the existing Studio worker to finish.')

    def _job(self, title, work, done, *, committed=False):
        self.check(idle=True)
        view, epoch = self.view, self.epoch
        if view is None or not self.active(view):
            raise ValueError('Reopen the playback queue before reviewing files.')
        cancel = threading.Event()
        self.pending = cancel
        self.announce(title + '…')

        def stopped():
            return (cancel.is_set() or self.closed or self.host.closed
                    or self.epoch != epoch or not self.active(view))

        def operation():
            self.check()
            if stopped():
                raise InterruptedError('The playlist review was cancelled before admission.')
            return work(stopped)

        def finish(value):
            if self.pending is cancel:
                self.pending = None
            if committed:
                # The saved document survives a later modal close. The callback
                # changes only passive state; announce itself checks the view.
                done(value)
            elif not stopped():
                self.check()
                done(value)
            self.announce()

        def failed(error):
            if self.pending is cancel:
                self.pending = None
            if not stopped():
                self.announce(str(error))

        try:
            accepted = self.submit(title, operation, finish, failed)
            if accepted is not True and self.pending is cancel:
                cancel.set()
                self.pending = None
                self.announce('The existing Studio worker did not admit this operation. Retry explicitly.')
        except (Exception, KeyboardInterrupt) as error:
            if self.pending is cancel:
                self.pending = None
            cancel.set()
            self.announce(str(error))
            return False
        return accepted is True or self.pending is None and not cancel.is_set()

    def add(self, paths):
        self.check(idle=True)
        chosen = tuple(paths)
        from ..speech.playlist import MAX_ITEMS
        previous = tuple(self.player.playlist)
        if self.pending_saved is not None:
            previous = ()
        if not chosen or len(chosen) + len(previous) > MAX_ITEMS:
            raise ValueError('Choose files within the 256-item queue limit.')
        self.host.controller.stop_current()
        def work(stopped):
            reviewed = prepare_playlist(chosen, cancelled=stopped)
            return retain_playlist(self.root, reviewed, retain=self.retain, cancelled=stopped)
        def done(items):
            self.player.set_playlist(previous + tuple(items))
            self.pending_saved = None
            self.epoch += 1
            self.announce('Reviewed copies added in order. Start the queue explicitly; Save keeps it for another session.')
        return self._job('Review and retain selected playlist audio', work, done)

    def review_saved(self, *, restore_controls=False):
        self.check(idle=True)
        if type(restore_controls) is not bool:
            raise ValueError('Choose the saved playback controls explicitly.')
        saved, token = self.pending_saved, self.token
        if saved is None:
            raise ValueError('There is no unreviewed saved queue in this session.')
        self.host.controller.stop_current()
        if restore_controls and (saved.skips[0] != saved.skips[1] or not 1 <= saved.skips[0] <= 3600):
            raise ValueError('This queue uses different skip intervals. Review it with current controls explicitly.')
        def work(stopped):
            current, current_token = self.store.load()
            if current != saved or current_token != token:
                raise ValueError('The saved queue changed. Reload it before reviewing.')
            items = saved.review(self.root, cancelled=stopped)
            current, current_token = self.store.load()
            if current != saved or current_token != token:
                raise ValueError('The saved queue changed during review.')
            return items
        def done(items):
            current, current_token = self.store.load()
            if current != saved or current_token != token:
                raise ValueError('The saved queue changed before adoption. Reload it.')
            saved.apply(self.player, items, use_current_controls=not restore_controls)
            if restore_controls:
                self.host.controller.set_skip(saved.skips[0])
            self.pending_saved = None
            self.epoch += 1
            self.announce('Saved media and original-frame position reviewed. No playback started.')
        return self._job('Review retained saved playlist', work, done)

    def reload_saved(self):
        self.check(idle=True)
        self.saved, self.token = self.store.load()
        self.pending_saved = self.saved
        self.epoch += 1
        self.announce('Saved queue loaded as passive data. Review its retained files before playback.'
                      if self.saved is not None else 'No saved queue exists in this library.')

    def save(self):
        self.check(idle=True)
        if self.pending_saved is not None:
            raise ValueError('Review the saved queue or add a new selection before replacing it.')
        value = SavedPlaylist.capture(self.root, self.player,
            skips=(self.host.controller.skip_seconds, self.host.controller.skip_seconds))
        token = self.token
        def work(stopped):
            if stopped():
                raise InterruptedError('Queue save was cancelled before publication.')
            saved_token = self.store.save(value, expected_token=token)
            return {'status': 'saved', 'token': saved_token, 'value': value,
                    'notice': self.store.notice or 'Playback queue and original-frame position saved.'}
        def done(receipt):
            self.saved, self.token = receipt['value'], receipt['token']
            self.announce(receipt['notice'])
        return self._job('Save the reviewed playback queue', work, done, committed=True)

    def select(self, index):
        self.check(idle=True)
        if self.pending_saved is not None:
            raise ValueError('Review the saved queue before selecting its recording.')
        self.host.controller.stop_current()
        self.player.select_queue(index)
        self.epoch += 1
        self.announce('Queue item selected silently. Start playback explicitly.')

    def change_order(self, index, direction):
        self.check(idle=True)
        if self.pending_saved is not None:
            raise ValueError('Review the saved queue before changing its order.')
        if type(index) is not int or not 0 <= index < len(self.player.playlist):
            raise ValueError('Choose a queue item first.')
        items = list(self.player.playlist)
        if direction is None:
            del items[index]
        elif type(direction) is int and direction in (-1, 1):
            destination = index + direction
            if not 0 <= destination < len(items):
                return
            items[index], items[destination] = items[destination], items[index]
        else:
            raise ValueError('Choose remove, move up or move down.')
        self.host.controller.stop_current()
        self.player.set_playlist(items)
        self.epoch += 1
        self.announce('Queue order changed silently. Save to keep this order.')

    def play(self, index=None):
        self.check(idle=True)
        if self.pending_saved is not None:
            raise ValueError('Review the saved queue before playback.')
        generation, token, epoch = self.host.controller.generation(), self.player.queue_token, self.epoch
        def current():
            if self.closed or self.host.closed or epoch != self.epoch:
                return False
            try:
                self.check(idle=True)
                return generation == self.host.controller.generation() and token == self.player.queue_token
            except (Exception, KeyboardInterrupt):
                return False
        def begin():
            if not current():
                return
            self.host.on_play()
            self.player.play_queue(index)
            if not self.host.controller._current():
                self.host.bind()
            self.announce('Playing the reviewed queue. Natural endings continue in order until Pause, Stop or the sleep timer.')
        self.host.controller.dispatch(begin, alive=current,
            on_error=lambda error: self.announce('Playback did not start: ' + str(error)))

    def move(self, direction):
        self.check(idle=True)
        if self.pending_saved is not None:
            raise ValueError('Review the saved queue before changing its selection.')
        self.host.controller.stop_current()
        if self.player.move_queue(direction):
            self.epoch += 1
            self.announce('Queue item selected silently. Start playback explicitly.')

    def pause(self):
        self.check()
        self.player.pause()
        self.announce('Queue paused. Play queue explicitly resumes this item and its later transitions.')

    def stop(self):
        self.cancel_review()
        self.host.controller.stop_current()
        self.epoch += 1
        self.announce('Stop requested; pending queue transitions are withdrawn.')


def create_playlist_dialog(session, *, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class QueueDialog(wx.Dialog):
        def __init__(self):
            super().__init__(session.host.owner, title='Playback queue', size=(780, 720),
                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive, self._destroy_requested = True, False
            self._native_id = self.GetId()
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            self.Bind(wx.EVT_CLOSE, self.close)
            body = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
            body.SetScrollRate(0, 20)
            body.SetMinSize((500, 320))
            layout = wx.BoxSizer(wx.VERTICAL)
            intro = wx.StaticText(body, label=(
                'Add local PCM WAV files. Review makes exact retained copies. '
                'Selection, order changes and restored queues stay silent until Play queue. '
                'Previous and Next select silently. Save keeps the queue and original-frame position.'))
            intro.Wrap(690)
            layout.Add(intro, 0, wx.ALL | wx.EXPAND, 8)
            self.items = wx.ListBox(body, size=(-1, 180))
            self.items.SetName('Playback queue items in order')
            layout.Add(self.items, 1, wx.ALL | wx.EXPAND, 8)
            self.restore_controls = wx.CheckBox(body, label='Restore saved playback controls as well as the queue')
            self.restore_controls.SetName('Explicitly restore saved playback controls')
            layout.Add(self.restore_controls, 0, wx.ALL, 8)
            self.buttons = {}
            actions = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            actions.AddGrowableCol(0); actions.AddGrowableCol(1)
            rows = (
                ('add', 'Add and review WAV files…', self.add),
                ('review', 'Review saved queue', lambda: session.review_saved(
                    restore_controls=self.restore_controls.GetValue())),
                ('reload', 'Reload saved queue as data', session.reload_saved),
                ('save', 'Save playback queue', session.save),
                ('select', 'Select item silently', lambda: session.select(self.items.GetSelection())),
                ('remove', 'Remove selected item', lambda: session.change_order(self.items.GetSelection(), None)),
                ('up', 'Move item up', lambda: session.change_order(self.items.GetSelection(), -1)),
                ('down', 'Move item down', lambda: session.change_order(self.items.GetSelection(), 1)),
                ('previous', 'Previous queue item', lambda: session.move(-1)),
                ('next', 'Next queue item', lambda: session.move(1)),
                ('play', 'Play queue', lambda: session.play(None)),
                ('pause', 'Pause queue', session.pause),
                ('stop', 'Stop queue', session.stop),
                ('settings', 'Playback speed, pitch and levels…', session.host.show_settings),
            )
            for key, label, action in rows:
                button = wx.Button(body, label=label)
                button.SetName(label)
                button.Bind(wx.EVT_BUTTON, lambda event, action=action: self.perform(action))
                self.buttons[key] = button
                actions.Add(button, 0, wx.EXPAND)
            layout.Add(actions, 0, wx.ALL | wx.EXPAND, 8)
            self.status = wx.TextCtrl(body, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
            self.status.SetName('Playback queue status and retained source')
            layout.Add(self.status, 0, wx.ALL | wx.EXPAND, 8)
            body.SetSizer(layout)
            shell = wx.BoxSizer(wx.VERTICAL)
            shell.Add(body, 1, wx.EXPAND)
            bottom = wx.BoxSizer(wx.HORIZONTAL)
            self.cancel_review = wx.Button(self, label='Cancel current review')
            self.cancel_review.SetName('Cancel playlist review')
            self.cancel_review.Bind(wx.EVT_BUTTON, lambda event: self.perform(session.cancel_review))
            close = wx.Button(self, wx.ID_CLOSE, 'Close queue controls')
            close.SetName('Close playlist controls')
            close.Bind(wx.EVT_BUTTON, self.close)
            bottom.Add(self.cancel_review, 0, wx.ALL, 6)
            bottom.Add(close, 0, wx.ALL, 6)
            shell.Add(bottom, 0, wx.ALIGN_RIGHT)
            self.SetSizer(shell)
            self.SetMinSize((650, 540))
            self.SetEscapeId(wx.ID_CLOSE)
            self.body = body
            session.view = self
            self.refresh()
            self.items.SetFocus()

        def current(self):
            return self.alive and session.active(self)

        def refresh(self):
            if not self.current():
                return
            selected = self.items.GetSelection()
            pending_saved = session.pending_saved
            rows = pending_saved.items if pending_saved is not None else session.player.playlist
            labels = [f'{number}. {item.label or getattr(item, "display_name", "")}'
                      for number, item in enumerate(rows, 1)]
            self.items.Set(labels)
            if labels:
                current = session.player.playlist_index
                index = current if pending_saved is None and current is not None else selected
                self.items.SetSelection(min(max(index, 0), len(labels)-1))
            busy = session.pending is not None or session.busy()
            for key, button in self.buttons.items():
                button.Enable(key in ('pause', 'stop') or not busy)
            for key in ('select', 'remove', 'up', 'down', 'previous', 'next', 'play'):
                self.buttons[key].Enable(not busy and pending_saved is None and bool(rows))
            self.buttons['review'].Enable(not busy and pending_saved is not None)
            self.buttons['save'].Enable(not busy and pending_saved is None)
            self.restore_controls.Enable(not busy and pending_saved is not None)
            self.cancel_review.Enable(session.pending is not None)
            current = session.player.playlist_index
            selection = ('No active queue item.' if current is None else
                         f'Current queue item {current+1} of {len(session.player.playlist)}.')
            self.status.ChangeValue(session.notice + '\n' + selection)

        def perform(self, action):
            if not self.current():
                return
            try:
                session.check()
                action()
                if self.current():
                    self.refresh()
            except (Exception, KeyboardInterrupt) as error:
                if self.current():
                    session.announce(str(error))
                    self.status.SetFocus()

        def add(self):
            if not self.current():
                return
            session.check(idle=True)
            chooser = wx.FileDialog(self, 'Choose PCM WAV files in playback order',
                wildcard='PCM WAV files (*.wav)|*.wav', style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST)
            try:
                accepted = chooser.ShowModal()
                if not self.current():
                    return
                session.check(idle=True)
                if accepted != wx.ID_OK:
                    return
                paths = tuple(chooser.GetPaths())
            finally:
                try:
                    if chooser and not chooser.IsBeingDeleted():
                        chooser.Destroy()
                except RuntimeError:
                    pass
            if self.current():
                session.add(paths)

        def dispose(self):
            if self.alive:
                self.alive = False
                session.detach(self)

        def destroyed(self, event):
            if event.GetId() == self._native_id:
                self.dispose()
            event.Skip()

        def close(self, event=None):
            if not self.current():
                return
            self.dispose()
            if self.IsModal():
                self.EndModal(wx.ID_CLOSE)

        def Destroy(self):
            self.dispose()
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

    return QueueDialog()


def show_playlist(session):
    if session.view is not None and session.view.alive:
        session.view.Raise()
        return
    session.check()
    dialog = create_playlist_dialog(session, wx_module=session.host.wx)
    try:
        dialog.ShowModal()
    finally:
        dialog.dispose()
        try:
            if dialog and not dialog.IsBeingDeleted():
                dialog.Destroy()
        except RuntimeError:
            pass
