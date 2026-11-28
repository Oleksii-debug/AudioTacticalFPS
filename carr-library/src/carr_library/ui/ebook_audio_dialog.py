"""Native recorded-overlay transport opened from the existing Reading Desk."""
import threading

from ..audio.cleanup import pending_cleanup
from ..reader.ebook_audio import EbookAudioSession
from ..speech.player import WavePlayer
from ..speech.progress_speech import play_when_quiet
from .ebook_model import native_position


class _ObservedTransport:
    """A quiet/recording stop also invalidates this owner's pending playback."""
    def __init__(self, owner):
        self.owner = owner

    @property
    def state(self):
        return self.owner.player.state

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
        self.player.close()
        if self.prepared is not None:
            self.prepared.close()
            self.prepared = None

    def close(self):
        self.stop()
        self.closed = True
        self.session.close()


class EbookAudioDialog:
    """Accessible chapter/fragment transport; opening never starts playback."""
    def __init__(self, parent, desk, ordinal, guard, *, progress=None,
                 executor, parent_cancelled=lambda: False,
                 session_factory=EbookAudioSession, player_factory=WavePlayer):
        import wx
        self.wx, self.guard, self.progress = wx, guard, progress
        self.desk, self.ordinal = desk, ordinal
        self.session_factory, self.player_factory = session_factory, player_factory
        self.dialog = wx.Dialog(parent, title='EPUB synchronized recorded audio', size=(900, 730),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        if not callable(getattr(executor, 'submit', None)):
            raise TypeError('Recorded EPUB audio borrows the existing reader worker.')
        self.executor, self.parent_cancelled = executor, parent_cancelled
        self._result_lock, self._completed = threading.Lock(), {}
        self.destroyed = False
        self._owner_id = self.dialog.GetId()
        self.cancelled = threading.Event()
        self.closed, self.busy, self.closing = False, False, False
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
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.timer = wx.Timer(self.dialog)
        self.dialog.Bind(wx.EVT_TIMER, self.tick, self.timer)
        self._enable(False)

    def _enable(self, available):
        if self.closed:
            return
        # Segment selection and Stop remain usable while a clip is preparing;
        # changing selection cancels and invalidates that pending playback.
        for index, button in enumerate(self.buttons):
            button.Enable(available and (not self.busy or index in (0, 1, 5, 6)))

    def report(self, message, *, focus=False):
        if not self.closed:
            self.status.ChangeValue(str(message))
            if focus:
                self.status.SetFocus()

    def _error(self, error):
        if self.closed:
            return
        self.timer.Stop()
        try:
            self.stop()
        except Exception as cleanup:
            error = f'{error}\nThe audio device has not confirmed closure: {cleanup}'
        self.report(error, focus=True)

    def stopped(self):
        return self.closed or self.closing or self.cancelled.is_set() or self.parent_cancelled()

    @staticmethod
    def _discard(result, discard):
        try:
            discard(result)
        except Exception:
            # Retain the exact failed native owner; no widget is used on retry.
            pending_cleanup(lambda: discard(result),
                message='Recorded EPUB cleanup is pending; its files remain retained.')

    def _drain_completed(self):
        with self._result_lock:
            rows, self._completed = tuple(self._completed.values()), {}
        for result, discard in rows:
            self._discard(result, discard)

    def _submit(self, operation, complete, discard):
        if self.busy or self.closed or self.closing:
            return
        self.guard()
        self.busy = True; self.cancelled.clear(); self._enable(self.transport is not None)
        def finished(future):
            try:
                result = future.result()
            except Exception as error:
                def fail(error=error):
                    self.busy = False
                    if not self.closed:
                        self._error(error); self._enable(self.transport is not None)
                if not self.closed:
                    self.wx.CallAfter(fail)
                return
            identity = id(future)
            with self._result_lock:
                closing = self.closed or self.closing
                if not closing:
                    self._completed[identity] = result, discard
            if closing:
                self._discard(result, discard)
                return
            def apply():
                with self._result_lock:
                    owned = self._completed.pop(identity, None)
                if owned is None:
                    return
                result, release = owned
                try:
                    if self.stopped():
                        self._discard(result, release)
                        return
                    self.guard()
                    complete(result)
                except Exception as error:
                    self._discard(result, release)
                    if not self.closed:
                        self._error(error)
                finally:
                    self.busy = False
                    if not self.closed:
                        self._enable(self.transport is not None)
            self.wx.CallAfter(apply)
        try:
            self.executor.submit(operation).add_done_callback(finished)
        except Exception:
            self.busy = False
            if not self.closed:
                self._enable(self.transport is not None)
            raise

    def load(self):
        self.report('Checking the imported EPUB and its recorded-audio index…')
        self._submit(lambda: self.session_factory(self.desk, self.ordinal, guard=self.guard,
                                                cancelled=self.stopped),
                     self._loaded, lambda session: session.close())

    def _loaded(self, session):
        # A session is adopted only after the native source view and player
        # registration both succeed. A failed highlight must not leave Play
        # attached to the session that _submit will discard.
        player = self.player_factory()
        transport = OverlayPlayback(session, player)
        registered = False

        def release_player(value):
            value.close()
            if registered and self.progress is not None:
                self.progress.unregister_player(transport.observed, silence_confirmed=True)

        try:
            self.segments.Set([f'{cue.index + 1}. {session.page.raw_text[cue.start:cue.end][:160]}'
                               for cue in session.chapter.cues])
            self.text.ChangeValue(session.page.raw_text)
            self.segments.SetSelection(0)
            self._highlight(session=session, index=0)
            self.report('Ready. No audio has started. Select a segment and choose Play. '
                        'Playback continues through this source section; Stop or a new selection cancels it.')
            self.segments.SetFocus()
            if self.progress is not None:
                registered = True
                self.progress.register_player(transport.observed)
            self.timer.Start(100)
        except Exception:
            self.timer.Stop()
            self._discard(player, release_player)
            raise
        self.session, self.player, self.transport = session, player, transport

    def _highlight(self, *, session=None, index=None):
        session = self.session if session is None else session
        index = self.transport.index if index is None else index
        cue = session.chapter.cues[index]
        # Existing native mapping verifies exact prefixes, including Windows
        # CRLF and UTF-16 positions; Python offsets alone are not sufficient.
        start = native_position(self.text, session.page.raw_text, cue.start)
        end = native_position(self.text, session.page.raw_text, cue.end)
        self.text.SetSelection(start, end)
        self.text.ShowPosition(start)

    def select(self, event=None):
        if self.transport is None:
            return
        try:
            self.guard()
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
            self.guard(); self.transport.stop()
            ticket = self.transport.ticket
            self.report('Preparing the exact recorded audio interval…')
            def ready(prepared):
                if ticket != self.transport.ticket:
                    prepared.close(); return
                self._pending = prepared
                def start():
                    self.guard()
                    if self.transport.start(prepared, ticket):
                        self._pending = None
                        self._highlight()
                        self.timer.Start(100)
                        self.report('Playing authored recorded segment. Pause, Stop and segment navigation are available.')
                play_when_quiet((self.progress,), start, schedule=self.wx.CallLater,
                    alive=lambda: not self.closed and ticket == self.transport.ticket and not self.stopped(),
                    on_error=self._error)
            self._submit(lambda: self.session.prepare(ticket[0], cancelled=self.stopped),
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
                    alive=lambda: not self.closed and ticket == self.transport.ticket and not self.stopped(),
                    on_error=self._error)
            self._submit(lambda: self.session.admit(self.transport.prepared), ready, lambda _: None)
        except Exception as exc:
            self._error(exc)

    def stop(self, event=None):
        self.cancelled.set()
        if self.transport is not None:
            self.transport.stop()
        if self._pending is not None:
            self._pending.close(); self._pending = None
        if self.progress is not None:
            self.progress.refresh_audio_state()
        self.report('Recorded audio stopped. Prepared playback was cancelled.')

    def where(self, event=None):
        if self.transport is not None:
            cue = self.session.chapter.cues[self.transport.index]
            approximation = ' (estimated playback clock)' if self.player.position_is_approximate else ''
            self.report(f'Source section {self.ordinal}; recorded segment {cue.index + 1} of '
                        f'{len(self.session.chapter.cues)}; {self.player.state}; '
                        f'{self.player.position_seconds:.2f} seconds into the segment{approximation}. '
                        'Synchronization is authored fragment timing; reading grants no quotation approval.', focus=True)

    def tick(self, event=None):
        if self.closed or self.transport is None:
            return
        try:
            self.guard()
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

    def _release(self):
        # Worker-safe, no native widget access. Device closure precedes files.
        if self.transport is not None:
            self.transport.close()
        elif self.session is not None:
            self.session.close()
        if self._pending is not None:
            self._pending.close(); self._pending = None
        self._drain_completed()

    def _unregister(self):
        if self.progress is not None and self.transport is not None:
            self.progress.unregister_player(self.transport.observed, silence_confirmed=True)

    def force_close(self):
        # Parent/direct destruction revokes callbacks before any owned cleanup.
        self.closed = self.closing = True
        self.cancelled.set()
        self.timer.Stop()
        try:
            self._release()
        except Exception:
            pending_cleanup(self._release,
                message='Recorded EPUB native cleanup is pending.',
                callbacks=(self._unregister,))
        else:
            self._unregister()

    def _destroyed(self, event):
        if event.GetId() == self._owner_id:
            self.destroyed = True
            self.force_close()
        event.Skip()

    def close(self, event=None):
        if self.closed:
            return
        self.closing = True
        self.cancelled.set()
        try:
            self._release()
        except Exception as exc:
            self.report(f'Audio closure is not confirmed. Try Stop or Close again. {exc}', focus=True)
            if event is not None and getattr(event, 'CanVeto', lambda: False)():
                event.Veto()
            return
        self.closed = True
        self.timer.Stop()
        self._unregister()
        # The parent Reading Desk owns this executor and shuts it down.
        if not self.destroyed and self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self):
        try:
            self.wx.CallAfter(self.load)
            self.dialog.ShowModal()
        finally:
            self.close()
            if self.closed and not self.destroyed:
                self.dialog.Destroy()
