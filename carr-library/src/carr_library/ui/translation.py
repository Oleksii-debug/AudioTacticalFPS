"""Keyboard-accessible reviewed text translation; opening never contacts Google."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from ..assistant.translation import TranslationReview, TranslationService
from ..reader.narration import NarrationStore
from .api_connections import parse_budget
from .operation_progress import OperationProgress
from .research_actions import NativeDialogs


class TranslationActions:
    """One explicit library, review generation and cancellable worker owner."""
    def __init__(self, frame, *, service=None):
        from .language_matrix import _host_scope
        from ..search.federated import _data_identity
        self.frame = frame
        self.library, self.host_current = _host_scope(frame)
        self.identity = _data_identity(self.library)
        if frame.active is None:
            raise ValueError('Open a library before translating selected text.')
        self.scope = tuple(frame.active)
        self.service = service or TranslationService(self.library)
        self.generation = 0
        self.running_generation = None
        self.closed = self.busy = False
        self.cancelled = Event()
        self.review = self.job_id = None

    def current(self, generation=None):
        from ..search.federated import _data_identity
        try:
            return (not self.closed and self.host_current()
                and _data_identity(self.library) == self.identity
                and (tuple(self.frame.active) if self.frame.active is not None else None) == self.scope
                and (generation is None or generation == self.generation))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def ensure(self):
        if not self.current():
            self.cancelled.set()
            raise PermissionError('The selected library changed or closed; open translation again.')

    def invalidate(self, *, source_changed=True):
        self.generation += 1
        self.cancelled.set()
        self.review = None
        if source_changed:
            self.job_id = None

    def accept(self, review):
        self.ensure()
        if type(review) is not TranslationReview or review.data['workspace'] != self.scope[0]:
            raise PermissionError('The translation review belongs to another library.')
        self.review, self.job_id = review, review.identity
        return review

    def start(self, executor, operation, *, set_running=lambda value: None, progress=lambda *args: None):
        self.ensure()
        if self.busy:
            raise RuntimeError('Wait for the current translation operation or choose Cancel.')
        self.generation += 1
        generation = self.running_generation = self.generation
        self.cancelled.clear()
        self.busy = True
        def stopped():
            if not self.current(generation):
                self.cancelled.set()
            return self.cancelled.is_set()
        def work():
            if stopped():
                raise InterruptedError('Translation cancelled before execution.')
            return operation(stopped, lambda *args: progress(*args) if self.current(generation) else None)
        try:
            set_running(True)
            self.ensure()
            future = executor.submit(work)
        except Exception:
            self.finish(generation)
            set_running(False)
            raise
        return generation, future

    def finish(self, generation):
        if self.running_generation == generation:
            self.running_generation = None
            self.busy = False
        return self.current(generation)

    def close(self):
        self.closed = True
        self.cancelled.set()
        self.generation += 1
        self.review = None


def describe_review(review):
    data = review.data
    money = lambda value: f'{value / 1_000_000:.6f} USD'
    start, boundaries = 0, []
    for index, length in enumerate(data['chunk_lengths']):
        boundaries.append(f'{index+1}: characters {start+1}–{start+length}'
                          + (' (already accepted)' if index < data['completed_chunks'] else ''))
        start += length
    return (f"Profile: {data['profile_name']}\nDestination: {data['endpoint']}\nModel: {data['model']}\n"
        f"Languages: {data['source_language']} → {data['target_language']}\n"
        f"Accepted chunks: {data['completed_chunks']}/{data['total_chunks']}; remaining calls: {data['remaining_calls']}\n"
        f"Your remaining budget: {money(data['budget_micros'])}; per-call reservation: {money(data['per_call_micros'])}\n"
        'These are caller-entered estimates. Actual provider billing is not measured.\n'
        'Selected text is sent to Google\'s global service; no processing region is selected.\n'
        'Data-use policy: https://docs.cloud.google.com/translate/data-usage\n'
        'Billing information: https://cloud.google.com/products/translate/pricing\n'
        'Only the exact text below is approved. A machine translation is a reading derivative, not quotation evidence.\n'
        'Plain text only; document formatting and glossaries are not preserved.\n'
        'Exact chunk boundaries (Unicode characters): '+ '; '.join(boundaries)+'\n'
        f'Review SHA256: {review.digest}\n\nEXACT TEXT TO SEND\n'+data['source_text'])


class TranslationDialog:
    def __init__(self, frame, *, actions=None, dialogs=None, wx_module=None, on_ready=None):
        if wx_module is None:
            import wx as wx_module
        self.wx = wx = wx_module
        self.frame = frame
        self.actions = actions or TranslationActions(frame)
        self.dialogs = dialogs or NativeDialogs()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reviewed-translation')
        self.alive = self.window_alive = True
        self.close_pending = False
        self.on_ready = on_ready
        self.measured = None
        self.origin = None
        self.result_state = None
        self.profile_rows, self.language_rows = (), ()
        self.dialog = wx.Dialog(frame, title='Translate text — explicit Google approval', size=(930, 850),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Reviewed text translation')
        layout = wx.BoxSizer(wx.VERTICAL)
        self.inputs = []
        def field(label, cls=wx.TextCtrl, *, proportion=0, **kw):
            layout.Add(wx.StaticText(self.dialog, label=label), 0, wx.LEFT | wx.TOP, 5)
            control = cls(self.dialog, **kw)
            control.SetName(label.replace('&', ''))
            layout.Add(control, proportion, wx.ALL | wx.EXPAND, 5)
            self.inputs.append(control)
            return control
        self.profile = field('Google Basic v2 &profile', wx.Choice)
        self.title = field('Reading-copy &title', value='Machine translation')
        self.source = field('&Exact text to send (no source files are sent)', proportion=1,
                            style=wx.TE_MULTILINE)
        self.source.SetMinSize((-1, 100))
        row = wx.BoxSizer(wx.HORIZONTAL)
        def choice(name):
            row.Add(wx.StaticText(self.dialog, label=name), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            result = wx.Choice(self.dialog)
            result.SetName(name.replace('&', ''))
            row.Add(result, 1, wx.ALL, 5)
            self.inputs.append(result)
            return result
        self.from_language, self.to_language = choice('Source &language'), choice('Target lan&guage')
        layout.Add(row, 0, wx.EXPAND)
        row = wx.BoxSizer(wx.HORIZONTAL)
        def budget(name):
            row.Add(wx.StaticText(self.dialog, label=name), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            result = wx.TextCtrl(self.dialog, value='', size=(100,-1))
            result.SetName(name.replace('&', ''))
            row.Add(result, 0, wx.ALL, 5)
            self.inputs.append(result)
            return result
        self.budget = budget('Your remaining &budget (USD)')
        self.per_call = budget('Your per-call &estimate (USD)')
        layout.Add(row, 0, wx.EXPAND)
        self.review = field('Exact request review and derivative result', proportion=1,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            value='Opening this window sends nothing. Configure a profile, explicitly refresh its languages, '
                  'choose the exact text and enter your estimates, then prepare a review.')
        self.inputs.remove(self.review)
        self.review.SetMinSize((-1, 150))
        self.status = field('Translation operation status', style=wx.TE_READONLY, value='Local only; no request sent.')
        self.inputs.remove(self.status)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.buttons = {}
        for name, label, handler in (
            ('connections', 'API connections...', self.connections),
            ('languages', 'Refresh provider languages...', self.refresh_languages),
            ('reading', 'Use saved reading copy...', self.reading_copy),
            ('prepare', 'Prepare exact request review', self.prepare),
            ('run', 'Approve and run...', self.approve_run),
            ('jobs', 'Open saved translation...', self.jobs),
            ('save', 'Save derivative reading copy', self.save_copy),
            ('export', 'Export translated TXT...', self.export_text),
            ('bilingual', 'Export bilingual TXT...', self.export_bilingual),
            ('cancel', 'Cancel current operation', self.cancel),
            ('close', 'Close translation', self.close)):
            button = wx.Button(self.dialog, label=label)
            button.SetName(label)
            button.Bind(wx.EVT_BUTTON, handler)
            row.Add(button, 0, wx.ALL, 4)
            self.buttons[name] = button
        layout.Add(row, 0, wx.EXPAND)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(self.buttons['close'].GetId())
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
        self.source.Bind(wx.EVT_TEXT, self.changed)
        self.title.Bind(wx.EVT_TEXT, lambda event: self.changed(event, text_changed=False))
        for control in (self.from_language, self.to_language):
            control.Bind(wx.EVT_CHOICE, lambda event: self.changed(event, text_changed=False))
        for control in (self.budget, self.per_call):
            control.Bind(wx.EVT_TEXT, lambda event: self.changed(event, source_changed=False))
        self.profile.Bind(wx.EVT_CHOICE, self.profile_changed)
        self.reload_profiles()

    def current(self, generation=None):
        return self.alive and self.window_alive and self.actions.current(generation)

    def set_running(self, value):
        if not self.current():
            return
        for control in self.inputs:
            control.Enable(not value)
        for name in ('connections','languages','reading','prepare','jobs'):
            self.buttons[name].Enable(not value)
        self.buttons['run'].Enable(not value and self.actions.review is not None)
        for name in ('save','export','bilingual'):
            self.buttons[name].Enable(not value and self.result_state == 'complete' and self.actions.job_id is not None)
        self.buttons['cancel'].Enable(value)

    def changed(self, event=None, *, source_changed=True, text_changed=True):
        if self.current():
            self.actions.invalidate(source_changed=source_changed)
            if source_changed:
                self.result_state = None
                if text_changed:
                    self.origin = None
            self.set_running(self.actions.busy)
        if event is not None:
            event.Skip()

    def _profile(self):
        self.actions.ensure()
        index = self.profile.GetSelection()
        if not 0 <= index < len(self.profile_rows):
            raise ValueError('Configure and select a Google Basic v2 profile first.')
        return self.profile_rows[index]

    def _languages(self, rows, source='auto', target=None):
        self.language_rows = tuple(rows)
        labels = [row['name']+' — '+row['language'] for row in rows]
        self.from_language.SetItems(['Automatic source-language detection']+labels)
        self.to_language.SetItems(labels)
        self.from_language.SetSelection(0 if source == 'auto' else next(
            (i+1 for i,row in enumerate(rows) if row['language']==source), -1))
        self.to_language.SetSelection(next((i for i,row in enumerate(rows) if row['language']==target), -1))

    def reload_profiles(self):
        if not self.current():
            return
        self.profile_rows = tuple(p for p in self.actions.service.profiles.profiles() if p.provider == 'google-translate')
        self.profile.SetItems([p.name+' — '+p.model for p in self.profile_rows])
        self.profile.SetSelection(0 if self.profile_rows else -1)
        self._languages(self.actions.service.languages(self.profile_rows[0].id) if self.profile_rows else ())
        self.set_running(False)

    def profile_changed(self, event=None):
        self.changed(event, text_changed=False)
        if self.current() and not self.actions.busy:
            try:
                self._languages(self.actions.service.languages(self._profile().id))
            except Exception as exc:
                self.error(exc)

    def error(self, error):
        if self.current():
            safe = str(error)[:1000] if isinstance(error, (ValueError, PermissionError, InterruptedError, OSError)) else (
                'The operation could not finish. Saved chunks were retained; review the saved job before any request.')
            self.status.ChangeValue(safe)

    def _start(self, label, operation, apply, *, committed_result=False):
        if not self.current() or self.actions.busy:
            return False
        measured = self.measured = OperationProgress(self.frame, label)
        pending, lock = {'latest':None,'queued':False,'active':True}, Lock()
        generation = None
        def notice():
            with lock:
                latest, pending['queued'] = pending['latest'], False
            if latest is not None and self.current(generation) and self.actions.busy:
                self.status.ChangeValue(f'{latest[0]}/{latest[1]}: {latest[2]}')
        def progress(completed, total, phase):
            measured.update(phase=phase, completed=completed, total=total)
            with lock:
                if not pending['active']:
                    return
                pending['latest'] = completed, total, str(phase)[:200]
                if pending['queued']:
                    return
                pending['queued'] = True
            if self.alive:
                self.wx.CallAfter(notice)
        try:
            self.status.ChangeValue(label)
            generation, future = self.actions.start(self.executor, operation,
                set_running=self.set_running, progress=progress)
        except Exception as exc:
            measured.failed(exc)
            self.error(exc)
            self.set_running(False)
            return False
        def completed(done):
            with lock:
                pending['active'] = False
            def deliver():
                accepted = self.current(generation)
                self.actions.finish(generation)
                if not accepted:
                    measured.finish(state='cancelled')
                    if self.current():
                        self.set_running(False)
                        self.status.ChangeValue('The view changed; saved progress remains available in Open saved translation.')
                    if self.close_pending and self.alive:
                        self.close()
                    return
                self.set_running(False)
                try:
                    value = done.result()
                    # A completed durable write remains a result even when a
                    # late Cancel arrived. A read-only review/chooser must not
                    # publish fresh authority after cancellation.
                    if self.actions.cancelled.is_set() and not committed_result:
                        measured.finish(state='cancelled')
                        self.status.ChangeValue('Cancelled. Saved progress remains available; no review was accepted.')
                    elif apply(value) is False or not self.current():
                        measured.finish(state='cancelled')
                    elif isinstance(value, dict) and value.get('status') == 'paused':
                        measured.finish(state='cancelled')
                    else:
                        measured.complete(value)
                except Exception as exc:
                    measured.failed(exc)
                    self.error(exc)
                self.set_running(False)
                if self.close_pending and self.alive:
                    self.close()
            if not self.alive or not self.window_alive:
                self.actions.finish(generation)
                measured.finish(state='cancelled')
                return
            try:
                self.wx.CallAfter(deliver)
            except RuntimeError:
                self.actions.close()
                measured.finish(state='cancelled')
        future.add_done_callback(completed)
        return True

    def _after_choice(self, label, operation, apply):
        """Load the selected large document after this modal callback unwinds."""
        generation = self.actions.generation
        self.wx.CallAfter(lambda: self._start(label, operation, apply)
            if self.current(generation) and not self.actions.busy and not self.actions.cancelled.is_set() else None)

    def connections(self, event=None):
        if not self.current() or self.actions.busy:
            return
        from .api_connections import show_api_connections
        generation = self.actions.generation
        show_api_connections(self.frame)
        if self.current(generation):
            self.changed(text_changed=False)
            self.reload_profiles()
            self.profile.SetFocus()

    def refresh_languages(self, event=None):
        if not self.current() or self.actions.busy:
            return
        try:
            selected = self._profile(); generation = self.actions.generation
            choice = self.dialogs.choose(self.dialog, 'Request supported languages',
                f'Contact {selected.name} at {selected.endpoint}/languages using its stored key?\n'
                'This GET sends the display language and model, not selected text. Estimated reservation: 0 USD; '
                'actual billing is not measured.', ('Request languages', 'Cancel'))
            if choice != 0 or not self.current(generation):
                return
            self._start('Fetching provider-declared languages', lambda stopped, progress:
                self.actions.service.refresh_languages(selected.id, approved_profile_fingerprint=selected.fingerprint,
                    cancelled=stopped), lambda rows: (self._languages(rows),
                    self.status.ChangeValue('Provider-declared language list saved. Choose the target explicitly.')))
        except Exception as exc:
            self.error(exc)

    def reading_copy(self, event=None):
        if not self.current() or self.actions.busy:
            return
        def loaded(rows):
            rows = tuple(row for row in rows if row['saved'])
            generation = self.actions.generation
            choice = self.dialogs.choose(self.dialog, 'Use saved reading copy',
                'Choose an explicitly saved reading revision. Recovery drafts are not sent.',
                [row['project'].title+' — '+row['project'].id for row in rows]) if rows else None
            if choice is None or not self.current(generation):
                return False
            identity = rows[choice]['project'].id
            def selected(value):
                project, digest = value
                if project.source_location is not None and project.source_location.workspace != self.actions.scope[0]:
                    raise PermissionError('This reading copy belongs to another library.')
                self.changed()
                self.origin = project.id, digest
                self.source.ChangeValue(project.current_text)
                self.title.ChangeValue(('Translation — '+project.title)[:200])
                self.source.SetFocus()
            self._after_choice('Opening the selected saved reading revision',
                lambda stopped, progress: NarrationStore(self.actions.library.root).load(identity), selected)
        self._start('Loading saved reading copies', lambda stopped, progress:
            NarrationStore(self.actions.library.root).list_projects(self.actions.scope[0]), loaded)

    def prepare(self, event=None):
        if not self.current() or self.actions.busy:
            return
        try:
            profile = self._profile()
            total, per_call = parse_budget(self.budget.GetValue()), parse_budget(self.per_call.GetValue())
            identity = self.actions.job_id
            if identity is None:
                source_index, target_index = self.from_language.GetSelection(), self.to_language.GetSelection()
                if not 0 <= source_index <= len(self.language_rows) or not 0 <= target_index < len(self.language_rows):
                    raise ValueError('Refresh the provider languages and choose an explicit source option and target.')
                source = 'auto' if source_index == 0 else self.language_rows[source_index-1]['language']
                target = self.language_rows[target_index]['language']
                text, title, origin = self.source.GetValue(), self.title.GetValue(), self.origin
            def operation(stopped, progress):
                job = identity
                if job is None:
                    if origin is not None:
                        job = self.actions.service.prepare_reading_copy(self.actions.scope[0], profile.id,
                            origin[0], origin[1], source, target, title=title,
                            topic=self.actions.scope[1], cancelled=stopped)
                    else:
                        job = self.actions.service.prepare(self.actions.scope[0], profile.id, text, source, target,
                            title=title, topic=self.actions.scope[1], cancelled=stopped)
                return self.actions.service.review(job, budget_micros=total, per_call_micros=per_call, cancelled=stopped)
            def reviewed(value):
                self.actions.accept(value)
                self.review.ChangeValue(describe_review(value))
                self.status.ChangeValue('Local review ready. No selected text has been sent by preparing it.')
                self.review.SetFocus()
                self.review.SetInsertionPoint(0)
            self._start('Preparing exact text and budget review', operation, reviewed)
        except Exception as exc:
            self.error(exc)

    def approve_run(self, event=None):
        if not self.current() or self.actions.busy or self.actions.review is None:
            return
        selected, generation = self.actions.review, self.actions.generation
        data = selected.data
        choice = self.dialogs.choose(self.dialog, 'Approve this exact translation',
            f"Send the reviewed text to {data['profile_name']} for {data['source_language']} → {data['target_language']}?\n"
            f"Remaining calls: {data['remaining_calls']}. Your remaining reservation: {data['budget_micros']/1_000_000:.6f} USD.\n"
            'Actual provider billing is not measured. Cancelling an in-flight request cannot guarantee that it was not charged.',
            ('Approve reviewed text and budget', 'Cancel'))
        if choice != 0 or not self.current(generation) or self.actions.review is not selected:
            return
        self.actions.review = None
        def operation(stopped, progress):
            grant = self.actions.service.approve(selected, approved_review_sha256=selected.digest, cancelled=stopped)
            result = self.actions.service.run(selected.identity, grant=grant, cancelled=stopped, progress=progress)
            if result['status'] == 'complete':
                _, result['translated_text'] = self.actions.service.result(selected.identity)
            elif result['completed']:
                saved = self.actions.service.details(selected.identity)
                result['translated_text'] = ''.join(row['text'] for row in saved['receipts'])
            return result
        def finished(value):
            self.actions.review = None
            self.result_state = value['status']
            self.status.ChangeValue(f"{value['status']}: {value['completed']}/{value['total']} chunks. "
                'Saved progress can be reopened; a new request requires a fresh review.')
            if value['status'] == 'complete':
                self.review.ChangeValue('Machine translation derivative; not quotation evidence.\n'
                    'Accuracy has not been independently reviewed.\n\n'+value['translated_text'])
                self.review.SetFocus()
            elif value.get('translated_text'):
                self.review.ChangeValue('Accepted partial machine translation — incomplete; not quotation evidence.\n'
                    + f"{value['completed']} of {value['total']} chunks were retained. A fresh review is required before resuming.\n\n"
                    + value['translated_text'])
                self.review.SetFocus()
        self._start('Running explicitly approved translation', operation, finished, committed_result=True)

    def jobs(self, event=None):
        if not self.current() or self.actions.busy:
            return
        def choose(rows):
            generation = self.actions.generation
            choice = self.dialogs.choose(self.dialog, 'Saved translations',
                'Opening a saved job never resumes a request. Uncertain requests remain blocked.',
                [f"{row['title']}; {row['status']}; {row['completed']}/{row['total']}; {row['id']}" for row in rows]) if rows else None
            if choice is None or not self.current(generation):
                return False
            identity = rows[choice]['id']
            def load(stopped, progress):
                value = self.actions.service.details(identity)
                index = next((i for i,p in enumerate(self.profile_rows) if p.id == value['plan']['profile']['id']), -1)
                languages = self.actions.service.languages(value['plan']['profile']['id']) if index >= 0 else ()
                return value, index, languages
            self._after_choice('Opening the selected saved translation', load, selected)
        def selected(loaded):
            value, index, rows = loaded
            plan = value['plan']
            self.changed()
            self.actions.job_id = value['id']
            self.result_state = value['status']
            self.origin = ((plan['origin']['project']['id'], plan['origin']['project_sha256'])
                           if plan['origin'] is not None else None)
            self.source.ChangeValue(plan['source_text'])
            self.title.ChangeValue(plan['title'])
            self.budget.ChangeValue(''); self.per_call.ChangeValue('')
            self.profile.SetSelection(index)
            self._languages(rows, plan['source_language'], plan['target_language'])
            self.review.ChangeValue('Saved translation: '+value['status']+'\n'
                + ('A request has an uncertain outcome. It will not be retried automatically.' if value['pending'] is not None else
                   'Enter a remaining budget and prepare a fresh review before resuming.'))
            if value['pending'] is not None:
                pending = value['pending']
                self.review.ChangeValue(self.review.GetValue()
                    + '\nLocal operation ID (not a provider receipt): '+pending['operation_id']
                    + '\nChunk: '+str(pending['index']+1)
                    + '\nExact request SHA256: '+pending['request_sha256']
                    + '\nCaller-entered reservation: '+f"{pending['estimate_micros']/1_000_000:.6f} USD"
                    + '\nActual transmission and billing cannot be confirmed from this local checkpoint.')
            if value['status'] == 'complete':
                self.review.ChangeValue('Machine translation derivative; not quotation evidence.\n\n'
                    + ''.join(row['text'] for row in value['receipts']))
            elif value['receipts']:
                self.review.ChangeValue(self.review.GetValue()
                    + '\n\nACCEPTED PARTIAL TRANSLATION — incomplete; not quotation evidence.\n'
                    + ''.join(row['text'] for row in value['receipts']))
            self.set_running(False)
            self.review.SetFocus()
        self._start('Loading saved translation history', lambda stopped, progress:
                    self.actions.service.jobs(self.actions.scope[0], cancelled=stopped), choose)

    def save_copy(self, event=None):
        if not self.current() or self.actions.busy or self.result_state != 'complete':
            return
        identity = self.actions.job_id
        self._start('Saving the derived reading copy', lambda stopped, progress:
            self.actions.service.save_reading_copy(identity, cancelled=stopped), lambda value:
            self.status.ChangeValue('Derived reading copy saved in this library. Open it from Reading copies. Original source unchanged.'),
            committed_result=True)

    def _export(self, bilingual):
        if not self.current() or self.actions.busy or self.result_state != 'complete':
            return
        generation, identity = self.actions.generation, self.actions.job_id
        picker = self.wx.FileDialog(self.dialog, 'Export bilingual text' if bilingual else 'Export translated text',
            wildcard='Text files (*.txt)|*.txt', style=self.wx.FD_SAVE)
        try:
            selected = picker.GetPath() if picker.ShowModal() == self.wx.ID_OK and self.current(generation) else None
        finally:
            try:
                if not picker.IsBeingDeleted():
                    picker.Destroy()
            except RuntimeError:
                pass
        if selected is not None and self.current(generation):
            self._start('Exporting the reviewed derivative', lambda stopped, progress:
                self.actions.service.export(identity, selected, bilingual=bilingual, cancelled=stopped), lambda value:
                self.status.ChangeValue('Text exported to '+value['path']+'. Existing files were not replaced.'),
                committed_result=True)

    def export_text(self, event=None): self._export(False)
    def export_bilingual(self, event=None): self._export(True)

    def cancel(self, event=None):
        if self.alive and self.actions.busy:
            self.actions.cancelled.set()
            if self.measured is not None:
                self.measured.cancel_requested()
            if self.window_alive:
                self.status.ChangeValue('Cancellation requested. Waiting for the current request to settle; accepted chunks will be kept.')

    def close(self, event=None):
        if not self.alive:
            return
        if self.actions.busy:
            self.close_pending = True
            self.cancel()
            if event is not None and hasattr(event, 'Veto') and (not hasattr(event, 'CanVeto') or event.CanVeto()):
                event.Veto()
            return
        self.actions.close()
        self.alive = False
        if self.window_alive:
            if self.dialog.IsModal():
                self.dialog.EndModal(self.wx.ID_CANCEL)
            else:
                self.dialog.Destroy()

    def destroyed(self, event):
        if event.GetEventObject() is self.dialog:
            self.window_alive = self.alive = False
            self.actions.close()
            if self.measured is not None and self.actions.busy:
                self.measured.cancel_requested()
            self.executor.shutdown(wait=False, cancel_futures=True)
        event.Skip()

    def show(self):
        try:
            if self.on_ready is not None:
                self.wx.CallAfter(lambda: self.on_ready(self) if self.current() else None)
            self.dialog.ShowModal()
        finally:
            self.actions.close()
            self.alive = False
            self.executor.shutdown(wait=False, cancel_futures=True)
            if self.window_alive:
                self.dialog.Destroy()


def show_translation(frame, *, on_ready=None):
    return TranslationDialog(frame, on_ready=on_ready).show()


def install_translation_actions(frame):
    existing = getattr(frame, '_translation_item', None)
    if existing is not None:
        return existing
    import wx
    bar = frame.GetMenuBar()
    if bar is None:
        raise ValueError('Install the menu bar before translation actions.')
    menu = next((bar.GetMenu(i) for i in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(i).replace('&','') == 'Research'), None)
    if menu is None:
        menu = wx.Menu(); bar.Append(menu, '&Research')
    item = menu.Append(wx.ID_ANY, 'Translate text...')
    def opened(event):
        try:
            if getattr(frame, 'working', False) or getattr(frame, 'busy', False):
                raise ValueError('Wait for the current library operation before opening translation.')
            show_translation(frame)
        except Exception as exc:
            frame.report(str(exc))
    frame.Bind(wx.EVT_MENU, opened, item)
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec('translation.open', 'Translate text'), item)
    frame._translation_item = item
    return item
