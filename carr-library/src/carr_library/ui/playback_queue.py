"""Completion-aware queue saves on the existing Audio Studio worker."""
from ..audio.managed import ManagedProjects
from ..audio.playlist import PlaylistStore, QueueChangeCommittedError
from ..audio.playback_processing import PlaybackControls
from .save_all import SaveResult


class StudioQueue:
    def __init__(self, owner):
        self.owner = owner
        self.library = getattr(getattr(owner, 'parent', None), 'library', None)
        self.key = None
        self.store = self.review = None
        self._save_warning = None
        self.cursor = (None, None, 'not_started')
        self.controls = PlaybackControls()
        self.unregister = lambda: None

    def bind(self):
        controller = self.owner.controller
        key = (controller.project_id, controller.project_path)
        if key != self.key:
            changed = self.key is not None and self.key[0] != key[0]
            self.key, self.store, self.review = key, None, None
            self._save_warning = None
            self.cursor = (None, None, 'not_started')
            self.controls = PlaybackControls()
            if changed:
                self.owner.playback.set_playlist([])
                self.owner._refresh_playback_choices()

    def _current(self):
        if self.owner.closed or (self.library is not None and
                getattr(getattr(self.owner, 'parent', None), 'library', None) is not self.library):
            raise InterruptedError('The library changed. Close this queue window and reopen its saved project.')

    def _accept_committed(self, error, *, reset=False):
        """Retain an exact receipt without another write or device activation."""
        if (not isinstance(error, QueueChangeCommittedError)
                or error.review.owner is not self.store):
            return False
        try:
            self._current()
        except InterruptedError:
            return False
        self.review = error.review
        self._save_warning = error
        if reset:
            state = error.review.state
            self.cursor = (state.index, state.frame, state.accuracy)
            self.controls = state.controls
        return True

    def _cancelled(self):
        self._current()
        return self.owner.cancelled.is_set()

    def _managed(self):
        self._current()
        controller = self.owner.controller
        if controller.project_path is None:
            if not self.owner.save():
                raise ValueError('Save the current Audio Studio project before retaining a playlist.')
        manager = ManagedProjects(controller.private_root)
        if manager.locate(controller.project_path, controller.project_id) is None:
            raise ValueError('Durable playlists use a saved library project. Save a library copy first.')
        self.bind()
        if self.store is None:
            self.store = PlaylistStore(controller.private_root, controller.project_id)
        return self.store

    def register(self, coordinator):
        def focus():
            try:
                self._current()
                window = getattr(self.owner, 'dialog', None)
                deleted = getattr(window, 'IsBeingDeleted', None)
                if window is not None and (not bool(window) or
                        (callable(deleted) and deleted())):
                    return
            except (InterruptedError, RuntimeError, ReferenceError):
                return
            self.owner.playlist_choice.SetFocus()
        self.unregister = coordinator.register(
            'studio.queue.' + str(id(self)), 'Audio Studio playback queue',
            dirty=lambda: not self.owner.closed and self.dirty(),
            save=self.save_for_all, focus=focus)

    def observe(self):
        if self.review is None:
            return
        player = self.owner.playback
        index, status = player.playlist_index, player.status()
        paths = tuple(self.store.audio / 'assets' / item.asset for item in self.review.state.items)
        if (tuple(player.playlist) != paths or type(index) is not int
                or not 0 <= index < len(paths) or player.path != paths[index]
                or status.state not in ('playing', 'starting', 'paused', 'completed')):
            return
        if status.position_accuracy in ('driver_reported', 'completed_buffers'):
            self.cursor = (index, status.position_frames, status.position_accuracy)
        elif status.position_accuracy == 'external_player':
            self.cursor = (index, None, 'external_player')

    def dirty(self):
        self.observe()
        if self.review is None:
            return False
        state = self.review.state
        return (self._save_warning is not None or
                self.cursor != (state.index, state.frame, state.accuracy) or self.controls != state.controls)

    def _apply(self, review, *, install=False):
        self._current()
        if install:
            paths = self.store.paths(review)
            self.owner.playback.set_playlist(paths)
            self._current()
        self.review = review
        self._save_warning = None
        if install:
            self.cursor = (review.state.index, review.state.frame, review.state.accuracy)
            self.controls = review.state.controls
            self.owner._refresh_playback_choices()
            if review.state.index is not None:
                self.owner.playlist_choice.SetSelection(review.state.index)

    def append(self, paths):
        try:
            store = self._managed()
            self.observe()
            self.save_now()
            review = self.review
            def saved(value):
                self._apply(value, install=True)
                self.owner._message(
                    f'Saved {len(value.state.items)} retained recordings. '
                    'Choose Play selected recording or Resume saved position.', focus=True)
            self.owner._run('Retaining verified playlist recordings',
                lambda: store.append(paths, review=review, cancelled=self._cancelled), saved,
                failed=lambda error: self._accept_committed(error, reset=True))
        except Exception as error:
            self.owner._error(error)

    def restore(self, event=None, *, resume=False):
        if self.owner.busy or self.owner.closed:
            return
        try:
            store = self._managed()
            selected = self.owner.playback_device.GetSelection()
            ids = self.owner.playback_device_ids
            device = ids[selected] if 0 <= selected < len(ids) else None
            def restored(value):
                self._apply(value, install=True)
                if resume:
                    state = value.state
                    if state.index is None or state.frame is None:
                        raise ValueError('No measured cursor is saved. Choose a recording and Play from its beginning.')
                    self.owner._playback_action(lambda: self.owner.playback.play_index(
                        state.index, device_id=device,
                        start_seconds=state.frame / state.items[state.index].sample_rate))
                    if state.accuracy == 'historical_cursor':
                        self.owner._message('Resuming the historical saved coordinate; it is not a newly '
                                            'measured playback position.', focus=True)
                else:
                    self.owner._message(
                        'Saved queue verified and restored in a stopped state. '
                        'Choose the current output device and an explicit Play or Resume action.' +
                        (' The saved position is a historical coordinate, not new device evidence.'
                         if value.state.accuracy == 'historical_cursor' else ''), focus=True)
            self.owner._run('Verifying the retained playback queue',
                lambda: store.review(cancelled=self._cancelled), restored)
        except Exception as error:
            self.owner._error(error)

    def clear(self, event=None):
        try:
            store = self._managed()
            review = self.review
            def clear():
                current = review or store.review(cancelled=self._cancelled)
                return store.checkpoint(current, clear=True, cancelled=self._cancelled)
            self.owner._run('Saving an empty playback queue', clear,
                            lambda value: self._apply(value, install=True),
                            failed=lambda error: self._accept_committed(error, reset=True))
        except Exception as error:
            self.owner._error(error)

    def _save(self, completed=None, *, quiet=False):
        if self.owner.closed or self.owner.busy:
            if completed is not None:
                completed(SaveResult(False, 'The playback queue has a running operation; keep its window open.'))
            return False
        try:
            self._current()
        except Exception as error:
            if completed is not None:
                completed(SaveResult(False, str(error)))
            else:
                self.owner._error(error)
            return False
        if self._save_warning is not None:
            if completed is not None:
                completed(SaveResult(False, str(self._save_warning)))
            elif not quiet:
                self.owner._error(self._save_warning)
            return False
        self.observe()
        if self.review is None:
            if completed is not None:
                completed(SaveResult(True))
            return True
        review, cursor, controls = self.review, self.cursor, self.controls
        replied = [False]
        def reply(result):
            if completed is not None and not replied[0]:
                replied[0] = True
                completed(result)
        def saved(value):
            self._apply(value)
            if not quiet:
                self.owner._message('Playback queue, measured position and playback settings saved.', focus=True)
            reply(SaveResult(True))
        def failed(error):
            self._accept_committed(error)
            reply(SaveResult(False, str(error)))
        return self.owner._run('Saving playback queue and measured position',
            lambda: self.store.checkpoint(review, index=cursor[0], frame=cursor[1], accuracy=cursor[2],
                                         controls=controls, cancelled=self._cancelled),
            saved, failed=failed, stop_playback=False, quiet=quiet)

    def save(self, event=None):
        return self._save()

    def save_for_all(self, completed):
        return self._save(completed)

    def autosave(self):
        if not self.owner.busy and self._save_warning is None and self.dirty():
            self._save(quiet=True)

    def save_now(self, *, closing=False):
        """Bounded metadata only, before switching/closing this project."""
        self.observe()
        if not self.dirty():
            return True
        try:
            self._current()
        except InterruptedError:
            if not closing:
                raise
            message = ('The library changed. The last saved queue remains intact; '
                       'the unsaved playback position was not written.')
            report = getattr(getattr(self.owner, 'parent', None), 'report', None)
            if callable(report):
                report(message)
            else:
                self.owner._message(message)
            return False
        if self._save_warning is not None:
            if closing:
                report = getattr(getattr(self.owner, 'parent', None), 'report', None)
                if callable(report):
                    report(str(self._save_warning))
                else:
                    self.owner._message(str(self._save_warning))
                return False
            raise self._save_warning
        self.owner.cancelled.clear()
        try:
            self.review = self.store.checkpoint(self.review, index=self.cursor[0],
                frame=self.cursor[1], accuracy=self.cursor[2], controls=self.controls,
                cancelled=self._cancelled)
        except QueueChangeCommittedError as error:
            self._accept_committed(error)
            raise
        return True


def install_queue_controls(owner, page, layout):
    manager = StudioQueue(owner)
    row = owner.wx.WrapSizer(owner.wx.HORIZONTAL)
    for label, action in (
        ('Save queue and position', manager.save),
        ('Restore saved queue', manager.restore),
        ('Resume saved position', lambda event: manager.restore(event, resume=True))):
        owner._button(page, row, label, action)
    layout.Add(row, 0, owner.wx.ALL, 5)
    from .legacy_playlist import install_legacy_queue_controls
    install_legacy_queue_controls(owner, page, layout)
    return manager
