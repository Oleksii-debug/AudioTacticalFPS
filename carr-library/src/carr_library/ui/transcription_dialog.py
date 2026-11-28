"""Keyboard-accessible native workspace for the supplied local Whisper worker."""
from pathlib import Path
import threading


PROFILE_CHOICES = (
    ('receipts-v1', 'Workspace jobs — receipt worker'),
    ('whisper-v5-manifest-v1', 'Workspace jobs — supplied Whisper V5'),
    ('supplied-whisper-v43-hardened-1', 'Library-wide jobs — reviewed runtime, 17 settings'),
)


def choose_profile(frame):
    """Select an existing contract and its explicit scope without migrating jobs."""
    import wx
    with wx.SingleChoiceDialog(frame,
            'Choose the installed worker profile. Workspace jobs and library-wide jobs keep their '
            'own saved settings, originals and result histories.',
            'Local transcription profile and scope', [label for _, label in PROFILE_CHOICES]) as picker:
        picker.SetName('Local transcription profile and scope')
        previous = getattr(frame, '_transcription_profile', PROFILE_CHOICES[0][0])
        ids = tuple(identity for identity, _ in PROFILE_CHOICES)
        picker.SetSelection(ids.index(previous) if previous in ids else 0)
        if picker.ShowModal() != wx.ID_OK:
            return None
        selected = picker.GetSelection()
        if type(selected) is not int or not 0 <= selected < len(ids):
            raise ValueError('Choose a transcription profile and scope.')
        frame._transcription_profile = ids[selected]
        return ids[selected]


def show_transcription(frame, *, protocol=None, service=None, on_ready=None):
    import wx
    from .save_all import SaveCoordinator, SaveResult, coordinator_for, defer_close
    if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
        frame.report('Finish or cancel the current operation before opening transcription.')
        return
    protocol = choose_profile(frame) if protocol is None else protocol
    if protocol is None:
        return
    if protocol not in tuple(identity for identity, _ in PROFILE_CHOICES):
        raise ValueError('Choose an explicitly supported transcription profile.')
    if protocol == 'supplied-whisper-v43-hardened-1':
        from .transcription_v43_dialog import show_transcription as show_supplied
        return show_supplied(frame, service=service, on_ready=on_ready)
    if service is not None or on_ready is not None:
        raise ValueError('The workspace editor uses its selected library service.')
    active = getattr(frame, 'active', None)
    if active is None:
        frame.report('Select an active library before opening transcription.')
        return
    service = frame.library.transcription(active[0])
    dialog = wx.Dialog(frame, title='Транскрипція — локальний Whisper', size=(900, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    coordinator = coordinator_for(frame) or SaveCoordinator()
    dialog._save_coordinator = coordinator
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
    save_all_button = wx.Button(dialog, label='Зберегти всі відкриті чернетки')
    save_all_button.SetName('Зберегти всі відкриті чернетки')
    layout.Add(save_all_button, 0, wx.EXPAND | wx.ALL, 3)
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
    def begin(label, operation, complete, *, allow_dirty=False, on_failed=None):
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
            try:
                controls()
                if alive:
                    complete(result)
                elif on_failed is not None:
                    on_failed(RuntimeError('The transcription editor closed before save completion.'))
            except (ValueError, OSError, RuntimeError) as exc:
                try:
                    report(exc)
                finally:
                    if on_failed is not None:
                        on_failed(exc)
        def failed(error):
            nonlocal busy
            busy = False
            try:
                controls()
                report(error)
            finally:
                if on_failed is not None:
                    on_failed(error)
        # The desktop runner binds one shared reporter. The service's existing
        # current_progress callback updates it; this dialog creates no second task.
        try:
            accepted = frame.run_job(label, operation, done=done, failed=failed,
                                     progress_unit='files')
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
                chooser.SetSelection(PROTOCOLS.index(protocol))
                if chooser.ShowModal() != wx.ID_OK:
                    return
                chosen_protocol = PROTOCOLS[chooser.GetSelection()]
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
            def configured(value):
                nonlocal protocol
                protocol = value['protocol']
                report('Профіль підключено: ' + value['protocol'] + '. ' + value.get('retry_policy', '') +
                       ' Якість моделі ще не підтверджена фактичним розпізнаванням.')
            begin('Підключення локального Whisper',
                  lambda: service.configure(python, worker, models_dir, protocol=chosen_protocol), configured)
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
                  lambda: service.submit_folder(selected, title=job_title, options=options,
                      protocol=protocol, cancelled=cancelled),
                  created)
        else:
            begin('Збереження оригінальних записів',
                  lambda: service.submit(selected, title=job_title, options=options,
                      protocol=protocol, cancelled=cancelled),
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
    def save(event=None, *, completed=None):
        replied = False
        def finish(saved, message=''):
            nonlocal replied
            if replied:
                return
            replied = True
            if completed is not None:
                completed(SaveResult(saved, message))
        if not alive or current is None or selected_source is None or not selected_ready:
            finish(False, 'The transcript is not ready for saving; the draft is retained.')
            return
        if busy or getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            message = 'Finish the current operation before saving transcript edits; the draft is retained.'
            report(message)
            finish(False, message)
            return
        if not dirty:
            finish(True)
            return
        identity, source_id, value = current, selected_source, text.GetValue()
        def saved(revision):
            nonlocal dirty
            same_draft = (alive and current == identity and selected_source == source_id
                          and text.GetValue() == value)
            if same_draft:
                dirty = False
            # A real service revision is durable before this callback. A newer
            # editor value remains dirty and cannot complete Save All or close.
            try:
                controls()
                report('Правки збережено окремою версією: ' + revision +
                       '. Початкове розпізнавання та аудіо залишилися незмінними.' +
                       ('' if same_draft else ' Новіші правки залишаються незбереженими.'))
            finally:
                finish(same_draft, '' if same_draft else 'Newer transcript edits are retained.')
        accepted = begin('Збереження правок транскрипції',
              lambda: service.save_edit(identity, source_id, value, cancelled=cancelled),
              saved, allow_dirty=True, on_failed=lambda error: finish(False, str(error)))
        if not accepted:
            finish(False, 'The transcript save did not start; the draft is retained.')
    def save_all(event=None):
        if not coordinator.save_all(lambda summary: report(summary.message())):
            report('Save All is already waiting for the current saves.')
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
    def close(event=None):
        nonlocal alive
        if defer_close(dialog, coordinator, close=close, report=report,
                       alive=lambda: alive, post=wx.CallAfter, event=event):
            return
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
        unregister_save()
        dialog.EndModal(wx.ID_CLOSE)
    for name, handler in [('configure', configure), ('create', create), ('folder', create_folder),
                          ('run', run), ('save', save),
                          ('export', export), ('refresh', update_jobs), ('close', close)]:
        buttons[name].Bind(wx.EVT_BUTTON, handler)
    buttons['stop'].Bind(wx.EVT_BUTTON, lambda event: cancel.set())
    save_all_button.Bind(wx.EVT_BUTTON, save_all)
    jobs.Bind(wx.EVT_CHOICE, select_job)
    sources.Bind(wx.EVT_CHOICE, select_source)
    text.Bind(wx.EVT_TEXT, changed)
    dialog.Bind(wx.EVT_CLOSE, close)
    unregister_save = coordinator.register(
        'transcription.' + str(id(dialog)), 'Transcript edits',
        dirty=lambda: alive and dirty, save=lambda completed: save(completed=completed),
        focus=text.SetFocus)
    try:
        refresh()
        controls()
        jobs.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        unregister_save()
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
