"""Native, offline-only installation of pinned OCR language and script data.

wx is imported only when a window is created. The action class also supports
headless routing/cancellation tests; those are not Windows/NVDA evidence.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock


PROFILES = (('fast', 'Fast — prioritizes speed'),
            ('best', 'Best — prioritizes recognition accuracy'))
INSTRUCTIONS = (
    'This manager installs recognition data from a folder you choose. It never downloads files.\n\n'
    'Choose Fast or Best to match your offline model files. These are separate official '
    'Tesseract model sets; actual speed and accuracy depend on the document and hardware. '
    'The catalogue shows the exact reviewed upstream revision, size and file identity.\n\n'
    'The selected source folder must contain files such as eng.traineddata and '
    'ukr.traineddata directly, and script models such as script/Latin.traineddata in '
    'a script subfolder. Select the model folder, not an archive. Each selected file '
    'must match this catalogue; files from another revision or profile are refused.\n\n'
    'Use Find models to search names or identifiers. Space checks or clears a model. '
    'Selections survive filtering; the selection summary includes hidden selections. '
    'Fast and Best keep separate selections. Select all shown affects the current '
    'search only; Clear selection clears the whole current profile.\n\n'
    'The size estimate is the selected model data, including already installed models. '
    'Temporary verification/copying may need additional free space. Cancel stops the '
    'operation without activating an incomplete selection. Choose OCR languages '
    'separately when importing; installing a script or orientation model is not '
    'automatic language detection.'
)


def format_bytes(value):
    """Keep an exact count alongside a readable disk-size estimate."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid model size.')
    unit, amount = ('GiB', value / (1024 ** 3)) if value >= 1024 ** 3 else ('MiB', value / (1024 ** 2))
    return f'{amount:.2f} {unit} ({value:,} bytes)'


