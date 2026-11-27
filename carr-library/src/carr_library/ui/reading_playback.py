"""Keyboard reading of the editable copy using existing local audio services."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from pathlib import Path
import threading
import time

from ..audio.reading import ReadingAudio, capture_reading_text, render_reading
from ..audio.voice_settings import session_for
from ..progress import set_media_active, progress_audio_quiescent


class ReadingPlaybackPane:
    def __init__(self, parent, editor, *, root, project_id, guard, say,
                 session=None, player=None, renderer=None):
        import wx
        from ..audio.player import WavePlayer
        self.wx, self.parent, self.editor = wx, parent, editor
        self.root, self.project_id, self.guard, self.say = root, project_id, guard, say
        self.session = session or session_for(root)
        self.player = player or WavePlayer()
        self.renderer = renderer
        self.audio = None
        self._closed, self._busy = False, False
        self._cancel = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reading-copy-audio')
        self._generation = 0
        self._start_token, self._start_timer = None, None
        self._close_ready = None
        self._last_state = None
        self.rows, self.controls, self.buttons = [], [], {}
        self.sizer = wx.StaticBoxSizer(wx.VERTICAL, parent, 'Read editable copy with a local voice')
        first = wx.BoxSizer(wx.HORIZONTAL)
        self.voice = wx.Choice(parent)
        self.voice.SetName('Local reading voice; explicitly choose after reloading voices')
        self.controls.append(self.voice)
        first.Add(self.voice, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
        self._button(first, 'Reload reading voices', self.reload, 'reload')
        self._button(first, 'Local voice profiles', self.profiles, 'profiles')
        self.sizer.Add(first, 0, wx.EXPAND)
        options = wx.WrapSizer(wx.HORIZONTAL)
        def field(label, widget):
            options.Add(wx.StaticText(parent, label=label), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
            widget.SetName(label)
            options.Add(widget, 0, wx.ALL, 3)
            self.controls.append(widget)
            return widget
        self.language = field('Reading language code', wx.TextCtrl(parent, value='en', size=(75, -1)))
        self.rate = field('Rate (-10 to 10)', wx.SpinCtrl(parent, min=-10, max=10, initial=0, size=(70, -1)))
        self.volume = field('Volume (0 to 100)', wx.SpinCtrl(parent, min=0, max=100, initial=100, size=(75, -1)))
        self._button(options, 'Read selection / all (F6)', lambda event: self.read('selection'), 'read')
        self._button(options, 'Read all', lambda event: self.read('all'), 'all')
        self._button(options, 'Read from cursor', lambda event: self.read('cursor'), 'cursor')
        self.sizer.Add(options, 0, wx.EXPAND)
        playback = wx.WrapSizer(wx.HORIZONTAL)
        self._button(playback, 'Pause / resume (F7)', self.pause_resume, 'pause')
        self._button(playback, 'Stop reading (F8)', self.stop, 'stop', busy_safe=True)
        self._button(playback, 'Save reading WAV', self.save, 'save')
        playback.Add(wx.StaticText(parent, label='Seek seconds'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
        self.seek = wx.TextCtrl(parent, value='0', size=(75, -1))
        self.seek.SetName('Reading playback seek position in seconds')
        self.controls.append(self.seek)
        playback.Add(self.seek, 0, wx.ALL, 3)
        self._button(playback, 'Seek reading', self.seek_to, 'seek')
        self.sizer.Add(playback, 0, wx.EXPAND)
        self.accelerators = [(wx.ACCEL_NORMAL, wx.WXK_F6, self.buttons['read'].GetId()),
                             (wx.ACCEL_NORMAL, wx.WXK_F7, self.buttons['pause'].GetId()),
                             (wx.ACCEL_NORMAL, wx.WXK_F8, self.buttons['stop'].GetId())]
        self.timer = wx.Timer(parent)
        parent.Bind(wx.EVT_TIMER, self.poll, self.timer)
        self.timer.Start(250)

    @property
    def busy(self):
        return self._busy

    def _button(self, sizer, label, handler, key, *, busy_safe=False):
        value = self.wx.Button(self.parent, label=label)
        value.SetName(label)
        value.Bind(self.wx.EVT_BUTTON, handler)
        self.parent.Bind(self.wx.EVT_MENU, handler, id=value.GetId())
        sizer.Add(value, 0, self.wx.ALL, 3)
        self.buttons[key] = value
        if not busy_safe:
            self.controls.append(value)
        return value

    def _say(self, message):
        if not self._closed:
            try:
                self.say(str(message), focus=False)
            except Exception:
                pass  # Display failure does not change a completed file operation.

    def _signature(self):
        self.guard()
        return self.project_id(), sha256(self.editor.GetValue().encode('utf-8')).hexdigest()

    def _sync_media(self):
        status = self.player.status()
        cleanup = getattr(getattr(self.player, 'native', None), '_cleanup_error', None)
        active = status.state in ('starting', 'playing', 'paused')
        set_media_active(self, bool(active or cleanup or self._start_token is not None))
        return status

    def _invalidate_start(self):
        self._start_token = None
        if self._start_timer is not None:
            self._start_timer.Stop()
            self._start_timer = None

    def _stop_player(self):
        self._invalidate_start()
        self.player.stop()
        self._sync_media()

    def _discard(self):
        if self.audio is not None:
            self.audio.close()
            self.audio = None

    def stop(self, event=None):
        self._generation += 1
        self._cancel.set()
        try:
            self._stop_player()
            self._say('Reading cancellation requested; waiting for the worker.' if self._busy
                      else 'Reading playback stopped.')
        except Exception as exc:
            self._say('Playback cleanup is not confirmed. ' + str(exc))
            return False

    def invalidate(self):
        """An edit never causes obsolete generated audio to begin or keep playing."""
        if self._closed:
            return
        self._generation += 1
        self._cancel.set()
        try:
            self._stop_player()
            if not self._busy:
                self._discard()
            return True
        except Exception as exc:
            self._say('The edit is preserved; playback cleanup still needs attention: ' + str(exc))
            return False

    def _submit(self, operation, done):
        if self._busy or self._closed:
            return
        self._busy = True
        self._cancel.clear()
        for control in self.controls:
            control.Disable()
        future = self._pool.submit(operation)
        def completed(value):
            if self._closed:
                try:
                    result = value.result()
                    if isinstance(result, ReadingAudio):
                        result.close()
                except Exception:
                    pass
            else:
                self.wx.CallAfter(self._finish, value, done)
        future.add_done_callback(completed)

    def _finish(self, future, done):
        if self._closed:
            try:
                result = future.result()
                if isinstance(result, ReadingAudio):
                    result.close()
            except Exception:
                pass
            return
        self._busy = False
        for control in self.controls:
            control.Enable()
        try:
            result = future.result()
            if self._close_ready is not None and isinstance(result, ReadingAudio):
                result.close()
            else:
                done(result)
        except Exception as exc:
            self._say(str(exc))
        if self._close_ready is not None:
            callback, self._close_ready = self._close_ready, None
            try:
                self._stop_player()
                self._discard()
                self.wx.CallAfter(callback)
            except Exception as exc:
                self._say('The editor remains open until playback cleanup succeeds. ' + str(exc))

    def reload(self, event=None):
        if self._busy:
            return
        try:
            if not self.invalidate():
                return
            registry = self.session.registry()
            def loaded(rows):
                self.rows = [asdict(row) if is_dataclass(row) else dict(row) for row in rows]
                self.rows = [row for row in self.rows if row.get('available', True)]
                self.voice.SetItems([row['name'] + ' — ' + ', '.join(row.get('languages', ())) +
                                     ' — ' + row['id'] for row in self.rows])
                self.voice.SetSelection(self.wx.NOT_FOUND)
                self._say('Local reading voices loaded. Choose one explicitly.' if self.rows
                          else 'No usable local reading voice is available. Review an installed profile.')
            self._submit(registry.voices, loaded)
        except Exception as exc:
            self._say(str(exc))

    def profiles(self, event=None):
        if self._busy:
            return
        try:
            if not self.invalidate():
                return
            from .local_voice_settings import open_local_voice_settings
            open_local_voice_settings(self.parent, self.root, session=self.session)
            self.reload()
        except Exception as exc:
            self._say(str(exc))

    def read(self, mode='selection'):
        if self._busy:
            self._say('Wait for this reading operation or choose Stop reading.')
            return
        try:
            self._stop_player()
            self._discard()
            signature = self._signature()
            selected = self.voice.GetSelection()
            if not 0 <= selected < len(self.rows):
                raise ValueError('Reload and explicitly choose a local reading voice.')
            text = capture_reading_text(self.editor, mode)
            voice = self.rows[selected]['id']
            language, rate, volume = self.language.GetValue().strip(), self.rate.GetValue(), self.volume.GetValue()
            registry = self.session.registry()
            generation = self._generation
            def progress(message):
                if not self._closed:
                    try:
                        self.wx.CallAfter(self._say, message)
                    except Exception:
                        pass  # A closed native observer cannot invalidate rendered bytes.
            def rendered(audio):
                try:
                    current = self._signature()
                except Exception:
                    audio.close()
                    raise
                if (generation != self._generation or self._cancel.is_set() or signature != current):
                    audio.close()
                    self._say('The reading copy changed; obsolete audio was discarded.')
                    return
                self.audio = audio
                self._quiet_start(signature, generation)
            self._say('Preparing literal reading of the current editable copy. Stop remains available.')
            self._submit(lambda: render_reading(self.root, text, registry=registry, voice=voice,
                language=language, rate=rate, volume=volume, cancelled=self._cancel.is_set,
                on_status=progress, renderer=self.renderer), rendered)
        except Exception as exc:
            self._say(str(exc))

    def _quiet_start(self, signature, generation):
        self._invalidate_start()
        token = self._start_token = object()
        deadline = time.monotonic() + 3
        set_media_active(self, True)
        def attempt():
            if self._closed or self._start_token is not token:
                return
            try:
                if (self._cancel.is_set() or generation != self._generation
                        or signature != self._signature()):
                    raise ValueError('The reading copy changed before playback.')
                if not progress_audio_quiescent():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Progress audio has not confirmed silence. Playback did not start.')
                    self._start_timer = self.wx.CallLater(25, attempt)
                    return
                self._start_token = None
                self.player.play(self.audio.path)
                self._say('Reading the editable copy. F7 pauses or resumes; F8 stops. Save WAV exports this reading.')
            except Exception as exc:
                self._start_token = None
                self._say('Reading audio is prepared; playback did not start: ' + str(exc))
            finally:
                if self._start_token is None:
                    self._sync_media()
        attempt()

    def pause_resume(self, event=None):
        try:
            if self.player.status().state == 'paused':
                changed = self.player.resume()
            else:
                changed = self.player.pause()
            self._sync_media()
            self._say('Playback control requested.' if changed else 'Start reading before pausing or resuming.')
        except Exception as exc:
            self._say(str(exc))

    def seek_to(self, event=None):
        try:
            self.player.seek(float(self.seek.GetValue()))
            self._sync_media()
        except Exception as exc:
            self._say(str(exc))

    def save(self, event=None):
        if self._busy:
            return
        if self.audio is None:
            self._say('Read the copy first to prepare a WAV for export.')
            return
        try:
            self.guard()
            picker = self.wx.FileDialog(self.parent, 'Save reading WAV as a new file',
                wildcard='WAV audio (*.wav)|*.wav', defaultFile='reading.wav', style=self.wx.FD_SAVE)
            try:
                target = Path(picker.GetPath()) if picker.ShowModal() == self.wx.ID_OK else None
            finally:
                picker.Destroy()
            if target is not None:
                audio = self.audio
                self._submit(lambda: audio.save_wav(target, cancelled=self._cancel.is_set),
                             lambda path: self._say('Reading WAV saved: ' + str(path)))
        except Exception as exc:
            self._say(str(exc))

    def poll(self, event=None):
        if self._closed:
            return
        try:
            self.player.poll()
            status = self._sync_media()
            if status.state != self._last_state:
                self._last_state = status.state
                if status.state in ('error', 'unavailable'):
                    self._say('Playback ' + status.state + ': ' + str(status.error or 'local device unavailable'))
                elif status.state == 'completed':
                    self._say('Reading playback completed. The WAV remains available for export.')
        except Exception as exc:
            self._say('Playback observation failed: ' + str(exc))

    def request_close(self, callback):
        """Veto the native close until both producer and player release ownership."""
        self._cancel.set()
        self._generation += 1
        self._invalidate_start()
        if self._busy:
            self._close_ready = callback
            self._say('Stopping reading before closing; waiting for the local worker.')
            return False
        self._stop_player()
        self._discard()
        return True

    def dispose(self):
        self._closed = True
        self._cancel.set()
        self._invalidate_start()
        self.timer.Stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.player.close()
        self._discard()
        set_media_active(self, False)
