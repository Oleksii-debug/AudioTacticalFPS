"""Accessible, persistent file-import plans in the existing desktop host."""
import unicodedata
from ..extract.document_formats import file_dialog_wildcard


def readable(value):
    """Keep filenames readable without allowing control characters to add rows."""
    return ''.join(' ' if unicodedata.category(c) in ('Cc', 'Cf') else c
                   for c in str(value)).strip()


def batch_summary(batch):
    counts = batch.get('counts', {})
    labels = [('complete', 'imported'), ('queued', 'queued'), ('running', 'running'),
              ('duplicate', 'exact duplicates'), ('rejected', 'not accepted'),
              ('failed', 'failed'), ('quarantined', 'need page review'), ('excluded', 'excluded')]
    detail = ', '.join(f'{counts.get(key, 0)} {label}' for key, label in labels if counts.get(key, 0))
    summary = f'{batch.get("total", len(batch.get("rows", ())))} files: {detail or "none"}.'
    scan = batch.get('scan')
    if scan is not None:
        cursor = scan.get('cursor', {})
        complete = scan.get('complete', cursor.get('complete', False))
        planned = scan.get('planned', cursor.get('planned'))
        summary = f'Scan page {scan["page"] + 1}. ' + summary
        if planned is not None:
            summary += f' {planned} source rows reviewed across saved pages.'
        if scan.get('review_required'):
            summary += ' External scan continuation needs a fresh source selection after restore.'
        elif not complete:
            summary += ' More source entries remain. Choose Continue source scan to review the next page.'
        else:
            summary += ' The selected source traversal is complete.'
        if not scan.get('classification_complete', True):
            summary += ' Some duplicate checks were paused; the catalog is checked before import.'
    return summary


def row_details(row):
    states = {'queued': 'Ready to import', 'running': 'Importing', 'complete': 'Imported',
              'duplicate': 'Exact file duplicate; not imported', 'rejected': 'Not accepted',
              'failed': 'Import failed', 'quarantined': 'Independent page review required',
              'excluded': 'Excluded from this batch'}
    lines = ['File: ' + readable(row['file']),
             'Status: ' + states.get(row['state'], readable(row['state']))]
    if row.get('message'):
        lines.append('Details: ' + readable(row['message']))
    result = row.get('result') or {}
    message = result.get('message') or result.get('error')
    if message and message != row.get('message'):
        lines.append('Result: ' + readable(message))
    warnings = result.get('warnings', ())
    if isinstance(warnings, (list, tuple)) and warnings:
        wording = {
            'logical_locator_not_page': 'Locations are logical segments, not verified printed pages.',
            'layout_not_verified': 'The document visual layout has not been verified.',
            'html_active_or_nontext_content_omitted': 'HTML scripts and active or nontext content were omitted.',
            'html_hidden_content_omitted': 'Hidden HTML content was omitted.',
            'html_styles_not_applied': 'HTML styles were not applied; text hidden only by a stylesheet may appear.',
            'html_images_and_media_not_transcribed': 'Images and media were not transcribed.',
            'email_headers_not_authenticated': 'Email header metadata has not been authenticated.',
            'email_attachments_omitted': 'Email attachments were omitted and were not opened.',
            'email_attached_messages_omitted': 'Attached email messages were omitted.',
            'email_related_resources_omitted': 'Related email resources were omitted.',
            'formula_cells_not_read': 'Cells containing formulas were omitted.',
            'formula_cells_not_evaluated': 'Spreadsheet formulas were not calculated.',
            'stored_cell_values_not_formatted': 'Stored cell values are shown without spreadsheet display formatting.',
            'hidden_sheets_not_read': 'Hidden sheets were omitted.',
            'hidden_rows_not_read': 'Hidden rows were omitted.',
            'hidden_columns_not_read': 'Hidden columns were omitted.',
            'hidden_slides_not_read': 'Hidden slides were omitted.',
            'external_resources_not_loaded': 'External resources were not loaded.',
            'speaker_notes_master_slides_not_read': 'Speaker notes and master slides were omitted.',
            'slide_shape_order_not_visual_order': 'Slide text follows stored shape order, which can differ from visual order.',
            'inline_notes_images_not_read': 'Inline notes and images were omitted.',
            'markdown_markup_preserved': 'Markdown notation is preserved as literal source text.',
            'long_text_split_without_changing_characters': 'Long text was split into readable segments without changing characters.',
        }
        lines.append('Extraction notes:')
        for warning in warnings[:100]:
            if isinstance(warning, str):
                lines.append(wording.get(warning, readable(warning[:256].replace('_', ' ')).capitalize() + '.'))
    if row.get('duplicate_of'):
        lines.append('Matches: ' + readable(row['duplicate_of']))
    if row.get('manifest'):
        lines.append('Size: ' + str(row['manifest']['size']) + ' bytes')
    if row['state'] == 'quarantined':
        lines.append('Close Import Center and choose Library, Review pending imported page. '
                     'After independent comparison, return here and refresh the batch.')
    return '\n'.join(lines)


