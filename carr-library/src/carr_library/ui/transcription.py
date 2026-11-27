"""Accessible optional transcription through the supplied private worker adapter."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Lock

from ..transcription import (PreparedTranscription, TranscriptionBudget, TranscriptionControl,
    TranscriptionOptions, TranscriptionReceipt, TranscriptionService)


class TranscriptionActions:
    """Headless generation, selected-library and physical-root ownership."""
    def __init__(self, frame, *, service=None):
        from .language_matrix import _host_scope
        self.frame = frame
        self.library, self._host_current = _host_scope(frame)
        self.scope = tuple(frame.active) if frame.active is not None else None
        if self.scope is None:
            raise ValueError('Choose an existing library before opening transcription.')
        self.workspace = self.scope[0]
        self.service = service or TranscriptionService(self.library)
        self.generation = 0
        self.closed = self.busy = False
        self.control = TranscriptionControl()
        self.prepared = None
        self.recovered_source = None

    def current(self, generation=None):
        try:
            return (not self.closed and self._host_current()
                    and (tuple(self.frame.active) if self.frame.active is not None else None) == self.scope
                    and (generation is None or generation == self.generation))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def ensure(self):
        if not self.current():
            self.control.stop()
            raise PermissionError('The selected library changed or its window closed; reopen transcription.')

    def invalidate(self):
        self.ensure()
        if self.busy:
            raise RuntimeError('Wait for this transcription operation to stop.')
        self.generation += 1
        self.prepared = None

    def accept(self, prepared):
        self.ensure()
        if type(prepared) is not PreparedTranscription or prepared.workspace != self.workspace:
            raise PermissionError('This prepared transcription belongs to another library.')
        self.prepared = prepared
        self.recovered_source = prepared if prepared.requires_reapproval else None
        return prepared

    def start(self, executor, operation, *, progress=lambda event: None, set_running=lambda value: None):
        self.ensure()
        if self.busy:
            raise RuntimeError('A transcription operation is already running.')
        self.generation += 1
        generation = self.generation
        control = self.control = TranscriptionControl()
        self.busy = True
        def cancelled():
            if not self.current(generation):
                control.stop()
            return control.stopped
        def report(event):
            if self.current(generation):
                progress(event)
        def work():
            if cancelled():
                raise InterruptedError('Transcription cancelled before execution.')
            return operation(cancelled, control, report)
        try:
            set_running(True)
            self.ensure()
            future = executor.submit(work)
        except Exception:
            control.stop()
            self.busy = False
            set_running(False)
            raise
        return generation, future

    def prepare(self, executor, manifest, sources, *, options, budget, **callbacks):
        self.invalidate()
        selected = tuple(sources)
        return self.start(executor, lambda stopped, control, report: self.service.prepare(
            self.workspace, manifest, selected, options=options, budget=budget,
            cancelled=stopped, on_progress=report), **callbacks)

    def run(self, executor, **callbacks):
        self.ensure()
        selected = self.prepared
        if type(selected) is not PreparedTranscription or selected.workspace != self.workspace:
            raise PermissionError('Prepare or review the exact saved request before choosing Run.')
        if selected.requires_reapproval:
            raise PermissionError('Choose a current profile and prepare a new request from the restored inputs first.')
        return self.start(executor, lambda stopped, control, report: self.service.run(
            selected.id, approved_request_sha256=selected.request_sha256, control=control,
            cancelled=stopped, on_progress=report), **callbacks)

    def reprepare(self, executor, manifest, *, options, budget, **callbacks):
        self.ensure()
        selected = self.recovered_source
        if (type(selected) is not PreparedTranscription or not selected.requires_reapproval
                or selected.workspace != self.workspace):
            raise PermissionError('Review a restored source request before preparing new recognition.')
        self.invalidate()
        return self.start(executor, lambda stopped, control, report: self.service.prepare_recovered(
            selected.id, manifest, approved_source_request_sha256=selected.request_sha256,
            options=options, budget=budget, cancelled=stopped, on_progress=report), **callbacks)

    def finish(self, generation):
        if generation == self.generation:
            self.busy = False
        return self.current(generation)

    def close(self):
        self.closed = True
        self.generation += 1
        self.control.stop()


def describe_request(prepared):
    recovery = ('RESTORED INERT REQUEST. Its old paths and approvals are provenance only. '
                'Choose a current runtime profile, then Prepare snapshots from the retained inputs. '
                'Review the new request before Run.\n\n') if prepared.requires_reapproval else ''
    return (recovery + 'Prepared source snapshots. Review this exact request, then choose Run / resume.\n'
        'Preparing or selecting a saved request does not run recognition.\n\n'
        f'Job: {prepared.id}\nRequest SHA256: {prepared.request_sha256}\n'
        f'Private profile: {prepared.profile_path}\nProfile SHA256: {prepared.profile_sha256}\n'
        f'Model: {prepared.model_id}\nRevision: {prepared.model_revision}\n'
        f'Sources: {prepared.source_count}; retained bytes: {prepared.source_bytes}\n'
        + '\n'.join(prepared.source_names) + '\n\n'
        + json.dumps(prepared.options.to_dict(), ensure_ascii=False, indent=2)
        + '\n\nApproved resource budget:\n' + json.dumps(prepared.budget.to_dict(), indent=2)
        + '\n\nJSON is retained for verification even when only another export is selected. '
          'Outputs are derivatives, not quotation evidence. Translation is into English only. '
          'Pause/stop is cooperative between inference calls; the supervisor stops an unresponsive owned process. '
          'A stopped partial file is retranscribed on resume; complete verified worker checkpoints may be reused.'
        + ('\n\n' + '\n'.join(prepared.warnings) if prepared.warnings else ''))


class NativeTranscriptionDialog:
    def __init__(self, frame, *, wx_module=None, on_ready=None):
        if wx_module is None:
            import wx as wx_module
        self.wx = wx = wx_module
        self.frame = frame
        self.actions = TranscriptionActions(frame)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-transcription')
        self.alive = self.window_alive = True
        self.receipt = None
        self.sources = ()
        self.latest = None
        self.latest_lock = Lock()
        self.measured = None
        self.on_ready = on_ready
        self.dialog = wx.Dialog(frame, title='Local transcription — reviewed private runtime', size=(850, 850),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._window_id = self.dialog.GetId()
        self.dialog.SetName('Local transcription')
        outer = wx.BoxSizer(wx.VERTICAL)
        page = wx.ScrolledWindow(self.dialog, style=wx.VSCROLL)
        page.SetScrollRate(0, 20)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.inputs = []
        def field(label, cls=wx.TextCtrl, proportion=0, **kwargs):
            layout.Add(wx.StaticText(page, label=label), 0, wx.LEFT | wx.TOP, 5)
            control = cls(page, **kwargs)
            control.SetName(label.replace('&', ''))
            layout.Add(control, proportion, wx.ALL | wx.EXPAND, 5)
            self.inputs.append(control)
            return control
        self.profile = field('Reviewed runtime &profile JSON')
        self.source_list = field('Selected source snapshots', wx.ListBox, choices=[])
        self.source_list.SetMinSize((-1, 70))
        self.language = field('Input &language code (auto or a profile-declared code)', value='auto')
        self.task = field('&Operation', wx.Choice, choices=['Transcribe speech', 'Translate speech into English'])
        self.task.SetSelection(0)
        self.device = field('&Device', wx.Choice, choices=['cpu', 'cuda'])
        self.device.SetSelection(0)
        self.compute = field('Compute &type', wx.Choice,
                             choices=['int8', 'int8_float16', 'int8_float32', 'float16', 'float32'])
        self.compute.SetSelection(0)
        formats = wx.BoxSizer(wx.HORIZONTAL)
        self.formats = {}
        for name in ('txt', 'srt', 'vtt', 'json'):
            box = wx.CheckBox(page, label=name.upper() + ' output')
            box.SetName(name.upper() + ' output')
            box.SetValue(name == 'txt')
            self.formats[name] = box
            self.inputs.append(box)
            formats.Add(box, 0, wx.ALL, 4)
        layout.Add(formats)
        self.disk = field('Working disk budget (&MiB, including retained inputs)', value='4096')
        self.deadline = field('Run or resume time budget (&seconds)', value='3600')
        self.receipt_token = field('Receipt SHA256 (enter an independent value for a saved result)')
        self.review = field('Request review and derivative results', proportion=1,
                            style=wx.TE_MULTILINE | wx.TE_READONLY,
                            value='Choose a reviewed local runtime and explicit input files. No model is downloaded. '
                                  'Prepare snapshots, review the request, then choose Run. Speech accuracy is not qualified here.')
        self.review.SetMinSize((-1, 180))
        self.inputs.remove(self.review)
        page.SetSizer(layout)
        outer.Add(page, 1, wx.EXPAND)
        self.status = wx.StaticText(self.dialog, label='Idle; no recognition has run.')
        self.status.SetName('Transcription operation status')
        outer.Add(self.status, 0, wx.ALL | wx.EXPAND, 5)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.buttons = {}
        definitions = [('profile', 'Choose profile', self.choose_profile), ('sources', 'Choose sources', self.choose_sources),
            ('prepare', '&Prepare snapshots', self.prepare), ('run', '&Run / resume', self.run),
            ('pause', '&Pause / continue', self.pause), ('stop', '&Stop', self.stop),
            ('jobs', 'Saved &jobs', self.jobs), ('read', '&Read derivative', self.read),
            ('close', '&Close', self.close)]
        for name, label, handler in definitions:
            button = wx.Button(self.dialog, label=label)
            button.SetName(label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, handler)
            self.buttons[name] = button
            buttons.Add(button, 0, wx.ALL, 3)
        outer.Add(buttons, 0, wx.EXPAND)
        self.dialog.SetSizer(outer)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
        for control in (self.profile, self.language, self.disk, self.deadline):
            control.Bind(wx.EVT_TEXT, self.changed)
        for control in (self.task, self.device, self.compute):
            control.Bind(wx.EVT_CHOICE, self.changed)
        for control in self.formats.values():
            control.Bind(wx.EVT_CHECKBOX, self.changed)
        self.receipt_token.Bind(wx.EVT_TEXT, lambda event: self.set_running(self.actions.busy))
        self.timer = wx.Timer(self.dialog)
        self.dialog.Bind(wx.EVT_TIMER, self.tick, self.timer)
        self.timer.Start(200)
        self.set_running(False)

    def current(self, generation=None):
        return self.alive and self.window_alive and self.actions.current(generation)

    def set_running(self, value):
        if not self.current():
            return
        for control in self.inputs:
            control.Enable(not value)
        for name in ('profile', 'sources', 'prepare', 'jobs'):
            self.buttons[name].Enable(not value)
        self.buttons['run'].Enable(not value and self.actions.prepared is not None
                                  and not self.actions.prepared.requires_reapproval and self.receipt is None)
        self.buttons['read'].Enable(not value and (self.receipt is not None or
            (self.actions.prepared is not None and bool(self.receipt_token.GetValue().strip()))))
        self.buttons['pause'].Enable(value)
        self.buttons['stop'].Enable(value)

    def changed(self, event=None):
        if self.current() and not self.actions.busy:
            self.actions.invalidate()
            self.receipt = None
            self.receipt_token.ChangeValue('')
            self.set_running(False)
        if event is not None:
            event.Skip()

    def _dispose(self, window):
        try:
            if window and not window.IsBeingDeleted():
                window.Destroy()
        except RuntimeError:
            pass

    def choose_profile(self, event=None):
        if not self.current() or self.actions.busy:
            return
        generation = self.actions.generation
        picker = self.wx.FileDialog(self.dialog, 'Choose reviewed private runtime profile',
                                    wildcard='JSON files (*.json)|*.json', style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == self.wx.ID_OK and self.current(generation):
                self.profile.SetValue(picker.GetPath())
        finally:
            self._dispose(picker)

    def choose_sources(self, event=None):
        if not self.current() or self.actions.busy:
            return
        generation = self.actions.generation
        picker = self.wx.FileDialog(self.dialog, 'Choose explicit local audio or video files',
            wildcard='Audio and video|*.wav;*.mp3;*.m4a;*.mp4;*.ogg;*.opus;*.flac;*.aac;*.wma;*.webm;*.mkv;*.3gp;*.amr;*.m4b',
            style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST | self.wx.FD_MULTIPLE)
        try:
            if picker.ShowModal() == self.wx.ID_OK and self.current(generation):
                self.sources = tuple(picker.GetPaths())
                self.actions.recovered_source = None
                self.source_list.Set(list(self.sources))
                self.changed()
        finally:
            self._dispose(picker)

    def _progress(self, event):
        with self.latest_lock:
            self.latest = self.actions.generation, event
        if self.measured is not None and event.state != 'completed':
            self.measured.update(phase=event.phase, completed=event.completed,
                                 total=event.total, state=event.state)

    def tick(self, event=None):
        if not self.current():
            self.actions.control.stop()
            return
        with self.latest_lock:
            latest, self.latest = self.latest, None
        if latest is not None and self.current(latest[0]):
            value = latest[1]
            self.status.SetLabel(f'{value.completed}/{value.total}: {value.phase}')

    def _launch(self, start, apply):
        if not self.current() or self.actions.busy:
            return
        try:
            from .operation_progress import OperationProgress
            self.measured = OperationProgress(self.frame, 'Local transcription', phase='Preparing the selected operation')
            measured = self.measured
            generation, future = start()
        except Exception as exc:
            self.set_running(False)
            if self.current():
                self.status.SetLabel(self._error(exc))
            if self.measured is not None:
                self.measured.failed(exc)
            return
        def completed(done):
            if not self.current(generation):
                measured.finish(state='cancelled')
                return
            def deliver():
                if not self.current(generation) or not self.actions.finish(generation):
                    measured.finish(state='cancelled')
                    return
                self.set_running(False)
                try:
                    result = done.result()
                    apply(result)
                    measured.complete()
                except Exception as exc:
                    if self.current():
                        self.status.SetLabel(self._error(exc))
                    measured.failed(exc)
                self.set_running(False)
            try:
                self.wx.CallAfter(deliver)
            except RuntimeError:
                self.actions.close()
                measured.finish(state='cancelled')
        future.add_done_callback(completed)

    @staticmethod
    def _error(error):
        if isinstance(error, InterruptedError):
            return 'Stopped. Completed worker checkpoints remain available; no unfinished transcript is marked complete.'
        if isinstance(error, TimeoutError):
            return 'The approved time budget expired. Review the saved job before resuming.'
        if isinstance(error, (ValueError, PermissionError, FileNotFoundError, FileExistsError)):
            return str(error)[:800]
        return 'The local operation could not complete. Retained inputs and checkpoints were kept; inspect the reviewed local runtime.'

    def _review(self, prepared):
        self.actions.accept(prepared)
        if prepared.requires_reapproval:
            self.sources = ()
            self.source_list.Set(['Retained input: ' + name for name in prepared.source_names])
        self.receipt = None
        self.receipt_token.ChangeValue('')
        self.review.ChangeValue(describe_request(prepared))
        self.status.SetLabel('Restored inputs are inert. Choose a current profile and Prepare snapshots again.'
            if prepared.requires_reapproval else
            'Request ready for explicit review. Choose Run / resume to authorize recognition.')
        self.review.SetFocus()

    def prepare(self, event=None):
        if not self.current() or self.actions.busy:
            return
        try:
            options = TranscriptionOptions(language=self.language.GetValue().strip(),
                task='translate' if self.task.GetSelection() == 1 else 'transcribe',
                device=self.device.GetStringSelection(), compute_type=self.compute.GetStringSelection(),
                formats=tuple(name for name, control in self.formats.items() if control.GetValue()))
            total = int(self.disk.GetValue()) * 1024**2
            budget = TranscriptionBudget(max_input_bytes=total // 2, max_job_bytes=total,
                                         max_seconds=float(self.deadline.GetValue()))
            manifest = self.profile.GetValue().strip()
        except (ValueError, TypeError) as exc:
            self.status.SetLabel(self._error(exc))
            return
        if self.actions.recovered_source is not None and not self.sources:
            self._launch(lambda: self.actions.reprepare(self.executor, manifest,
                options=options, budget=budget, progress=self._progress, set_running=self.set_running), self._review)
        else:
            self._launch(lambda: self.actions.prepare(self.executor, manifest, self.sources,
                options=options, budget=budget, progress=self._progress, set_running=self.set_running), self._review)

    def run(self, event=None):
        def complete(receipt):
            if type(receipt) is not TranscriptionReceipt or receipt.quotation_authority is not False:
                raise ValueError('Invalid completed derivative receipt.')
            self.receipt = receipt
            self.receipt_token.ChangeValue(receipt.receipt_sha256)
            self.review.ChangeValue('Verified derivative files were published. Speech accuracy still requires review.\n'
                'Quotation authority: false.\nReceipt SHA256: ' + receipt.receipt_sha256 + '\n\n'
                + '\n'.join(receipt.outputs) + '\n' + '\n'.join(receipt.warnings))
            self.status.SetLabel('Completed derivative receipt retained. Choose Read derivative to verify and view the text.')
        self._launch(lambda: self.actions.run(self.executor, progress=self._progress,
                                              set_running=self.set_running), complete)

    def pause(self, event=None):
        if self.current() and self.actions.busy:
            if self.actions.control.paused:
                self.actions.control.resume()
                self.status.SetLabel('Continue requested.')
            else:
                self.actions.control.pause()
                self.status.SetLabel('Pause requested; waiting for an inference boundary.')

    def stop(self, event=None):
        if self.current() and self.actions.busy:
            self.actions.control.stop()
            self.status.SetLabel('Stop requested; waiting for the owned process to exit.')
            if self.measured is not None:
                self.measured.cancel_requested()

    def jobs(self, event=None):
        def choose(rows):
            rows = tuple(row for row in rows if row.workspace == self.actions.workspace)
            generation = self.actions.generation
            picker = self.wx.SingleChoiceDialog(self.dialog, 'Select a saved request, then review it before Run.',
                'Saved transcription jobs', [f'{row.id}; {row.source_count} files; {row.options.task}' for row in rows])
            try:
                if (picker.ShowModal() == self.wx.ID_OK and self.current(generation)
                        and 0 <= picker.GetSelection() < len(rows)):
                    self._review(rows[picker.GetSelection()])
            finally:
                self._dispose(picker)
        self._launch(lambda: self.actions.start(self.executor,
            lambda stopped, control, report: self.actions.service.jobs(workspace=self.actions.workspace,
                cancelled=stopped), set_running=self.set_running), choose)

    def read(self, event=None):
        selected, receipt = self.actions.prepared, self.receipt
        token = receipt.receipt_sha256 if receipt is not None else self.receipt_token.GetValue().strip()
        if selected is None:
            return
        def operation(stopped, control, report):
            verified = self.actions.service.verify(selected.id, expected_receipt_sha256=token, cancelled=stopped)
            return verified, self.actions.service.read_text(verified, cancelled=stopped)
        def display(result):
            self.receipt, rows = result
            self.review.ChangeValue('Derived transcript; not quotation evidence.\n\n' + '\n\n'.join(
                name + '\n' + text for name, text in rows))
            self.review.SetFocus()
        self._launch(lambda: self.actions.start(self.executor, operation, set_running=self.set_running), display)

    def close(self, event=None):
        if self.actions.busy and self.measured is not None:
            self.measured.cancel_requested()
        self.actions.close()
        self.alive = False
        if self.window_alive:
            self.timer.Stop()
            if self.dialog.IsModal():
                self.dialog.EndModal(self.wx.ID_CANCEL)
            else:
                self.dialog.Destroy()

    def destroyed(self, event):
        # The native destroy event carries the window ID even when its Python
        # object is a different wrapper; never query the deleted dialog here.
        if event.GetId() == self._window_id:
            if self.actions.busy and self.measured is not None:
                self.measured.cancel_requested()
            self.window_alive = self.alive = False
            self.actions.close()
            self.executor.shutdown(wait=False, cancel_futures=True)
        event.Skip()

    def show(self):
        try:
            if self.on_ready is not None:
                self.wx.CallAfter(lambda: self.on_ready(self) if self.current() else None)
            self.dialog.ShowModal()
        finally:
            if self.actions.busy and self.measured is not None:
                self.measured.cancel_requested()
            self.actions.close()
            self.alive = False
            self.executor.shutdown(wait=False, cancel_futures=True)
            if self.window_alive:
                self.timer.Stop()
                self._dispose(self.dialog)
        return self.receipt


def show_transcription(frame, *, on_ready=None):
    return NativeTranscriptionDialog(frame, on_ready=on_ready).show()


def install_transcription_actions(frame):
    """Root composes this action before installing the shared keyboard manager."""
    if getattr(frame, '_transcription_item', None) is not None:
        return frame._transcription_item
    import wx
    bar = frame.GetMenuBar()
    if bar is None:
        raise ValueError('Install the native menu bar before transcription.')
    menu = None
    for index in range(bar.GetMenuCount()):
        if bar.GetMenuLabel(index).replace('&', '') == 'Audio':
            menu = bar.GetMenu(index)
            break
    if menu is None:
        menu = wx.Menu()
        bar.Append(menu, '&Audio')
    item = menu.Append(wx.ID_ANY, 'Local transcription...')
    def open_dialog(event):
        if not getattr(frame, 'active', None):
            wx.MessageBox('Choose an existing library first.', 'Local transcription', wx.OK | wx.ICON_INFORMATION, frame)
            return
        show_transcription(frame)
    frame.Bind(wx.EVT_MENU, open_dialog, item)
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec('transcription.open', 'Local transcription'), item)
    frame._transcription_item = item
    return item

