"""Native review/run UI for the bounded local library workflow adapter.

This module does not own the audio production director or a language model.
Controls are native, named and keyboard reachable; actual Windows/NVDA speech
acceptance remains a separate test from the headless controller regressions.
"""
from ..library_assistant import LocalLibraryAssistant, describe_job, describe_plan


HELP = '''Local library workflows — explicit commands, one per line.

search: railway history
пошук: історія
import: book.txt
імпорт: підпапка/книга.txt
topic: Literature review
тема: Огляд літератури
export: topic-id -> new-file.md
export: @1 -> new-topic.md
topics
теми
backup: new-backup-folder

@1 means the topic created by step 2 (step references start at zero).
Input and output paths stay inside the folders you choose. Existing output is
never overwritten. TXT import preserves exact UTF-8 text; use Import Center for
PDF, DOCX and EPUB. Importing a document never executes instructions inside it.

Backup contains ALL libraries and original documents in this data folder, so it
requires the separate all-library backup checkbox. It saves a new folder with a
snapshot subfolder. A running backup finishes its atomic snapshot before pause.

Preview performs no library changes. Read the complete plan, then choose Approve
and run. Pause preserves completed steps. Select a saved job, choose Load for
review, then Approve and run to resume. Failed jobs retain their checkpoints.

This is a deterministic local workflow adapter. It does not understand arbitrary
natural language, invoke remote APIs, execute shell commands or produce audio.
JSON plans can use search, list_topics, create_topic, export_topic, import_text,
and backup. The running guide documents their exact arguments.
'''


class LibraryWorkflowController:
    def __init__(self, frame, view):
        self.frame, self.view = frame, view
        self.library, self.scope = frame.library, tuple(frame.active)
        self.root = self.library.root
        self.service = self.plan = self.job_id = None
        self.running = False
        self.alive = True
        self.closing = False

    def _current(self):
        return (self.alive and self.frame.library is self.library and self.library.root == self.root
                and self.frame.active == self.scope and not getattr(self.frame, '_closing', False))

    def _ready(self):
        if not self._current():
            raise ValueError('The library or topic changed. Reopen the library workflow assistant.')
        if self.running or self.frame.working or getattr(self.frame, 'busy', False):
            raise ValueError('An operation is running. Wait or pause it first.')

    def _cancelled(self):
        return self.frame.cancel_event.is_set() or not self._current()

    def changed(self):
        if self.running:
            self.frame.cancel_event.set()
        self.plan = self.job_id = None
        self.view.enable_run(False)

    def _job(self, title, operation, complete, *, accept_cancelled_result=False):
        self._ready()
        self.running = True
        self.view.set_busy(True)
        self.view.report(title + '…', focus=True)

        def finish():
            self.running = False
            if self.alive:
                self.view.set_busy(False)
                if self.closing:
                    self.alive = False
                    self.view.finish_close()

        def done(value):
            finish()
            if not self.alive:
                return
            if not self._current():
                self.changed()
                self.view.report('Library scope changed. Reopen this dialog before continuing.', focus=True)
                return
            if self.frame.cancel_event.is_set() and not accept_cancelled_result:
                self.view.report('Operation cancelled. Preview or load the plan again when ready.', focus=True)
                return
            try:
                complete(value)
            except Exception as exc:
                failed(exc)

        def failed(error):
            finish()
            if self.alive:
                self.view.report('Workflow not completed: ' + str(error), focus=True)

        def work():
            if self._cancelled():
                raise InterruptedError('The selected scope changed or the operation was cancelled.')
            return operation()

        try:
            self.frame.run_job(title, work, done, failed)
        except Exception as exc:
            failed(exc)

    def preview(self, goal, input_root=None, output_root=None, allow_full_backup=False):
        try:
            self._ready()
            self.changed()
            def work():
                service = LocalLibraryAssistant(self.library, self.scope[0], input_root=input_root,
                    output_root=output_root, allow_full_backup=allow_full_backup)
                return service, service.plan(goal)
            def complete(value):
                self.service, self.plan = value
                self.view.show_plan(describe_plan(self.plan))
                self.view.enable_run(True)
                self.view.report('Plan ready for review. Choose Approve and run to execute these steps.', focus=True)
            self._job('Preparing local library plan', work, complete)
        except Exception as exc:
            self.view.report(str(exc), focus=True)

    def approve_run(self):
        try:
            self._ready()
            if self.service is None or self.plan is None:
                raise ValueError('Preview or load the exact plan before approving it.')
            service, plan, identity = self.service, self.plan, self.job_id
            def work():
                nonlocal identity
                if identity is None:
                    identity = service.start(plan, approved=True)['id']
                    self.job_id = identity
                return service.run(identity, cancelled=self._cancelled,
                                   progress=self.view.progress)
            def complete(job):
                self.job_id = job['id']
                self.view.show_result(describe_job(job))
                self.view.enable_run(job['state'] != 'complete')
                self.view.report('Workflow ' + job['state'] + '. ' + (job['error'] or
                    'Every completed step has a saved result.'), focus=True)
            self._job('Running reviewed library workflow', work, complete, accept_cancelled_result=True)
        except Exception as exc:
            self.view.report(str(exc), focus=True)

    def refresh_jobs(self):
        try:
            self._job('Loading saved library workflows',
                lambda: LocalLibraryAssistant(self.library, self.scope[0]).jobs(),
                self.view.show_jobs)
        except Exception as exc:
            self.view.report(str(exc), focus=True)

    def load_job(self, identity):
        try:
            self._ready()
            self.changed()
            def work():
                service = LocalLibraryAssistant.open_job(self.library, self.scope[0], identity)
                job = service.load(identity)
                return service, job, service._restore_plan(job['plan'], checkpoints=job['steps'])
            def complete(value):
                self.service, job, self.plan = value
                self.job_id = job['id']
                self.view.show_configuration(self.plan)
                self.view.show_plan(describe_plan(self.plan))
                self.view.show_result(describe_job(job))
                self.view.enable_run(job['state'] != 'complete')
                self.view.report('Saved plan loaded. Review its folders and remaining steps before resuming.', focus=True)
            self._job('Loading saved plan for review', work, complete)
        except Exception as exc:
            self.view.report(str(exc), focus=True)

    def pause(self):
        if self.running:
            self.frame.cancel_event.set()
            self.view.report('Pause requested. The current operation will stop at its next safe checkpoint.', focus=True)

    def close(self):
        if self.running:
            self.closing = True
            self.pause()
            return False
        self.alive = False
        return True


