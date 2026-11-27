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
    text = f'{batch.get("total", len(batch.get("rows", ())))} files: {detail or "none"}.'
    scan = batch.get('scan')
    if scan:
        text = f'Scan page {scan["page"]}. ' + text
        if scan.get('next_offset') is not None:
            text += ' More source files remain. Choose Next scan page to check them.'
        unscanned = len(scan.get('unscanned', ())) or scan.get('unscanned_count', 0)
        if unscanned:
            text += f' Scan incomplete: {unscanned} source scopes were not fully scanned; select smaller folders.'
    return text


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
    unscanned = (batch.get('scan') or {}).get('unscanned', ())
    incomplete = ('\nUnscanned or partly scanned source scopes:\n' +
                  '\n'.join(readable(path) for path in unscanned) + '\n') if unscanned else ''
    return ('Import report\n' + batch_summary(batch) + '\n' + incomplete + '\n' +
            '\n\n'.join(row_details(row) for row in batch.get('rows', ())) + '\n')



def confirm_restored_import(parent, review):
    """Native, keyboard-operable review of the exact captured-source selection."""
    import wx
    dialog = wx.Dialog(parent, title='Review restored import', size=(830, 650),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    destinations = ', '.join(readable(item['name']) for item in review['destination_collections'])
    summary = ('Library: ' + readable(review['workspace_name']) + '. Collections: '
               + (destinations or 'Whole library; no collection assignment') + '.\n'
               + f'{review["file_count"]} captured files; {review["source_bytes"]} bytes. '
               'Only the saved local copies will be read. Recorded external folders are not followed.')
    metadata = review.get('metadata')
    if metadata is not None:
        summary += ('\nTitle: ' + readable(metadata['title'])
                    + '. Authors: ' + ', '.join(readable(a) for a in metadata['authors'])
                    + '. Language: ' + readable(metadata['language'] or 'Not entered')
                    + '. Edition: ' + readable(metadata['edition'] or 'Not entered')
                    + '. Volume: ' + readable(metadata['volume'] or 'Not entered') + '.')
    recognition = review.get('text_recognition')
    if recognition is not None:
        summary += ('\nText recognition language choice: ' + readable(recognition['languages'])
                    + '. This approval does not verify OCR text or printed page labels.')
    if review['continuations_disabled']:
        summary += ' The old scan continuation is disabled.'
    if review['previous_scan_incomplete']:
        summary += (' The earlier scan did not cover all selected sources. '
                    'Choose Add folder again for any sources that were never captured.')
    if review['new_import_required']:
        summary += (' The old inline TXT import did not save its title, work or collection choices. '
                    'Approval creates a new ordinary import plan from these exact bytes. '
                    'The old journal is preserved; its missing metadata is not inferred.')
    heading = wx.TextCtrl(dialog, value=summary, style=wx.TE_MULTILINE | wx.TE_READONLY,
                          size=(-1, 110))
    heading.SetName('Restored import scope and continuation status')
    layout.Add(heading, 0, wx.EXPAND | wx.ALL, 8)
    files = wx.ListBox(dialog, choices=[
        f'{i}. {readable(row["file"])} — {row["size"]} bytes'
        for i, row in enumerate(review['files'], 1)])
    files.SetName('Exact captured files in this restored import')
    layout.Add(files, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
    details = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
    details.SetName('Selected captured file type and SHA256')
    def selected(event=None):
        index = files.GetSelection()
        if 0 <= index < len(review['files']):
            row = review['files'][index]
            details.SetValue('File: ' + readable(row['file']) + '\nType: ' + row['mime']
                             + '\nSHA256: ' + row['sha256'])
        else:
            details.SetValue('')
    files.Bind(wx.EVT_LISTBOX, selected)
    if review['files']:
        files.SetSelection(0)
        selected()
    layout.Add(details, 0, wx.EXPAND | wx.ALL, 8)
    consent = wx.CheckBox(dialog, label='I &reviewed this captured-source selection')
    consent.SetName('Confirm review of this restored import scope')
    layout.Add(consent, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    approve = wx.Button(dialog, wx.ID_OK, '&Approve captured sources')
    approve.SetName('Approve captured sources without starting import')
    approve.Disable()
    consent.Bind(wx.EVT_CHECKBOX, lambda event: approve.Enable(consent.GetValue()))
    buttons.Add(approve, 0, wx.RIGHT, 8)
    buttons.Add(wx.Button(dialog, wx.ID_CANCEL, '&Cancel'))
    layout.Add(buttons, 0, wx.ALL, 8)
    dialog.SetSizer(layout)
    dialog.SetMinSize((680, 500))
    heading.SetFocus()
    try:
        return dialog.ShowModal() == wx.ID_OK and consent.GetValue()
    finally:
        dialog.Destroy()

class ImportCenterActions:
    """Handlers separated from native construction for meaningful event tests."""
    def _bound(self):
        self._scope_current()
        with self.library.read(self.workspace):
            pass

    def _scope_current(self):
        if (self.closed or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library):
            raise ValueError('The library folder changed. Reopen Import Center.')
        guard = getattr(self, '_library_guard', None)
        if guard is not None:
            guard._bound()

    def _cancelled(self):
        self._scope_current()
        return self.frame.cancel_event.is_set()

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
        self.scan_timeout.SetValue(int((batch.get('scan') or {}).get('timeout_seconds', 120)))
        self.rows.SetItems([f'{i}. {readable(row["file"])} — {row["state"].replace("_", " ")}'
                           for i, row in enumerate(batch['rows'], 1)])
        self.rows.SetSelection(min(max(selected, 0), len(batch['rows']) - 1))
        self.select_row()
        self.report(batch_summary(batch), focus=False)
        next_button = getattr(self, 'next_scan_button', None)
        if next_button is not None:
            if not self.busy and (batch.get('scan') or {}).get('next_offset') is not None:
                next_button.Enable()
            else:
                next_button.Disable()

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
            def guarded_operation():
                self._bound()
                return operation()
            self.frame.run_job(title, guarded_operation, lambda value: finish(value),
                               failed=lambda exc: finish(error=exc))
        except Exception as exc:
            finish(error=exc)

    def plan(self, paths):
        self._bound()
        index = self.collection.GetSelection()
        if not 0 <= index < len(self.collection_ids):
            raise ValueError('Choose a destination collection and text recognition languages.')
        config = self._new_plan_ocr_config()
        collections = () if self.collection_ids[index] is None else (self.collection_ids[index],)
        recursive = self.recursive.GetValue()
        from ..ingest.batches import _scan_timeout
        scan_timeout = _scan_timeout(self.scan_timeout.GetValue())

        def done(batch):
            self.refresh(selected=batch['batch_id'])
            self.report('Plan saved. ' + batch_summary(batch) +
                        ' Review the files, exclude any you do not want, then choose Start queued files.')

        self._job('Checking files and duplicates', lambda: self.library.plan_import_batch(
            self.workspace, paths, collections=collections, recursive=recursive, config=config,
            cancelled=self._cancelled, scan_timeout=scan_timeout), done, planning=True)


    def review_restored_imports(self, event=None):
        import wx
        if self.busy or self.frame.working:
            self.report('An operation is already running. Wait or choose Cancel operation.')
            return
        try:
            self._bound()
            choices = self.library.import_recovery_items(self.workspace)
            if not choices:
                self.report('No restored imports require a new captured-source review in this library.')
                return
            if len(choices) == 1:
                selected = choices[0]
            else:
                dialog = wx.SingleChoiceDialog(self, 'Choose a restored import to review.',
                    'Restored imports', [readable(item['label']) for item in choices])
                try:
                    if dialog.ShowModal() != wx.ID_OK:
                        return
                    index = dialog.GetSelection()
                finally:
                    dialog.Destroy()
                self._bound()
                if not 0 <= index < len(choices):
                    raise ValueError('Choose a restored import from the displayed list.')
                selected = choices[index]
            kind, identity = selected['kind'], selected['identity']

            def reviewed(value):
                self._bound()
                if (value['kind'], value['identity'], value['workspace']) != (kind, identity, self.workspace):
                    raise ValueError('The restored import review belongs to another selection.')
                if not confirm_restored_import(self, value):
                    self.report('Restored import remains paused; no source approval was saved.')
                    return
                self._bound()
                checksum = value['checksum']
                def approved(receipt):
                    self._bound()
                    if not receipt.get('approved') or receipt.get('checksum') != checksum:
                        raise ValueError('The restored import did not return its reviewed approval.')
                    batch_id = receipt.get('batch_id') or (identity if kind == 'batch' else None)
                    self.refresh(selected=batch_id)
                    self.report(receipt['message'] + (
                        ' Choose Start queued files to execute the reviewed plan.' if batch_id else
                        ' Close Import Center and choose Library, Resume interrupted import.'))
                self._job('Approving captured import sources', lambda: self.library.approve_import_recovery(
                    self.workspace, kind, identity, expected_checksum=checksum, confirmed=True,
                    cancelled=self._cancelled), approved)
            self._job('Preparing restored import review', lambda: self.library.review_import_recovery(
                self.workspace, kind, identity, cancelled=self._cancelled), reviewed)
        except Exception as exc:
            self.report(str(exc))

    def next_scan_page(self, event=None):
        """Use the selected saved page's choices, never the mutable new-plan controls."""
        if self.busy or self.frame.working:
            self.report('An operation is already running. Wait or choose Cancel operation.')
            return
        try:
            self._bound()
            if self.batch is None or (self.batch.get('scan') or {}).get('next_offset') is None:
                raise ValueError('Select a saved scan page with more source files to check.')
            batch_id = self.batch['batch_id']
            from ..ingest.batches import _scan_timeout
            scan_timeout = _scan_timeout(self.scan_timeout.GetValue())
            def done(batch):
                self.refresh(selected=batch['batch_id'])
                self.report('Next scan page saved. ' + batch_summary(batch) +
                    ' Review this page before starting its queued files. Earlier pages were kept.')
            self._job('Checking next scan page', lambda: self.library.next_import_scan_page(
                self.workspace, batch_id, cancelled=self._cancelled,
                scan_timeout=scan_timeout), done, planning=True)
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
                        f'Page orientation detection: {orientation}.')
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
        from ..research.export_file import write_export
        try:
            self._bound()
            if self.batch is None:
                raise ValueError('Select a saved import batch first.')
            batch = self.library.import_batch(self.workspace, self.batch['batch_id'])
            dialog = wx.FileDialog(self, 'Save import report', defaultFile='Import-report.txt',
                wildcard='Text report (*.txt)|*.txt', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
            try:
                path = dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()
            if path:
                write_export(path, batch_report(batch), private_root=self.library.root)
                self.report('Import report saved.')
        except Exception as exc:
            self.report(str(exc))

    def cancel_operation(self, event=None):
        if self.busy:
            self.frame.cancel_event.set()
            self.report('Stopping the file check. An unfinished scan has not saved a new plan.'
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
    from .ocr_options import LANGUAGES

    class ImportCenter(wx.Dialog, ImportCenterActions):
        def __init__(self):
            super().__init__(frame, title='Import Center', size=(920, 760),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.frame, self.library, self.workspace = frame, frame.library, workspace
            from ..ingest.batches import ImportBatchStore
            self._library_guard = ImportBatchStore(self.library)
            self.busy = self.closed = False
            self.batch, self.summaries = None, ()
            self.custom_ocr_config = None
            self.mutable = []
            layout = wx.BoxSizer(wx.VERTICAL)
            introduction = wx.StaticText(self, label='Prepare a file or folder import, review the plan, then start. '
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
            self.collection = wx.Choice(self, choices=collection_labels)
            self.languages = wx.Choice(self, choices=[label for label, _ in LANGUAGES])
            for label, control in [('Destination for new plans', self.collection),
                                   ('Text recognition languages for new plans', self.languages)]:
                control.SetName(label)
                control.SetSelection(0)
                options.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                options.Add(control, 1, wx.EXPAND)
                self.mutable.append(control)
            scan_label = 'Scan time limit per page (30–600 seconds)'
            self.scan_timeout = wx.SpinCtrl(self, min=30, max=600, initial=120)
            self.scan_timeout.SetName(scan_label)
            options.Add(wx.StaticText(self, label=scan_label), 0, wx.ALIGN_CENTER_VERTICAL)
            options.Add(self.scan_timeout, 1, wx.EXPAND)
            self.mutable.append(self.scan_timeout)
            layout.Add(options, 0, wx.EXPAND | wx.ALL, 8)
            self.ocr_models_button = wx.Button(self, label='Choose installed or managed OCR &models…')
            self.ocr_models_button.SetName('Choose installed or managed OCR models for new plans')
            self.ocr_models_button.Bind(wx.EVT_BUTTON, self.choose_ocr_models)
            self.mutable.append(self.ocr_models_button)
            layout.Add(self.ocr_models_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.recursive = wx.CheckBox(self, label='Include subfol&ders in new plans')
            self.recursive.SetValue(True)
            self.mutable.append(self.recursive)
            layout.Add(self.recursive, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            add = wx.BoxSizer(wx.HORIZONTAL)
            for label, handler in [('&Add files...', self.add_files), ('Add &folder...', self.add_folder),
                                   ('Refresh saved &queues', self.refresh)]:
                control = wx.Button(self, label=label)
                control.Bind(wx.EVT_BUTTON, handler)
                self.mutable.append(control)
                add.Add(control, 0, wx.RIGHT, 8)
            self.next_scan_button = wx.Button(self, label='&Next scan page')
            self.next_scan_button.SetName('Check and save the next source scan page')
            self.next_scan_button.Bind(wx.EVT_BUTTON, self.next_scan_page)
            self.mutable.append(self.next_scan_button)
            add.Add(self.next_scan_button, 0, wx.RIGHT, 8)
            layout.Add(add, 0, wx.ALL, 8)
            layout.Add(wx.StaticText(self, label='Saved import batches'), 0, wx.LEFT | wx.RIGHT, 8)
            self.batches = wx.ListBox(self, size=(-1, 65))
            self.batches.SetName('Saved import batches')
            self.batches.Bind(wx.EVT_LISTBOX, self.select_batch)
            self.mutable.append(self.batches)
            layout.Add(self.batches, 1, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(self, label='Files in the selected batch'), 0, wx.LEFT | wx.RIGHT, 8)
            self.rows = wx.ListBox(self, size=(-1, 100))
            self.rows.SetName('Files and import status')
            self.rows.Bind(wx.EVT_LISTBOX, self.select_row)
            layout.Add(self.rows, 2, wx.EXPAND | wx.ALL, 8)
            self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 70))
            self.details.SetName('Selected file details')
            layout.Add(self.details, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            actions = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            actions.AddGrowableCol(0)
            actions.AddGrowableCol(1)
            for label, handler in [('&Start queued files', self.run),
                    ('&Retry failed or reviewed files', lambda event: self.run(event, retry=True)),
                    ('&Exclude or include selected file', self.toggle_row), ('Save re&port...', self.save_report)]:
                control = wx.Button(self, label=label)
                control.Bind(wx.EVT_BUTTON, handler)
                self.mutable.append(control)
                actions.Add(control, 0, wx.EXPAND)
            layout.Add(actions, 0, wx.EXPAND | wx.ALL, 8)
            self.recovery_button = wx.Button(self, label='Review restored &imports…')
            self.recovery_button.SetName('Review restored imports and captured source permissions')
            self.recovery_button.Bind(wx.EVT_BUTTON, self.review_restored_imports)
            self.mutable.append(self.recovery_button)
            layout.Add(self.recovery_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 45))
            self.status.SetName('Import Center status')
            layout.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            bottom = wx.BoxSizer(wx.HORIZONTAL)
            self.cancel = wx.Button(self, label='&Cancel operation')
            self.cancel.Disable()
            self.cancel.Bind(wx.EVT_BUTTON, self.cancel_operation)
            bottom.Add(self.cancel, 0, wx.RIGHT, 8)
            close = wx.Button(self, wx.ID_CANCEL, 'Cl&ose')
            close.Bind(wx.EVT_BUTTON, self.close)
            bottom.Add(close)
            layout.Add(bottom, 0, wx.ALL, 8)
            self.SetSizer(layout)
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
