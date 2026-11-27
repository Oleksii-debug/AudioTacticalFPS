"""Keyboard-accessible native workspace for the supplied local Whisper worker."""
from pathlib import Path
import threading


def show_transcription(frame):
    import wx
    from .save_all import SaveResult, coordinator_for

    if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
        frame.report('Finish or cancel the current operation before opening transcription.')
        return
    active = getattr(frame, 'active', None)
    if active is None:
        frame.report('Select an active library before opening transcription.')
        return
    bound_library, bound_workspace = frame.library, active[0]
    bound_root = bound_library.root
    service = bound_library.transcription(bound_workspace)
    coordinator = coordinator_for(frame)
    dialog = wx.Dialog(frame, title='Транскрипція — локальний Whisper', size=(900, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    current, selected_source, alive, busy, dirty = None, None, True, False, False
    selected_ready = False
    saved_text, pending_save = None, None
    rows, source_rows = [], []
    cancel = threading.Event()
    def current_scope():
        selection = getattr(frame, 'active', None)
        return (alive and not getattr(frame, '_closing', False)
                and getattr(frame, 'library', None) is bound_library
                and bound_library.root == bound_root
                and selection is not None and selection[0] == bound_workspace)
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
    row = wx.GridSizer(rows=0, cols=3, vgap=3, hgap=3)
    for name, label in [('configure', 'Підключити &Whisper'), ('create', '&Додати записи'),
                        ('folder', 'Додати пап&ку записів'),
                        ('run', 'Запустити / &продовжити'), ('stop', 'З&упинити'),
                        ('save', 'Зберегти &правки'), ('saveall', 'Зберегти &всі відкриті правки'),
                        ('export', '&Експорт'),
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
    def unsaved():
        return (alive and selected_ready
                and (not current_scope() or text.GetValue() != saved_text))
    def controls():
        nonlocal dirty
        if not alive:
            return
        dirty = unsaved()
        for control in (jobs, title, model, language, device, words, mixed, sources, text):
            control.Enable(not busy)
        for name, button in buttons.items():
            button.Enable(not busy and (name not in ('run', 'export', 'save') or current is not None))
        buttons['stop'].Enable(busy)
        text.Enable(not busy and selected_ready)
        buttons['save'].Enable(not busy and selected_ready and dirty)
        buttons['saveall'].Enable(not busy and coordinator is not None)
    def can_replace():
        nonlocal dirty
        if not unsaved():
            return True
        discard = wx.MessageBox('Відкинути незбережені правки поточного тексту? '
                                'Щоб зберегти їх, оберіть «Ні», потім «Зберегти правки».',
                                'Незбережені правки', wx.YES_NO | wx.NO_DEFAULT, dialog)
        if discard == wx.YES:
            text.ChangeValue(saved_text if saved_text is not None else '')
            dirty = False
            return True
        text.SetFocus()
        return False
    def load_source():
        nonlocal selected_source, selected_ready, dirty, saved_text
        index = sources.GetSelection()
        selected_source = source_rows[index]['filename'] if 0 <= index < len(source_rows) else None
        selected_ready = False
        saved_text = None
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
                    nonlocal selected_ready, saved_text
                    if current == job_id and selected_source == source_id:
                        text.ChangeValue(value)
                        saved_text = value
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
    def begin(label, operation, complete, *, allow_dirty=False, with_progress=False, completed=None):
        nonlocal busy
        finished = False
        def settle(saved, message=''):
            nonlocal finished
            if finished:
                return
            finished = True
            if completed is not None:
                completed(SaveResult(saved, str(message)))
        if not current_scope() or busy or (not allow_dirty and not can_replace()):
            settle(False, 'Вікно закрите або інше завдання ще працює; правки не підтверджено.')
            return False
        if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            message = 'Спочатку завершіть поточну операцію програми.'
            try:
                report(message)
            finally:
                settle(False, message)
            return False
        busy = True
        cancel.clear()
        def done(result):
            nonlocal busy
            if finished:
                return
            busy = False
            if not current_scope():
                settle(False, 'Вікно або бібліотека змінилися; стан правок не підтверджено.')
                return
            try:
                controls()
                complete(result)
            except Exception as exc:
                try:
                    report(exc)
                finally:
                    settle(False, exc)
            else:
                settle(True)
        def failed(error):
            nonlocal busy
            if finished:
                return
            busy = False
            try:
                if current_scope():
                    controls()
                    report(error)
            finally:
                settle(False, error)
        try:
            controls()
            accepted = frame.run_job(label, operation, done=done, failed=failed,
                                     with_progress=with_progress)
        except Exception as exc:
            failed(exc)
            return False
        if accepted is False or (not finished and accepted is None
                                  and not getattr(frame, 'working', False)):
            failed(RuntimeError('Фонову операцію не розпочато; правки збережено у вікні.'))
            return False
        return True
    def cancelled():
        return cancel.is_set() or frame.cancel_event.is_set() or not current_scope()
    def configure(event):
        try:
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
            begin('Підключення локального Whisper', lambda: service.configure(python, worker, models_dir),
                  lambda value: report('Рушій підключено. Якість і доступність моделі перевіряються під час фактичного розпізнавання.'))
        except (ValueError, OSError, RuntimeError) as exc:
            report(exc)
    def create(event, *, folder=False):
        if not can_replace():
            return
        try:
            if folder:
                with wx.DirDialog(dialog, 'Аудіо й відео лише з цієї папки: 1–64 файли, без підпапок',
                                  style=wx.DD_DIR_MUST_EXIST) as chooser:
                    if chooser.ShowModal() != wx.ID_OK:
                        return
                    selected = Path(chooser.GetPath())
            else:
                with wx.FileDialog(dialog, 'Оберіть аудіо або відео', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as chooser:
                    if chooser.ShowModal() != wx.ID_OK:
                        return
                    selected = tuple(chooser.GetPaths())
            options = {'model': model.GetValue().strip(), 'language': language.GetValue().strip(),
                       'word_timestamps': words.GetValue(), 'multilingual': mixed.GetValue(),
                       'device': 'cuda' if device.GetSelection() == 1 else 'cpu',
                       'compute_type': 'float16' if device.GetSelection() == 1 else 'int8'}
            job_title = title.GetValue()
            submit = service.submit_folder if folder else service.submit
            begin('Збереження оригінальних записів',
                  lambda progress: submit(selected, title=job_title, options=options,
                                          cancelled=cancelled, progress=progress),
                  lambda identity: refresh(identity), with_progress=True)
        except (ValueError, OSError, RuntimeError) as exc:
            report(exc)
    def run(event):
        identity = current
        if identity is None:
            report('Спочатку додайте записи або оберіть збережене завдання.')
            return
        def operation(report_progress):
            def advanced(done, total, phase):
                report_progress(done, total, phase)
                if done is None or total is None:
                    message = ('Розпізнавання триває. Поточна кількість оброблених файлів невідома; '
                               'результати буде перевірено після завершення рушія.')
                else:
                    message = f'{done}/{total}: {phase}. Кількість файлів повідомляє рушій; '
                    message += 'підтверджені результати показані у стані завдання.'
                wx.CallAfter(report, message)
            return service.run(identity, cancelled=cancelled, progress=advanced)
        begin('Локальна транскрипція Whisper', operation, lambda result: refresh(identity),
              with_progress=True)
    def save(event=None, *, completed=None):
        nonlocal pending_save
        if not current_scope() or busy or pending_save is not None:
            if completed is not None:
                completed(SaveResult(False, 'Бібліотека змінилася, вікно закрите або транскрипція ще працює.'))
            return False
        if current is None or selected_source is None or not selected_ready:
            if completed is not None:
                completed(SaveResult(False, 'Оберіть перевірений текст для збереження правок.'))
            return False
        if not unsaved():
            if completed is not None:
                completed(SaveResult(True))
            return True
        identity, source_id, value = current, selected_source, text.GetValue()
        replied = False
        def finish(result):
            nonlocal replied, pending_save
            if replied:
                return
            replied = True
            if pending_save is finish:
                pending_save = None
            if completed is not None:
                completed(result)
        def saved(revision):
            nonlocal dirty, saved_text
            if (not current_scope() or current != identity
                    or selected_source != source_id or not selected_ready):
                raise ValueError('Відкритий запис змінився під час збереження; перевірте його правки.')
            saved_text = value
            dirty = unsaved()
            controls()
            report('Правки збережено окремою версією: ' + revision +
                   '. Початкове розпізнавання та аудіо залишилися незмінними.' +
                   (' Новіші незбережені правки залишилися у вікні.' if dirty else ''))
        pending_save = finish
        return begin('Збереження правок транскрипції',
              lambda: service.save_edit(identity, source_id, value, cancelled=cancelled),
              saved, allow_dirty=True, completed=finish)
    def save_all(event=None):
        if not alive or coordinator is None:
            return False
        if not current_scope():
            report('Бібліотека змінилася; відкрийте транскрипцію знову перед збереженням.')
            return False
        if busy:
            report('Зупиніть поточне завдання перед збереженням усіх відкритих правок.')
            return False
        if not coordinator.save_all(lambda summary: report(summary.message()) if current_scope() else None,
                                    cancelled=lambda: not alive):
            report('Збереження всіх правок ще очікує підтвердження попереднього запису.')
            return False
        return True
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
        dirty = unsaved()
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
    for name, handler in [('configure', configure), ('create', create),
                          ('folder', lambda event: create(event, folder=True)), ('run', run), ('save', save),
                          ('saveall', save_all), ('export', export), ('refresh', update_jobs), ('close', close)]:
        buttons[name].Bind(wx.EVT_BUTTON, handler)
    buttons['stop'].Bind(wx.EVT_BUTTON, lambda event: cancel.set())
    jobs.Bind(wx.EVT_CHOICE, select_job)
    sources.Bind(wx.EVT_CHOICE, select_source)
    text.Bind(wx.EVT_TEXT, changed)
    dialog.Bind(wx.EVT_CLOSE, close)
    unregister = (coordinator.register('transcription.' + str(id(dialog)), 'Transcript edit',
                    dirty=unsaved, save=lambda completed: save(completed=completed), focus=text.SetFocus)
                  if coordinator is not None else lambda: None)
    try:
        refresh()
        controls()
        if not busy:
            jobs.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        unregister()
        try:
            if pending_save is not None:
                pending_save(SaveResult(False, 'Вікно закрите до підтвердження збереження правок.'))
        finally:
            dialog.Destroy()


def install_transcription_actions(frame):
    import wx
    index = frame.menu_bar.FindMenu('Library')
    if index == wx.NOT_FOUND:
        raise ValueError('The Library menu is unavailable.')
    menu = frame.menu_bar.GetMenu(index)
    item = menu.Append(wx.ID_ANY, 'Transcription: local audio and video...')
    def open_dialog(event):
        try:
            show_transcription(frame)
        except (ValueError, OSError, RuntimeError) as exc:
            frame.report(str(exc))
    frame.Bind(wx.EVT_MENU, open_dialog, item)