class NativeLibraryWorkflow:
    def __init__(self, frame):
        import wx
        self.wx = wx
        self.dialog = dialog = wx.Dialog(frame, title='Local library workflow assistant', size=(850, 800))
        self.controller = LibraryWorkflowController(frame, self)
        self.job_ids = []
        self.folder_buttons = []
        layout = wx.BoxSizer(wx.VERTICAL)
        heading = wx.StaticText(dialog, label='LOCAL ONLY — review a deterministic library plan before running it.')
        layout.Add(heading, 0, wx.ALL, 8)
        self.input_root = self._folder(layout, 'Allowed input folder', 'Choose input folder')
        self.output_root = self._folder(layout, 'Allowed output folder', 'Choose output folder')
        self.backup_permission = wx.CheckBox(dialog, label='Allow complete backups of ALL libraries and original documents')
        self.backup_permission.SetName('Allow complete backups of all libraries and original documents')
        layout.Add(self.backup_permission, 0, wx.ALL, 8)
        layout.Add(wx.StaticText(dialog, label='Commands or JSON plan'), 0, wx.LEFT | wx.TOP, 8)
        self.goal = wx.TextCtrl(dialog, value='topics', style=wx.TE_MULTILINE)
        self.goal.SetName('Commands or JSON library plan')
        layout.Add(self.goal, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.preview = wx.Button(dialog, label='&Preview plan')
        self.run = wx.Button(dialog, label='&Approve and run')
        self.pause_button = wx.Button(dialog, label='&Pause workflow')
        self.help_button = wx.Button(dialog, label='Command &help')
        for button in (self.preview, self.run, self.pause_button, self.help_button):
            buttons.Add(button, 0, wx.RIGHT, 8)
        layout.Add(buttons, 0, wx.LEFT | wx.RIGHT, 8)
        self.run.Disable()
        self.pause_button.Disable()
        layout.Add(wx.StaticText(dialog, label='Saved workflows in this library'), 0, wx.LEFT | wx.TOP, 8)
        saved = wx.BoxSizer(wx.HORIZONTAL)
        self.saved = wx.Choice(dialog, choices=[])
        self.saved.SetName('Saved workflows in the selected library')
        self.refresh = wx.Button(dialog, label='&Refresh saved jobs')
        self.load_button = wx.Button(dialog, label='&Load for review')
        saved.Add(self.saved, 1, wx.RIGHT, 8)
        saved.Add(self.refresh, 0, wx.RIGHT, 8)
        saved.Add(self.load_button, 0)
        layout.Add(saved, 0, wx.EXPAND | wx.ALL, 8)
        self.review = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.review.SetName('Reviewed plan and permitted folders')
        layout.Add(wx.StaticText(dialog, label='Plan review'), 0, wx.LEFT, 8)
        layout.Add(self.review, 1, wx.EXPAND | wx.ALL, 8)
        self.results = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.results.SetName('Saved step results and recovery status')
        layout.Add(wx.StaticText(dialog, label='Step results'), 0, wx.LEFT, 8)
        layout.Add(self.results, 1, wx.EXPAND | wx.ALL, 8)
        self.status = wx.TextCtrl(dialog, value='Choose folders if needed, enter commands, then preview the plan.',
                                   style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Library workflow status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self.close_button = wx.Button(dialog, wx.ID_CANCEL, '&Close')
        layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        dialog.SetSizer(layout)
        dialog.SetEscapeId(wx.ID_CANCEL)
        self.edit_controls = (self.input_root, self.output_root, self.goal, self.backup_permission,
                              self.preview, self.saved, self.refresh, self.load_button, *self.folder_buttons)
        for control in (self.input_root, self.output_root, self.goal):
            control.Bind(wx.EVT_TEXT, lambda event: self.controller.changed())
        self.backup_permission.Bind(wx.EVT_CHECKBOX, lambda event: self.controller.changed())
        self.preview.Bind(wx.EVT_BUTTON, lambda event: self.controller.preview(self.goal.GetValue(),
            self.input_root.GetValue() or None, self.output_root.GetValue() or None,
            self.backup_permission.GetValue()))
        self.run.Bind(wx.EVT_BUTTON, lambda event: self.controller.approve_run())
        self.pause_button.Bind(wx.EVT_BUTTON, lambda event: self.controller.pause())
        self.refresh.Bind(wx.EVT_BUTTON, lambda event: self.controller.refresh_jobs())
        self.load_button.Bind(wx.EVT_BUTTON, self._load)
        self.help_button.Bind(wx.EVT_BUTTON, self._help)
        self.close_button.Bind(wx.EVT_BUTTON, self._close)
        dialog.Bind(wx.EVT_CLOSE, self._close)

    def _folder(self, layout, label, action):
        wx = self.wx
        layout.Add(wx.StaticText(self.dialog, label=label), 0, wx.LEFT | wx.TOP, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        entry = wx.TextCtrl(self.dialog)
        entry.SetName(label)
        button = wx.Button(self.dialog, label=action)
        self.folder_buttons.append(button)
        row.Add(entry, 1, wx.RIGHT, 8)
        row.Add(button, 0)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        def select(event):
            if self.controller.running:
                return
            dialog = wx.DirDialog(self.dialog, message=action, style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
            try:
                if dialog.ShowModal() == wx.ID_OK:
                    entry.SetValue(dialog.GetPath())
            finally:
                dialog.Destroy()
        button.Bind(wx.EVT_BUTTON, select)
        return entry

    def _load(self, event):
        index = self.saved.GetSelection()
        if not 0 <= index < len(self.job_ids):
            self.report('Select a saved workflow first.', focus=True)
            return
        self.controller.load_job(self.job_ids[index])

    def _help(self, event):
        from .research_actions import NativeDialogs
        NativeDialogs().show(self.dialog, 'Local library workflow commands', HELP)

    def _close(self, event):
        if self.controller.close():
            self.finish_close()
        elif hasattr(event, 'Veto'):
            event.Veto()

    def finish_close(self):
        self.dialog.EndModal(self.wx.ID_CANCEL)

    def show_configuration(self, plan):
        self.input_root.ChangeValue(plan.input_root or '')
        self.output_root.ChangeValue(plan.output_root or '')
        self.backup_permission.SetValue(plan.allow_full_backup)
        self.goal.ChangeValue('Saved job loaded. Its reviewed plan appears below.')

    def show_plan(self, text):
        self.review.SetValue(text)
        self.review.SetInsertionPoint(0)

    def show_result(self, text):
        self.results.SetValue(text)
        self.results.SetInsertionPoint(0)

    def show_jobs(self, rows):
        self.job_ids = [row['id'] for row in rows]
        self.saved.SetItems([f'{row["created_utc"]} — {row["state"]}; '
            f'{row["completed"]}/{row["total"]} steps; {row["id"]}' for row in rows])
        if rows:
            self.saved.SetSelection(0)
            self.saved.SetFocus()
        self.report(f'{len(rows)} saved workflows in the selected library.', focus=not rows)

    def set_busy(self, busy):
        for control in self.edit_controls:
            control.Enable(not busy)
        self.pause_button.Enable(busy)
        if busy:
            self.run.Disable()

    def enable_run(self, enabled):
        self.run.Enable(enabled)

    def report(self, text, *, focus=False):
        self.status.SetValue(text)
        if focus:
            self.status.SetFocus()

    def progress(self, message):
        def update():
            if self.controller.alive:
                self.report(message)
        self.wx.CallAfter(update)

    def show(self):
        try:
            self.goal.SetFocus()
            self.dialog.ShowModal()
        finally:
            self.controller.alive = False
            self.dialog.Destroy()


def show_library_assistant(frame):
    try:
        if frame.active is None:
            raise ValueError('Open a library before starting a local workflow.')
        if frame.working or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
            raise ValueError('Wait for the current operation or cancel it first.')
        NativeLibraryWorkflow(frame).show()
    except Exception as exc:
        frame.report('Library workflow assistant unavailable: ' + str(exc))


def install_library_assistant_actions(frame):
    import wx
    if getattr(frame, 'library_assistant_menu_item', None) is not None:
        return frame.library_assistant_menu_item
    menu = None
    for index in range(frame.menu_bar.GetMenuCount()):
        if frame.menu_bar.GetMenuLabel(index).replace('&', '') == 'Tools':
            menu = frame.menu_bar.GetMenu(index)
            break
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Tools')
    item = menu.Append(wx.ID_ANY, 'Local &library workflow assistant…')
    frame.Bind(wx.EVT_MENU, lambda event: show_library_assistant(frame), item)
    frame.library_assistant_menu_item = item
    return item
