"""Read an exact source passage with the same native transport as Audio Studio."""
from concurrent.futures import ThreadPoolExecutor
import threading

from ..reader.live_reader import ReaderNarrator
from .operation_progress import OperationProgress
from .playback_controls import PlaybackControls


class LiveReaderDialog:
    def __init__(self, parent, frame, reader, selection, *, narrator=None):
        import wx
        self.wx, self.frame, self.reader = wx, frame, reader
        self.dialog = wx.Dialog(parent, title='Read source aloud — ' + selection.title,
                                size=(960, 790), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.alive = self.window_alive = True
        self.busy = self.close_pending = False
        self._epoch = 0
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='source-narration')
        self.narrator = narrator or ReaderNarrator(selection, reader.desk.validate, guard=reader.check)
        self.voices = ()
        self.measured = None
        self._playback_stopped = threading.Event()
        layout = wx.BoxSizer(wx.VERTICAL)
        source = wx.TextCtrl(self.dialog, value=selection.status + '\nExact selected source text; narration does not approve quotations.\n' + selection.text,
                             style=wx.TE_MULTILINE | wx.TE_READONLY)
        source.SetName('Exact source passage and verification status')
        source.SetMinSize((-1, 110))
        layout.Add(source, 1, wx.EXPAND | wx.ALL, 5)
        row = wx.WrapSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self.dialog, label='Installed local voice'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.voice = wx.Choice(self.dialog)
        self.voice.SetName('Installed local voice; choose explicitly')
        self.voice.Bind(wx.EVT_CHOICE, self.voice_changed)
        row.Add(self.voice, 1, wx.ALL, 4)
        self.reload = wx.Button(self.dialog, label='Reload local voices')
        self.reload.SetName('Reload local voices')
        self.reload.Bind(wx.EVT_BUTTON, self.reload_voices)
        row.Add(self.reload, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.language = self._field(row, 'Language code', 'en')
        self.rate = self._spin(row, 'Speech rate, minus ten to ten', -10, 10, 0)
        self.pitch = self._spin(row, 'Speech pitch, minus ten to ten', -10, 10, 0)
        self.volume = self._spin(row, 'Speech volume percent', 0, 100, 100)
        layout.Add(row, 0, wx.EXPAND)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.render = wx.Button(self.dialog, label='Prepare and read exact passage')
        self.render.SetName('Prepare and read exact passage')
        self.render.Bind(wx.EVT_BUTTON, self.read)
        row.Add(self.render, 0, wx.ALL, 4)
        self.autoplay = wx.CheckBox(self.dialog, label='Start playback after preparation')
        self.autoplay.SetName('Start playback after preparation')
        self.autoplay.SetValue(True)
        row.Add(self.autoplay, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.cancel = wx.Button(self.dialog, label='Cancel narration preparation')
        self.cancel.SetName('Cancel narration preparation')
        self.cancel.Bind(wx.EVT_BUTTON, self.cancel_operation)
        self.cancel.Disable()
        row.Add(self.cancel, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
        self.playback = PlaybackControls(self.dialog, reader.desk.library.root, guard=self.check,
            progress=getattr(getattr(frame, 'progress_actions', None), 'panel', None))
        layout.Add(self.playback.window, 0, wx.EXPAND | wx.ALL, 5)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Source narration preparation status')
        self.status.SetMinSize((-1, 65))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 5)
        close = wx.Button(self.dialog, wx.ID_CANCEL, 'Close source narration')
        close.SetName('Close source narration')
        close.Bind(wx.EVT_BUTTON, self.close)
        layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 5)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.controls = (self.voice, self.reload, self.language, self.rate, self.pitch, self.volume, self.render, self.autoplay)

    def _field(self, row, name, value):
        row.Add(self.wx.StaticText(self.dialog, label=name), 0, self.wx.ALL | self.wx.ALIGN_CENTER_VERTICAL, 4)
        control = self.wx.TextCtrl(self.dialog, value=value, size=(85, -1))
        control.SetName(name)
        row.Add(control, 0, self.wx.ALL, 4)
        return control

    def _spin(self, row, name, minimum, maximum, value):
        row.Add(self.wx.StaticText(self.dialog, label=name), 0, self.wx.ALL | self.wx.ALIGN_CENTER_VERTICAL, 4)
        control = self.wx.SpinCtrl(self.dialog, min=minimum, max=maximum, initial=value, size=(80, -1))
        control.SetName(name)
        row.Add(control, 0, self.wx.ALL, 4)
        return control

    def check(self):
        if not self.alive or not self.window_alive:
            raise InterruptedError('The source narration window closed.')
        self.reader.check()

    def _enable(self, busy):
        self.busy = busy
        for control in self.controls:
            control.Enable(not busy)
        self.cancel.Enable(busy)
        if not busy:
            self.voice_changed()

    def voice_changed(self, event=None):
        if not self.alive or not self.window_alive or self.busy:
            return
        index = self.voice.GetSelection()
        controls = self.voices[index].controls if 0 <= index < len(self.voices) else ()
        for name, control, default in (('rate', self.rate, 0), ('pitch', self.pitch, 0),
                                        ('volume', self.volume, 100)):
            control.Enable(name in controls)
            if name not in controls:
                control.SetValue(default)

    def _run(self, label, operation, done):
        if not self.alive or self.busy:
            return
        try:
            self.check()
            self.playback.quiesce()
        except Exception as exc:
            self.status.ChangeValue(str(exc))
            return
        self.cancelled.clear()
        self._epoch += 1
        epoch = self._epoch
        self._enable(True)
        self.status.ChangeValue(label)
        measured = self.measured = OperationProgress(self.frame, label)
        pending, lock = {'text': '', 'queued': False, 'active': True}, threading.Lock()
        def deliver():
            with lock:
                text, pending['queued'] = pending['text'], False
            if self.alive and self.window_alive and self.busy and self._epoch == epoch:
                try:
                    self.check()
                    self.status.ChangeValue(text)
                except Exception:
                    pass
        def report(text):
            if not self.alive or self.cancelled.is_set():
                return
            with lock:
                if not pending['active']:
                    return
                pending['text'] = str(text)[:2000]
                if pending['queued']:
                    return
                pending['queued'] = True
            measured.update(phase=text)
            self.wx.CallAfter(deliver)
        def work():
            try:
                self.check()
                if self.cancelled.is_set():
                    raise InterruptedError('Narration cancelled.')
                return operation(report)
            finally:
                with lock:
                    pending['active'] = False
        def finished(future):
            def apply():
                if not self.alive or not self.window_alive or self._epoch != epoch:
                    measured.finish(state='cancelled')
                    return
                self._enable(False)
                try:
                    result = future.result()
                    self.check()
                    if self.cancelled.is_set():
                        raise InterruptedError('Narration cancelled; no recording was started.')
                    done(result)
                    measured.complete(result)
                except Exception as exc:
                    measured.failed(exc)
                    self.status.ChangeValue(str(exc))
                if self.close_pending:
                    self.close_pending = False
                    self.close()
            if self.alive:
                self.wx.CallAfter(apply)
            else:
                measured.finish(state='cancelled')
        try:
            self.executor.submit(work).add_done_callback(finished)
        except Exception as exc:
            self._enable(False)
            measured.failed(exc)
            self.status.ChangeValue(str(exc))

    def reload_voices(self, event=None):
        def loaded(rows):
            self.voices = tuple(row for row in rows if getattr(row, 'available', True))
            self.voice.SetItems([f'{row.name} — {", ".join(row.languages)} — {row.id}' for row in self.voices])
            self.voice.SetSelection(self.wx.NOT_FOUND)
            self.voice_changed()
            self.status.ChangeValue('Choose an installed voice, language and performance settings, then Prepare and read. '
                                    'No voice or model is downloaded automatically.' if self.voices else
                                    'No configured local voice is available. Configure a supported engine before reading aloud.')
        self._run('Discovering local voices', lambda report: self.narrator.voices(refresh=event is not None), loaded)

    def read(self, event=None):
        if not self.alive or self.busy:
            return
        try:
            index = self.voice.GetSelection()
            if not 0 <= index < len(self.voices):
                raise ValueError('Choose an installed local voice explicitly.')
            values = dict(voice=self.voices[index].id, language=self.language.GetValue(),
                          rate=self.rate.GetValue(), pitch=self.pitch.GetValue(), volume=self.volume.GetValue() / 100)
            autoplay = self.autoplay.GetValue()
            def rendered(result):
                selection = self.narrator.selection
                rate = result.get('sample_rate', 48000)
                segments = tuple((row['start_frame'] / rate, row['title']) for row in result.get('chapters', ()))
                self.playback.set_source(result['output'], owner=selection.owner, segments=segments, autoplay=autoplay,
                                        authorize=lambda: self.narrator.check(self.cancelled.is_set))
                self.status.ChangeValue('Exact source narration prepared. Audio timing comes from the rendered recording; '
                    'word or sentence highlighting is not available. The original source is unchanged. '
                    'This recording is temporary and is removed on close; use Audio Studio for saved projects and audio exports.')
            self._run('Preparing exact source narration', lambda report: self.narrator.render(**values,
                cancelled=self._cancelled,
                on_status=report), rendered)
        except Exception as exc:
            self.status.ChangeValue(str(exc))

    def cancel_operation(self, event=None):
        self.cancelled.set()
        if self.measured is not None:
            self.measured.cancel_requested()

    def _cancelled(self):
        try:
            self.check()
        except Exception:
            self.cancelled.set()
        return self.cancelled.is_set()

    def close(self, event=None):
        if not self.alive:
            return
        if self.busy:
            self.close_pending = True
            self.cancel_operation()
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        if self.playback.busy:
            self.playback.stop(on_stopped=lambda: self.close() if self.alive else None)
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        try:
            self.playback.quiesce()
        except Exception as exc:
            self.status.ChangeValue(str(exc))
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        self._dispose()
        if self.window_alive and self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CANCEL)

    def _dispose(self):
        if not self.alive:
            return
        self.alive = False
        self._epoch += 1
        self.cancelled.set()
        if self.measured is not None:
            self.measured.finish(state='cancelled')
        self.playback.dispose(on_stopped=self._playback_stopped.set)
        def clean():
            if self._playback_stopped.wait(5):
                self.narrator.close()
            # An unconfirmed audio stop retains its temporary recording.
        try:
            self.executor.submit(clean)
        except RuntimeError:
            if self._playback_stopped.is_set():
                self.narrator.close()
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _destroyed(self, event):
        if event.GetEventObject() is self.dialog:
            self.window_alive = False
            self._dispose()
        event.Skip()

    def show(self):
        try:
            self.reload_voices()
            self.dialog.ShowModal()
        finally:
            self._dispose()
            if self.window_alive:
                self.dialog.Destroy()
