"""Accessible synchronized publication reading on the current audio owners."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from ..audio.jobs import checked_directory
from ..audio.player import WavePlayer
from ..audio.playback_processing import PlaybackControls
from ..audio.playback import output_devices
from ..reader.media_overlay import OverlaySession, ReadingBudget, prepare_overlay
from ..reader.publication_state import PublicationPositions
from ..security.admission import admit_new_operation, operation_context


def choice_window(rows, index, size=200):
    """Bound native choice population while keeping every interval reachable."""
    if (type(index) is not int or not 0 <= index < len(rows)
            or type(size) is not int or not 1 <= size <= 200):
        raise ValueError('Choose an available bounded publication navigation window.')
    start = index // size * size
    return start, rows[start:start + size], index - start


class MediaOverlayDialog:
    def __init__(self, frame, *, on_ready=None):
        if (getattr(frame, '_closing', False) or getattr(frame, 'working', False)
                or getattr(frame, 'busy', False)):
            raise ValueError('Finish or cancel the current operation before opening recorded reading.')
        if on_ready is not None and not callable(on_ready):
            raise ValueError('A callable reader observer is required.')
        self.frame, self.library = frame, frame.library
        self.root = checked_directory(self.library.root)
        root_info = self.root.stat()
        self.root_identity = (root_info.st_dev, root_info.st_ino)
        self.trust_controller = getattr(self.library, 'trust_controller', None)
        self.on_ready = on_ready
        self.guard()
        import wx
        self.wx = wx
        self.closed = self.destroyed = self.showing = False
        self.close_pending = self.stop_pending = self.settling = False
        self.future = self.session = self.media_request = None
        self.executor = self.player = self.timer = None
        self.unregister_player = lambda: None
        self.cancelled = Event()
        self.serial = 0
        self.warning = ''
        self.device_ids = [None]
        self.chapter_start = self.interval_start = 0
        self.dialog = wx.Dialog(frame, title='Read synchronized EPUB or DAISY',
                                size=(940, 800), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            self.dialog.SetName('Synchronized publication reading')
            self._build()
            # Create native/worker owners only after every control was built.
            self.player = WavePlayer()
            from .operation_progress import operation_progress_for
            self.progress = operation_progress_for(frame)
            if self.progress is not None:
                self.unregister_player = self.progress.register_player(self.player)
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='publication-reading')
            self.timer = wx.Timer(self.dialog)
            self.dialog.Bind(wx.EVT_TIMER, self.tick, self.timer)
            self.timer.Start(100)
            self._enable()
        except BaseException:
            if self.timer is not None:
                self.timer.Stop()
            if self.player is not None:
                self.player.close()
            self.unregister_player()
            if self.executor is not None:
                self.executor.shutdown(wait=False, cancel_futures=True)
            self.dialog.Destroy()
            raise

    def guard(self):
        current = checked_directory(self.library.root).stat()
        if (self.frame.library is not self.library or getattr(self.frame, '_closing', False)
                or checked_directory(self.library.root) != self.root
                or (current.st_dev, current.st_ino) != self.root_identity
                or getattr(self.library, 'trust_controller', None) is not self.trust_controller):
            raise ValueError('The library changed or is closing. Open recorded reading again.')

    def _build(self):
        wx = self.wx
        layout = wx.BoxSizer(wx.VERTICAL)
        panel = wx.ScrolledWindow(self.dialog, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
        panel.SetName('Publication selection and measured navigation')
        panel.SetScrollRate(0, 12)
        panel.SetMinSize((-1, 220))
        grid = wx.FlexGridSizer(cols=2, vgap=3, hgap=6)
        grid.AddGrowableCol(1, 1)

        def field(label, factory=wx.TextCtrl, **options):
            grid.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
            control = factory(panel, **options)
            control.SetName(label.replace('&', ''))
            grid.Add(control, 1, wx.EXPAND | wx.ALL, 3)
            return control

        self.path = field('Selected synchronized publication', value='')
        self.max_mib = field('Preparation budget, MiB', value='2048')
        self.minutes = field('Preparation time allowance, minutes', value='60')
        self.chapter = field('Chapter in the current navigation window', wx.Choice, choices=[])
        self.interval = field('Measured text interval in the current window', wx.Choice, choices=[])
        self.seek_seconds = field('Go to original recorded time, seconds', value='0')
        self.playback_speed = field('Playback speed, 0.25 to 4', value='1')
        self.playback_pitch = field('Playback pitch, -12 to 12 semitones', value='0')
        self.playback_volume = field('Playback volume, 0 to 1', value='1')
        self.playback_pan = field('Playback balance, -1 left to 1 right', value='0')
        self.device = field('Audio output device', wx.Choice, choices=['System default output'])
        self.device.SetSelection(0)
        panel.SetSizer(grid)
        layout.Add(panel, 1, wx.EXPAND | wx.ALL, 5)
        layout.Add(wx.StaticText(self.dialog, label='Text of the measured audio interval'), 0, wx.ALL, 4)
        self.text = wx.TextCtrl(self.dialog, value='', style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.text.SetName('Read-only measured publication text')
        self.text.SetMinSize((-1, 140))
        layout.Add(self.text, 2, wx.EXPAND | wx.ALL, 4)
        self.mapping = wx.TextCtrl(self.dialog, value='', style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.mapping.SetName('Recorded source mapping; quotation approval is separate')
        self.mapping.SetMinSize((-1, 70))
        layout.Add(self.mapping, 0, wx.EXPAND | wx.ALL, 4)
        self.status = wx.TextCtrl(self.dialog, value=
            'Choose an EPUB or DAISY ZIP made by Resonance with measured audio intervals. '
            'Preparing and navigating do not start playback. '
            'Highlight follows measured intervals; word timing is unavailable.',
            style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Publication reading status')
        self.status.SetMinSize((-1, 70))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 4)
        self.controls = {}
        row = wx.WrapSizer(wx.HORIZONTAL)
        for key, label, action in (
                ('choose', '&Choose publication...', self.choose),
                ('load', '&Prepare publication', self.load),
                ('previous_chapter', 'Previous chapter', lambda event: self.move_chapter(-1)),
                ('next_chapter', 'Next chapter', lambda event: self.move_chapter(1)),
                ('previous_interval', 'Previous measured interval', lambda event: self.move_interval(-1)),
                ('next_interval', 'Next measured interval', lambda event: self.move_interval(1)),
                ('play', '&Play / resume', self.play),
                ('pause', 'P&ause', self.pause),
                ('stop', '&Stop and save position', self.stop),
                ('seek', '&Go to audio time', self.seek),
                ('where', '&Where am I?', self.where),
                ('cancel', 'Cancel preparation', self.stop),
                ('close', '&Close reader', self.close)):
            control = wx.Button(self.dialog, id=wx.ID_CLOSE if key == 'close' else wx.ID_ANY, label=label)
            control.SetName(label.replace('&', ''))
            control.Bind(wx.EVT_BUTTON, action)
            self.controls[key] = control
            row.Add(control, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CLOSE)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.chapter.Bind(wx.EVT_CHOICE, self.select_chapter)
        self.interval.Bind(wx.EVT_CHOICE, self.select_interval)

    def _say(self, message):
        if not self.closed:
            self.status.ChangeValue(str(message))

    def _error(self, error):
        self._say(str(error))
        self._enable()

    def _stopped(self):
        self.guard()
        return self.cancelled.is_set() or self.closed

    def _report(self, message):
        serial = self.serial
        self.wx.CallAfter(lambda: self._say(message) if not self.closed
                          and not self.close_pending and serial == self.serial else None)

    def _enable(self):
        if self.closed:
            return
        idle = self.future is None and not self.close_pending and not self.stop_pending
        ready = idle and self.session is not None
        active = self.session is not None and self.session._transport_active
        for control in (self.path, self.max_mib, self.minutes):
            control.Enable(idle)
        for control in (self.chapter, self.interval, self.seek_seconds, self.device,
                        self.playback_speed, self.playback_pitch, self.playback_volume, self.playback_pan):
            control.Enable(ready and not active)
        for key in ('choose', 'load'):
            self.controls[key].Enable(idle)
        for key in ('previous_chapter', 'next_chapter', 'previous_interval', 'next_interval', 'seek'):
            self.controls[key].Enable(ready)
        self.controls['play'].Enable(ready and (not active or self.player.status().state == 'paused'))
        self.controls['pause'].Enable(ready and active and self.player.native is not None)
        self.controls['stop'].Enable(self.session is not None or self.future is not None)
        self.controls['cancel'].Enable(self.future is not None and not self.settling)
        self.controls['where'].Enable(self.session is not None)
        self.controls['close'].Enable(not self.close_pending)

    def _work(self, action, done, *, dispose=None, control=False, failed=None):
        if self.closed or self.future is not None:
            return False
        def authorized():
            if control:
                # Native cleanup remains available after the data window closes.
                return action()
            with operation_context(self.trust_controller):
                self.guard()
                admit_new_operation(self.trust_controller, 'data.read')
                return action()
        future = self.executor.submit(authorized)
        self.future = future
        self._enable()

        def completed(completed_future):
            def apply():
                if self.future is not completed_future:
                    return
                self.future = None
                result = None
                try:
                    result = completed_future.result()
                    if not control:
                        self.guard()
                        if self.close_pending or self.stop_pending or self.cancelled.is_set():
                            if dispose is not None:
                                dispose(result)
                            result = None
                            return
                    done(result)
                    result = None
                except Exception as exc:
                    if result is not None and dispose is not None:
                        dispose(result)
                    if failed is not None:
                        failed(exc)
                    else:
                        self._error(exc)
                finally:
                    self._enable()
                    if (self.close_pending or self.stop_pending) and not self.settling:
                        self._settle()
            self.wx.CallAfter(apply)
        future.add_done_callback(completed)
        return True

    def _cancel_media(self):
        self.serial += 1
        if self.media_request is not None:
            self.media_request.close()
            self.media_request = None

    def choose(self, event=None):
        if self.future is not None or self.close_pending:
            return
        wx = self.wx
        dialog = wx.FileDialog(self.dialog, 'Choose a synchronized publication',
            wildcard='Synchronized publications (*.epub;*.zip)|*.epub;*.zip',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.path.ChangeValue(dialog.GetPath())
        finally:
            dialog.Destroy()

    def _dispose_loaded(self, result):
        document = result[0]
        if self.session is None or self.session.document is not document:
            document.close()

    def load(self, event=None):
        if self.future is not None or self.close_pending or self.stop_pending:
            return
        try:
            self.guard()
            selected = self.path.GetValue()
            budget = ReadingBudget(max_bytes=int(self.max_mib.GetValue()) * 1024**2,
                                   timeout=float(self.minutes.GetValue()) * 60)
            self._cancel_media()
            previous = self.session
            if previous is not None:
                previous.stop()
            self.cancelled.clear()
            self._say('Capturing and verifying the selected publication...')
            def prepare():
                if previous is not None:
                    previous.remember(PublicationPositions(self.root), guard=self.guard)
                document = prepare_overlay(selected, library_root=self.root, budget=budget,
                    cancelled=self._stopped, progress=self._report)
                try:
                    position = PublicationPositions(self.root).load(document)
                    devices = output_devices() if self.player.native is not None else ()
                    if self._stopped():
                        raise InterruptedError('Publication preparation cancelled.')
                    return document, position, devices
                except BaseException:
                    document.close()
                    raise
            def loaded(result):
                document, position, devices = result
                new_session = OverlaySession(document, self.player)
                if position is not None:
                    new_session.seek(position)
                if previous is not None:
                    previous.dispose_document()
                self.session = new_session
                self.warning = ''
                self.device_ids = [None] + [device.id for device in devices]
                self.device.SetItems(['System default output'] + [device.name for device in devices])
                self.device.SetSelection(0)
                self._display()
                self._say(document.title + '. Prepared ' + str(len(document.intervals)) +
                    ' measured intervals. ' + ('Saved position restored. ' if position is not None else '') +
                    'Choose Play to start recorded audio.')
            self._work(prepare, loaded, dispose=self._dispose_loaded)
        except Exception as exc:
            self._error(exc)

    def _chapter_index(self):
        index = self.session.index
        return max(i for i, row in enumerate(self.session.document.chapters) if row['first_interval'] <= index)

    def _display(self):
        if self.session is None:
            return
        document, index = self.session.document, self.session.index
        row = document.intervals[index]
        self.text.ChangeValue(row['text'])
        self.text.SetSelection(0, self.text.GetLastPosition())
        fields = ('kind', 'source_ref', 'source_start', 'source_end', 'start', 'end', 'sample_rate')
        self.mapping.ChangeValue('\n'.join(key + ': ' + str(row[key])[:2000] for key in fields if key in row)
            + '\nThis packaged source mapping does not approve a research quotation.')
        self.seek_seconds.ChangeValue(format(self.session.cursor, '.6f'))
        self.interval_start, rows, selected = choice_window(document.intervals, index)
        self.interval.SetItems([str(self.interval_start + i + 1) + '. ' +
            str(item['kind']) + ' — ' + format(item['start'], '.3f') + ' s' for i, item in enumerate(rows)])
        self.interval.SetSelection(selected)
        self.chapter_start, chapters, selected = choice_window(document.chapters, self._chapter_index())
        self.chapter.SetItems([str(self.chapter_start + i + 1) + '. ' + item['title'][:500]
                               for i, item in enumerate(chapters)])
        self.chapter.SetSelection(selected)

    def _navigate(self, index):
        if self.future is not None or self.session is None or self.close_pending or self.stop_pending:
            return
        try:
            self.guard()
            self._cancel_media()
            self.session.select(index)
            self._display()
            self.where()
            self._enable()
        except Exception as exc:
            self._error(exc)

    def select_interval(self, event=None):
        selected = self.interval.GetSelection()
        if selected >= 0:
            self._navigate(self.interval_start + selected)

    def select_chapter(self, event=None):
        if self.session is None:
            return
        selected = self.chapter_start + self.chapter.GetSelection()
        if self.chapter.GetSelection() >= 0:
            self._navigate(self.session.document.chapters[selected]['first_interval'])

    def move_interval(self, direction):
        if self.session is not None:
            index = self.session.index + direction
            if 0 <= index < len(self.session.document.intervals):
                self._navigate(index)

    def move_chapter(self, direction):
        if self.session is not None:
            index = self._chapter_index() + direction
            if 0 <= index < len(self.session.document.chapters):
                self._navigate(self.session.document.chapters[index]['first_interval'])

    def seek(self, event=None):
        if self.future is not None or self.session is None or self.close_pending or self.stop_pending:
            return
        try:
            self.guard()
            self._cancel_media()
            self.session.seek(float(self.seek_seconds.GetValue()))
            self._display()
            self.where()
            self._enable()
        except Exception as exc:
            self._error(exc)

    def _playback_settings(self):
        return (PlaybackControls(float(self.playback_speed.GetValue()),
            float(self.playback_pitch.GetValue()), float(self.playback_volume.GetValue()),
            float(self.playback_pan.GetValue())),
            ReadingBudget(max_bytes=int(self.max_mib.GetValue()) * 1024**2,
                timeout=float(self.minutes.GetValue()) * 60))

    def play(self, event=None):
        if self.future is not None or self.session is None or self.close_pending or self.stop_pending:
            return
        session = self.session
        try:
            self.guard()
            controls, budget = self._playback_settings()
        except Exception as exc:
            self._error(exc)
            return
        self.cancelled.clear()
        self._cancel_media()
        serial = self.serial
        def alive():
            return (not self.closed and not self.close_pending and not self.stop_pending
                    and self.session is session and self.serial == serial and not self.cancelled.is_set())
        def begin():
            if not alive():
                return
            try:
                self.guard()
                index = self.device.GetSelection()
                if not 0 <= index < len(self.device_ids):
                    raise ValueError('Choose an available output device.')
                session.play(device_id=self.device_ids[index])
                self._say('Recorded audio started. Source-time mapping: ' + session.source_time_accuracy + '. '
                    + ('Text follows observed audio intervals.'
                    if self.player.native is not None else
                    'This external player does not report a playhead; text remains at your selected interval.'))
                self._enable()
            except Exception as exc:
                self._error(exc)
        def pinned(_):
            if self.progress is None:
                begin()
            else:
                self.media_request = self.progress.media_action(begin, alive=alive, failed=self._error)
        self._say('Verifying audio and preparing the selected playback controls...')
        self._work(lambda: session.prepare_controls(controls, library_root=self.root, budget=budget,
            cancelled=self._stopped, progress=self._report), pinned)

    def pause(self, event=None):
        if self.session is not None and self.future is None:
            try:
                self.session.pause()
                self.where()
                self._enable()
            except Exception as exc:
                self._error(exc)

    def where(self, event=None):
        if self.session is None:
            self._say('No synchronized publication is loaded.')
            return
        try:
            previous = self.session.index
            position = self.session.position
            if previous != self.session.index:
                self._display()
            status = self.player.status()
            self._say(self.session.document.title + '. Chapter ' + str(self._chapter_index() + 1) +
                ', measured interval ' + str(self.session.index + 1) + ' of ' +
                str(len(self.session.document.intervals)) + '. Selected/observed time ' +
                format(position, '.3f') + ' of ' + format(self.session.document.duration, '.3f') +
                ' seconds. Playback ' + status.state + '; position: ' + status.position_accuracy +
                '; source-time mapping: ' + self.session.source_time_accuracy +
                '. Word timing is unavailable.')
        except Exception as exc:
            self._error(exc)

    def tick(self, event=None):
        if self.closed or self.future is not None or self.session is None or not self.session._transport_active:
            return
        try:
            old = self.session.index
            self.session.position
            if old != self.session.index:
                self._display()
            state = self.player.status()
            if state.state == 'completed':
                self.stop()
            elif state.state in ('error', 'unavailable'):
                self.warning = state.error or 'Audio output is unavailable.'
                self.stop()
        except Exception as exc:
            self._error(exc)

    def stop(self, event=None):
        if self.closed:
            return
        self.cancelled.set()
        self._cancel_media()
        self.stop_pending = True
        if self.settling:
            self._enable()
            return
        try:
            # Stop audible output immediately; release the verifier only after
            # its worker settles. Never remove its private PCM early.
            if self.session is not None:
                self.session.halt()
            else:
                self.player.stop()
        except Exception as exc:
            self.close_pending = self.stop_pending = False
            self._error(exc)
            return
        self._say('Stopping audio and waiting for preparation to release its files...')
        self._enable()
        if self.future is None:
            self._settle()

    def _settle(self):
        if self.closed or self.future is not None or self.settling:
            return
        self.settling = True
        closing, session = self.close_pending, self.session
        def release():
            warning = self.warning
            if session is not None:
                session.stop()
                try:
                    session.remember(PublicationPositions(self.root), guard=self.guard)
                except Exception as exc:
                    warning = 'The reading position could not be saved: ' + str(exc)
                if closing:
                    session.close()
            elif closing:
                self.player.close()
            return warning
        def released(warning):
            self.warning = warning
            self.settling = self.stop_pending = False
            if closing:
                self.session = None
                self._finish_close()
            else:
                self._display()
                self._say(warning or 'Stopped. The position is saved for these exact publication bytes.')
        def refused(error):
            self.settling = self.close_pending = self.stop_pending = False
            self._error(error)
        # Cleanup must run even after the library/frame guard was revoked.
        # Only saving uses that guard. Native ownership must always settle.
        self._work(release, released, control=True, failed=refused)

    def close(self, event=None):
        if self.closed:
            return
        if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
            event.Veto()
        self.close_pending = True
        self.stop()

    def _finish_close(self):
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
            self.showing = False
            if not self.closed:
                self.close()
            if self.closed:
                self._destroy()
        return self.warning


def show_media_overlay(frame, *, on_ready=None):
    return MediaOverlayDialog(frame, on_ready=on_ready).show()


def install_media_overlay_actions(frame):
    existing = getattr(frame, 'media_overlay_action', None)
    if existing is not None:
        return existing
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    index = frame.menu_bar.FindMenu('Library')
    if index < 0:
        raise ValueError('Install the Library menu before synchronized reading.')
    def open_reader(event=None):
        try:
            return show_media_overlay(frame)
        except Exception as exc:
            frame.report('Synchronized reading could not open: ' + str(exc))
    item = frame.menu_bar.GetMenu(index).Append(wx.ID_ANY, 'Read synchronized EPUB or DAISY...')
    frame.Bind(wx.EVT_MENU, open_reader, item)
    register_menu_command(frame, CommandSpec('reading.publication',
                          'Read synchronized EPUB or DAISY', 'main'), item)
    frame.media_overlay_action = open_reader
    return open_reader