class OcrModelManagerActions:
    """Bind stable identifiers to native rows; publish only verified results."""

    def configure(self, store, catalogue, executor, call_after):
        self.store, self.catalogue = store, catalogue
        self.executor, self.call_after = executor, call_after
        self.pending = self.closed = self.close_when_idle = False
        self.result = None
        self.cancelled = Event()
        self._progress_lock = Lock()
        self._progress_scheduled = False
        self._latest_progress = None
        self._job = 0
        self._profile = 'fast'
        self._selected = {name: set() for name, _ in PROFILES}
        self._all = {}
        self._installed = {}
        self._rows = ()
        self.profile.SetSelection(0)
        self._enable(True)
        self.refresh()

    def report(self, message, *, focus=True):
        if not self.closed:
            self.status.SetValue(message)
            if focus:
                self.status.SetFocus()

    def _enable(self, enabled):
        for control in self.mutable:
            control.Enable(enabled)
        self.cancel_button.Enable(not enabled)

    def _remember(self):
        selected = self._selected[self._profile]
        selected.difference_update(row.identifier for row in self._rows)
        selected.update(row.identifier for i, row in enumerate(self._rows)
                        if self.entries.IsChecked(i))

    def _render(self):
        highlighted = self.entries.GetSelection()
        old_identifier = self._rows[highlighted].identifier if 0 <= highlighted < len(self._rows) else None
        all_rows = self._all[self._profile]
        self._rows = tuple(self.catalogue(self._profile, query=self.search.GetValue()))
        if any(row.identifier not in all_rows for row in self._rows):
            raise ValueError('The model catalogue changed. Reopen the manager.')
        installed = self._installed.get(self._profile)
        labels = []
        for row in self._rows:
            state = ('status unknown' if installed is None else
                     'installed' if row.identifier in installed else 'not installed')
            labels.append(f'{row.label} — {row.identifier} — {row.kind} — {state}')
        self.entries.SetItems(labels)
        for i, row in enumerate(self._rows):
            self.entries.Check(i, row.identifier in self._selected[self._profile])
        index = next((i for i, row in enumerate(self._rows) if row.identifier == old_identifier),
                     0 if self._rows else -1)
        self.entries.SetSelection(index)
        self.selection_changed()
        self.highlight_changed()

    def refresh(self, event=None):
        if self.pending or self.closed:
            return
        try:
            self._remember()
            rows = tuple(self.catalogue(self._profile))
            self._all[self._profile] = {row.identifier: row for row in rows}
            self._selected[self._profile].intersection_update(self._all[self._profile])
            # available() validates a small manifest, not the model contents.
            # Installation itself verifies selected model bytes in the worker.
            self._installed[self._profile] = set(self.store.available(self._profile))
            self._render()
            self.report(f'{len(rows)} official {self._profile} models in the catalogue. '
                        'Check the models to install from your offline folder.', focus=False)
        except Exception as exc:
            self._installed[self._profile] = None
            if self._profile in self._all:
                try:
                    self._render()
                except Exception:
                    self._rows = ()
                    self.entries.SetItems([])
            self.report(f'Model status could not be refreshed: {exc}')

    def profile_changed(self, event=None):
        if self.pending or self.closed:
            return
        index = self.profile.GetSelection()
        if not 0 <= index < len(PROFILES):
            self.report('Choose Fast or Best.')
            return
        self._remember()
        self._profile = PROFILES[index][0]
        self._rows = ()
        self.entries.SetItems([])
        self.refresh()

    def filter_changed(self, event=None):
        if self.pending or self.closed:
            return
        try:
            self._remember()
            self._render()
        except Exception as exc:
            self.report(f'Model search could not be completed: {exc}')

    def selection_changed(self, event=None):
        if self.pending or self.closed:
            return
        self._remember()
        selected = self._selected[self._profile]
        rows = self._all.get(self._profile, {})
        total = sum(rows[identifier].size for identifier in selected)
        visible = {row.identifier for row in self._rows}
        hidden = len(selected - visible)
        self.summary.SetValue(f'{len(selected)} models selected; {hidden} outside the current search.\n'
                              f'Selected model data: {format_bytes(total)}. Temporary space may also be needed.')

    def highlight_changed(self, event=None):
        index = self.entries.GetSelection()
        if not 0 <= index < len(self._rows):
            self.details.SetValue('No model matches this search. Change the search or profile.')
            return
        row = self._rows[index]
        self.details.SetValue(
            f'{row.label}\nIdentifier: {row.identifier}\nKind: {row.kind}; profile: {row.profile}\n'
            f'Model data: {format_bytes(row.size)}\nOffline file: {row.relative_path}\n'
            f'Upstream revision: {row.revision}\nGit file identity: {row.blob_sha1}')

    def select_shown(self, event=None):
        if self.pending or self.closed:
            return
        for index in range(len(self._rows)):
            self.entries.Check(index, True)
        self.selection_changed()
        self.entries.SetFocus()

    def clear_selection(self, event=None):
        if self.pending or self.closed:
            return
        self._selected[self._profile].clear()
        self._render()
        self.entries.SetFocus()

    def choose_folder(self, event=None):
        if self.pending or self.closed:
            return
        import wx
        dialog = wx.DirDialog(self, 'Choose the offline folder containing traineddata files',
                              defaultPath=self.source.GetValue(),
                              style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.source.SetValue(dialog.GetPath())
        finally:
            dialog.Destroy()

    def _queue_progress(self, job, identifier, completed, total):
        # A large model must not enqueue thousands of native UI callbacks.
        if (self.closed or job != self._job
                or type(completed) is not int or type(total) is not int
                or not 0 <= completed <= total or total <= 0):
            return
        with self._progress_lock:
            self._latest_progress = (job, str(identifier), completed, total)
            if self._progress_scheduled:
                return
            self._progress_scheduled = True
        self.call_after(self._flush_progress)

    def _flush_progress(self):
        with self._progress_lock:
            update = self._latest_progress
            self._progress_scheduled = False
        if self.closed or not self.pending or update is None or update[0] != self._job:
            return
        _, identifier, completed, total = update
        # Copied bytes still need final verification and manifest activation.
        self.progress.SetValue(min(999, completed * 1000 // total))
        if self.cancelled.is_set():
            self.report('Cancellation requested. Waiting for the current copy to stop safely.', focus=False)
        else:
            self.report(f'{identifier}: {completed:,} of {total:,} bytes processed.', focus=False)

    def install_selected(self, event=None):
        if self.pending or self.closed:
            return
        try:
            self._remember()
            profile = self._profile
            identifiers = tuple(identifier for identifier in self._all[profile]
                                if identifier in self._selected[profile])
            if not identifiers:
                raise ValueError('Select at least one model. Space checks the current model.')
            folder = self.source.GetValue()
            if not folder or '://' in folder or '\x00' in folder:
                raise ValueError('Choose an offline source folder first.')
            source = Path(folder)
            self._job += 1
            job = self._job
            self.pending = True
            self.cancelled.clear()
            self._enable(False)
            self.progress.SetValue(0)
            self.report(f'Verifying and installing {len(identifiers)} {profile} models from the selected folder.')

            def operation():
                return self.store.install(profile, source, identifiers,
                    cancelled=self.cancelled.is_set,
                    progress=lambda identifier, completed, total:
                        self._queue_progress(job, identifier, completed, total))

            future = self.executor.submit(operation)
            def completed_callback(completed):
                if not self.closed:
                    self.call_after(lambda: self._finish_install(job, profile, identifiers, completed))
            future.add_done_callback(completed_callback)
        except Exception as exc:
            self.pending = False
            self._enable(True)
            self.report(f'Installation could not start: {exc}')

    def _finish_install(self, job, profile, identifiers, future):
        if self.closed or job != self._job:
            return
        self.pending = False
        self._enable(True)
        error = None
        try:
            report = future.result()
            known = self._all[profile]
            revision = {known[identifier].revision for identifier in identifiers}
            if (report.profile != profile or set(report.installed) != set(identifiers)
                    or not set(report.already_present) <= set(identifiers)
                    or type(report.bytes) is not int or report.bytes < 0
                    or revision != {report.revision}):
                raise ValueError('The installation result does not match the selected models.')
            installed = set(self.store.available(profile))
            if not set(identifiers) <= installed:
                raise ValueError('Installed status does not confirm the complete selected set.')
            self._installed[profile] = installed
            self.result = report
            self.progress.SetValue(1000)
            new_count = len(report.installed) - len(report.already_present)
            message = (f'{len(report.installed)} selected models verified and available: '
                       f'{new_count} newly installed, {len(report.already_present)} already present. '
                       f'New copied data: {format_bytes(report.bytes)}.')
        except Exception as exc:
            error = exc
            message = ('Installation cancelled. The previous active model set is preserved.'
                       if isinstance(exc, InterruptedError) else f'Installation not completed: {exc}')
            try:
                self._installed[profile] = set(self.store.available(profile))
            except Exception:
                self._installed[profile] = None
                message += ' Installed status could not be refreshed; choose Refresh installed.'
        try:
            self._render()
        except Exception as exc:
            message += f' Model list could not be refreshed: {exc}'
        self.report(message)
        if error is not None:
            self.progress.SetValue(0)
        if self.close_when_idle:
            self._end()

    def cancel_operation(self, event=None):
        if self.pending and not self.closed:
            self.cancelled.set()
            self.cancel_button.Enable(False)
            self.report('Cancellation requested. Waiting for the current copy to stop safely.')

    def close(self, event=None):
        if self.closed:
            return
        if self.pending:
            self.close_when_idle = True
            self.cancel_operation()
        else:
            self._end()

    def on_close(self, event):
        self.close()
        if self.pending:
            if event.CanVeto():
                event.Veto()
            else:
                self.closed = True
                self.cancelled.set()
                event.Skip()

    def _end(self):
        import wx
        self.closed = True
        self.cancelled.set()
        self.EndModal(wx.ID_CANCEL)

    def show_help(self, event=None):
        import wx
        wx.MessageBox(INSTRUCTIONS, 'Offline OCR model installation', wx.OK | wx.ICON_INFORMATION, self)


def create_ocr_model_manager(parent, store=None, *, catalogue=None, executor=None):
    """Create native controls; dependency injection is for focused UI tests."""
    import wx
    if store is None:
        from ..extract.ocr_model_store import OcrModelStore
        store = OcrModelStore()
    if catalogue is None:
        from ..extract.model_catalog import models
        catalogue = models

    class ModelDialog(OcrModelManagerActions, wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Offline OCR model manager', size=(840, 780),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            layout = wx.BoxSizer(wx.VERTICAL)
            intro = wx.StaticText(self, label='Choose official language or script models and install verified files '
                                 'from your offline folder. No downloads are performed.')
            intro.Wrap(780)
            layout.Add(intro, 0, wx.EXPAND | wx.ALL, 8)

            def labelled(label, constructor, *, proportion=0, **kwargs):
                layout.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
                control = constructor(self, **kwargs)
                layout.Add(control, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
                return control

            self.profile = labelled('&Profile', wx.Choice, choices=[label for _, label in PROFILES])
            self.profile.SetName('OCR model profile')
            self.search = labelled('&Find models', wx.TextCtrl)
            self.search.SetName('Find OCR models by language, script or identifier')
            self.search.SetMaxLength(200)
            self.entries = labelled('&Models to install', wx.CheckListBox, choices=[], proportion=1)
            self.entries.SetName('OCR models; Space selects or clears a model')
            self.entries.SetMinSize((-1, 140))

            selection_buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.select_button = wx.Button(self, label='Select &all shown')
            self.clear_button = wx.Button(self, label='C&lear selection')
            self.refresh_button = wx.Button(self, label='&Refresh installed')
            for button in (self.select_button, self.clear_button, self.refresh_button):
                selection_buttons.Add(button, 0, wx.ALL, 4)
            layout.Add(selection_buttons, 0, wx.LEFT, 4)
            self.summary = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.summary.SetName('Selected models and disk-size estimate')
            self.summary.SetMinSize((-1, 48))
            layout.Add(self.summary, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.details = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.details.SetName('Selected model details and verified catalogue identity')
            self.details.SetMinSize((-1, 100))
            layout.Add(self.details, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.source = labelled('Offline source folder', wx.TextCtrl, style=wx.TE_READONLY)
            self.source.SetName('Offline model source folder')
            self.choose_button = wx.Button(self, label='Choose &source folder…')
            layout.Add(self.choose_button, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            self.progress = wx.Gauge(self, range=1000)
            self.progress.SetName('Current model copy progress')
            layout.Add(self.progress, 0, wx.EXPAND | wx.ALL, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.status.SetName('OCR model installation status and errors')
            self.status.SetMinSize((-1, 64))
            layout.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.install_button = wx.Button(self, label='&Install selected')
            self.cancel_button = wx.Button(self, label='Cancel &operation')
            self.help_button = wx.Button(self, label='&Help')
            self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
            for button in (self.install_button, self.cancel_button, self.help_button, self.close_button):
                buttons.Add(button, 0, wx.ALL, 8)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((700, 650))
            self.SetEscapeId(wx.ID_CANCEL)
            self.mutable = (self.profile, self.search, self.entries, self.select_button,
                            self.clear_button, self.refresh_button, self.choose_button,
                            self.install_button)
            self.profile.Bind(wx.EVT_CHOICE, self.profile_changed)
            self.search.Bind(wx.EVT_TEXT, self.filter_changed)
            self.entries.Bind(wx.EVT_CHECKLISTBOX, self.selection_changed)
            self.entries.Bind(wx.EVT_LISTBOX, self.highlight_changed)
            for button, handler in ((self.select_button, self.select_shown),
                                    (self.clear_button, self.clear_selection),
                                    (self.refresh_button, self.refresh),
                                    (self.choose_button, self.choose_folder),
                                    (self.install_button, self.install_selected),
                                    (self.cancel_button, self.cancel_operation),
                                    (self.help_button, self.show_help),
                                    (self.close_button, self.close)):
                button.Bind(wx.EVT_BUTTON, handler)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self._owned_executor = executor is None
            worker = executor if executor is not None else ThreadPoolExecutor(
                max_workers=1, thread_name_prefix='carr-ocr-models')
            self.configure(store, catalogue, worker, wx.CallAfter)
            self.search.SetFocus()

    return ModelDialog()


def show_ocr_model_manager(parent, store=None):
    """Show one modal manager and return its last verified install report."""
    dialog = create_ocr_model_manager(parent, store)
    try:
        dialog.ShowModal()
        return dialog.result
    finally:
        dialog.closed = True
        dialog.cancelled.set()
        if dialog._owned_executor:
            dialog.executor.shutdown(wait=False, cancel_futures=True)
        dialog.Destroy()
