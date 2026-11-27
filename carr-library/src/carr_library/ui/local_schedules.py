"""Accessible, explicit local schedules on the existing desktop worker/timer."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..local_schedules import RuleView, ScheduleSession, ScheduleStore, directory_identity

COMMAND_ID = 'library.local_schedules'


def _text(parent, title, message, value=''):
    import wx
    dialog = wx.TextEntryDialog(parent, message, title, value)
    dialog.SetName(title)
    try:
        return dialog.GetValue() if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def _directory(parent, title):
    import wx
    dialog = wx.DirDialog(parent, title, style=wx.DD_DIR_MUST_EXIST)
    dialog.SetName(title)
    try:
        return Path(dialog.GetPath()) if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def _confirm(parent, title, message):
    import wx
    dialog = wx.MessageDialog(parent, message, title, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
    try:
        return dialog.ShowModal() == wx.ID_YES
    finally:
        dialog.Destroy()


def _future_time():
    return (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec='seconds')


def review_text(view, *, armed=False):
    row = view.record
    lines = [row['title'], 'Armed in this window.' if armed else 'Saved only; not armed.',
        'Operation: ' + ('Complete library backup' if row['kind'] == 'backup' else 'Watched folder import'),
        'Library folder: ' + row['data_dir'], 'Selected folder: ' + row['target'],
        'First or next UTC start: ' + row['next_due'],
        'Repeat interval: ' + str(row['interval_seconds']) + ' seconds',
        'Start window: ' + str(row['lateness_seconds']) + ' seconds',
        'Rule SHA256: ' + view.sha256]
    if row['kind'] == 'watch':
        lines += ['Target library ID: ' + row['workspace'],
                  'Include subfolders: ' + ('yes' if row['recursive'] else 'no'),
                  'Selected collections: ' + (', '.join(row['collections']) or 'none'),
                  'OCR languages: ' + row['config']['languages'],
                  'New supported files in this folder will enter the existing import service. '
                  'Links and rejected inputs stop automatic work. Original bytes are never changed.']
    else:
        lines += ['Current approved parent token: ' + (row['parent'] or 'empty repository'),
                  'Include supported generated audio: ' + ('yes' if row['include_generated'] else 'no'),
                  'The repository must already be initialized. Each successor is a complete verified backup.']
    if row['disabled']:
        lines.append('This saved policy is disabled.')
    if row['last'] is not None:
        last = row['last']
        lines += ['Last attempt (historical): ' + last['status'], last['message']]
        if last['batch_id']:
            lines.append('Existing import batch: ' + last['batch_id'])
        if last['result'] and row['kind'] == 'backup':
            lines += ['Returned snapshot SHA256: ' + last['result']['snapshot_sha256'],
                      'Returned backup manifest SHA256: ' + last['result']['backup_manifest_sha256']]
    lines += ['', 'Saving or restoring this rule never arms it. Arming lasts only while this window stays open.',
              'Missed or unresolved attempts require explicit review; no automatic catch-up or repair occurs.']
    return '\n'.join(lines)


class LocalScheduleController:
    """One host-owned native timer; every file read/job runs on frame.run_job."""
    def __init__(self, frame, *, timer=None):
        self.frame = frame
        self._closed = False
        self._generation = 0
        self.dialog = None
        self.store = None
        self.session = None
        self.library = None
        self.root = None
        self.root_identity = None
        if timer is None:
            import wx
            timer = wx.Timer(frame)
            frame.Bind(wx.EVT_TIMER, self.on_timer, timer)
        self.timer = timer

    def _attach(self, dialog):
        if self._closed or getattr(self.frame, '_closing', False):
            raise RuntimeError('The desktop is closing.')
        self._generation += 1
        generation = self._generation
        self.library = self.frame.library
        self.root, self.root_identity = directory_identity(self.library.root)
        self.dialog = dialog
        self.store = ScheduleStore(self.library)
        self.session = ScheduleSession(self.store, scope_guard=lambda: self.guard(generation))
        self.guard()

    def current(self):
        if (self._closed or self.dialog is None or self.dialog.closed
                or getattr(self.frame, '_closing', False) or self.frame.library is not self.library):
            return False
        try:
            root, identity = directory_identity(self.library.root)
            return root == self.root and identity == self.root_identity
        except (OSError, ValueError):
            return False

    def guard(self, generation=None):
        if generation is not None and generation != self._generation or not self.current():
            raise InterruptedError('The local schedule window or exact library directory changed.')

    def stopped(self, generation=None):
        return (generation is not None and generation != self._generation
                or not self.current() or self.frame.cancel_event.is_set())

    def _queue(self, title, operation, done):
        self.guard()
        dialog, generation = self.dialog, self._generation
        dialog.set_busy(True)
        stop = lambda: self.stopped(generation)
        def work(progress):
            self.guard(generation)
            if stop():
                raise InterruptedError('The selected schedule operation was cancelled before execution.')
            return operation(progress, stop)
        def accepted(value):
            if generation == self._generation and self.current() and self.dialog is dialog:
                dialog.set_busy(False)
                done(value)
        def failed(error):
            if generation == self._generation and self.current() and self.dialog is dialog:
                dialog.set_busy(False)
                dialog.set_status(str(error), focus=True)
        future = self.frame.run_job(title, work, accepted, failed, with_progress=True)
        if future is None and generation == self._generation and self.current() and self.dialog is dialog:
            dialog.set_busy(False)
        return future

    def refresh(self):
        store = self.store
        return self._queue('Reviewing saved local schedules',
            lambda _, stop: store.list(), lambda rows: self.dialog.set_rules(rows))

    def save(self, kind, title, target, **options):
        store = self.store
        def work(_, stop):
            return store.create(kind, title, target, cancelled=stop, **options)
        def done(view):
            self.dialog.put_rule(view)
            self.dialog.set_status('Rule saved; not armed.' if view.durability_confirmed else
                'Rule saved, but disk synchronization is unconfirmed. Keep its ID and review before arming.', focus=True)
        return self._queue('Saving an inactive local schedule', work, done)

    def arm(self, view):
        generation = self._generation
        self.guard(generation)
        if not _confirm(self.dialog, 'Arm this exact local rule',
            review_text(view) + '\n\nAllow these repeated local operations while this window stays open?'):
            return None
        self.guard(generation)
        session = self.session
        def work(_, stop):
            return session.arm(view.id, expected=view.sha256, cancelled=stop)
        def done(identity):
            self.timer.Start(500)
            self.dialog.selection_changed()
            self.dialog.set_status('The reviewed rule is armed in this window. Disarm or close to stop future work.', focus=True)
        return self._queue('Arming the reviewed local schedule', work, done)

    def disarm(self):
        if self.session is not None:
            self.session.disarm()
        self.timer.Stop()
        if self.current():
            self.dialog.selection_changed()
            self.dialog.set_status('This window is disarmed. An active owned operation is stopping; '
                                   'its existing service retains completed work.', focus=True)

    def disable(self, view):
        if self.session is not None:
            self.session.disarm((view.id,))
        store = self.store
        def done(saved):
            self.dialog.put_rule(saved)
            self.dialog.set_status('Saved policy disabled.', focus=True)
        return self._queue('Disabling a saved local rule',
            lambda _, stop: store.disable(view.id, expected=view.sha256), done)

    def review_attention(self, view):
        generation = self._generation
        self.guard(generation)
        if not _confirm(self.dialog, 'Review an unresolved local attempt',
            review_text(view) + '\n\nFirst inspect the existing import batch or independently verify the saved snapshot. '
            'Continuing does not declare the old attempt successful. Save a new start time?'):
            return
        self.guard(generation)
        due = _text(self.dialog, 'New UTC start', 'Complete UTC time for the next reviewed attempt.', _future_time())
        if due is None:
            return
        self.guard(generation)
        parent = None
        if view.record['kind'] == 'backup':
            parent = _text(self.dialog, 'Reviewed current snapshot token',
                'Paste the separately retained current snapshot SHA256; leave empty only for an empty repository.',
                view.record['parent'] or '')
            if parent is None:
                return
            parent = parent.strip() or None
        self.guard(generation)
        def done(saved):
            self.dialog.put_rule(saved)
            self.dialog.set_status('Review saved. The prior result was not reclassified; this rule is still not armed.', focus=True)
        store = self.store
        return self._queue('Saving explicit local schedule review',
            lambda _, stop: store.acknowledge(view.id, expected=view.sha256, next_due=due,
                parent=parent, cancelled=stop), done)

    def on_timer(self, event=None):
        if not self.current():
            self.timer.Stop()
            if self.session is not None:
                self.session.disarm()
            return
        if getattr(self.frame, 'working', False):
            return
        try:
            due = self.session.has_due()
        except (ValueError, RuntimeError, OSError) as error:
            self.disarm()
            if self.current():
                self.dialog.set_status(str(error), focus=True)
            return
        if not due:
            return
        def done(outcome):
            if outcome is None:
                return
            if self.frame.cancel_event.is_set():
                self.disarm()  # Keep returned completion; stop subsequent occurrences.
            if outcome.get('saved_rule'):
                self.dialog.put_rule(RuleView(**outcome['saved_rule']))
            lines = ['Scheduled operation: ' + outcome['status'], outcome.get('message', '')]
            result = outcome.get('result')
            if result is not None and outcome['kind'] == 'backup':
                lines += ['Snapshot SHA256: ' + result['snapshot_sha256'],
                          'Backup manifest SHA256: ' + result['backup_manifest_sha256'],
                          'Keep these checksums separately.']
            elif result is not None and result['batch_id']:
                lines.append('Existing import batch: ' + result['batch_id'])
            self.dialog.selection_changed()
            self.dialog.set_status('\n'.join(filter(None, lines)), focus=True)
            if not self.session.armed:
                self.timer.Stop()
        session = self.session
        return self._queue('Running an explicitly armed local schedule',
                           lambda progress, stop: session.tick(cancelled=stop, progress=progress), done)

    def dismiss(self):
        self._generation += 1
        if self.session is not None:
            self.session.close()
        self.timer.Stop()
        dialog, self.dialog = self.dialog, None
        if dialog is not None:
            dialog.closed = True

    def change_root(self):
        dialog = self.dialog
        self.dismiss()
        self.store = self.session = self.library = None
        if dialog is not None:
            dialog.close_view()

    def close(self):
        # Repeated native Stop remains allowed if a previous close attempt raised.
        self._closed = True
        dialog = self.dialog
        self.dismiss()
        if dialog is not None:
            dialog.close_view()

    def open(self):
        if self._closed or getattr(self.frame, '_closing', False):
            return
        if getattr(self.frame, 'working', False):
            self.frame.report('Wait for the current operation or cancel it before reviewing local schedules.')
            return
        if self.dialog is not None:
            self.dialog.Raise()
            return
        dialog = _dialog_type()(self.frame, self)
        try:
            self._attach(dialog)
            self.refresh()
            dialog.ShowModal()
        except (OSError, ValueError, RuntimeError) as error:
            if not getattr(self.frame, '_closing', False):
                self.frame.report(str(error))
        finally:
            self.dismiss()
            dialog.Destroy()


def _dialog_type():
    import wx
    class LocalSchedulesDialog(wx.Dialog):
        def __init__(self, parent, controller):
            super().__init__(parent, title='Local schedules', size=(840, 700))
            self.SetName('Local schedules')
            self.controller, self.closed = controller, False
            self.views = []
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(self, label='Explicit local backups and watched-folder imports. '
                'Save first, then arm a reviewed rule. Closing this window disarms every rule.'), 0, wx.ALL, 8)
            self.rules = wx.ListBox(self)
            self.rules.SetName('Saved local schedule policies')
            layout.Add(self.rules, 1, wx.EXPAND | wx.ALL, 8)
            self.review = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.review.SetName('Local schedule review')
            layout.Add(self.review, 2, wx.EXPAND | wx.ALL, 8)
            row = wx.WrapSizer()
            self.mutable = []
            for label, handler in (
                ('New backup rule...', lambda event: self.new_rule('backup')),
                ('New watched folder rule...', lambda event: self.new_rule('watch')),
                ('Arm selected rule', self.arm_selected),
                ('Review attention...', self.review_selected),
                ('Disable selected rule', self.disable_selected),
                ('Refresh', lambda event: controller.refresh()),
            ):
                button = wx.Button(self, label=label)
                button.SetName(label)
                button.Bind(wx.EVT_BUTTON, handler)
                self.mutable.append(button)
                row.Add(button, 0, wx.ALL, 4)
            self.stop_button = wx.Button(self, label='Disarm this window')
            self.stop_button.SetName('Disarm this window')
            self.stop_button.Bind(wx.EVT_BUTTON, lambda event: controller.disarm())
            row.Add(self.stop_button, 0, wx.ALL, 4)
            layout.Add(row, 0, wx.EXPAND | wx.ALL, 4)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Local schedule status')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            close = wx.Button(self, wx.ID_CLOSE, 'Close')
            close.SetName('Close local schedules')
            close.Bind(wx.EVT_BUTTON, lambda event: self.close_view())
            layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            self.SetSizer(layout)
            self.rules.Bind(wx.EVT_LISTBOX, lambda event: self.selection_changed())
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)

        def set_busy(self, value):
            for button in self.mutable:
                button.Enable(not value)
            self.rules.Enable(not value)

        def set_status(self, message, *, focus=False):
            if not self.closed:
                self.status.SetValue(message)
                if focus:
                    self.status.SetFocus()

        def set_rules(self, values):
            self.views = list(values)
            self.rules.Set([view.record['title'] for view in self.views])
            if self.views:
                self.rules.SetSelection(0)
            self.selection_changed()

        def put_rule(self, view):
            values = [row for row in self.views if row.id != view.id] + [view]
            values.sort(key=lambda row: (row.record['next_due'], row.id))
            self.set_rules(values)
            self.rules.SetSelection(next(index for index, row in enumerate(self.views) if row.id == view.id))
            self.selection_changed()

        def selected(self):
            index = self.rules.GetSelection()
            if not 0 <= index < len(self.views):
                raise ValueError('Select one saved rule to review.')
            return self.views[index]

        def selection_changed(self):
            if self.closed:
                return
            try:
                view = self.selected()
                self.review.SetValue(review_text(view, armed=view.id in self.controller.session.armed))
            except ValueError:
                self.review.SetValue('No rule is selected. New rules are saved inactive.')

        def _selected_action(self, function):
            try:
                return function(self.selected())
            except (ValueError, RuntimeError, OSError) as error:
                if self.controller.current():
                    self.set_status(str(error), focus=True)

        def arm_selected(self, event=None):
            return self._selected_action(self.controller.arm)

        def disable_selected(self, event=None):
            return self._selected_action(self.controller.disable)

        def review_selected(self, event=None):
            return self._selected_action(self.controller.review_attention)

        def new_rule(self, kind):
            generation = self.controller._generation
            try:
                self.controller.guard(generation)
                target = _directory(self, 'Choose an initialized backup repository' if kind == 'backup'
                    else 'Choose the exact local folder to watch')
                if target is None:
                    return
                self.controller.guard(generation)
                title = _text(self, 'Local rule title', 'Name for this explicit local rule.',
                              'Library backup' if kind == 'backup' else 'Watched source folder')
                if title is None:
                    return
                self.controller.guard(generation)
                interval = _text(self, 'Repeat interval in minutes',
                    'From 1 to 10080 minutes. No catch-up or automatic retry after a failure.', '60' if kind == 'backup' else '5')
                if interval is None:
                    return
                self.controller.guard(generation)
                due = _text(self, 'First UTC start', 'Complete UTC timestamp; saving does not arm the rule.', _future_time())
                if due is None:
                    return
                options = dict(first_due=due, interval_seconds=int(interval) * 60, lateness_seconds=3600)
                self.controller.guard(generation)
                if kind == 'backup':
                    parent = _text(self, 'Current snapshot token',
                        'Paste the separately retained current snapshot SHA256. Leave empty only for an empty initialized repository.')
                    if parent is None:
                        return
                    options.update(parent=parent.strip() or None, include_generated=False)
                else:
                    workspace = self.controller.frame._workspace()
                    recursive = _confirm(self, 'Watch subfolders', 'Include supported files in subfolders of this exact selected folder?')
                    self.controller.guard(generation)
                    language = _text(self, 'Watched-folder OCR languages',
                        'Existing installed OCR language IDs, joined by +. No model is downloaded.', 'eng')
                    if language is None:
                        return
                    from ..extract.page_ocr import Config
                    options.update(workspace=workspace, recursive=recursive, config=Config(languages=language))
                self.controller.guard(generation)
                return self.controller.save(kind, title, target, **options)
            except (ValueError, RuntimeError, OSError) as error:
                if self.controller.current():
                    self.set_status(str(error), focus=True)

        def close_view(self):
            self.controller.dismiss()
            if self.IsModal():
                self.EndModal(wx.ID_CLOSE)
            else:
                self.Hide()

        def on_close(self, event):
            self.close_view()

        def destroyed(self, event):
            if event.GetEventObject() is self:
                self.controller.dismiss()
                self.closed = True
            event.Skip()

    return LocalSchedulesDialog


def install_local_schedules(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    controller = LocalScheduleController(frame)
    index = frame.menu_bar.FindMenu('Library')
    if index == wx.NOT_FOUND:
        raise ValueError('Local schedules require the existing Library menu.')
    menu = frame.menu_bar.GetMenu(index)
    item = menu.Append(wx.ID_ANY, 'Local backup and folder schedules...')
    frame.Bind(wx.EVT_MENU, lambda event: controller.open(), item)
    register_menu_command(frame, CommandSpec(COMMAND_ID, 'Local backup and folder schedules', 'main', ''), item)
    return controller
