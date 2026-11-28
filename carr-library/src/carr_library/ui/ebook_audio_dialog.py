"""Native recorded-overlay transport opened from the existing Reading Desk."""
from pathlib import Path
import threading

from ..reader.ebook_audio import EbookAudioSession
from ..audio.cleanup import pending_cleanup
from ..audio.models import AudioCancelled, AudioCleanupPending
from ..speech.player import WavePlayer
from ..speech.progress_speech import play_when_quiet
from .ebook_model import native_position


class _ObservedTransport:
    """A quiet/recording stop also invalidates this owner's pending playback."""
    def __init__(self, owner):
        self.owner = owner

    @property
    def state(self):
        return 'closing' if self.owner.cleanup_pending else self.owner.player.state

    def close(self):
        self.owner.stop()
        self.owner.interrupted = True

    stop = close


class OverlayPlayback:
    """Generation-bound native playback; late preparations never start audio."""
    def __init__(self, session, player):
        self.session, self.player = session, player
        self.index = self.generation = 0
        self.prepared = None
        self.closed = self.active = False
        self.interrupted = False
        self.cleanup_pending = False
        self.observed = _ObservedTransport(self)

    @property
    def ticket(self):
        return self.index, self.generation

    def select(self, index):
        if type(index) is not int or not 0 <= index < len(self.session.chapter.cues):
            raise ValueError('Choose a recorded EPUB segment.')
        self.stop()
        self.index = index

    def start(self, prepared, ticket):
        if self.closed or ticket != self.ticket or prepared.cue.index != self.index:
            prepared.close()
            return False
        try:
            self.session.start_check(prepared)
            self.player.play(prepared.path)
        except BaseException:
            # Retain the recording if the native device cannot confirm closure.
            self.prepared = prepared
            self.stop()
            raise
        self.prepared, self.active = prepared, True
        self.interrupted = False
        return True

    def pause(self):
        self.session.guard()
        return self.player.pause()

    def resume(self):
        if self.prepared is None:
            raise ValueError('Play a recorded segment before resuming.')
        self.session.start_check(self.prepared)
        return self.player.resume()

    def next_after_eof(self):
        if not self.active:
            return None
        self.session.start_check(self.prepared)
        state = self.player.state
        if state == 'error':
            raise RuntimeError('The recorded-audio device stopped with an error.')
        if state == 'stopped':
            following = self.index + 1
            self.stop()
            return following
        return None

    def stop(self):
        self.generation += 1
        self.active = False
        try:
            self.player.close()
        except BaseException:
            self.cleanup_pending = True
            raise
        self.cleanup_pending = False
        if self.prepared is not None:
            self.prepared.close()
            self.prepared = None

    def close(self):
        self.stop()
        self.closed = True
        self.session.close()


