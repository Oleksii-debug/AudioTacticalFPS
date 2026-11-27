"""Accessible explicit document batches on the desktop's existing job owner."""
from pathlib import Path
from threading import Event

from ..conversion.batches import BatchPreview, DocumentBatches
from ..conversion.planning import SplitOptions
from ..durable_state import local_path
from ..speech._io import check_cancelled


METHODS = (
    ('whole', 'Whole reading copy'), ('paragraphs', 'Stored paragraph / line breaks'),
    ('blank-lines', 'Blank lines'), ('sentences', 'Sentence punctuation heuristic'),
    ('characters', 'Maximum characters'), ('literal', 'Literal marker, retained in text'),
    ('regex', 'Bounded regular expression, marker retained'),
    ('sections', 'Actual extracted sections / combined document boundaries'),
    ('pages', 'Actual supplied physical-page positions'),
    ('estimated-duration', 'Estimated duration — explicit words/minute, not measured'),
)
FORMATS = ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma')


def preview_text(preview):
    rows = [f'Job: {preview.job_id}', f'New destination: {preview.destination}',
            f'Action: {preview.action}; combine documents: {preview.combined}',
            'Every range uses Unicode character offsets [start, end). Originals remain unchanged.']
    if preview.action == 'studio':
        rows.extend((f'Explicit voice: {preview.voice_id}; format: {preview.format}',
                     'Each document becomes one existing Studio job with a master and measured chapter files.',
                     'Studio keeps its canonical internal filenames. The text naming template does not rename audio files.'))
    for index, plan in enumerate(preview.plans, 1):
        rows.extend(('', f'Document {index}: {plan["source_name"]}',
                     f'Reading copy SHA256: {plan["reading_sha256"]}', *plan['notices']))
        for part in plan['parts']:
            estimate = '' if part['estimated_seconds'] is None else f'; ESTIMATE ONLY {part["estimated_seconds"]} s'
            rows.append(f'{part["name"]}: [{part["start"]}, {part["end"]}); '
                        f'{part["label"]}; reading-copy source mapping exact: {part["source_exact"]}{estimate}; '
                        f'SHA256 {part["sha256"]}')
        if sum(map(len, rows)) > 4_000_000:
            raise ValueError('This native preview exceeds its 4-million-character budget. Choose fewer boundaries.')
    rows.extend(('', 'Actual new output names:', *preview.outputs))
    if preview.conflicts:
        rows.extend(('', 'Occupied names — choose another template or destination before approval:', *preview.conflicts))
    else:
        rows.append('No occupied names were found. Save reviewed plan, then Run / resume saved plan explicitly.')
    return '\n'.join(rows)


