"""Keyboard-accessible native workspace for the supplied local Whisper worker."""
from pathlib import Path
import threading


def show_transcription(frame):
    import wx
    if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
        frame.report('Finish or cancel the current operation before opening transcription.')
        return
    active = getattr(frame, 'active', None)
    if active is None:
        frame.report('Select an active library before opening transcription.')
        return
    service = frame.library.transcription(active[0])
    dialog = wx.Dialog(frame, title='Транскрипція — локальний Whisper', size=(900, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    current, selected_source, alive, busy, dirty = None, None, True, False, False
    destroyed = False
    owner_id = dialog.GetId()
    selected_ready = False
    rows, source_rows = [], []
    cancel = threading.Event()
    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control
    jobs = field('Збережені &завдання', wx.Choice, choices=[])
    title = field('&Назва нового завдання', value='Транскрипція')
    model = field('Локальна &модель', value='small')
    language = field('&Мова: auto, uk, ru, sk, en або код Whisper', value='auto')
    device = field('Пристрій &обчислення', wx.Choice, choices=['CPU — int8', 'CUDA — float16'])
    device.SetSelection(0)
    words = wx.CheckBox(dialog, label='Часові мітки кожного &слова')
    words.SetName('Часові мітки кожного слова')
    words.SetValue(True)
    layout.Add(words, 0, wx.ALL, 4)
    mixed = wx.CheckBox(dialog, label='Кілька мов у записі; потрібна сумісна &багатомовна модель')
    mixed.SetName('Кілька мов у записі')
    layout.Add(mixed, 0, wx.ALL, 4)
    sources = field('Запис для &редагування', wx.Choice, choices=[])
    text = field('&Текст: редагування зберігається окремою версією', proportion=2,
                 style=wx.TE_MULTILINE)
    status = field('Стан транскрипції', proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY,
        value='Оригінальне аудіо зберігається окремо. Результати: TXT, SRT, VTT, JSON; '
              'часові мітки слів — за вибором. Моделі не завантажуються автоматично. '
              'Після зупинки завершені файли повторно використовуються, незавершений файл обробляється спочатку.')
    buttons = {}
    row = wx.GridSizer(rows=3, cols=3, vgap=3, hgap=3)
    for name, label in [('configure', 'Підключити &Whisper'), ('create', '&Додати записи'),
                        ('folder', 'Додати папку з п&ідпапками'),
                        ('run', 'Запустити / &продовжити'), ('stop', 'З&упинити'),
                        ('save', 'Зберегти &правки'), ('export', '&Експорт'),
                        ('refresh', '&Оновити'), ('close', '&Закрити')]:
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
        buttons[name] = button
        row.Add(button, 0, wx.EXPAND | wx.ALL, 3)
    layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    def report(value):
        if alive:
            status.ChangeValue(str(value))
    def controls():
        if not alive:
            return
        for control in (jobs, title, model, language, device, words, mixed, sources, text):
            control.Enable(not busy)
        for name, button in buttons.items():
            button.Enable(not busy and (name not in ('run', 'export', 'save') or current is not None))
        buttons['stop'].Enable(busy)
        text.Enable(not busy and selected_ready)
        buttons['save'].Enable(not busy and selected_ready and dirty)
    def can_replace():
        nonlocal dirty
        if not dirty:
            return True
        discard = wx.MessageBox('Відкинути незбережені правки поточного тексту? '
                                'Щоб зберегти їх, оберіть «Ні», потім «Зберегти правки».',
                                'Незбережені правки', wx.YES_NO | wx.NO_DEFAULT, dialog)
        if discard == wx.YES:
            dirty = False
            return True
        text.SetFocus()
        return False
    def load_source():
        nonlocal selected_source, selected_ready, dirty
        index = sources.GetSelection()
        selected_source = source_rows[index]['filename'] if 0 <= index < len(source_rows) else None
        selected_ready = False
        text.ChangeValue('')
        dirty = False
        controls()
        if current and selected_source:
            job = service.get(current)
            if any(item['source'] == selected_source and item['format'] == 'txt' for item in job['artifacts']):
                job_id, source_id = current, selected_source
                def read_text():
                    value = service.text(job_id, source_id, cancelled=cancelled)
                    edits = [item for item in service.edits(job_id) if item['source'] == source_id]
                    return edits[-1]['text'] if edits else value
                def loaded(value):
                    nonlocal selected_ready
                    if current == job_id and selected_source == source_id:
                        text.ChangeValue(value)
                        selected_ready = True
                        controls()
                        text.SetFocus()
                begin('Перевірка й читання транскрипції', read_text, loaded)
    def load_job():
        nonlocal current, source_rows
        index = jobs.GetSelection()
        current = rows[index]['id'] if 0 <= index < len(rows) else None
        source_rows = service.get(current)['sources'] if current else []
        sources.SetItems([item['original_name'] for item in source_rows])
        if source_rows:
            sources.SetSelection(0)
        load_source()
        if current:
            job = service.get(current)
            report(f"{job['title']}: {job['status']}; {job['done']}/{len(job['sources'])}.\n" +
                   job.get('last_error', '') +
                   '\nПравки тексту не змінюють аудіо та початкові часові мітки.')
    def refresh(select=None):
        nonlocal rows
        rows = list(service.list())
        jobs.SetItems([f"{item['title']} — {item['status']} ({item['done']}/{len(item['sources'])})" for item in rows])
        if rows:
            ids = [item['id'] for item in rows]
            jobs.SetSelection(ids.index(select) if select in ids else 0)
        load_job()
    def begin(label, operation, complete, *, allow_dirty=False):
        nonlocal busy
        if busy or (not allow_dirty and not can_replace()):
            return
        if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            report('Спочатку завершіть поточну операцію програми.')
            return
        busy = True
        cancel.clear()
        controls()
        def done(result):
            nonlocal busy
            busy = False
            if not alive:
                return
            controls()
            if alive:
                try:
                    complete(result)
                except (ValueError, OSError, RuntimeError) as exc:
                    report(exc)
        def failed(error):
            nonlocal busy
            busy = False
            if not alive:
                return
            controls()
            report(error)
        if frame.run_job(label, operation, done=done, failed=failed) is False:
            busy = False
            controls()
    def cancelled():
        return cancel.is_set() or frame.cancel_event.is_set()
    def configure(event):
        try:
            with wx.FileDialog(dialog, 'Оберіть Python локального Whisper', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                python = chooser.GetPath()
            with wx.FileDialog(dialog, 'Оберіть перевірене доповнення до worker із квитанціями результатів', wildcard='Python (*.py)|*.py',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                worker = chooser.GetPath()
            with wx.DirDialog(dialog, 'Оберіть папку вже встановлених моделей', style=wx.DD_DIR_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                models_dir = chooser.GetPath()
            begin('Підключення локального Whisper', lambda: service.configure(python, worker, models_dir),
                  lambda value: report('Рушій підключено. Якість і доступність моделі перевіряються під час фактичного розпізнавання.'))
        except (ValueError, OSError, RuntimeError) as exc:
            report(exc)
    def create_selected(selected, *, folder=False):
        options = {'model': model.GetValue().strip(), 'language': language.GetValue().strip(),
                   'word_timestamps': words.GetValue(), 'multilingual': mixed.GetValue(),
                   'device': 'cuda' if device.GetSelection() == 1 else 'cpu',
                   'compute_type': 'float16' if device.GetSelection() == 1 else 'int8'}
        job_title = title.GetValue()
        if folder:
            def created(result):
                refresh(result['id'])
                report(f"Збережено записів: {result['files']}. Непідтримуваних файлів пропущено: "
                       f"{result['excluded_files']}. Підпапки включено. Завдання готове до запуску.")
            begin('Перевірка папки й збереження оригінальних записів',
                  lambda: service.submit_folder(selected, title=job_title, options=options, cancelled=cancelled),
                  created)
        else:
            begin('Збереження оригінальних записів',
                  lambda: service.submit(selected, title=job_title, options=options, cancelled=cancelled),
                  lambda identity: refresh(identity))
    def create(event):
        if not can_replace():
            return
        try:
            with wx.FileDialog(dialog, 'Оберіть аудіо або відео', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                selected = chooser.GetPaths()
            create_selected(selected)
        except (ValueError, OSError, RuntimeError) as exc:
            report(exc)
    def create_folder(event):
        if not can_replace():
            return
        try:
            with wx.DirDialog(dialog, 'Оберіть папку аудіо або відео; підпапки буде включено',
                              style=wx.DD_DIR_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                selected = chooser.GetPath()
            create_selected(selected, folder=True)
        except (ValueError, OSError, RuntimeError) as exc:
            report(exc)
    def run(event):
        identity = current
        if identity is None:
            report('Спочатку додайте записи або оберіть збережене завдання.')
            return
        restored = service.get(identity).get('restore_requires_review', False)
        if restored and wx.MessageBox(
                'Це відновлене завдання. Повторно перевірити збережені оригінали, '
                'вибраний локальний рушій і модель та дозволити новий запуск? '
                'Спочатку підключіть перевірений рушій на цьому комп’ютері.',
                'Перевірка після відновлення', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
            return
        def operation():
            if restored:
                service.review_restored(identity, cancelled=cancelled)
            progress = getattr(frame, 'operation_progress', None)
            token = progress.event.token if progress is not None else None
            def advanced(done, total, phase):
                if progress is not None:
                    progress.update(done, total, phase, token=token)
                wx.CallAfter(report, f'{done}/{total}: {phase}. Кількість файлів повідомляє рушій; '
                            'підтверджені результати показані у стані завдання.')
            return service.run(identity, cancelled=cancelled, progress=advanced)
        begin('Локальна транскрипція Whisper', operation, lambda result: refresh(identity))
    def save(event):
        if current is None or selected_source is None or not selected_ready:
            return
        identity, source_id, value = current, selected_source, text.GetValue()
        def saved(revision):
            nonlocal dirty
            dirty = False
            controls()
            report('Правки збережено окремою версією: ' + revision +
                   '. Початкове розпізнавання та аудіо залишилися незмінними.')
        begin('Збереження правок транскрипції',
              lambda: service.save_edit(identity, source_id, value, cancelled=cancelled),
              saved, allow_dirty=True)
    def export(event):
        if current is None or not can_replace():
            return
        identity = current
        try:
            with wx.DirDialog(dialog, 'Оберіть батьківську папку експорту', style=wx.DD_DIR_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                parent = Path(chooser.GetPath())
            with wx.TextEntryDialog(dialog, 'Назва нової папки', 'Експорт транскрипції',
                                    'transcription-' + identity[:8]) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                name = chooser.GetValue().strip()
            from ..state_backup import relative_name
            if len(relative_name(name).parts) != 1:
                raise ValueError('Вкажіть лише назву нової папки.')
            begin('Експорт транскрипції', lambda: service.export(identity, parent / name, cancelled=cancelled),
                  lambda path: report('Експорт завершено: ' + str(path)))
        except (ValueError, OSError, RuntimeError) as exc:
            report(exc)
    def changed(event):
        nonlocal dirty
        dirty = True
        controls()
        event.Skip()
    def select_job(event):
        if not can_replace():
            ids = [item['id'] for item in rows]
            if current in ids:
                jobs.SetSelection(ids.index(current))
            return
        try:
            load_job()
        except (ValueError, OSError, RuntimeError) as exc:
            report(exc)
    def select_source(event):
        if not can_replace():
            ids = [item['filename'] for item in source_rows]
            if selected_source in ids:
                sources.SetSelection(ids.index(selected_source))
            return
        try:
            load_source()
        except (ValueError, OSError, RuntimeError) as exc:
            report(exc)
    def update_jobs(event):
        if can_replace():
            try:
                refresh(current)
            except (ValueError, OSError, RuntimeError) as exc:
                report(exc)
    def close(event):
        nonlocal alive
        if busy:
            cancel.set()
            report('Зупиняю поточне завдання. Після зупинки вікно можна закрити.')
            if hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        if not can_replace():
            if hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        alive = False
        dialog.EndModal(wx.ID_CLOSE)
    for name, handler in [('configure', configure), ('create', create), ('folder', create_folder),
                          ('run', run), ('save', save),
                          ('export', export), ('refresh', update_jobs), ('close', close)]:
        buttons[name].Bind(wx.EVT_BUTTON, handler)
    buttons['stop'].Bind(wx.EVT_BUTTON, lambda event: cancel.set())
    jobs.Bind(wx.EVT_CHOICE, select_job)
    sources.Bind(wx.EVT_CHOICE, select_source)
    text.Bind(wx.EVT_TEXT, changed)
    def destroyed_owner(event):
        nonlocal alive, destroyed
        if event.GetId() == owner_id:
            alive, destroyed = False, True
            cancel.set()
        event.Skip()
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed_owner)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        refresh()
        controls()
        jobs.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        if not destroyed:
            dialog.Destroy()



from pathlib import Path
from threading import Event, Lock
from uuid import uuid4
import json
import re

from ..speech.reading import ReadingDocument
from ..durable_state import local_path, regular


DEVICES = (('cpu', 'int8', 'CPU — int8'), ('cpu', 'float32', 'CPU — float32'),
           ('cuda', 'float16', 'CUDA GPU — float16'),
           ('cuda', 'int8_float16', 'CUDA GPU — int8 / float16'))
FORMATS = ('txt', 'srt', 'vtt', 'json')
MEDIA_FILTER = ('Audio or video|*.wav;*.mp3;*.m4a;*.m4b;*.flac;*.ogg;*.opus;*.aac;'
                '*.mp4;*.mkv;*.webm;*.wma;*.3gp;*.amr|All files|*.*')
HELP = (
    'Use an existing local Python runtime, the reviewed transcription worker, and a '
    'concrete local model folder. Inspect checks their identity without running them. '
    'Registration requires your explicit trust of those exact files. No model is '
    'downloaded, and no audio is uploaded. Registration does not establish recognition '
    'accuracy or language coverage.\n\n'
    'Choose files or a folder in New transcription. Folder recursion is optional. '
    'Auto asks the selected model to detect the spoken language. An explicit language '
    'code avoids detection; availability depends on that model. Translate speech means '
    'English output only. CPU and CUDA are explicit choices; CUDA needs a compatible '
    'installed runtime.\n\n'
    'Start creates a durable job before inference. Pause checkpoints and stops the '
    'worker; Resume uses verified saved work. Cancellation and pause wait for a safe '
    'boundary. Completed files remain available when a later file fails. Closing during '
    'work requests a checkpoint and waits. Resume never substitutes newly edited options '
    'for a saved job. After restoring a backup, explicitly inspect and register a runtime '
    'again, then use Rebind runtime on the saved job.\n\n'
    'Saved jobs contains source results. Load transcript opens a ReadingDocument copy. '
    'Save edits writes a separate derivative; Undo and Redo operate on the reading copy. '
    'Read aloud opens the existing local voice reader. Recognition and edited text are '
    'unverified derivatives, not authenticated quotations. Export writes configured '
    'formats into a new subfolder. Edited text is separate from timed original subtitles.\n\n'
    'Use Tab and Shift+Tab between native controls, Ctrl+Tab between pages, and the '
    'labelled buttons to operate the workspace. Escape closes after work is stopped. '
    'Progress does not move keyboard focus. Screen-reader modifier keys are untouched.'
)


class TranscriptionController:
    """One library binding, with service construction deferred to a worker."""

    def __init__(self, private_root, *, service=None):
        self.private_root = Path(private_root).absolute()
        self._service = service

    @property
    def service(self):
        if self._service is None:
            from ..operations import ResourceBroker
            from ..transcription import TranscriptionService
            self._service = TranscriptionService(self.private_root / 'state' / 'transcription',
                broker=ResourceBroker.for_app(self.private_root), protocol='local-file-job/1')
        return self._service

    def check_root(self, root):
        if Path(root).absolute() != self.private_root:
            raise ValueError('The library folder changed. Close and reopen transcription.')


class TranscriptionActions:
    """Testable native actions: immutable job choices and guarded callbacks."""

    def configure(self, frame, controller):
        self.frame, self.controller = frame, controller
        self.library = frame.library
        self.pending = self.closed = self.close_when_idle = False
        self.cancel_event = Event()
        self._progress_lock = Lock()
        self._latest_progress = None
        self._epoch = 0
        self._active_job_id = None
        self._stop_intent = None
        self._kind = ''
        self.profiles, self.job_rows, self.sources = (), (), ()
        self.current_job = self.document = self.transcript_source_id = None
        self.saved_text = ''
        self.inspection = None
        self._enable()

    def report(self, message, *, focus=False):
        if not self.closed:
            self.status.ChangeValue(str(message))
            if focus:
                self.status.SetFocus()

    def _check_root(self):
        self.controller.check_root(self.frame.library.root)

    def _enable(self):
        if self.closed:
            return
        for control in self.mutable:
            control.Enable(not self.pending)
        editable = not self.pending and self.current_job is None
        for control in self.new_controls:
            control.Enable(editable)
        have_text = not self.pending and self.document is not None
        self.editor.Enable(have_text)
        for button in self.read_buttons:
            button.Enable(have_text)
        self.start_button.Enable(not self.pending and
            (self.current_job is None or self.current_job.get('status') != 'complete'))
        self.pause_button.Enable(self.pending and self._kind == 'transcribe')
        self.cancel_button.Enable(self.pending)

    def _usable(self):
        if self.closed or self.pending:
            return False
        try:
            self._check_root()
        except ValueError as exc:
            self.report(str(exc), focus=True)
            return False
        if getattr(self.frame, 'working', False) or getattr(self.frame, '_closing', False):
            self.report('Another operation is active. Finish or cancel it before starting this action.', focus=True)
            return False
        return True

    def _cancelled(self):
        return self.cancel_event.is_set() or self.frame.cancel_event.is_set()

    def submit(self, title, operation, complete, *, kind='metadata'):
        if not self._usable():
            return False
        self._epoch += 1
        epoch = self._epoch
        self.pending, self._kind = True, kind
        self._active_job_id, self._stop_intent = None, None
        self.cancel_event.clear()
        with self._progress_lock:
            self._latest_progress = None
        self._enable()
        self.report(title + '…')

        def current():
            if self.closed or epoch != self._epoch:
                return False
            try:
                self._check_root()
            except ValueError as exc:
                self.report(str(exc), focus=True)
                return False
            return True

        def work(update_progress):
            self._check_root()
            def update(event):
                if self.closed or epoch != self._epoch or not isinstance(event, dict):
                    return
                phase = str(event.get('phase') or event.get('status') or 'Transcribing')
                phase = ' '.join(phase.split())[:200] or 'Transcribing'
                done, total = event.get('done'), event.get('total')
                measured = (type(done) is int and type(total) is int and 0 <= done <= total and total > 0
                            and not event.get('indeterminate', False))
                message = phase + (f': {done} of {total} files' if measured else ' — working; total not yet known')
                # The existing frame owns the operation token and isolates
                # observer failures. Phase units are incomparable: keep the
                # overall task indeterminate and include measured files in text.
                try:
                    update_progress(None, None, message[:512])
                except Exception as exc:
                    message += '\nGlobal progress reporting unavailable: ' + str(exc)[:400]
                if event.get('current'):
                    message += '\n' + str(event['current'])[:1000]
                with self._progress_lock:
                    self._latest_progress = (epoch, message)

            return operation(self.controller.service, self._cancelled, update)

        def finished(result):
            if epoch != self._epoch or self.closed:
                return
            self.pending = False
            with self._progress_lock:
                self._latest_progress = None
            self._enable()
            if current():
                complete(result)
            if self.close_when_idle and not self.pending:
                self.close()

        def failed(error):
            if epoch != self._epoch or self.closed:
                return
            self.pending = False
            with self._progress_lock:
                self._latest_progress = None
            self._enable()
            if current():
                message = ('Operation stopped before completion. ' if isinstance(error, InterruptedError)
                           else 'Operation did not complete. ')
                self.report(message + str(error) + '\nRefresh saved jobs to inspect retained work.', focus=True)
            if self.close_when_idle:
                self.close()

        try:
            accepted = self.frame.run_job(title, work, done=finished, failed=failed, with_progress=True)
        except Exception as exc:
            failed(exc)
            return False
        if accepted is False:
            failed(RuntimeError('The host did not start this operation.'))
            return False
        return True

    def flush_progress(self, event=None):
        with self._progress_lock:
            update, self._latest_progress = self._latest_progress, None
        if not self.closed and self.pending and update is not None and update[0] == self._epoch:
            self.report(update[1])

    def refresh(self, event=None):
        def loaded(value):
            self.profiles, self.job_rows = tuple(value['profiles']), tuple(value['jobs'])
            self.runtime.SetItems([f"{row['name']} — {row.get('model_name', 'local model')} — "
                f"{'trusted' if row.get('trusted') else 'not trusted'}; {row.get('qualification', 'unqualified')}"
                for row in self.profiles])
            if self.profiles:
                self.runtime.SetSelection(0)
            self._render_jobs()
            self.report(f'{len(self.profiles)} registered runtimes; {len(self.job_rows)} saved jobs. '
                        'Select local media to start, or load a saved job. No inference starts automatically.')
        self.submit('Loading local transcription profiles and jobs',
            lambda service, cancel, progress: {'profiles': service.list_runtimes(), 'jobs': service.list_jobs()}, loaded)

    def _render_jobs(self):
        self.jobs.SetItems([f"{row.get('title', 'Transcription')} — {row['status']} — "
            f"{row.get('done', 0)}/{row.get('total', len(row.get('sources', [])))} files — {row['id']}"
            for row in self.job_rows])
        identity = self.current_job['id'] if self.current_job else None
        index = next((i for i, row in enumerate(self.job_rows) if row['id'] == identity),
                     0 if self.job_rows else -1)
        self.jobs.SetSelection(index)

    def _selected_profile(self):
        index = self.runtime.GetSelection()
        if not 0 <= index < len(self.profiles):
            raise ValueError('Register and choose an existing local runtime first.')
        profile = self.profiles[index]
        if profile.get('trusted') is not True:
            raise ValueError('Inspect and explicitly trust this runtime before starting it.')
        return profile

    def _settings(self):
        language = self.language.GetValue().strip().lower()
        if not re.fullmatch(r'auto|[a-z]{2,3}', language):
            raise ValueError('Enter auto or a language code such as uk or en.')
        selection = self.task.GetSelection()
        if selection not in (0, 1):
            raise ValueError('Choose transcription or translation to English.')
        index = self.device.GetSelection()
        if not 0 <= index < len(DEVICES):
            raise ValueError('Choose CPU or CUDA and its compute type.')
        device, compute, _ = DEVICES[index]
        formats = {name: bool(control.GetValue()) for name, control in self.formats.items()}
        if not any(formats.values()):
            raise ValueError('Select at least one output format.')
        return dict(language=language, task=('transcribe', 'translate')[selection], device=device,
            compute_type=compute, beam_size=self.beam.GetValue(), parallel_files=self.parallel.GetValue(),
            vad_filter=bool(self.vad.GetValue()), condition_on_previous_text=bool(self.context.GetValue()),
            multilingual=bool(self.multilingual.GetValue()), include_headers=bool(self.headers.GetValue()),
            output_mode='separate', **{'output_' + key: value for key, value in formats.items()})

    def start(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        try:
            if self.current_job is None:
                profile = self._selected_profile()
                sources, settings = tuple(self.sources), self._settings()
                title, recursive = self.title.GetValue().strip() or 'Transcription', bool(self.recursive.GetValue())
                if not sources:
                    raise ValueError('Choose at least one local audio file or folder.')
                def run(service, cancel, progress):
                    job = service.create_job(profile['id'], sources, settings=settings,
                        title=title, recursive=recursive, cancel=cancel, progress=progress)
                    self._active_job_id = job['id']
                    if cancel():
                        (service.pause if self._stop_intent == 'paused' else service.cancel)(job['id'])
                        return service.get_job(job['id'])
                    return service.run(job['id'], cancel=cancel, progress=progress)
            else:
                identity = self.current_job['id']
                if self.current_job['status'] == 'complete':
                    raise ValueError('This job is complete. Choose New job for another transcription.')
                def run(service, cancel, progress):
                    self._active_job_id = identity
                    if cancel():
                        (service.pause if self._stop_intent == 'paused' else service.cancel)(identity)
                        return service.get_job(identity)
                    return service.resume(identity, cancel=cancel, progress=progress)
            self.submit('Transcribing local audio', run, self.publish_job, kind='transcribe')
        except Exception as exc:
            self.report(str(exc), focus=True)

    def publish_job(self, job):
        self.current_job = job
        self.job_rows = (job,) + tuple(row for row in self.job_rows if row['id'] != job['id'])
        self._render_jobs()
        self.result_source.SetItems([Path(row['original_path']).name + ' — ' + row['id']
                                    for row in job.get('sources', ())])
        if job.get('sources'):
            self.result_source.SetSelection(0)
        self.document = self.transcript_source_id = None
        self.saved_text = ''
        self.editor.ChangeValue('')
        self.provenance.ChangeValue('Load a completed source result. Recognition text is an unverified derivative.')
        summary = (f"{job.get('title', 'Transcription')}: {job['status']}; "
                   f"{job.get('done', 0)} of {job.get('total', len(job.get('sources', [])))} files.\n"
                   f"Saved job: {job['id']}. Choose a source result and Load transcript.")
        settings = job.get('settings', {})
        summary += (f"\nSaved options: {settings.get('language', 'auto')}; {settings.get('task', 'transcribe')}; "
                    f"{settings.get('device', 'cpu')}/{settings.get('compute_type', 'int8')}. "
                    'Resume uses these saved options.')
        if job.get('error'):
            summary += '\n' + str(job['error'])
        self.report(summary)
        self._enable()

    def load_job(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        index = self.jobs.GetSelection()
        if not 0 <= index < len(self.job_rows):
            self.report('Choose a saved job.', focus=True)
            return
        identity = self.job_rows[index]['id']
        self.submit('Opening saved transcription job',
                    lambda service, cancel, progress: service.get_job(identity), self.publish_job)

    def new_job(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        self.current_job = self.document = self.transcript_source_id = None
        self.saved_text = ''
        self.editor.ChangeValue('')
        self.result_source.SetItems([])
        self.provenance.ChangeValue('No transcript is loaded.')
        self._enable()
        self.report('New job. Select a registered runtime, local files and options.')

    def request_stop(self, *, pause=False):
        if not self.pending or self.closed:
            return
        self._stop_intent = 'paused' if pause else 'cancelled'
        try:
            if self._kind == 'transcribe' and self._active_job_id is not None:
                service = self.controller.service
                (service.pause if pause else service.cancel)(self._active_job_id)
        except Exception as exc:
            self.report('Could not record the stop request: ' + str(exc), focus=True)
            return
        self.cancel_event.set()
        self.frame.cancel_event.set()
        self.report(('Pause' if pause else 'Cancellation') + ' requested. Waiting for a safe checkpoint; '
                    'completed files remain saved. Preparation may stop before a new job is created.')

    def pause(self, event=None):
        self.request_stop(pause=True)

    def cancel(self, event=None):
        self.request_stop()

    def close(self, event=None):
        if self.pending:
            self.close_when_idle = True
            self.request_stop(pause=self._kind == 'transcribe')
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        if not self.confirm_discard():
            self.close_when_idle = False
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        self.closed = True
        self.finish_close()

    def _paths(self):
        return tuple(control.GetValue().strip() for control in
                     (self.python_path, self.worker_path, self.model_path))

    def runtime_changed(self, event=None):
        self.inspection = None
        self.trust.SetValue(False)
        self.runtime_details.ChangeValue('Inspect these exact files before trusting and registering this runtime.')
        if event is not None:
            event.Skip()

    def inspect_runtime(self, event=None):
        if not self._usable():
            return
        paths = self._paths()
        self.inspection = None
        self.trust.SetValue(False)
        def inspected(result):
            if paths != self._paths():
                self.runtime_changed()
                return
            self.inspection = (paths, result)
            summary = {key: result[key] for key in ('python_executable', 'python_resolved',
                'python_identity', 'worker_path', 'worker_identity', 'model_directory', 'model_name',
                'model_bytes', 'model_content_fingerprint', 'fingerprint') if key in result}
            summary['model_file_count'] = len(result.get('model_files', {}))
            self.runtime_details.ChangeValue(json.dumps(summary, ensure_ascii=False, indent=2))
            self.report('Files inspected without execution. Review their origin and licence, then explicitly '
                        'trust and register them. Recognition quality remains unqualified.')
        self.submit('Inspecting existing local transcription files',
            lambda service, cancel, progress: service.inspect_runtime(*paths, cancel=cancel, progress=progress), inspected)

    def register_runtime(self, event=None):
        if not self._usable():
            return
        paths = self._paths()
        if self.inspection is None or self.inspection[0] != paths or self.trust.GetValue() is not True:
            self.report('Inspect these exact files, then check the explicit trust confirmation.', focus=True)
            return
        name, fingerprint = self.runtime_name.GetValue().strip(), self.inspection[1]['fingerprint']
        def registered(profile):
            self.profiles = tuple(row for row in self.profiles if row['id'] != profile['id']) + (profile,)
            self.runtime.SetItems([f"{row['name']} — {row.get('model_name', 'local model')} — "
                f"{'trusted' if row.get('trusted') else 'not trusted'}; {row.get('qualification', 'unqualified')}"
                for row in self.profiles])
            self.runtime.SetSelection(len(self.profiles) - 1)
            self.report('Runtime registered using the verified local files. No inference has run. '
                        'Choose media in New transcription to start.')
        self.submit('Registering the reviewed local runtime',
            lambda service, cancel, progress: service.register_runtime(name, *paths, owner_trusted=True,
                expected_fingerprint=fingerprint, cancel=cancel, progress=progress), registered)

    def rebind_runtime(self, event=None):
        if not self._usable() or not self.current_job or not self.confirm_discard():
            return
        try:
            profile, identity = self._selected_profile(), self.current_job['id']
            if not self.confirm_rebind(profile):
                return
            self.submit('Binding reviewed runtime to saved job',
                lambda service, cancel, progress: service.rebind_runtime(identity, profile['id']), self.publish_job)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def review_interruption(self, event=None):
        if not self._usable() or not self.current_job or not self.confirm_discard():
            return
        if (self.current_job.get('status') == 'complete'
                or not self.current_job.get('recovery_review_required')):
            self.report('This job does not require an interrupted-worker review.')
            return
        if not self.confirm_worker_stopped():
            return
        identity = self.current_job['id']
        self.submit('Recording interrupted worker review',
            lambda service, cancel, progress: service.acknowledge_interruption(identity, worker_stopped=True),
            self.publish_job)

    def load_transcript(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        index = self.result_source.GetSelection()
        sources = self.current_job.get('sources', ()) if self.current_job else ()
        if not 0 <= index < len(sources):
            self.report('Load a saved job and choose a completed source result.', focus=True)
            return
        identity, source = self.current_job['id'], sources[index]['id']
        edited = bool(self.latest_edit.GetValue())
        def read(service, cancel, progress):
            original = service.read_transcript(identity, source)
            latest = original
            if edited:
                try:
                    latest = service.read_transcript(identity, source, edited=True)
                except FileNotFoundError:
                    pass
            return {'original': original, 'latest': latest}
        def loaded(value):
            original, latest = value['original'], value['latest']
            document = ReadingDocument(original['text'], title='Transcription reading copy')
            document.set_text(latest['text'], label='Loaded saved transcript derivative')
            self.document, self.transcript_source_id = document, source
            self.saved_text = latest['text']
            self.editor.ChangeValue(document.text)
            self.provenance.ChangeValue('Unverified recognition derivative. Edits never overwrite the original.\n' +
                json.dumps(latest.get('provenance', {}), ensure_ascii=False, indent=2))
            self.report('Transcript loaded. Edit the reading copy or open the local voice reader.')
            self._enable()
            self.editor.SetFocus()
        self.submit('Loading transcript reading copy', read, loaded)

    def is_dirty(self):
        return self.document is not None and self.editor.GetValue() != self.saved_text

    def _capture_edit(self):
        if self.document is None:
            raise ValueError('Load a transcript first.')
        self.document.set_text(self.editor.GetValue(), label='Edit transcript reading copy')
        return self.document.text

    def save_edits(self, event=None):
        if not self._usable() or self.document is None:
            return
        try:
            text = self._capture_edit()
            identity, source = self.current_job['id'], self.transcript_source_id
            def saved(receipt):
                self.saved_text = text
                self.provenance.ChangeValue('Saved separate edited derivative. Original recognition text unchanged.\n' +
                    json.dumps(receipt.get('provenance', {}), ensure_ascii=False, indent=2))
                self.report('Edited reading copy saved as a separate derivative.')
            self.submit('Saving transcript derivative',
                lambda service, cancel, progress: service.save_edit(identity, source, text), saved)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def undo(self, event=None):
        if not self._usable() or self.document is None:
            return
        try:
            self._capture_edit()
            self.document.undo()
            self.editor.ChangeValue(self.document.text)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def redo(self, event=None):
        if not self._usable() or self.document is None:
            return
        try:
            self._capture_edit()
            self.document.redo()
            self.editor.ChangeValue(self.document.text)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def export_to(self, directory):
        if not self._usable() or self.current_job is None:
            return
        if self.is_dirty():
            self.report('Save the edited reading copy before exporting, or undo the unsaved edit.', focus=True)
            return
        identity = self.current_job['id']
        settings = self.current_job['settings']
        formats = tuple(name for name in FORMATS if settings.get('output_' + name))
        library_root = self.controller.private_root
        def export(service, cancel, progress):
            if cancel():
                raise InterruptedError('Export cancelled before publication.')
            destination = local_path(directory)
            root = local_path(library_root, exists=True)
            regular(destination.parent, directory=True)
            if destination.is_relative_to(root) or root.is_relative_to(destination):
                raise ValueError('Choose an export folder outside the private library.')
            destination.mkdir()  # exclusive new folder; never reuse an existing export
            return service.export(identity, destination, formats=formats, include_edits=True, cancel=cancel)
        self.submit('Exporting verified transcription results',
            export,
            lambda receipt: self.report('Export saved.\n' + json.dumps(receipt, ensure_ascii=False, indent=2)))


def build_transcription_dialog(frame, controller=None):
    """Create native controls; callers own ShowModal/Destroy and may inject a service."""
    import wx
    controller = controller or TranscriptionController(frame.library.root)

    class Dialog(wx.Dialog, TranscriptionActions):
        def __init__(self):
            wx.Dialog.__init__(self, frame, title='Local transcription workstation', size=(980, 860),
                               style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.SetName('Local transcription workstation')
            self.mutable, self.new_controls, self.read_buttons = [], [], []
            layout = wx.BoxSizer(wx.VERTICAL)
            note = wx.StaticText(self, label='Local media and installed models. No uploads or model downloads. '
                'Recognition text and edits are separate, unverified derivatives.')
            note.Wrap(900)
            layout.Add(note, 0, wx.ALL | wx.EXPAND, 8)
            self.pages = wx.Notebook(self)
            self.pages.SetName('Transcription workstation pages')
            new = wx.ScrolledWindow(self.pages)
            new.SetScrollRate(0, 16)
            runtimes = wx.ScrolledWindow(self.pages)
            runtimes.SetScrollRate(0, 16)
            results = wx.Panel(self.pages)
            self.pages.AddPage(new, 'New transcription')
            self.pages.AddPage(runtimes, 'Local runtime')
            self.pages.AddPage(results, 'Saved jobs and results')
            layout.Add(self.pages, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

            def field(parent, box, label, factory=wx.TextCtrl, *, proportion=0, new_option=False, **kwargs):
                box.Add(wx.StaticText(parent, label=label), 0, wx.TOP | wx.LEFT | wx.RIGHT, 5)
                control = factory(parent, **kwargs)
                control.SetName(label.replace('&', ''))
                box.Add(control, proportion, wx.EXPAND | wx.ALL, 5)
                self.mutable.append(control)
                if new_option:
                    self.new_controls.append(control)
                return control

            def button(parent, row, label, action, *, new_option=False, reading=False):
                control = wx.Button(parent, label=label)
                control.SetName(label.replace('&', ''))
                control.Bind(wx.EVT_BUTTON, action)
                row.Add(control, 0, wx.ALL, 4)
                self.mutable.append(control)
                if new_option:
                    self.new_controls.append(control)
                if reading:
                    self.read_buttons.append(control)
                return control

            box = wx.BoxSizer(wx.VERTICAL)
            self.runtime = field(new, box, 'Registered transcription &runtime', wx.Choice)
            row = wx.BoxSizer(wx.HORIZONTAL)
            button(new, row, 'Manage local runtime', lambda event: self.pages.SetSelection(1))
            button(new, row, 'Refresh profiles and jobs', self.refresh)
            box.Add(row)
            self.title = field(new, box, 'Job &title', value='Transcription', new_option=True)
            self.source_paths = field(new, box, 'Selected local audio or video',
                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80), new_option=True)
            row = wx.BoxSizer(wx.HORIZONTAL)
            button(new, row, 'Add &files…', self.choose_files, new_option=True)
            button(new, row, 'Add f&older…', self.choose_folder, new_option=True)
            button(new, row, 'Clear files', self.clear_sources, new_option=True)
            box.Add(row)
            self.recursive = wx.CheckBox(new, label='Include subfolders of selected folders')
            self.recursive.SetName('Include subfolders of selected folders')
            box.Add(self.recursive, 0, wx.ALL, 5)
            self.mutable.append(self.recursive)
            self.new_controls.append(self.recursive)
            self.language = field(new, box, 'Source lan&guage code or auto', value='auto', new_option=True)
            self.task = field(new, box, 'Transcription task', wx.Choice, new_option=True,
                choices=['Transcribe in the source language', 'Translate speech to English'])
            self.task.SetSelection(0)
            self.device = field(new, box, 'Processing &device and compute type', wx.Choice,
                choices=[row[2] for row in DEVICES], new_option=True)
            self.device.SetSelection(0)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(new, label='Beam size'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.beam = wx.SpinCtrl(new, min=1, max=20, initial=5)
            self.beam.SetName('Beam size, one to twenty')
            row.Add(self.beam, 0, wx.ALL, 5)
            row.Add(wx.StaticText(new, label='Parallel files'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.parallel = wx.SpinCtrl(new, min=1, max=4, initial=1)
            self.parallel.SetName('Parallel files, one to four')
            row.Add(self.parallel, 0, wx.ALL, 5)
            box.Add(row)
            self.mutable.extend((self.beam, self.parallel))
            self.new_controls.extend((self.beam, self.parallel))
            row = wx.WrapSizer(wx.HORIZONTAL)
            for attr, label, default in (('vad', 'Voice activity detection', True),
                    ('context', 'Use previous text as context', True),
                    ('multilingual', 'Allow multilingual segments', False),
                    ('headers', 'Include text headers', False)):
                control = wx.CheckBox(new, label=label)
                control.SetName(label)
                control.SetValue(default)
                setattr(self, attr, control)
                row.Add(control, 0, wx.ALL, 5)
                self.mutable.append(control)
                self.new_controls.append(control)
            box.Add(row, 0, wx.EXPAND)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(new, label='Output formats'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.formats = {}
            for name in FORMATS:
                control = wx.CheckBox(new, label=name.upper())
                control.SetName('Output ' + name.upper())
                control.SetValue(name in ('txt', 'json'))
                row.Add(control, 0, wx.ALL, 5)
                self.formats[name] = control
                self.mutable.append(control)
                self.new_controls.append(control)
            box.Add(row)
            new.SetSizer(box)

            box = wx.BoxSizer(wx.VERTICAL)
            text = wx.StaticText(runtimes, label='Point to existing, reviewed files. Inspection does not execute them. '
                'Registration records their exact identity and your trust; quality remains unqualified.')
            text.Wrap(880)
            box.Add(text, 0, wx.ALL | wx.EXPAND, 6)
            self.runtime_name = field(runtimes, box, 'Runtime profile name', value='My local Whisper runtime')
            for attr, label, directory in (('python_path', 'Python executable', False),
                    ('worker_path', 'Reviewed transcription worker', False),
                    ('model_path', 'Existing local model directory', True)):
                control = field(runtimes, box, label)
                setattr(self, attr, control)
                row = wx.BoxSizer(wx.HORIZONTAL)
                button(runtimes, row, 'Browse ' + label.lower() + '…',
                    lambda event, target=control, is_directory=directory: self.choose_runtime_path(target, is_directory))
                box.Add(row)
            self.runtime_details = field(runtimes, box, 'Runtime inspection and fingerprint',
                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 130))
            self.trust = wx.CheckBox(runtimes, label='I trust these inspected runtime, worker and model files and have reviewed their licence')
            self.trust.SetName('Explicit trust of inspected runtime, worker and model files')
            box.Add(self.trust, 0, wx.ALL, 5)
            self.mutable.append(self.trust)
            row = wx.BoxSizer(wx.HORIZONTAL)
            button(runtimes, row, '&Inspect files', self.inspect_runtime)
            button(runtimes, row, '&Register trusted runtime', self.register_runtime)
            box.Add(row)
            runtimes.SetSizer(box)

            box = wx.BoxSizer(wx.VERTICAL)
            self.jobs = field(results, box, 'Saved transcription &jobs', wx.Choice)
            row = wx.WrapSizer(wx.HORIZONTAL)
            button(results, row, 'Load selected job', self.load_job)
            button(results, row, 'Refresh saved jobs', self.refresh)
            button(results, row, 'New job', self.new_job)
            button(results, row, 'Rebind runtime…', self.rebind_runtime)
            button(results, row, 'Review interrupted worker…', self.review_interruption)
            box.Add(row, 0, wx.EXPAND)
            self.result_source = field(results, box, 'Transcription source &result', wx.Choice)
            self.latest_edit = wx.CheckBox(results, label='Load the latest saved edit when available')
            self.latest_edit.SetName('Load the latest saved edit when available')
            self.latest_edit.SetValue(True)
            self.mutable.append(self.latest_edit)
            box.Add(self.latest_edit, 0, wx.ALL, 5)
            row = wx.BoxSizer(wx.HORIZONTAL)
            button(results, row, 'L&oad transcript', self.load_transcript)
            button(results, row, '&Export results…', self.choose_export)
            box.Add(row)
            self.provenance = field(results, box, 'Transcript provenance', value='No transcript is loaded.',
                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 70))
            self.editor = field(results, box, 'Transcript reading cop&y', proportion=1,
                style=wx.TE_MULTILINE | wx.TE_DONTWRAP)
            row = wx.WrapSizer(wx.HORIZONTAL)
            button(results, row, 'Sa&ve edits', self.save_edits, reading=True)
            button(results, row, '&Undo reading edit', self.undo, reading=True)
            button(results, row, 'Redo reading edit', self.redo, reading=True)
            button(results, row, 'Read &aloud…', self.read_aloud, reading=True)
            box.Add(row, 0, wx.EXPAND)
            results.SetSizer(box)

            layout.Add(wx.StaticText(self, label='Transcription progress and result'), 0, wx.LEFT | wx.TOP, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
            self.status.SetName('Transcription progress and result')
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            row = wx.WrapSizer(wx.HORIZONTAL)
            self.start_button = wx.Button(self, label='&Start / resume')
            self.pause_button = wx.Button(self, label='&Pause at checkpoint')
            self.cancel_button = wx.Button(self, label='&Cancel operation')
            self.help_button = wx.Button(self, label='&Help')
            self.close_button = wx.Button(self, wx.ID_CLOSE, 'C&lose')
            for control, action in ((self.start_button, self.start), (self.pause_button, self.pause),
                    (self.cancel_button, self.cancel), (self.help_button, self.help), (self.close_button, self.close)):
                control.SetName(control.GetLabel().replace('&', ''))
                control.Bind(wx.EVT_BUTTON, action)
                row.Add(control, 0, wx.ALL, 5)
            layout.Add(row, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((700, 620))
            self.SetEscapeId(wx.ID_CLOSE)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.configure(frame, controller)
            for control in (self.python_path, self.worker_path, self.model_path):
                control.Bind(wx.EVT_TEXT, self.runtime_changed)
            self._destroyed = False
            owner_id = self.GetId()
            self.timer = wx.Timer(self)
            timer = self.timer
            def destroyed_owner(event):
                if event.GetId() == owner_id:
                    self.closed, self._destroyed = True, True
                    self.cancel_event.set()
                    timer.Stop()
                event.Skip()
            self.Bind(wx.EVT_WINDOW_DESTROY, destroyed_owner)
            self.Bind(wx.EVT_TIMER, self.flush_progress, self.timer)
            self.timer.Start(250)
            self.runtime.SetFocus()

        def confirm_discard(self):
            return not self.is_dirty() or wx.MessageBox(
                'Discard the unsaved reading-copy edits? The original transcript and saved edits remain available.',
                'Unsaved transcript edits', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES

        def confirm_rebind(self, profile):
            return wx.MessageBox('Bind this saved job to the selected reviewed runtime: ' + profile['name'] +
                '? The service will verify model compatibility before resuming.', 'Rebind saved job',
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES

        def confirm_worker_stopped(self):
            return wx.MessageBox('Confirm that the previous external transcription worker has stopped. '
                'Check its process on this computer before allowing another worker for the same job. '
                'Have you confirmed that it is stopped?', 'Review interrupted worker',
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES

        def finish_close(self):
            self.timer.Stop()
            self.EndModal(wx.ID_CLOSE)

        def _show_sources(self):
            self.source_paths.ChangeValue('\n'.join(str(path) for path in self.sources))

        def choose_files(self, event=None):
            if not self._usable() or self.current_job is not None:
                return
            with wx.FileDialog(self, 'Choose local audio or video files', wildcard=MEDIA_FILTER,
                    style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as picker:
                if picker.ShowModal() == wx.ID_OK:
                    self.sources = tuple(dict.fromkeys((*self.sources, *(Path(path) for path in picker.GetPaths()))))
                    self._show_sources()

        def choose_folder(self, event=None):
            if not self._usable() or self.current_job is not None:
                return
            with wx.DirDialog(self, 'Choose a folder containing local audio or video',
                    style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as picker:
                if picker.ShowModal() == wx.ID_OK:
                    self.sources = tuple(dict.fromkeys((*self.sources, Path(picker.GetPath()))))
                    self._show_sources()

        def clear_sources(self, event=None):
            if self._usable() and self.current_job is None:
                self.sources = ()
                self._show_sources()

        def choose_runtime_path(self, control, directory):
            if not self._usable():
                return
            picker = (wx.DirDialog(self, 'Choose the existing local model directory',
                        style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) if directory else
                      wx.FileDialog(self, 'Choose the reviewed local executable or worker file',
                        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST))
            try:
                if picker.ShowModal() == wx.ID_OK:
                    control.SetValue(picker.GetPath())
            finally:
                picker.Destroy()

        def choose_export(self, event=None):
            if not self._usable() or self.current_job is None:
                self.report('Load a saved job before exporting.', focus=True)
                return
            with wx.DirDialog(self, 'Choose where to create a new transcription export folder',
                    style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as picker:
                if picker.ShowModal() == wx.ID_OK:
                    target = Path(picker.GetPath()) / ('transcription-' + uuid4().hex[:12])
                    self.export_to(target)

        def read_aloud(self, event=None):
            if not self._usable() or self.document is None:
                return
            from .speech_dialog import show_speech_reader
            show_speech_reader(self, text=self.editor.GetValue(), title='Unverified transcription reading copy',
                source_guard=self._check_root, recovery_slot='transcription-' + self.current_job['id'])

        def help(self, event=None):
            with wx.Dialog(self, title='Transcription help', size=(750, 620),
                    style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER) as dialog:
                box = wx.BoxSizer(wx.VERTICAL)
                text = wx.TextCtrl(dialog, value=HELP, style=wx.TE_MULTILINE | wx.TE_READONLY)
                text.SetName('Local transcription help')
                box.Add(text, 1, wx.EXPAND | wx.ALL, 10)
                box.Add(dialog.CreateButtonSizer(wx.OK), 0, wx.ALIGN_RIGHT | wx.ALL, 8)
                dialog.SetSizer(box)
                text.SetFocus()
                dialog.ShowModal()

    return Dialog()


def show_transcription_dialog(frame, controller=None):
    import wx
    dialog = build_transcription_dialog(frame, controller)
    try:
        wx.CallAfter(dialog.refresh)
        dialog.ShowModal()
    finally:
        dialog.closed = True
        dialog.cancel_event.set()
        dialog.timer.Stop()
        if not dialog._destroyed:
            dialog.Destroy()


def show_transcription_router(frame, *, protocol=None):
    """One installed entry point; choosing a protocol never reinterprets a job."""
    import wx
    if getattr(frame, '_closing', False) or getattr(frame, 'working', False):
        frame.report('Finish or cancel the current operation before opening transcription.')
        return
    choices = (
        ('carr-transcription-receipts/1', 'Library workspace — strict receipts and optional word timestamps'),
        ('local-file-job/1', 'Reviewed local runtime profile — file checkpoints, pause and resume'),
    )
    if protocol is None:
        with wx.SingleChoiceDialog(frame,
                'Choose the protocol supported by the explicitly reviewed local worker. '
                'Existing jobs remain in their original store; no automatic fallback is used.',
                'Local transcription protocol', [label for _, label in choices]) as picker:
            picker.SetName('Local transcription protocol')
            picker.SetSelection(0)
            if picker.ShowModal() != wx.ID_OK:
                return
            index = picker.GetSelection()
            if not 0 <= index < len(choices):
                raise ValueError('Choose one explicitly supported transcription protocol.')
            protocol = choices[index][0]
    if getattr(frame, '_closing', False) or getattr(frame, 'working', False):
        return
    if protocol == 'carr-transcription-receipts/1':
        return show_transcription(frame)
    if protocol == 'local-file-job/1':
        controller = getattr(frame, 'transcription_controller', None)
        if controller is None or controller.private_root != Path(frame.library.root).absolute():
            controller = TranscriptionController(frame.library.root)
            frame.transcription_controller = controller
        return show_transcription_dialog(frame, controller)
    raise ValueError('Unsupported transcription protocol; no alternative decoder was selected.')


def install_transcription_menu(frame, menu):
    """The host's existing menu capture supplies keyboard preferences/palette."""
    import wx
    existing = getattr(frame, '_transcription_open', None)
    if existing is not None:
        return existing
    def open_dialog(event=None):
        try:
            show_transcription_router(frame)
        except (ValueError, OSError, RuntimeError) as exc:
            frame.report('Transcription could not open: ' + str(exc))
    item = menu.Append(wx.ID_ANY, 'Transcription: local audio and video...')
    frame.Bind(wx.EVT_MENU, open_dialog, item)
    frame._transcription_open = open_dialog
    return open_dialog


def install_transcription_actions(frame):
    # Keep the existing host menu path, so its current shortcut bindings retain
    # their identity. There is no second registry or second installed command.
    return install_transcription_menu(frame, frame.menu_bar.GetMenu(0))
