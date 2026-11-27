"""Native live reading using current voices, resource admission and WavePlayer."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from ..audio.player import WavePlayer
from ..audio.playback import output_devices
from ..audio.jobs import checked_directory
from ..reader.live_speech import (MODES, SpeechPositionStore, SpeechSelection, SpeechSession,
                                 SpeechSettings, select_range, text_hash, MAX_TEXT_CHARACTERS)
from ..reader.reading_state import ReadingLocation


def _normalized(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def source_offset(source, displayed_prefix):
    """Translate native UTF-16/CRLF cursor prefixes to exact Python offsets."""
    prefix = _normalized(displayed_prefix)
    if not _normalized(source).startswith(prefix):
        raise ValueError('The visible reader text changed; reopen the selected source.')
    count = offset = 0
    while count < len(prefix):
        if source[offset:offset + 2] == '\r\n':
            offset += 2
        else:
            offset += 1
        count += 1
    return offset


def selected_source_offsets(control, source):
    if _normalized(control.GetValue()) != _normalized(source):
        raise ValueError('The visible reader text changed; reopen the selected source.')
    start, end = control.GetSelection()
    return (source_offset(source, control.GetRange(0, start)),
            source_offset(source, control.GetRange(0, end)))


def native_offset(control, source, offset):
    """Verify actual native prefixes; never split an emoji or CRLF boundary.

    Supported native text controls count Unicode characters or UTF-16 units,
    and may expose LF, CR or CRLF positions. Candidate counts are only hints:
    the control must return the exact prefix before any position is accepted.
    Probing arbitrary binary-search midpoints can cut a UTF-16 surrogate pair.
    """
    if type(offset) is not int or not 0 <= offset <= len(source):
        raise ValueError('Choose a character offset inside the current source.')
    expected = _normalized(source[:offset])
    candidates = set()
    for newline in ('\n', '\r', '\r\n'):
        prefix = expected.replace('\n', newline)
        candidates.update((len(prefix), len(prefix.encode('utf-16-le')) // 2))
    last = control.GetLastPosition()
    for position in sorted(candidates):
        if not 0 <= position <= last:
            continue
        try:
            actual = control.GetRange(0, position)
            if (_normalized(actual) == expected and source_offset(source, actual) == offset):
                return position
        except UnicodeError:
            continue
    raise ValueError('This native text control cannot identify the exact source offset.')


def validate_input(text, cursor, selection, location, source_loader, ordinal, total):
    """Validate before creating native controls, a timer or a worker owner."""
    if (not isinstance(text, str) or len(text) > MAX_TEXT_CHARACTERS or '\x00' in text
            or not text.strip() and source_loader is None):
        raise ValueError('Choose a nonempty source section of at most two million characters.')
    try:
        text.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Reading requires valid Unicode text.') from exc
    if type(cursor) is not int or not 0 <= cursor <= len(text):
        raise ValueError('The text cursor is outside the source.')
    if selection is not None and (not isinstance(selection, (tuple, list)) or len(selection) != 2
            or any(type(value) is not int for value in selection)
            or not 0 <= selection[0] <= selection[1] <= len(text)):
        raise ValueError('The selected text range is outside the source.')
    if location is not None and (type(location) is not ReadingLocation
            or location.text_sha256 != text_hash(text)):
        raise ValueError('The reader text does not match its exact source location.')
    if source_loader is not None and not callable(source_loader):
        raise ValueError('A callable source reader is required for book reading.')
    if (type(ordinal) is not int or type(total) is not int or not 1 <= ordinal <= total <= 1000000):
        raise ValueError('The source location is outside the selected book.')


class LiveReadingDialog:
    def __init__(self, parent, root, text, *, title='Read aloud', cursor=0, selection=None,
                 location=None, guard=lambda: None, source_loader=None, ordinal=1, total=1,
                 registry=None, on_ready=None):
        validate_input(text, cursor, selection, location, source_loader, ordinal, total)
        if not callable(guard) or on_ready is not None and not callable(on_ready):
            raise ValueError('A valid reader owner is required.')
        self.root = checked_directory(Path(root).absolute())
        guard()
        import wx
        self.wx, self.guard = wx, guard
        self.source, self.location = text, location
        self.source_loader, self.ordinal, self.total = source_loader, ordinal, total
        self.registry, self.on_ready = registry, on_ready
        self.dialog = wx.Dialog(parent, title='Read aloud — ' + title,
                                size=(920, 780), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Live source reading')
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='live-reading')
        self.cancelled = threading.Event()
        self.future = self.session = self.media_request = None
        self.closed = self.close_pending = self.playing = self.through_book = False
        self.showing = self.destroyed = False
        self.auto_advance = False
        self.pause_requested = False
        self.cleanup_pending = False
        self.warning = ''
        self.voice_rows, self.device_ids = [], [None]
        self.player = WavePlayer()
        from .operation_progress import operation_progress_for
        self.progress = operation_progress_for(parent)
        self.unregister_player = (self.progress.register_player(self.player)
                                  if self.progress is not None else lambda: None)
        layout = wx.BoxSizer(wx.VERTICAL)

        def field(label, factory=wx.TextCtrl, proportion=0, *, parent=None, sizer=None, **options):
            parent = self.dialog if parent is None else parent
            sizer = layout if sizer is None else sizer
            sizer.Add(wx.StaticText(parent, label=label), 0, wx.ALL, 4)
            control = factory(parent, **options)
            control.SetName(label.replace('&', ''))
            sizer.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
            return control

        self.text = field('Read-only &text; the highlighted utterance is an exact source range',
                          value=text, proportion=2, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.text.SetMinSize((-1, 140))
        settings_panel = wx.ScrolledWindow(self.dialog, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
        settings_panel.SetName('Local voice and reading settings')
        settings_panel.SetScrollRate(0, 12)
        settings_panel.SetMinSize((-1, 140))
        settings_grid = wx.FlexGridSizer(cols=2, vgap=0, hgap=4)
        settings_grid.AddGrowableCol(1, 1)
        def setting(label, factory=wx.TextCtrl, **options):
            return field(label, factory, parent=settings_panel, sizer=settings_grid, **options)
        self.voice = setting('Explicit local &voice', wx.Choice, choices=[])
        self.language = setting('&Language code for the selected voice', value='en')
        self.rate = setting('Synthesis &rate, -10 to 10', value='0')
        self.pitch = setting('Voice-native &pitch, -12 to 12; when supported', value='0')
        self.volume = setting('Synthesis &volume, 0 to 1', value='0.8')
        self.modes = list(MODES) + (['book'] if source_loader is not None else [])
        labels = ['Selected text', 'From cursor', 'Current word', 'Current line',
                  'Current sentence (punctuation boundaries)', 'Current paragraph',
                  'Whole current source location']
        if source_loader is not None:
            labels.append('Whole book from its first source location')
        self.mode = setting('Reading &range', wx.Choice, choices=labels)
        self.mode.SetSelection(0 if selection is not None and selection[0] != selection[1] else 1)
        self.device = setting('Audio output &device', wx.Choice, choices=['System default output'])
        self.device.SetSelection(0)
        self.seek_seconds = setting('Seek within the current utterance, seconds', value='0')
        settings_panel.SetSizer(settings_grid)
        layout.Add(settings_panel, 1, wx.EXPAND | wx.ALL, 4)
        self.status = field('Reading status; sentence-level highlight, no synthesized word timing',
            value='Choose Refresh voices, then explicitly choose a voice and reading range. '
                  'Source text stays unchanged. Pause and precise seeking require native Windows playback.',
            style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetMinSize((-1, 64))
        self.controls = {}
        row = wx.WrapSizer(wx.HORIZONTAL)
        for name, label, action in (
                ('voices', '&Refresh voices and output devices', self.reload),
                ('read', '&Read aloud', self.read),
                ('prepare', 'Prepare first &excerpt', lambda event: self.read(event, play=False)),
                ('play', 'Play prepared excerpt', self.play),
                ('pause', '&Pause / resume', self.pause),
                ('stop', '&Stop reading', self.stop),
                ('seek', 'Seek within utterance', self.seek),
                ('resume', 'Restore saved text cursor', self.restore_cursor),
                ('where', 'Where am I?', self.where),
                ('close', '&Close reader', self.close)):
            button = wx.Button(self.dialog, id=wx.ID_CLOSE if name == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, action)
            row.Add(button, 0, wx.ALL, 3)
            self.controls[name] = button
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CLOSE)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.voice.Bind(wx.EVT_CHOICE, self._voice_controls)
        self.timer = wx.Timer(self.dialog)
        self.dialog.Bind(wx.EVT_TIMER, self.tick, self.timer)
        self.timer.Start(100)
        try:
            self._select(selection or (cursor, cursor))
        except BaseException:
            self.timer.Stop()
            self.player.close()
            self.unregister_player()
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.dialog.Destroy()
            raise
        self._enable()

    def _say(self, message):
        if not self.closed:
            self.status.ChangeValue(str(message))

    def _error(self, error):
        self._say(str(error))
        self._enable()

    def _select(self, selected):
        first, last = selected
        self.text.SetSelection(native_offset(self.text, self.source, first),
                               native_offset(self.text, self.source, last))

    def _stopped(self):
        self.guard()
        return self.cancelled.is_set() or self.closed

    def _source_guard(self):
        self.guard()
        if self.source_loader is not None:
            page = self.source_loader(self.ordinal, self._stopped)
            if page.location != self.location or page.raw_text != self.source:
                raise ValueError('The source changed during live reading. Reopen it explicitly.')

    def _enable(self):
        if self.closed:
            return
        idle = self.future is None and self.session is None and not self.close_pending
        for control in (self.voice, self.language, self.rate, self.pitch, self.volume, self.mode):
            control.Enable(idle)
        for key in ('voices', 'read', 'prepare', 'resume'):
            self.controls[key].Enable(idle)
        ready = self.session is not None and self.session.current is not None and self.future is None
        self.controls['play'].Enable(ready and not self.playing and not self.close_pending)
        self.controls['pause'].Enable(self.playing and self.player.native is not None)
        self.controls['seek'].Enable(ready and self.player.native is not None and self.player.path is not None)
        self.controls['stop'].Enable(self.session is not None or self.future is not None)
        if idle:
            self._voice_controls()

    def _voice_controls(self, event=None):
        index = self.voice.GetSelection()
        if 0 <= index < len(self.voice_rows):
            voice = self.voice_rows[index]
            for name, control, default in (('rate', self.rate, '0'), ('pitch', self.pitch, '0'),
                                            ('volume', self.volume, '1')):
                supported = name in voice.controls
                control.Enable(supported and self.session is None and self.future is None)
                if not supported:
                    control.ChangeValue(default)

    def _work(self, action, done):
        if self.future is not None or self.closed:
            return False
        self.future = self.executor.submit(action)
        self._enable()
        def completed(future):
            def apply():
                if self.closed:
                    return
                self.future = None
                try:
                    result = future.result()
                    done(result)
                except Exception as exc:
                    self._error(exc)
                finally:
                    if self.cleanup_pending and self.future is None:
                        self._cleanup()
                    self._enable()
            self.wx.CallAfter(apply)
        self.future.add_done_callback(completed)
        return True

    def reload(self, event=None):
        self.cancelled.clear()
        self._say('Discovering explicitly configured local voices and output devices…')
        def discover():
            self.guard()
            if self.registry is None:
                from ..runtime_settings import configured_voice_registry
                self.registry = configured_voice_registry(self.root)
            voices = self.registry.voices(refresh=True, cancelled=self._stopped)
            devices = output_devices() if self.player.native is not None else ()
            return voices, devices
        def discovered(result):
            self.voice_rows = list(result[0])
            self.voice.SetItems([voice.name + ' — ' + ', '.join(voice.languages) + ' — ' + voice.id
                                for voice in self.voice_rows])
            self.voice.SetSelection(self.wx.NOT_FOUND)
            self.device_ids = [None] + [device.id for device in result[1]]
            self.device.SetItems(['System default output'] + [device.name for device in result[1]])
            self.device.SetSelection(0)
            self._say(f'{len(self.voice_rows)} local voices found. Choose one explicitly. '
                      'Voice controls are independent of playback speed and pitch.')
        self._work(discover, discovered)

    def _settings(self):
        index = self.voice.GetSelection()
        if not 0 <= index < len(self.voice_rows):
            raise ValueError('Refresh local voices and explicitly choose one before reading.')
        result = SpeechSettings(self.voice_rows[index].id, self.language.GetValue().strip(),
                                float(self.rate.GetValue()), float(self.pitch.GetValue()),
                                float(self.volume.GetValue()))
        result.check_voice(self.voice_rows[index])
        return result

    def read(self, event=None, *, play=True):
        if self.future is not None or self.session is not None or self.close_pending:
            return
        try:
            self.guard()
            settings = self._settings()
            index = self.mode.GetSelection()
            if not 0 <= index < len(self.modes):
                raise ValueError('Choose a reading range.')
            mode = self.modes[index]
            first, last = selected_source_offsets(self.text, self.source)
            cursor = source_offset(self.source, self.text.GetRange(0, self.text.GetInsertionPoint()))
            self.cancelled.clear()
            self.through_book = mode == 'book' and play
            if mode == 'book':
                self._load_page(1, settings, play)
            else:
                selected = select_range(self.source, mode, cursor=cursor, selection=(first, last))
                self._new_session(settings, selected, play)
        except Exception as exc:
            self._error(exc)

    def _new_session(self, settings, selected, play):
        self.session = SpeechSession(self.root, self.source, settings, selected,
                                     registry=self.registry, guard=self.guard, location=self.location,
                                     validate_source=self._source_guard)
        self.auto_advance = play
        self._prepare(play)

    def _load_page(self, ordinal, settings, play):
        def loaded(page):
            if self.cleanup_pending or self.close_pending or self.cancelled.is_set():
                return
            self.source, self.location, self.ordinal, self.total = page.raw_text, page.location, page.ordinal, page.total
            self.text.ChangeValue(self.source)
            if not self.source.strip():
                if self.through_book and ordinal < self.total:
                    self._load_page(ordinal + 1, settings, play)
                else:
                    self._say('The selected source location has no text to speak.')
                return
            self._new_session(settings, SpeechSelection(0, len(self.source)), play)
        self._work(lambda: self.source_loader(ordinal, self._stopped), loaded)

    def _prepare(self, play):
        session = self.session
        self._say('Preparing the next bounded utterance with the selected local voice…')
        def prepared(result):
            if self.session is not session or self.cleanup_pending or self.cancelled.is_set():
                return
            if result is None:
                self.playing = False
                settings, following = session.settings, self.ordinal + 1
                self._finish_source(settings, following)
                return
            self._select((result.selection.start, result.selection.end))
            self._say(f'Prepared characters {result.selection.start}–{result.selection.end}; '
                      f'{result.frames / result.sample_rate:.2f} measured seconds. '
                      'Highlight covers this utterance; word timings are unavailable.')
            if play:
                self.play()
        self._work(session.render_next, prepared)

    def _finish_source(self, settings, following):
        session = self.session
        def finish():
            session.checkpoint()
            session.close()
        def finished(_):
            self.session = None
            if self.through_book and following <= self.total and not self.close_pending:
                self._load_page(following, settings, True)
            else:
                self._say('Selected reading completed. The exact completed text cursor was saved.')
        self._work(finish, finished)

    def play(self, event=None):
        if self.session is None or self.session.current is None or self.future is not None:
            return
        session, excerpt = self.session, self.session.current
        def begin():
            if self.session is not session or self.cleanup_pending or self.closed:
                return
            try:
                session.check_playback()
                index = self.device.GetSelection()
                if not 0 <= index < len(self.device_ids):
                    raise ValueError('Choose an available audio output device.')
                self.player.play(excerpt.path, device_id=self.device_ids[index])
                self.playing = True
                self.pause_requested = False
                self._enable()
            except Exception as exc:
                self._error(exc)
        def pinned(result):
            if (self.session is not session or result is not excerpt or self.cleanup_pending
                    or self.close_pending or self.cancelled.is_set()):
                return
            if self.media_request is not None:
                self.media_request.close()
            if self.progress is None:
                begin()
            else:
                self.media_request = self.progress.media_action(begin,
                    alive=lambda: not self.closed and self.session is session and not self.cleanup_pending,
                    failed=self._error)
        self._say('Verifying the prepared utterance before audio playback…')
        self._work(session.pin_playback, pinned)

    def pause(self, event=None):
        try:
            if self.player.status().state == 'paused':
                self.player.resume()
                self.pause_requested = False
                self._say('Reading resumed.')
            else:
                self.player.pause()
                self.pause_requested = True
                self._say('Reading paused. The current utterance and source range are retained.')
        except Exception as exc:
            self._error(exc)

    def seek(self, event=None):
        try:
            self.player.seek(float(self.seek_seconds.GetValue()))
            self.where()
        except Exception as exc:
            self._error(exc)

    def where(self, event=None):
        try:
            status = self.player.status()
            span = None if self.session is None or self.session.current is None else self.session.current.selection
            self._say(f'Source location {self.ordinal} of {self.total}. '
                      + (f'Utterance characters {span.start}–{span.end}. ' if span is not None else '')
                      + f'Playback {status.state}; {status.position_seconds:.2f} of '
                        f'{status.duration_seconds:.2f} seconds; position: {status.position_accuracy}. '
                        'This is an utterance highlight, not word-level alignment.')
        except Exception as exc:
            self._error(exc)

    def restore_cursor(self, event=None):
        text, location = self.source, self.location
        self.cancelled.clear()
        def load():
            self._source_guard()
            if self._stopped():
                raise InterruptedError('Restoring the saved text cursor was stopped.')
            row = SpeechPositionStore(self.root).load(text, location)
            self._source_guard()
            if self._stopped():
                raise InterruptedError('Restoring the saved text cursor was stopped.')
            return row
        def restored(row):
            if (self.cleanup_pending or self.close_pending or self.cancelled.is_set()
                    or (self.source, self.location) != (text, location)):
                return
            if row is None:
                self._say('There is no saved cursor for this exact source text.')
                return
            self._select((row['cursor'], row['cursor']))
            self.mode.SetSelection(self.modes.index('cursor'))
            self._say(f'Saved text cursor {row["cursor"]} restored. Choose a local voice explicitly; '
                      'restoring a cursor does not start speech or activate an old runtime.')
        self._work(load, restored)

    def tick(self, event=None):
        if self.closed or self.future is not None or not self.playing or self.session is None:
            return
        try:
            status = self.player.status()
            if status.state == 'completed':
                self.player.stop()
                self.playing = False
                self.session.release(completed=True)
                session = self.session
                def remembered(_):
                    if self.cleanup_pending:
                        return
                    if self.auto_advance:
                        self._prepare(True)
                    else:
                        self.stop()
                self._work(session.checkpoint, remembered)
            elif status.state in ('error', 'unavailable'):
                self.player.stop()
                self.playing = False
                self._error(status.error or 'Audio output is unavailable. The prepared excerpt was retained.')
        except Exception as exc:
            self._error(exc)

    def stop(self, event=None):
        self.playing = self.through_book = False
        self.cancelled.set()
        if self.session is not None:
            self.session.cancel.set()
        if self.media_request is not None:
            self.media_request.close()
            self.media_request = None
        try:
            self.player.stop()
        except Exception as exc:
            self._error(exc)
            return
        self.cleanup_pending = True
        self._say('Stopping the selected voice and releasing audio. The last completed text cursor is retained.')
        if self.future is None:
            self._cleanup()

    def _cleanup(self):
        if self.future is not None:
            return
        self.cleanup_pending = False
        session = self.session
        if session is None:
            if self.close_pending:
                self._close_now()
            return
        def release():
            warning = ''
            try:
                session.checkpoint()
            except Exception as exc:
                warning = 'The last completed cursor could not be saved: ' + str(exc)
            session.close()
            return warning
        def released(warning):
            self.session = None
            self.warning = warning
            self._say(warning or 'Reading stopped; the current source text is unchanged.')
            if self.close_pending:
                self._close_now()
        self._work(release, released)

    def close(self, event=None):
        if self.closed:
            return
        self.close_pending = True
        self.stop()

    def _close_now(self):
        if self.closed:
            return
        self.player.close()
        self.unregister_player()
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
            # Normal close keeps the dialog alive until all native owners have
            # settled. Never delete a live utterance from a late UI callback.
            self.showing = False
            if not self.closed:
                self.close()
            if self.closed:
                self._destroy()
        return self.warning


def show_live_reader(parent, root, text, **options):
    return LiveReadingDialog(parent, root, text, **options).show()