def create_document_batch_dialog(frame, *, service=None, wx_module=None, initial_settings=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class DocumentBatchDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Document batches — split, name and produce', size=(1000, 800),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self._destroy_requested = False
            self.cancelled = Event()
            self.library = frame.library
            self.root = local_path(self.library.root, exists=True)
            self.service = service or DocumentBatches(self.root)
            self.alive, self.pending, self.closing = True, False, False
            self.identity, self.preview, self.approved_scope = None, None, None
            self.active_identity = None
            self.jobs, self.voices, self.preview_scope = (), (), None
            self.generation = 0
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label=(
                'Select explicit files or one folder, including its subfolders. Capture retains bounded source snapshots.\n'
                'Prepare reading text, review exact boundaries and portable output names, then approve and run. '
                'Pause / stop preserves completed receipts; resume is explicit. No folder watcher or automatic speech.'))
            notice.Wrap(940); box.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
            self.fields = []
            def field(label, *, choices=None, value='', style=0, proportion=0, size=None):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
                kwargs = {} if size is None else {'size': size}
                control = wx.Choice(panel, choices=choices) if choices is not None else wx.TextCtrl(
                    panel, value=value, style=style, **kwargs)
                control.SetName(label.replace('&', ''))
                box.Add(control, proportion, wx.ALL | wx.EXPAND, 4)
                self.fields.append(control)
                return control
            self.job_choice = field('Saved document &batch — select explicitly', choices=[])
            self.languages = field('Installed OCR languages for new captures; plus-separated identifiers (optional)')
            self.method = field('Split &method', choices=[label for _, label in METHODS]); self.method.SetSelection(0)
            self.size = field('Maximum &characters per part', value='5000')
            self.marker = field('Literal marker or bounded regular expression')
            self.ignore_case = field('Marker case matching', choices=['Exact case', 'Ignore case']); self.ignore_case.SetSelection(0)
            self.seconds = field('Estimated seconds per part — not measured audio', value='300')
            self.wpm = field('Explicit estimate in whitespace words per minute', value='150')
            self.combine = field('Document grouping', choices=['Separate documents', 'Combine with two LF separators']); self.combine.SetSelection(0)
            self.template = field('Text filename &template', value='{index:04d}-{source}-{part:03d}')
            self.destination = field('Existing new &output directory')
            self.action = field('Output action', choices=['Exact UTF-8 text and provenance', 'Canonical Studio audio jobs']); self.action.SetSelection(0)
            self.voice = field('Explicit installed Studio &voice', choices=[])
            self.language = field('Studio language (optional explicit provider tag)')
            self.format = field('Studio audio &format', choices=list(FORMATS)); self.format.SetSelection(0)
            self.review = field('Exact split and filename preview', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 180))
            self.status = field('Document batch status', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL); layout.Add(panel, 1, wx.EXPAND)
            self.buttons = {}
            for items in (
                (('files', 'Capture &files', lambda e: self.capture('files')),
                 ('folder', 'Capture fol&der', lambda e: self.capture('folder')),
                 ('reading', 'Capture saved reading &copies', lambda e: self.capture('reading')),
                 ('reload', '&Reload saved jobs', self.reload)),
                (('prepare', '&Prepare reading text', self.prepare), ('destination', 'Choose output directory', self.choose_destination),
                 ('voices', 'Load installed voices', self.load_voices), ('preview', 'Pre&view split and names', self.on_preview),
                 ('preset', 'Save batch &preset...', self.save_preset)),
                (('approve', '&Save reviewed plan', self.approve), ('run', '&Run / resume saved plan', self.run_saved),
                 ('pause', '&Pause', self.pause), ('stop', 'S&top operation', self.stop), ('close', '&Close', self.close))):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in items:
                    options = {'id': wx.ID_CANCEL} if key == 'close' else {}
                    button = wx.Button(self, label=label, **options); button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, handler); row.Add(button, 0, wx.ALL, 3); self.buttons[key] = button
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout); panel.FitInside(); self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.job_choice.Bind(wx.EVT_CHOICE, self.select_job)
            for control in (self.method, self.ignore_case, self.combine, self.action, self.voice, self.format):
                control.Bind(wx.EVT_CHOICE, self.changed)
            for control in (self.size, self.marker, self.seconds, self.wpm, self.template, self.destination, self.language):
                control.Bind(wx.EVT_TEXT, self.changed)
            self.report('Choose an explicit capture or reload saved jobs. No conversion or synthesis has started.')
            if initial_settings is not None:
                self.apply_preset(initial_settings)
            self.enable()

        def dispose(self):
            """Detach callbacks without native calls, foreign cancellation or I/O."""
            self.alive = False
            self.closing = True
            self.cancelled.set()

        def Destroy(self):
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            self.dispose()
            return super().Destroy()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                self.dispose()
            event.Skip()

        def report(self, value):
            if self.alive:
                self.status.ChangeValue(str(value))

        def guard(self):
            if (not self.alive or self.closing or getattr(frame, '_closing', False)
                    or frame.library is not self.library
                    or local_path(frame.library.root, exists=True) != self.root):
                raise ValueError('The library changed or this window closed. Reopen document batches.')

        def stopping(self):
            if self.cancelled.is_set() or frame.cancel_event.is_set():
                return True
            try:
                self.guard()
            except (OSError, ValueError, RuntimeError):
                return True
            return False

        def signature(self):
            self.guard()
            return (self.identity, self.method.GetSelection(), self.size.GetValue(), self.marker.GetValue(),
                    self.ignore_case.GetSelection(), self.seconds.GetValue(), self.wpm.GetValue(),
                    self.combine.GetSelection(), self.template.GetValue(), self.destination.GetValue(),
                    self.action.GetSelection(), self.voice.GetSelection(), tuple(row['id'] for row in self.voices),
                    self.language.GetValue(), self.format.GetSelection())

        def enable(self):
            if not self.alive:
                return
            idle = not self.pending and not self.closing
            for control in self.fields:
                if control not in (self.review, self.status):
                    control.Enable(idle)
            for key, button in self.buttons.items():
                allowed = idle
                if key in ('prepare', 'preview'):
                    allowed = idle and self.identity is not None
                elif key == 'approve':
                    allowed = idle and self.preview is not None and not self.preview.conflicts
                elif key == 'run':
                    allowed = idle and self.approved_scope is not None
                elif key in ('pause', 'stop'):
                    allowed = self.pending and not self.closing
                elif key == 'close':
                    allowed = True
                button.Enable(allowed)

        def changed(self, event=None):
            if not self.alive:
                return
            self.generation += 1
            self.preview = self.preview_scope = self.approved_scope = None
            self.review.ChangeValue('Settings changed. Preview and approve again before running.')
            self.enable()
            if event is not None:
                event.Skip()

        def begin(self, title, operation, complete, *, committed=False, controlled_identity=None):
            if self.pending or not self.alive or frame.working:
                self.report('Finish or stop the active operation before starting another.'); return False
            try:
                self.guard()
            except Exception as exc:
                self.report(exc); return False
            generation = self.generation
            self.cancelled.clear()
            self.active_identity = controlled_identity
            self.pending = True; self.enable()
            def stopped():
                return self.stopping() or self.generation != generation
            def work(progress):
                self.guard(); check_cancelled(stopped)
                result = operation(stopped, lambda message: progress(phase=message))
                if not committed:
                    check_cancelled(stopped); self.guard()
                return result
            def failure(error):
                self.pending = False
                self.active_identity = None
                if self.closing:
                    self.finish_close(); return
                if self.alive:
                    self.report(error); self.enable(); self.status.SetFocus()
            def done(value):
                self.pending = False
                self.active_identity = None
                if self.closing:
                    self.finish_close(); return
                if not self.alive:
                    return
                try:
                    self.guard()
                    if self.generation != generation:
                        raise ValueError('The selected settings changed. Reload the saved job and review it again.')
                    if not committed:
                        check_cancelled(stopped)
                    complete(value)
                    if self.alive:
                        self.enable(); self.status.SetFocus()
                except Exception as exc:
                    failure(exc)
            try:
                frame.run_job(title, work, done, failure, with_progress=True)
                if self.pending and not frame.working:
                    failure(RuntimeError('The shared operation did not start. Try again.')); return False
            except Exception as exc:
                failure(exc); return False
            return True

        def selected(self):
            self.guard()
            if self.identity is None:
                raise ValueError('Capture or select a saved document batch first.')
            return self.identity

        def capture(self, kind):
            if self.pending or not self.alive:
                return False
            try:
                self.guard()
                languages = tuple(value for value in self.languages.GetValue().strip().split('+') if value)
                if kind == 'folder':
                    picker = wx.DirDialog(self, 'Choose one explicit document folder, including subfolders')
                else:
                    picker = wx.FileDialog(self, 'Choose saved reading-copy JSON files' if kind == 'reading' else 'Choose explicit documents',
                        wildcard='Reading copies (*.json)|*.json' if kind == 'reading' else 'Documents|*.*',
                        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
                try:
                    result = picker.ShowModal()
                    if not self.alive or not picker or result != wx.ID_OK:
                        return False
                    self.guard()
                    paths = (Path(picker.GetPath()),) if kind == 'folder' else tuple(map(Path, picker.GetPaths()))
                finally:
                    if picker:
                        picker.Destroy()
                if not self.alive or not paths:
                    return False
                def captured(record):
                    self.identity = record['id']; self.changed()
                    self.jobs = (*self.jobs, {'id': record['id'], 'status': 'preparing', 'files': len(record['sources']), 'outputs': 0})
                    self.fill_jobs(); self.report('Source snapshots retained. Prepare reading text before previewing outputs.\n'
                                                  + '\n'.join(row['name'] for row in record['sources'])
                                                  + ('\nUnsupported entries skipped: ' + ', '.join(record['skipped']) if record['skipped'] else ''))
                def work(stopped, progress):
                    identity = self.service.create_reading_copies(paths, cancelled=stopped) if kind == 'reading' else self.service.create(
                        paths, ocr_languages=languages, cancelled=stopped)
                    return self.service.get(identity)
                return self.begin('Retaining selected document snapshots', work, captured, committed=True)
            except Exception as exc:
                self.report(exc); return False

        def fill_jobs(self):
            if not self.alive:
                return
            self.job_choice.Set([f'{row["id"]}; {row["status"]}; {row["files"]} sources; {row["outputs"]} receipts' for row in self.jobs])
            self.job_choice.SetSelection(next((i for i, row in enumerate(self.jobs) if row['id'] == self.identity), -1))

        def reload(self, event=None):
            if not self.alive:
                return
            def done(rows):
                self.jobs = rows; self.fill_jobs()
                self.report('Choose a saved batch explicitly. Saved status is metadata; Run verifies current sources and completed bytes.')
            return self.begin('Loading saved document batches', lambda stopped, progress: self.service.jobs(cancelled=stopped), done)

        def select_job(self, event=None):
            if not self.alive:
                return
            try:
                index = self.job_choice.GetSelection()
                if not 0 <= index < len(self.jobs):
                    raise ValueError('Choose a saved document batch.')
                self.identity = self.jobs[index]['id']; self.changed()
                identity = self.identity
                return self.begin('Opening saved document plan', lambda stopped, progress: self.service.get(identity), self.loaded)
            except Exception as exc:
                self.report(exc); return False

        def loaded(self, record):
            if not self.alive:
                return
            self.identity = record['id']
            self.approved_scope = None
            review = record['review']
            if review is not None:
                options = review['plans'][0]['options']
                self.method.SetSelection(next((i for i, row in enumerate(METHODS) if row[0] == options['mode']), 0))
                for control, value in ((self.size, options['size']), (self.marker, options['marker']),
                    (self.seconds, options['seconds']), (self.wpm, options['words_per_minute']),
                    (self.template, review['plans'][0]['template']), (self.destination, review['destination']),
                    (self.language, review['language'])):
                    control.ChangeValue(str(value))
                self.ignore_case.SetSelection(int(options['ignore_case'])); self.combine.SetSelection(int(review['combined']))
                self.action.SetSelection(int(review['action'] == 'studio')); self.format.SetSelection(FORMATS.index(review['format']))
                if review['voice_id'] and not any(row['id'] == review['voice_id'] for row in self.voices):
                    self.voices = (*self.voices, {'id': review['voice_id'], 'name': 'Saved selection — availability checked at run'})
                self.fill_voices(review['voice_id'])
                if record['status'] != 'waiting-input':
                    self.approved_scope = self.signature()
            self.preview = self.preview_scope = None
            self.review.ChangeValue(preview_text(BatchPreview.from_dict(review)) if review else '')
            self.report(f'{record["status"]}: {record["message"]}\n' + '\n'.join(row['name'] for row in record['sources']))

        def prepare(self, event=None):
            try:
                identity = self.selected()
                self.preview = self.preview_scope = None
                return self.begin('Preparing document reading copies',
                    lambda stopped, progress: self.service.prepare(identity, cancelled=stopped, progress=progress), self.loaded,
                    committed=True, controlled_identity=identity)
            except Exception as exc:
                self.report(exc); return False

        def choose_destination(self, event=None):
            if not self.alive or self.pending:
                return
            try:
                self.guard()
                picker = wx.DirDialog(self, 'Choose an existing directory for new outputs')
                try:
                    result = picker.ShowModal()
                    if not self.alive or not picker or result != wx.ID_OK:
                        return
                    self.guard()
                    self.destination.ChangeValue(picker.GetPath()); self.changed()
                finally:
                    if picker:
                        picker.Destroy()
            except Exception as exc:
                self.report(exc)

        def fill_voices(self, selected=''):
            if not self.alive:
                return
            self.voice.Set([f'{row["name"]} — {row["id"]}' for row in self.voices])
            self.voice.SetSelection(next((i for i, row in enumerate(self.voices) if row['id'] == selected), -1))

        def load_voices(self, event=None):
            if not self.alive:
                return
            def work(stopped, progress):
                from ..runtime_settings import voice_registry
                registry = voice_registry(self.root / 'state', cancelled=stopped)
                try:
                    return tuple({'id': voice.id, 'name': voice.name} for voice in registry.voices())
                finally:
                    if registry.close() is False:
                        raise RuntimeError('Voice discovery finished, but its owned runtime has not confirmed cleanup. Retry after it stops.')
            def loaded(rows):
                self.voices = rows; self.fill_voices(); self.changed()
                self.report('Choose an installed voice explicitly. Availability does not qualify its language or pronunciation quality.')
            return self.begin('Listing installed local voices', work, loaded)

        def preset_settings(self):
            self.guard()
            from dataclasses import asdict
            from ..assistant.templates import batch_settings
            def choice(control, count):
                selected = control.GetSelection()
                if not 0 <= selected < count:
                    raise ValueError('Choose every required split, grouping and format option.')
                return selected
            options = SplitOptions(METHODS[choice(self.method, len(METHODS))][0], size=int(self.size.GetValue()),
                marker=self.marker.GetValue(), ignore_case=bool(choice(self.ignore_case, 2)),
                seconds=float(self.seconds.GetValue()), words_per_minute=int(self.wpm.GetValue()))
            action = ('text', 'studio')[choice(self.action, 2)]
            index = self.voice.GetSelection()
            voice = self.voices[index]['id'] if 0 <= index < len(self.voices) else ''
            if action == 'studio' and not voice:
                raise ValueError('Load and explicitly choose an installed Studio voice.')
            return batch_settings(dict(options=asdict(options), template=self.template.GetValue(),
                combined=bool(choice(self.combine, 2)), action=action, voice_id=voice,
                language=self.language.GetValue(), format=FORMATS[choice(self.format, len(FORMATS))]))

        def apply_preset(self, settings):
            self.guard()
            from ..assistant.templates import batch_settings
            settings = batch_settings(settings)
            options = settings['options']
            methods = [key for key, _ in METHODS]
            if options['mode'] not in methods:
                raise ValueError('This split mode is not exposed by the current native batch controls.')
            self.method.SetSelection(methods.index(options['mode']))
            for control, value in ((self.size, options['size']), (self.marker, options['marker']),
                    (self.seconds, options['seconds']), (self.wpm, options['words_per_minute']),
                    (self.template, settings['template']), (self.language, settings['language'])):
                control.ChangeValue(str(value))
            self.ignore_case.SetSelection(int(options['ignore_case']))
            self.combine.SetSelection(int(settings['combined']))
            self.action.SetSelection(int(settings['action'] == 'studio'))
            self.format.SetSelection(FORMATS.index(settings['format']))
            self.voices = ()
            if settings['voice_id']:
                self.voices = ({'id': settings['voice_id'],
                    'name': 'Template voice — current availability must be checked before rendering'},)
            self.fill_voices(settings['voice_id'])
            self.identity = None
            self.preview = self.preview_scope = self.approved_scope = None
            self.destination.ChangeValue('')
            self.review.ChangeValue('')
            self.report('Preset settings loaded. Select current source files and a NEW output directory. '
                        'Capture, prepare, preview and approve explicitly; no saved job or permission was reused.')

        def save_preset(self, event=None):
            if not self.alive or self.pending:
                return
            try:
                settings = self.preset_settings()
                from .workflow_templates import show_workflow_templates
                show_workflow_templates(frame, initial=('document-batch', settings))
            except Exception as exc:
                self.report(exc)

        def on_preview(self, event=None):
            try:
                identity = self.selected()
                settings = self.preset_settings()
                options = SplitOptions(**settings['options'])
                arguments = {key: value for key, value in settings.items() if key != 'options'}
                if not self.destination.GetValue().strip():
                    raise ValueError('Choose an existing output directory.')
                destination, scope = Path(self.destination.GetValue()), self.signature()
                self.preview = self.preview_scope = self.approved_scope = None
                def work(stopped, progress):
                    preview = self.service.preview(identity, destination, options, cancelled=stopped, **arguments)
                    return preview, preview_text(preview)
                def done(result):
                    if self.signature() != scope:
                        raise ValueError('The controls changed after preview. Preview the current settings again.')
                    self.preview, text = result; self.preview_scope = scope
                    self.review.ChangeValue(text)
                    self.report('Review every boundary and name. Occupied names block approval.' if self.preview.conflicts
                                else 'Exact preview ready. Save reviewed plan explicitly; no output has been published.')
                return self.begin('Reviewing exact document boundaries and filenames', work, done)
            except Exception as exc:
                self.report(exc); return False

        def approve(self, event=None):
            try:
                preview, scope = self.preview, self.preview_scope
                if preview is None or scope != self.signature():
                    raise ValueError('The controls changed or no preview is available. Preview before approval.')
                def done(record):
                    if self.signature() != scope:
                        raise ValueError('Controls changed; the saved plan remains available, but review before running.')
                    self.approved_scope = scope
                    self.report(record['message'])
                return self.begin('Saving the reviewed document plan',
                    lambda stopped, progress: self.service.approve(preview, cancelled=stopped), done, committed=True)
            except Exception as exc:
                self.report(exc); return False

        def run_saved(self, event=None):
            try:
                identity = self.selected()
                if self.approved_scope is None or self.signature() != self.approved_scope:
                    raise ValueError('Preview and approve the current controls before running; edited settings cannot reuse old approval.')
                return self.begin('Running the saved document batch',
                    lambda stopped, progress: self.service.run(identity, cancelled=stopped, progress=progress),
                    lambda record: self.report(f'{record["status"]}: {record["message"]}\nVerified receipts: {len(record["outputs"])}'),
                    committed=True, controlled_identity=identity)
            except Exception as exc:
                self.report(exc); return False

        def pause(self, event=None):
            if not self.alive:
                return
            if self.pending:
                try:
                    if self.active_identity is not None:
                        self.service.pause(self.active_identity)
                    self.cancelled.set()
                    self.report('Pause requested. Wait for the worker to finish stopping; completed receipts remain resumable.')
                except Exception as exc:
                    self.report(exc)

        def stop(self, event=None):
            if not self.alive:
                return
            if self.pending:
                try:
                    if self.active_identity is not None:
                        self.service.cancel(self.active_identity)
                    self.cancelled.set()
                    self.report('Stop requested. Wait for the owned operation; completed outputs remain verified on explicit resume.')
                except Exception as exc:
                    self.report(exc)

        def finish_close(self):
            if not self.alive:
                return
            self.alive = False
            self.cancelled.set()
            if self and self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            elif self:
                self.Hide()

        def close(self, event=None):
            if not self.alive:
                return
            if self.pending:
                self.stop(); self.closing = True; self.enable()
                self.report('Stopping the owned operation before closing. Retained sources and completed receipts remain available.')
            else:
                self.finish_close()

    return DocumentBatchDialog()


def show_document_batches(frame, *, initial_settings=None):
    dialog = (create_document_batch_dialog(frame) if initial_settings is None else
              create_document_batch_dialog(frame, initial_settings=initial_settings))
    try:
        dialog.ShowModal()
    finally:
        dialog.dispose()
        if dialog:
            dialog.Destroy()