def batch_report(batch):
    """A user-requested report contains filenames/results, never source passages."""
    return ('Import report\n' + batch_summary(batch) + '\n\n' +
            '\n\n'.join(row_details(row) for row in batch.get('rows', ())) + '\n')


class ImportCenterActions:
    """Handlers separated from native construction for meaningful event tests."""
    def _bound(self):
        if (self.closed or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library):
            raise ValueError('The library folder changed. Reopen Import Center.')
        with self.library.read(self.workspace):
            pass

    def report(self, text, *, focus=True):
        self.status.SetValue(text)
        if focus:
            self.status.SetFocus()

    def refresh(self, event=None, *, selected=None):
        if self.busy:
            return
        try:
            self._bound()
            selected = selected or (self.batch or {}).get('batch_id')
            self.summaries = self.library.import_batches(self.workspace)
            self.batches.SetItems([f'{i}. {readable(row["created"])} — {batch_summary(row)}'
                                   for i, row in enumerate(self.summaries, 1)])
            index = next((i for i, row in enumerate(self.summaries)
                          if row['batch_id'] == selected), 0 if self.summaries else -1)
            self.batches.SetSelection(index)
            self.select_batch()
        except Exception as exc:
            self.batch = None
            self.rows.SetItems([])
            self.details.SetValue('')
            self.report(str(exc))

    def select_batch(self, event=None):
        if self.busy:
            return
        try:
            self._bound()
            index = self.batches.GetSelection()
            if not 0 <= index < len(self.summaries):
                self.batch = None
                self.rows.SetItems([])
                self.details.SetValue('')
                self.report('No saved import batches. Choose Add files or Add folder.', focus=False)
                return
            self._display(self.library.import_batch(self.workspace,
                                                    self.summaries[index]['batch_id']))
        except Exception as exc:
            self.batch = None
            self.rows.SetItems([])
            self.details.SetValue('')
            self.report(str(exc))

    def _display(self, batch):
        selected = self.rows.GetSelection()
        self.batch = batch
        self.rows.SetItems([f'{i}. {readable(row["file"])} — {row["state"].replace("_", " ")}'
                           for i, row in enumerate(batch['rows'], 1)])
        self.rows.SetSelection(min(max(selected, 0), len(batch['rows']) - 1))
        self.select_row()
        self.report(batch_summary(batch), focus=False)

    def select_row(self, event=None):
        index = self.rows.GetSelection()
        rows = (self.batch or {}).get('rows', ())
        self.details.SetValue(row_details(rows[index]) if 0 <= index < len(rows) else '')

    def _job(self, title, operation, done, *, token=None, planning=False):
        if self.busy or self.frame.working:
            self.report('An operation is already running. Wait or choose Cancel operation.')
            return
        self.busy = True
        token = token if token is not None else object()
        self._job_token = token
        self.planning = planning
        for control in self.mutable:
            control.Disable()
        self.cancel.Enable()
        self.report(title + '…')

        def finish(value=None, error=None):
            if self.closed or self._job_token is not token:
                return
            self.busy = False
            for control in self.mutable:
                control.Enable()
            self.cancel.Disable()
            if error is not None:
                self.report(f'{title}: {error}')
            else:
                try:
                    self._bound()
                    done(value)
                except Exception as exc:
                    self.report(str(exc))

        try:
            self.frame.run_job(title, operation, lambda value: finish(value),
                               failed=lambda exc: finish(error=exc))
        except Exception as exc:
            finish(error=exc)

    def _scan_options(self):
        from ..ingest.batch_scan import scan_page_limits
        return scan_page_limits(max_files=int(self.page_files.GetValue()),
            max_entries=int(self.page_entries.GetValue()), timeout=float(self.page_seconds.GetValue()))

    def plan(self, paths):
        self._bound()
        selected = tuple(paths)
        limits = self._scan_options()
        index = self.collection.GetSelection()
        if not 0 <= index < len(self.collection_ids):
            raise ValueError('Choose a destination collection and text recognition languages.')
        config = self._new_plan_ocr_config()
        collections = () if self.collection_ids[index] is None else (self.collection_ids[index],)
        recursive = self.recursive.GetValue()

        def done(batch):
            self.refresh(selected=batch['batch_id'])
            self.report('Plan saved. ' + batch_summary(batch) +
                        ' Review the files, exclude any you do not want, then choose Start queued files.')

        def operation():
            self._bound()
            return self.library.plan_import_page(self.workspace, selected, collections=collections,
                recursive=recursive, config=config, cancelled=self.frame.cancel_event.is_set, **limits)
        self._job('Checking the next source page and duplicates', operation, done, planning=True)

    def continue_scan(self, event=None):
        try:
            self._bound()
            if self.batch is None or self.batch.get('version') != 2:
                raise ValueError('Choose a saved scan page, or select source folders for a new paged scan.')
            batch_id = self.batch['batch_id']
            limits = self._scan_options()
            def operation():
                self._bound()
                return self.library.continue_import_scan(self.workspace, batch_id,
                    cancelled=self.frame.cancel_event.is_set, **limits)
            def done(batch):
                self.refresh(selected=batch['batch_id'])
                self.report('Scan page saved. ' + batch_summary(batch)
                            + ' Review this page before choosing Start queued files.')
            self._job('Continuing the saved source scan', operation, done, planning=True)
        except Exception as exc:
            self.report(str(exc))

    def _new_plan_ocr_config(self):
        from ..extract.page_ocr import Config
        from .ocr_options import LANGUAGES
        language = self.languages.GetSelection()
        if 0 <= language < len(LANGUAGES):
            return Config(languages=LANGUAGES[language][1])
        custom = getattr(self, 'custom_ocr_config', None)
        if language == len(LANGUAGES) and isinstance(custom, Config):
            return custom
        raise ValueError('Choose a destination collection and text recognition languages.')

    def choose_ocr_models(self, event=None):
        """Change only the explicit settings captured by future import plans."""
        from ..extract.page_ocr import Config
        from .ocr_options import LANGUAGES
        from .ocr_selection import choose_model_config
        if self.busy or self.frame.working:
            self.report('An operation is already running. Wait or choose Cancel operation.')
            return
        try:
            self._bound()
            try:
                initial = self._new_plan_ocr_config()
            except ValueError:
                initial = Config()
            chosen = choose_model_config(self, initial=initial)
            if chosen is None:
                return
            if not isinstance(chosen, Config):
                raise ValueError('The OCR model selection did not return valid import settings.')
            self._bound()
            if self.busy or self.frame.working:
                raise ValueError('An import operation started. Choose model settings when it finishes.')
            profile = {'installed': 'Installed', 'fast': 'Fast', 'best': 'Best'}[chosen.model_profile]
            identifiers = chosen.languages.split('+')
            preview = ', '.join(identifiers[:6])
            if len(identifiers) > 6:
                preview += f'; {len(identifiers) - 6} more'
            label = f'Custom — {profile}: {preview}'
            self.languages.SetItems([text for text, _ in LANGUAGES] + [label])
            self.languages.SetSelection(len(LANGUAGES))
            self.custom_ocr_config = chosen
            orientation = 'on' if chosen.orientation else 'off'
            self.report(f'OCR settings for new plans: {profile}; {", ".join(identifiers)}. '
                        f'Page orientation detection: {orientation}. '
                        f'Automatic language/region review: {"on" if chosen.automatic_languages else "off"}.')
        except Exception as exc:
            self.report(str(exc))

    def add_files(self, event=None):
        import wx
        dialog = wx.FileDialog(self, 'Select books for an import plan',
            wildcard=file_dialog_wildcard(),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        try:
            paths = tuple(dialog.GetPaths()) if dialog.ShowModal() == wx.ID_OK else ()
        finally:
            dialog.Destroy()
        if paths:
            try:
                self.plan(paths)
            except Exception as exc:
                self.report(str(exc))

    def add_folder(self, event=None):
        import wx
        dialog = wx.DirDialog(self, 'Select a folder of books', style=wx.DD_DIR_MUST_EXIST)
        try:
            path = dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()
        if path:
            try:
                self.plan((path,))
            except Exception as exc:
                self.report(str(exc))

    def run(self, event=None, *, retry=False):
        try:
            self._bound()
            if self.batch is None:
                raise ValueError('Select a saved import batch first.')
            batch_id = self.batch['batch_id']
            token = object()

            def progress(batch):
                import wx
                def apply():
                    if (not self.closed and self.busy and self.frame.library is self.library
                            and getattr(self, '_job_token', None) is token
                            and batch.get('batch_id') == batch_id):
                        self._display(batch)
                wx.CallAfter(apply)

            def done(batch):
                self.refresh(selected=batch_id)
                if self.frame.book is not None:
                    self.frame.book.GetPage(1).refresh()
                self.report(batch_summary(self.batch or batch) +
                            (' Continue with Start queued files when ready.'
                             if (self.batch or batch).get('counts', {}).get('queued') else ''))

            self._job('Importing saved batch', lambda: self.library.run_import_batch(
                self.workspace, batch_id, cancelled=self.frame.cancel_event.is_set,
                retry_failed=retry, progress=progress), done, token=token)
        except Exception as exc:
            self.report(str(exc))

    def toggle_row(self, event=None):
        try:
            self._bound()
            index = self.rows.GetSelection()
            rows = (self.batch or {}).get('rows', ())
            if not 0 <= index < len(rows):
                raise ValueError('Select a file first.')
            row = rows[index]
            batch = self.library.set_import_batch_row(self.workspace, self.batch['batch_id'],
                row['row_id'], included=row['state'] in ('excluded', 'duplicate'))
            self._display(batch)
            self.report(row_details(batch['rows'][index]))
        except Exception as exc:
            self.report(str(exc))

    def save_report(self, event=None):
        import wx
        from ..research.export_file import write_export, export_completion
        from .export_destination import confirm_export_destination
        try:
            self._bound()
            if self.batch is None:
                raise ValueError('Select a saved import batch first.')
            batch = self.library.import_batch(self.workspace, self.batch['batch_id'])
            dialog = wx.FileDialog(self, 'Save import report', defaultFile='Import-report.txt',
                wildcard='Text report (*.txt)|*.txt', style=wx.FD_SAVE)
            try:
                path = dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()
            if path:
                self._bound()
                approval = confirm_export_destination(self, path, private_root=self.library.root,
                                                       title='Replace import report', wx_module=wx)
                if approval is None:
                    return
                self._bound()
                receipt = write_export(path, batch_report(batch), private_root=self.library.root,
                                       approval=approval)
                self.report(export_completion('Import report saved.', receipt))
        except Exception as exc:
            self.report(str(exc))

    def cancel_operation(self, event=None):
        if self.busy:
            self.frame.cancel_event.set()
            self.report('Stopping source review. Saved pages are kept; the checked prefix will be saved after source checks finish.'
                        if getattr(self, 'planning', False) else
                        'Stopping the current operation. Queued files will remain saved.')

    def close(self, event=None):
        if self.busy:
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            self.report('Choose Cancel operation or wait before closing. '
                        'Existing saved plans and completed imports are kept.')
            return
        self.closed = True
        import wx
        self.EndModal(wx.ID_CANCEL)


def show_import_center(frame, workspace):
    import wx
    from wx.lib.scrolledpanel import ScrolledPanel
    from .ocr_options import LANGUAGES

    class ImportCenter(wx.Dialog, ImportCenterActions):
        def __init__(self):
            super().__init__(frame, title='Import Center', size=(920, 820),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.frame, self.library, self.workspace = frame, frame.library, workspace
            self.busy = self.closed = False
            self.batch, self.summaries = None, ()
            self.custom_ocr_config = None
            self.mutable = []
            # Keep every source-review control reachable at the minimum window
            # size; focused children scroll into view through wx's tab traversal.
            self.body = ScrolledPanel(self, style=wx.TAB_TRAVERSAL)
            self.body.SetName('Import Center scrollable options and results')
            self.body.SetMinSize((1, 1))
            layout = wx.BoxSizer(wx.VERTICAL)
            introduction = wx.StaticText(self.body, label='Prepare a file or folder import, review the plan, then start. '
                       'Saved queues can be continued after restarting the program.')
            introduction.Wrap(750)
            layout.Add(introduction, 0, wx.ALL, 8)
            options = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            options.AddGrowableCol(1)
            self.collection_ids = [None]
            collection_labels = ['Whole library; no collection assignment']
            for number, (identity, name) in enumerate(self.library.collections(workspace), 1):
                self.collection_ids.append(identity)
                collection_labels.append(f'{number}. Collection: {readable(name)}')
            self.collection = wx.Choice(self.body, choices=collection_labels)
            self.languages = wx.Choice(self.body, choices=[label for label, _ in LANGUAGES])
            for label, control in [('Destination for new plans', self.collection),
                                   ('Text recognition languages for new plans', self.languages)]:
                control.SetName(label)
                control.SetSelection(0)
                options.Add(wx.StaticText(self.body, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                options.Add(control, 1, wx.EXPAND)
                self.mutable.append(control)
            layout.Add(options, 0, wx.EXPAND | wx.ALL, 8)
            self.page_files = wx.SpinCtrl(self.body, min=1, max=5000, initial=1000)
            self.page_entries = wx.SpinCtrl(self.body, min=1, max=20000, initial=20000)
            self.page_seconds = wx.SpinCtrl(self.body, min=1, max=600, initial=60)
            limits = wx.FlexGridSizer(cols=3, hgap=8, vgap=4)
            controls = [('Files per scan page', self.page_files),
                        ('Traversal entries per scan page', self.page_entries),
                        ('Seconds per source scan page', self.page_seconds)]
            for index, (label, _control) in enumerate(controls):
                limits.AddGrowableCol(index)
                limits.Add(wx.StaticText(self.body, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            for label, control in controls:
                control.SetName(label)
                limits.Add(control, 1, wx.EXPAND)
                self.mutable.append(control)
            layout.Add(limits, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.ocr_models_button = wx.Button(self.body, label='Choose installed or managed OCR &models…')
            self.ocr_models_button.SetName('Choose installed or managed OCR models for new plans')
            self.ocr_models_button.Bind(wx.EVT_BUTTON, self.choose_ocr_models)
            self.mutable.append(self.ocr_models_button)
            layout.Add(self.ocr_models_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.recursive = wx.CheckBox(self.body, label='Include subfol&ders in new plans')
            self.recursive.SetValue(True)
            self.mutable.append(self.recursive)
            layout.Add(self.recursive, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            add = wx.BoxSizer(wx.HORIZONTAL)
            for label, handler in [('&Add files...', self.add_files), ('Add &folder...', self.add_folder),
                                   ('Continue source &scan', self.continue_scan),
                                   ('Refresh saved &queues', self.refresh)]:
                control = wx.Button(self.body, label=label)
                control.SetName(label.replace('&', ''))
                control.Bind(wx.EVT_BUTTON, handler)
                self.mutable.append(control)
                add.Add(control, 0, wx.RIGHT, 8)
            layout.Add(add, 0, wx.ALL, 8)
            layout.Add(wx.StaticText(self.body, label='Saved import batches'), 0, wx.LEFT | wx.RIGHT, 8)
            self.batches = wx.ListBox(self.body, size=(-1, 65))
            self.batches.SetName('Saved import batches')
            self.batches.Bind(wx.EVT_LISTBOX, self.select_batch)
            self.mutable.append(self.batches)
            layout.Add(self.batches, 1, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(self.body, label='Files in the selected batch'), 0, wx.LEFT | wx.RIGHT, 8)
            self.rows = wx.ListBox(self.body, size=(-1, 100))
            self.rows.SetName('Files and import status')
            self.rows.Bind(wx.EVT_LISTBOX, self.select_row)
            layout.Add(self.rows, 2, wx.EXPAND | wx.ALL, 8)
            self.details = wx.TextCtrl(self.body, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 70))
            self.details.SetName('Selected file details')
            layout.Add(self.details, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            actions = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            actions.AddGrowableCol(0)
            actions.AddGrowableCol(1)
            for label, handler in [('&Start queued files', self.run),
                    ('&Retry failed or reviewed files', lambda event: self.run(event, retry=True)),
                    ('&Exclude or include selected file', self.toggle_row), ('Save re&port...', self.save_report)]:
                control = wx.Button(self.body, label=label)
                control.SetName(label.replace('&', ''))
                control.Bind(wx.EVT_BUTTON, handler)
                self.mutable.append(control)
                actions.Add(control, 0, wx.EXPAND)
            layout.Add(actions, 0, wx.EXPAND | wx.ALL, 8)
            self.status = wx.TextCtrl(self.body, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 45))
            self.status.SetName('Import Center status')
            layout.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            bottom = wx.BoxSizer(wx.HORIZONTAL)
            self.cancel = wx.Button(self, label='&Cancel operation')
            self.cancel.Disable()
            self.cancel.Bind(wx.EVT_BUTTON, self.cancel_operation)
            bottom.Add(self.cancel, 0, wx.RIGHT, 8)
            close = wx.Button(self, wx.ID_CANCEL, 'Cl&ose')
            close.SetName('Close Import Center')
            close.Bind(wx.EVT_BUTTON, self.close)
            bottom.Add(close)
            self.body.SetSizer(layout)
            self.body.SetupScrolling(scroll_x=True, scroll_y=True, scrollIntoView=True)
            shell = wx.BoxSizer(wx.VERTICAL)
            shell.Add(self.body, 1, wx.EXPAND)
            shell.Add(bottom, 0, wx.ALL, 8)
            self.SetSizer(shell)
            self.SetMinSize((800, 650))
            self.Bind(wx.EVT_CLOSE, self.close)
            self.refresh()
            self.batches.SetFocus() if self.summaries else self.collection.SetFocus()

    dialog = ImportCenter()
    try:
        dialog.ShowModal()
    finally:
        dialog.closed = True
        dialog.Destroy()
