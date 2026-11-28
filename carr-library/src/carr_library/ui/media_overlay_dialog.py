"""Native synchronized navigation for the verified Resonance EPUB subset."""
import json
from pathlib import Path
from threading import Event

from ..audio.models import AudioBudget
from ..durable_state import local_path
from ..reader.media_overlay import PreparedOverlay, OverlaySession, prepare_overlay
from ..speech.player import WavePlayer
from ..speech.progress_speech import play_when_quiet


NOTICE = ('Opens verified Resonance synchronized publications. Measured utterance intervals, '
          'not word alignment. Navigation stops playback; press Play explicitly. '
          'General EPUB media-overlay compatibility and DAISY are not claimed.')


def create_media_overlay_dialog(frame, *, wx_module=None, _player_factory=WavePlayer):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class OverlayDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Read synchronized EPUB — measured utterance navigation',
                             size=(940, 800), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.library_root = local_path(frame.library.root, exists=True)
            self.alive, self.busy, self.generation = True, False, 0
            self.cancel = Event()
            self.document = self.session = None
            self.player = _player_factory()
            self.progress = getattr(frame, 'operation_progress', None)
            if self.progress is not None:
                self.progress.register_player(self.player)
            self.last_index = None
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label=NOTICE); notice.Wrap(880)
            box.Add(notice, 0, wx.ALL | wx.EXPAND, 5)

            def field(label, *, value='', choices=None, style=0, proportion=0):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
                control = wx.Choice(panel, choices=choices) if choices is not None else wx.TextCtrl(panel, value=value, style=style)
                control.SetName(label.replace('&', '')); box.Add(control, proportion, wx.ALL | wx.EXPAND, 3)
                return control
            self.path = field('Selected synchronized &EPUB file')
            self.max_gib = field('Maximum captured and decoded size in &GiB', value='32')
            self.minutes = field('Preparation time budget in &minutes', value='60')
            self.chapter = field('&Chapter navigation', choices=[])
            self.interval = field('Measured &utterance or untranscribed interval', choices=[])
            self.seek_seconds = field('Seek position in &seconds', value='0')
            self.text = field('Current &reading copy', style=wx.TE_MULTILINE | wx.TE_READONLY, proportion=1)
            self.source = field('Original &source and accepted mapping', style=wx.TE_MULTILINE | wx.TE_READONLY, proportion=1)
            self.status = field('Synchronized reading &status', style=wx.TE_MULTILINE | wx.TE_READONLY, proportion=1)
            panel.SetSizer(box); panel.FitInside()
            layout = wx.BoxSizer(wx.VERTICAL); layout.Add(panel, 1, wx.ALL | wx.EXPAND, 4)
            self.buttons = {}
            for controls in (
                    (('choose', '&Choose EPUB', self.on_choose), ('load', '&Load verified publication', self.on_load),
                     ('previous', 'Previous &interval', self.on_previous), ('next', '&Next interval', self.on_next)),
                    (('play', '&Play', self.on_play), ('pause', 'Pa&use', self.on_pause), ('stop', '&Stop', self.on_stop),
                     ('seek', 'See&k', self.on_seek), ('cancel', 'Cancel &loading', self.on_cancel),
                     ('close', '&Close', self.on_close))):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in controls:
                    button = wx.Button(self, label=label); button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, handler); row.Add(button, 0, wx.ALL, 3); self.buttons[key] = button
                layout.Add(row, 0, wx.ALL, 2)
            self.SetSizer(layout)
            self.chapter.Bind(wx.EVT_CHOICE, self.on_chapter)
            self.interval.Bind(wx.EVT_CHOICE, self.on_interval)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.on_tick, self.timer); self.timer.Start(200)
            self.report('Choose an EPUB and load it. Loading and navigation never start audio.')
            self.controls()

        def report(self, message, *, focus=False):
            if self.alive:
                self.status.ChangeValue(str(message))
                if focus:
                    self.status.SetFocus()

        def stopping(self):
            return (not self.alive or self.cancel.is_set() or frame.cancel_event.is_set()
                    or getattr(frame, '_closing', False) or frame.library.root != self.library_root)

        def controls(self):
            if not self.alive:
                return
            loaded = self.session is not None and not self.busy
            for control in (self.path, self.max_gib, self.minutes):
                control.Enable(not self.busy)
            for control in (self.chapter, self.interval, self.seek_seconds):
                control.Enable(loaded)
            for name in ('choose', 'load'):
                self.buttons[name].Enable(not self.busy)
            for name in ('previous', 'next', 'pause', 'stop', 'seek'):
                self.buttons[name].Enable(loaded)
            self.buttons['play'].Enable(loaded and self.player.available)
            self.buttons['cancel'].Enable(self.busy)

        def budget(self):
            return AudioBudget(max_output_bytes=int(self.max_gib.GetValue()) * 1024**3,
                               timeout=float(self.minutes.GetValue()) * 60)

        def begin(self, title, operation, complete):
            if self.busy or not self.alive:
                return False
            if local_path(frame.library.root, exists=True) != self.library_root:
                raise ValueError('The library changed; reopen synchronized reading.')
            if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                self.report('Another operation is active. Finish or cancel it first.')
                return False
            self.cancel.clear(); self.busy = True; self.controls()
            def done(value):
                self.busy = False
                if not self.alive:
                    if isinstance(value, PreparedOverlay):
                        value.close()
                    self.dispose()
                    return
                try:
                    if frame.library.root != self.library_root:
                        if isinstance(value, PreparedOverlay):
                            value.close()
                        raise ValueError('The library changed; reopen synchronized reading.')
                    complete(value)
                except Exception as exc:
                    self.report(exc, focus=True)
                self.controls()
            def failed(error):
                self.busy = False
                if self.alive:
                    self.report(error, focus=True); self.controls()
                else:
                    self.dispose()
            try:
                frame.run_job(title, operation, done=done, failed=failed)
            except Exception:
                self.busy = False; self.controls()
                raise
            return True

        def refresh_audio(self):
            if self.progress is not None:
                self.progress.refresh_audio_state()

        def dispose(self):
            if self.document is not None:
                self.document.close()
                self.document = self.session = None

        def on_choose(self, event=None):
            picker = wx.FileDialog(self, 'Choose a Resonance synchronized EPUB',
                                   wildcard='EPUB (*.epub)|*.epub', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if picker.ShowModal() == wx.ID_OK:
                    self.path.ChangeValue(picker.GetPath())
            finally:
                picker.Destroy()

        def on_load(self, event=None):
            try:
                if not self.path.GetValue().strip():
                    raise ValueError('Choose a synchronized EPUB file first.')
                if self.busy or getattr(frame, 'working', False):
                    self.report('Another operation is active. Finish or cancel it first.'); return
                selected, budget = Path(self.path.GetValue()), self.budget()
                self.generation += 1
                self.player.close(); self.refresh_audio(); self.dispose()
                def operation():
                    token = self.progress.event.token if self.progress is not None else None
                    def progress(message):
                        if self.progress is not None:
                            self.progress.update(phase=message, token=token)
                        wx.CallAfter(self.report, message)
                    return prepare_overlay(selected, library_root=self.library_root, budget=budget,
                                           cancelled=self.stopping, progress=progress)
                def loaded(document):
                    self.document = document
                    self.session = OverlaySession(document, self.player)
                    self.chapter.Set([row['title'] for row in document.chapters])
                    self.interval.Set([f'{i + 1}. {row["kind"]}: {row["text"][:100]}'
                                       for i, row in enumerate(document.intervals)])
                    self.show_interval(0)
                    self.report('Verified publication loaded; playback is stopped.\n' + document.title +
                                '\nPublication SHA-256: ' + document.publication_sha256, focus=True)
                self.begin('Preparing synchronized EPUB reading', operation, loaded)
            except Exception as exc:
                self.report(exc, focus=True); self.controls()

        def show_interval(self, index, *, update_seek=True):
            if not self.alive or self.document is None:
                return
            row = self.document.intervals[index]
            self.last_index = index; self.interval.SetSelection(index)
            chapter_index = next(i for i, chapter in enumerate(self.document.chapters) if chapter['id'] == row['chapter'])
            self.chapter.SetSelection(chapter_index)
            self.text.ChangeValue(row['text'])
            details = row.get('source_details')
            self.source.ChangeValue(json.dumps(details if details is not None else
                {'source_ref': row.get('source_ref', ''), 'source': {key: self.document.provenance['source'][key]
                     for key in ('job_id', 'project_id', 'project_sha256', 'source_script_sha256')},
                 'notice': 'This interval has no primary Studio character-offset mapping.'},
                ensure_ascii=False, indent=2))
            if update_seek:
                self.seek_seconds.ChangeValue(str(row['start']))

        def select(self, index):
            if self.session is None or self.busy:
                return
            self.generation += 1
            row = self.session.select(index); self.refresh_audio(); self.show_interval(index)
            self.report(f'Selected interval {index + 1}; {row["start"]:.3f}–{row["end"]:.3f} seconds. Press Play explicitly.')

        def on_interval(self, event=None):
            try:
                self.select(self.interval.GetSelection())
            except Exception as exc:
                self.report(exc, focus=True)

        def on_chapter(self, event=None):
            try:
                if self.document is not None:
                    index = self.chapter.GetSelection()
                    if not 0 <= index < len(self.document.chapters):
                        raise ValueError('Choose a chapter.')
                    self.select(self.document.chapters[index]['first_interval'])
            except Exception as exc:
                self.report(exc, focus=True)

        def on_previous(self, event=None):
            try:
                if self.session is not None:
                    self.select(max(0, self.session.index - 1))
            except Exception as exc:
                self.report(exc, focus=True)

        def on_next(self, event=None):
            try:
                if self.session is not None:
                    self.select(min(len(self.document.intervals) - 1, self.session.index + 1))
            except Exception as exc:
                self.report(exc, focus=True)

        def on_play(self, event=None):
            try:
                if self.session is None or self.busy:
                    return
                if self.player.state == 'playing':
                    self.report('Playback is already running.')
                    return
                self.generation += 1
                generation, session = self.generation, self.session
                def verified(value):
                    play_when_quiet((self.progress,), lambda: (session.play(), self.refresh_audio(),
                        self.report('Playing from ' + str(round(session.position, 3)) + ' seconds.')),
                        schedule=wx.CallLater,
                        alive=lambda: self.alive and generation == self.generation and not self.stopping(),
                        on_error=lambda exc: self.report(exc, focus=True))
                self.begin('Verifying prepared synchronized audio',
                           lambda: session.verify_audio(cancelled=self.stopping), verified)
            except Exception as exc:
                self.report(exc, focus=True)

        def on_pause(self, event=None):
            try:
                self.generation += 1
                if self.session is None or self.player.state != 'playing':
                    self.report('Playback is not running.')
                    return
                self.session.pause(); self.refresh_audio()
                self.report('Playback paused. Press Play to resume.' if self.player.state == 'paused'
                            else 'Playback is not running.')
            except Exception as exc:
                self.report(exc, focus=True)

        def on_stop(self, event=None):
            try:
                self.generation += 1
                if self.session is not None:
                    self.session.stop(); self.refresh_audio()
                    self.report('Playback stopped at ' + str(round(self.session.position, 3)) + ' seconds.')
            except Exception as exc:
                self.report(exc, focus=True)

        def on_seek(self, event=None):
            try:
                if self.session is not None and not self.busy:
                    self.generation += 1
                    self.session.seek(float(self.seek_seconds.GetValue()))
                    self.refresh_audio(); self.show_interval(self.session.index, update_seek=False)
                    self.report('Seek position selected; playback is stopped. Press Play explicitly.')
            except Exception as exc:
                self.report(exc, focus=True)

        def on_tick(self, event=None):
            if not self.alive or self.busy or self.session is None:
                return
            try:
                if frame.library.root != self.library_root or getattr(frame, '_closing', False):
                    self.on_close(); return
                self.session.position
                if self.last_index != self.session.index:
                    self.show_interval(self.session.index, update_seek=False)
                if self.player.state == 'error':
                    self.report('The playback device stopped with an error. Source text remains available.')
            except Exception as exc:
                self.report(exc)

        def on_cancel(self, event=None):
            self.generation += 1
            if self.busy:
                self.cancel.set(); frame.cancel_event.set()
                self.report('Cancellation requested. Private preparation files are removed after the worker stops.')

        def on_close(self, event=None):
            if not self.alive:
                return
            self.on_cancel()
            self.alive = False; self.timer.Stop()
            silence = False
            try:
                self.player.close(); silence = True
            except Exception as exc:
                try: frame.report('Playback close could not be confirmed: ' + str(exc))
                except Exception: pass
            if self.progress is not None:
                try:
                    self.progress.unregister_player(self.player, silence_confirmed=silence)
                except Exception as exc:
                    try: frame.report('Progress playback state could not be refreshed: ' + str(exc))
                    except Exception: pass
            if silence and not self.busy:
                try:
                    self.dispose()
                except Exception as exc:
                    retained = getattr(frame, '_reader_playback_retained', None)
                    if not isinstance(retained, list):
                        retained = []; frame._reader_playback_retained = retained
                    retained.append((self.player, self.document))
                    self.document = self.session = None
                    try: frame.report('Temporary playback files retained after cleanup error: ' + str(exc))
                    except Exception: pass
            elif not silence:
                retained = getattr(frame, '_reader_playback_retained', None)
                if not isinstance(retained, list):
                    retained = []; frame._reader_playback_retained = retained
                retained.append((self.player, self.document))
                self.document = self.session = None
            if self.IsModal():
                self.EndModal(wx.ID_CLOSE)
            else:
                self.Hide()

    return OverlayDialog()


def show_media_overlay(frame):
    dialog = create_media_overlay_dialog(frame)
    try:
        dialog.ShowModal()
    finally:
        dialog.on_close(); dialog.Destroy()
