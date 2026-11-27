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
            originals = {item['filename']: item['original_name'] for item in job['sources']}
            actual = '\n'.join(
                f"{originals.get(item['source'], item['source'])}: {item['attempt']}; "
                f"beam={item['settings']['beam_size']}, VAD={item['settings']['vad_filter']}, "
                f"previous_text={item['settings']['condition_on_previous_text']}"
                for item in job.get('effective_runs', ()))
            report(f"{job['title']}: {job['status']}; {job['done']}/{len(job['sources'])}.\n" +
                   f"Профіль: {job['protocol']}; межа спроб: {job['settings']['max_attempts']}.\n" +
                   job.get('last_error', '') + ('\nФактичні параметри:\n' + actual if actual else '') +
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
        task = None
        busy = True
        cancel.clear()
        controls()
        def done(result):
            nonlocal busy
            busy = False
            controls()
            if alive:
                try:
                    complete(result)
                except (ValueError, OSError, RuntimeError) as exc:
                    report(exc)
        def failed(error):
            nonlocal busy
            from ..transcription_host import fail_progress
            fail_progress(task, error)
            busy = False
            controls()
            report(error)
        from ..progress import ProgressHub
        from ..transcription_host import observe_operation
        hub = getattr(frame, 'progress_hub', None)
        if hub is None:
            frame.progress_hub = hub = ProgressHub()
        task = hub.start(label, unit='files')
        try:
            accepted = frame.run_job(label, lambda: observe_operation(task, operation),
                                     done=done, failed=failed)
        except Exception as error:
            failed(error)
            return False
        if accepted is False:
            if busy:
                failed(RuntimeError('The transcription operation did not start.'))
            return False
        # This host returns None on both accepted work and a synchronous submit
        # failure. The actual callback owns busy/terminal state.
        return busy
    def cancelled():
        return cancel.is_set() or frame.cancel_event.is_set()
    def configure(event):
        try:
            from ..transcription import PROTOCOLS
            with wx.SingleChoiceDialog(dialog,
                    'Оберіть контракт саме встановленого worker.py. V5 потребує також local_models.py і transcript_features.py; '
                    'виконує до трьох спроб зі збереженням мови. Старі завдання зберігають свій профіль.',
                    'Профіль локального Whisper',
                    ['receipts-v1 — worker із квитанціями результатів',
                     'whisper-v5-manifest-v1 — виправлений приватний V5']) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                protocol = PROTOCOLS[chooser.GetSelection()]
            with wx.FileDialog(dialog, 'Оберіть Python локального Whisper', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                python = chooser.GetPath()
            with wx.FileDialog(dialog, 'Оберіть перевірений worker.py', wildcard='Python (*.py)|*.py',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                worker = chooser.GetPath()
            with wx.DirDialog(dialog, 'Оберіть папку вже встановлених моделей', style=wx.DD_DIR_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                models_dir = chooser.GetPath()
            begin('Підключення локального Whisper', lambda: service.configure(python, worker, models_dir, protocol=protocol),
                  lambda value: report('Профіль підключено: ' + value['protocol'] + '. ' + value.get('retry_policy', '') +
                      ' Якість моделі ще не підтверджена фактичним розпізнаванням.'))
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
        def operation():
            from ..progress import current_progress
            progress = current_progress()
            def advanced(done, total, phase):
                if progress is not None:
                    from ..transcription_host import report_progress
                    report_progress(progress, done, total, phase)
                wx.CallAfter(report, f'{done}/{total}: {phase}. Кількість файлів повідомляє рушій; '
                            'підтверджені результати показані у стані завдання.')
            from ..transcription_host import require_completed
            return require_completed(service.run(identity, cancelled=cancelled, progress=advanced))
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
            from ..backup_files import portable_relative as relative_name
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
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        refresh()
        controls()
        jobs.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        dialog.Destroy()


class TranscriptionActions:
    def __init__(self, frame):
        self.frame = frame

    def open(self, event=None):
        try:
            show_transcription(self.frame)
        except (ValueError, OSError, RuntimeError) as exc:
            self.frame.report(str(exc))


def install_transcription_actions(frame):
    """Register the same semantic command before or after keyboard setup."""
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'transcription_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'file'), None)
    if menu is None:
        raise ValueError('Install the shared File menu before transcription actions.')
    actions = TranscriptionActions(frame)
    item = menu.Append(wx.ID_ANY, 'Transcription: local audio and video...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    register_menu_command(frame,
        CommandSpec('transcription.local', 'Transcription: local audio and video', 'main'), item)
    frame.transcription_actions = actions
    return actions