class EbookAudioDialog:
    """Accessible chapter/fragment transport; opening never starts playback."""
    def __init__(self, parent, desk, ordinal, guard, *, host=None, progress=None,
                 session_factory=EbookAudioSession, player_factory=WavePlayer):
        import wx
        self.host = parent if host is None else host
        if not callable(getattr(self.host, 'run_job', None)):
            raise ValueError('Recorded EPUB preparation requires the current host background worker.')
        self.wx, self.guard = wx, guard
        self.progress = progress if progress is not None else getattr(self.host, 'progress_panel', None)
        self.desk, self.ordinal = desk, ordinal
        self.library, self.library_root = desk.library, Path(desk.library.root)
        self._check_scope()
        self.session_factory, self.player_factory = session_factory, player_factory
        self.dialog = wx.Dialog(parent, title='EPUB synchronized recorded audio', size=(900, 730),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.cancelled = threading.Event()
        self.closed, self.busy = False, False
        self._closing = self._destroyed = False
        self._future = None
        self._take_result = lambda: None
        self._release_result = lambda: None
        self._cleanup_owners = []
        self._last_error = None
        self.session = self.transport = self.player = None
        self._pending = None
        layout = wx.BoxSizer(wx.VERTICAL)
        self.description = wx.StaticText(self.dialog, label=(
            'Recorded audio from the imported EPUB. Synchronization follows the author’s SMIL fragments, '
            'which may be words, sentences or larger passages. Extracted text remains unverified.'))
        self.description.Wrap(840)
        layout.Add(self.description, 0, wx.EXPAND | wx.ALL, 8)
        self.segments = wx.ListBox(self.dialog)
        self.segments.SetName('Recorded EPUB segments in source order')
        self.segments.Bind(wx.EVT_LISTBOX, self.select)
        layout.Add(self.segments, 0, wx.EXPAND | wx.ALL, 6)
        self.text = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.text.SetName('Exact imported section with synchronized source fragment selected')
        layout.Add(self.text, 1, wx.EXPAND | wx.ALL, 6)
        controls = wx.WrapSizer(wx.HORIZONTAL)
        self.buttons = []
        for label, handler in (
                ('Previous recorded segment', lambda _: self.move(-1)),
                ('Next recorded segment', lambda _: self.move(1)),
                ('Play from selected segment', self.play), ('Pause recorded audio', self.pause),
                ('Resume recorded audio', self.resume), ('Stop recorded audio', self.stop),
                ('Where am I in recorded audio?', self.where)):
            button = wx.Button(self.dialog, label=label)
            button.SetName(label); button.Bind(wx.EVT_BUTTON, handler)
            controls.Add(button, 0, wx.ALL, 3); self.buttons.append(button)
        layout.Add(controls, 0, wx.EXPAND | wx.ALL, 4)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Recorded EPUB playback status and limitations')
        self.status.SetMinSize((-1, 90)); layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 6)
        close = wx.Button(self.dialog, wx.ID_CANCEL, 'Close recorded audio')
        close.Bind(wx.EVT_BUTTON, self.close)
        layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 6)
        self.dialog.SetSizer(layout); self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.timer = wx.Timer(self.dialog)
        self.dialog.Bind(wx.EVT_TIMER, self.tick, self.timer)
        self._enable(False)

    def _enable(self, available):
        # Segment selection and Stop remain usable while a clip is preparing;
        # changing selection cancels and invalidates that pending playback.
        for index, button in enumerate(self.buttons):
            button.Enable(available and (not self.busy or index in (0, 1, 5, 6)))

    def report(self, message, *, focus=False):
        if not self.closed and not self._destroyed:
            self.status.ChangeValue(str(message))
            if focus:
                self.status.SetFocus()

    def _can_present(self):
        # Source availability is checked separately: an archived source in the
        # same view must still stop playback and explain why it is unavailable.
        return (not self.closed and not self._closing and not self._destroyed
                and not getattr(self.host, '_closing', False)
                and self.host.library is self.library
                and Path(self.host.library.root) == self.library_root)

    def _error(self, error):
        if not self.closed and not self._destroyed:
            self.timer.Stop()
        try:
            self.stop(notify=self._can_present())
        except Exception as cleanup:
            error = f'{error}\nThe audio device has not confirmed closure: {cleanup}'
        if self._can_present():
            self.report(error, focus=True)

    def _check_scope(self):
        self.guard()
        if (getattr(self.host, '_closing', False)
                or self.host.library is not self.library
                or Path(self.host.library.root) != self.library_root):
            raise ValueError('The active library changed. Reopen recorded EPUB audio.')

    def stopping(self):
        return (self.cancelled.is_set() or self._closing or self.closed
                or getattr(self.host, '_closing', False)
                or self.host.cancel_event.is_set()
                or self.host.library is not self.library
                or Path(self.host.library.root) != self.library_root)

    def _remember_cleanup(self, owner):
        if owner not in self._cleanup_owners:
            self._cleanup_owners.append(owner)

    def _discard(self, value, discard):
        try:
            discard(value)
        except AudioCleanupPending as error:
            self._last_error = error
            self._remember_cleanup(error.cleanup_owner)
        except Exception as error:
            # Preserve the actual owner/result if a filesystem or native close
            # cannot yet finish. Use the existing cleanup lifetime, never a
            # detached replacement process or a private executor.
            self._last_error = error
            pending = pending_cleanup(lambda: discard(value),
                message='Recorded EPUB cleanup is pending; its temporary files remain owned.')
            self._remember_cleanup(pending.cleanup_owner)

    def _submit(self, operation, complete, discard, *, title='Preparing recorded EPUB audio'):
        if self.busy or self.closed or self._closing:
            return False
        self._check_scope()
        if getattr(self.host, 'working', False):
            self.report('Another application operation is active. Wait or cancel it before preparing recorded audio.')
            return False
        self.busy = True
        self.cancelled.clear()
        self._enable(self.transport is not None)
        held, lock = {}, threading.Lock()

        def take():
            with lock:
                return held.pop('value', None)
        self._take_result = take

        def release():
            value = take()
            if value is not None:
                self._discard(value, discard)

        self._release_result = release

        def work():
            self._check_scope()
            if self.stopping():
                raise InterruptedError('Recorded EPUB preparation was cancelled before it started.')
            try:
                value = operation()
            except AudioCancelled as error:
                if self.stopping():
                    raise InterruptedError('Recorded EPUB preparation was cancelled.') from error
                raise
            with lock:
                held['value'] = value
            if self.stopping():
                release()
                raise InterruptedError('Recorded EPUB preparation was cancelled before presentation.')
            # The host progress contract describes completed preparation only.
            # No playback or acoustic/quotation qualification is implied.
            return {'status': 'completed'}

        def accepted(_result):
            value = take()
            self.busy = False
            if self.stopping():
                if value is not None:
                    self._discard(value, discard)
                return
            try:
                self._check_scope()
            except Exception as error:
                if value is not None:
                    self._discard(value, discard)
                self._error(error)
                if self._can_present():
                    self._enable(self.transport is not None)
                return
            if value is None:
                if self._can_present():
                    self._enable(self.transport is not None)
                return
            # Ownership passes to the presentation callback before it runs.
            # Its native failure path must close the player before deleting PCM.
            try:
                complete(value)
            except Exception as error:
                self._error(error)
            finally:
                if self._can_present():
                    self._enable(self.transport is not None)

        def failed(error):
            release()
            self.busy = False
            self._last_error = error
            if isinstance(error, AudioCleanupPending):
                self._remember_cleanup(error.cleanup_owner)
            # Error cleanup remains owned even after the view becomes stale.
            # _error stops transport but presents only to the same live host.
            self._error(error)
            if self._can_present():
                self._enable(self.transport is not None)

        try:
            future = self.host.run_job(title, work, accepted, failed)
            if future is None or future is False:
                release()
                self.busy = False
                if self._can_present():
                    self._enable(self.transport is not None)
                return False
            self._future = future

            def finished(result):
                # The host intentionally omits UI callbacks after host close.
                # This callback still owns and disposes an unpresented result.
                try:
                    result.result()
                except AudioCleanupPending as error:
                    self._last_error = error
                    self._remember_cleanup(error.cleanup_owner)
                except BaseException as error:
                    self._last_error = error
                if self.stopping():
                    release()
            future.add_done_callback(finished)
            return True
        except BaseException:
            release()
            self.busy = False
            if self._can_present():
                self._enable(self.transport is not None)
            raise

    def load(self):
        self.report('Checking the imported EPUB and its recorded-audio index…')
        self._submit(lambda: self.session_factory(self.desk, self.ordinal, guard=self._check_scope,
                                                cancelled=self.stopping),
                     self._loaded, lambda session: session.close())

    def _loaded(self, session):
        self.session = session
        self.player = self.player_factory()
        self.transport = OverlayPlayback(session, self.player)
        if self.progress is not None:
            self.progress.register_player(self.transport.observed)
        self.segments.Set([f'{cue.index + 1}. {session.page.raw_text[cue.start:cue.end][:160]}'
                           for cue in session.chapter.cues])
        self.text.ChangeValue(session.page.raw_text)
        self.segments.SetSelection(0); self._highlight()
        self.report('Ready. No audio has started. Select a segment and choose Play. '
                    'Playback continues through this source section; Stop or a new selection cancels it.')
        self.segments.SetFocus(); self.timer.Start(100)

    def _highlight(self):
        cue = self.session.chapter.cues[self.transport.index]
        # Existing native mapping verifies exact prefixes, including Windows
        # CRLF and UTF-16 positions; Python offsets alone are not sufficient.
        start = native_position(self.text, self.session.page.raw_text, cue.start)
        end = native_position(self.text, self.session.page.raw_text, cue.end)
        self.text.SetSelection(start, end)
        self.text.ShowPosition(start)

    def select(self, event=None):
        if self.transport is None:
            return
        try:
            self._check_scope()
            self.cancelled.set()
            if self._pending is not None:
                self._pending.close(); self._pending = None
            self.transport.select(self.segments.GetSelection())
            self._highlight()
            self.report('Recorded segment selected. Choose Play to start; selection does not start audio.')
        except Exception as exc:
            self._error(exc)

    def move(self, direction):
        if self.transport is None:
            return
        index = self.transport.index + direction
        if 0 <= index < len(self.session.chapter.cues):
            self.segments.SetSelection(index); self.select()
        else:
            self.report('This is the first or last recorded segment in the current source section.', focus=True)

    def play(self, event=None):
        if self.transport is None or self.busy or self.closed:
            return
        try:
            self._check_scope(); self.transport.stop()
            transport, session = self.transport, self.session
            ticket = transport.ticket
            self.report('Preparing the exact recorded audio interval…')
            reported = False
            def failed(error):
                nonlocal reported
                if reported:
                    return
                reported = True
                self._last_error = error
                # A failed start closes its own player and advances generation
                # once. That cleanup must not hide its error, while a genuine
                # Stop, selection, replaced source/view or close still revokes it.
                if (self.stopping() or not self._can_present()
                        or self.transport is not transport or self.session is not session
                        or transport.session is not session or transport.closed
                        or transport.interrupted or transport.index != ticket[0]
                        or transport.generation not in (ticket[1], ticket[1] + 1)):
                    return
                try:
                    self._error(error)
                except Exception as presentation_error:
                    # Keep the original failure even if its native status view
                    # cannot present it. The diagnostic owns this same object.
                    self._last_error = error
                    try:
                        error.add_note('Recorded EPUB error presentation failed: '
                            + type(presentation_error).__name__ + ': ' + str(presentation_error)[:2048])
                    except Exception:
                        pass
            def ready(prepared):
                if self.transport is not transport or self.session is not session or ticket != transport.ticket:
                    prepared.close(); return
                self._pending = prepared
                def start():
                    self._check_scope()
                    try:
                        started = transport.start(prepared, ticket)
                    except Exception as error:
                        failed(error)
                        raise
                    if started:
                        self._pending = None
                        self._highlight()
                        self.timer.Start(100)
                        self.report('Playing authored recorded segment. Pause, Stop and segment navigation are available.')
                play_when_quiet((self.progress,), start, schedule=self.wx.CallLater,
                    alive=lambda: not self.stopping() and self.transport is transport
                        and self.session is session and ticket == transport.ticket,
                    on_error=failed)
            self._submit(lambda: self.session.prepare(ticket[0], cancelled=self.stopping),
                         ready, lambda prepared: prepared.close())
        except Exception as exc:
            self._error(exc)

    def pause(self, event=None):
        try:
            if self.transport is not None and self.transport.pause():
                self.report('Recorded audio paused. Resume continues this exact segment.')
        except Exception as exc:
            self._error(exc)

    def resume(self, event=None):
        if self.transport is None or self.busy or self.player.state != 'paused':
            return
        try:
            ticket = self.transport.ticket
            def ready(prepared):
                play_when_quiet((self.progress,), lambda: self.transport.resume(), schedule=self.wx.CallLater,
                    alive=lambda: not self.stopping() and ticket == self.transport.ticket,
                    on_error=self._error)
            self._submit(lambda: self.session.admit(self.transport.prepared), ready, lambda _: None)
        except Exception as exc:
            self._error(exc)

    def stop(self, event=None, *, notify=True):
        self.cancelled.set()
        if self.transport is not None:
            self.transport.stop()
        if self._pending is not None:
            self._pending.close(); self._pending = None
        if self.progress is not None:
            self.progress.refresh_audio_state()
        if notify:
            self.report('Recorded audio stopped. Prepared playback was cancelled.')

    def where(self, event=None):
        try:
            self._check_scope()
            if self.transport is not None:
                cue = self.session.chapter.cues[self.transport.index]
                approximation = ' (estimated playback clock)' if self.player.position_is_approximate else ''
                self.report(f'Source section {self.ordinal}; recorded segment {cue.index + 1} of '
                            f'{len(self.session.chapter.cues)}; {self.player.state}; '
                            f'{self.player.position_seconds:.2f} seconds into the segment{approximation}. '
                            'Synchronization is authored fragment timing; reading grants no quotation approval.', focus=True)
        except Exception as error:
            self._error(error)

    def tick(self, event=None):
        if self.closed or self.transport is None:
            return
        try:
            self._check_scope()
            if self.transport.interrupted:
                self.transport.interrupted = False
                if self._pending is not None:
                    self._pending.close(); self._pending = None
                self.report('Recorded audio was stopped by another audio operation. '
                            'The selected segment is unchanged; choose Play to restart it.')
            following = self.transport.next_after_eof()
            if following is not None:
                if following < len(self.session.chapter.cues):
                    self.transport.index = following
                    self.segments.SetSelection(following); self._highlight()
                    self.play()
                else:
                    self.report('End of the recorded segments in this source section. '
                                'Return to the Reading Desk to choose another section.')
        except Exception as exc:
            self._error(exc)

    def _unregister(self):
        if self.progress is not None and self.player is not None:
            self.progress.unregister_player(self.transport.observed, silence_confirmed=True)

    def _on_destroy(self, event):
        if event.GetEventObject() is self.dialog:
            self._destroyed = True
            self.close(destroyed=True)
        event.Skip()

    def close(self, event=None, *, destroyed=False):
        if self.closed:
            return
        self._closing = True
        self.cancelled.set()
        self.timer.Stop()
        try:
            self.stop()
            if self.transport is not None:
                self.transport.close()
            elif self.session is not None:
                self.session.close()
        except Exception as error:
            self._last_error = error
            if not destroyed:
                self.report(f'Audio closure is not confirmed. Try Stop or Close again. {error}', focus=True)
                if event is not None and getattr(event, 'CanVeto', lambda: False)():
                    event.Veto()
                return
            owner = self.transport or self.session
            if self.progress is not None and self.transport is not None:
                self.progress.unregister_player(self.transport.observed, silence_confirmed=False)
            if owner is not None:
                pending = pending_cleanup(owner.close,
                    message='Recorded EPUB native playback is closing; its PCM and source remain owned.',
                    callbacks=(lambda: self.wx.CallAfter(self._unregister),))
                self._remember_cleanup(pending.cleanup_owner)
            # A forced window destruction cannot revoke actual native ownership.
            # Progress keeps the observed transport until silence is confirmed.
        else:
            self._unregister()
        self.closed = True
        future = self._future
        if future is not None:
            def release_after_work(_future):
                self._release_result()
            if future.done():
                release_after_work(future)
            else:
                future.add_done_callback(release_after_work)
        if any(not owner.quiescent for owner in self._cleanup_owners):
            if not getattr(self.host, '_closing', False):
                self.host.report('Recorded EPUB cleanup is still pending. Its native owner and temporary files are retained.')
        if not destroyed and self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self):
        try:
            self.load()
            self.dialog.ShowModal()
        finally:
            self.close()
            if self.closed and not self._destroyed:
                self.dialog.Destroy()
