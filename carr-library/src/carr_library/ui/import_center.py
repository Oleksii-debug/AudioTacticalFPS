"""Accessible, persistent file-import plans in the existing desktop host."""
import unicodedata
from pathlib import Path
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
    if batch.get('version') == 3 and 'source_profile' in batch:
        from ..ingest.streamed_text import profile_from_value
        from .import_resources import profile_summary
        profile = profile_from_value(batch['source_profile'])
        if profile.import_budget is not None:
            summary += ' ' + profile_summary(profile.import_budget)
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
    def _ui_alive(self):
        return (not self.closed and getattr(self, '_native_alive', True)
                and not getattr(self.frame, '_closing', False))

    def _dispose(self):
        if self.closed and not getattr(self, '_native_alive', True):
            return
        self._native_alive = False
        self.closed = True
        if self.busy and getattr(self, '_job_token', None) is not None and self.frame.working:
            self.frame.cancel_event.set()

    def _bound(self):
        if (self.closed or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library):
            raise ValueError('The library folder changed. Reopen Import Center.')
        with self.library.read(self.workspace):
            pass

    def report(self, text, *, focus=True):
        if not self._ui_alive():
            return
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
        if not self._ui_alive():
            return
        pending = getattr(self, '_cleanup_owner', None)
        if pending is not None:
            if not pending.quiescent or not pending.notifications_complete_event.is_set():
                self.report('Native import cleanup is still pending. Wait for the same owner before starting another operation.')
                return
            self._cleanup_owner = None
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
            if self._job_token is not token:
                return
            # Keep the exact token through the existing report approval's
            # second worker phase; later jobs replace it before dispatch.
            self.busy = False
            from ..audio.cleanup import AudioCleanupPending
            if isinstance(error, AudioCleanupPending):
                self._cleanup_owner = error.cleanup_owner
            if not self._ui_alive():
                return
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

        def work():
            # Check only captured Python ownership on the worker thread.
            # Returning a saved plan/result is not revoked by a later UI close.
            self._bound()
            if self._job_token is not token:
                raise InterruptedError('The import window no longer owns this work.')
            return operation()

        try:
            self.frame.run_job(title, work, lambda value: finish(value),
                               failed=lambda exc: finish(error=exc))
            if self.busy and not self.frame.working:
                finish(error=RuntimeError('The Desktop queue did not admit this import operation. Retry explicitly.'))
        except (Exception, KeyboardInterrupt) as exc:
            finish(error=exc)

    def _scan_options(self):
        from ..ingest.batch_scan import scan_page_limits
        return scan_page_limits(max_files=int(self.page_files.GetValue()),
            max_entries=int(self.page_entries.GetValue()), timeout=float(self.page_seconds.GetValue()))

    def plan(self, paths):
        self._bound()
        selected = tuple(paths)
        limits = self._scan_options()
        from ..ingest.resource_budget import checked_budget
        import_budget = checked_budget(getattr(self, 'import_budget', None))
        index = self.collection.GetSelection()
        if not 0 <= index < len(self.collection_ids):
            raise ValueError('Choose a destination collection and text recognition languages.')
        from dataclasses import replace
        from ..extract.page_ocr import Config
        if not isinstance(self.ocr_config, Config):
            raise ValueError('Choose text recognition languages for this source page.')
        config = replace(self.ocr_config)
        collections = () if self.collection_ids[index] is None else (self.collection_ids[index],)
        recursive = self.recursive.GetValue()

        def done(batch):
            self.refresh(selected=batch['batch_id'])
            self.report('Plan saved. ' + batch_summary(batch) +
                        ' Review the files, exclude any you do not want, then choose Start queued files.')

        def operation():
            self._bound()
            return self.library.plan_import_page(self.workspace, selected, collections=collections,
                recursive=recursive, config=config, cancelled=self.frame.cancel_event.is_set,
                import_budget=import_budget, **limits)
        self._job('Checking the next source page and duplicates', operation, done, planning=True)

    def continue_scan(self, event=None):
        try:
            self._bound()
            if self.batch is None or self.batch.get('version') not in (2, 3):
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

    def choose_resources(self, event=None):
        from .import_resources import choose_import_resources, profile_summary
        if self.busy or self.frame.working:
            self.report('Wait for the current operation before choosing new import allowances.')
            return
        try:
            self._bound()
            accepted, chosen = choose_import_resources(self, getattr(self, 'import_budget', None),
                current=lambda: self._ui_alive() and self.frame.library is self.library)
            self._bound()
            if self.busy or self.frame.working:
                raise ValueError('An import operation started. Review resource allowances after it finishes.')
            if accepted:
                self.import_budget = chosen
                self.resource_summary.SetValue(profile_summary(chosen))
                self.report('Future import plans will use these reviewed allowances. ' + profile_summary(chosen))
        except (Exception, KeyboardInterrupt) as exc:
            self.report(str(exc))

    def choose_languages(self, event=None):
        from ..extract.page_ocr import Config
        from .ocr_options import choose_model_config, config_summary
        try:
            self._bound()
            if self.busy or self.frame.working:
                raise ValueError('Wait for the current operation before changing recognition models.')
            config = choose_model_config(self, self.ocr_config, preset_root=self.library.root)
            if config is None:
                return
            if not isinstance(config, Config):
                raise ValueError('The OCR model selection did not return valid import settings.')
            self._bound()
            if self.busy or self.frame.working:
                raise ValueError('An import operation started. Choose model settings when it finishes.')
            self.ocr_config = config
            self.languages.SetValue(config_summary(config))
            self.report('New plans will use ' + config_summary(config) +
                        '. Existing saved plans keep their previous settings.')
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

    def import_streamed_text(self, path):
        """Explicit single-source action with a frozen legacy or reviewed policy."""
        from dataclasses import replace
        from ..ingest.streamed_text import TextProfile
        self._bound()
        maximum = self.stream_mib.GetValue()
        if type(maximum) is not int or not 1 <= maximum <= 128:
            raise ValueError('Choose a streamed source allowance from 1 through 128 MiB.')
        index = self.collection.GetSelection()
        if not 0 <= index < len(self.collection_ids):
            raise ValueError('Choose the destination for this source.')
        collections = () if self.collection_ids[index] is None else (self.collection_ids[index],)
        from ..ingest.resource_budget import checked_budget
        policy = checked_budget(getattr(self, 'import_budget', None))
        profile = TextProfile(maximum * 1024**2) if policy is None else TextProfile(policy.source_bytes, policy)
        config = replace(self.ocr_config)
        library, workspace = self.library, self.workspace
        def operation():
            self._bound()
            return library.import_file(workspace, path, source_profile=profile,
                collections=collections, config=config, timeout=180 if policy is None else None,
                cancelled=self.frame.cancel_event.is_set)
        def done(result):
            if result.get('state') == 'complete':
                if self.frame.book is not None:
                    self.frame.book.GetPage(1).refresh()
                self.report(f'Streamed text imported: {result["completed"]} exact logical fragments. '
                            'Original bytes are retained; these locations are unpaginated.')
            else:
                self.report('Streamed text request: ' + readable(result.get('state', 'failed')) + '. '
                    + readable(result.get('message', '')) +
                    ' Close Import Center and choose Library, Resume interrupted import. '
                    'The saved request keeps its original byte allowance.')
        self._job('Importing selected streamed UTF-8 text', operation, done)

    def choose_streamed_text(self, event=None):
        import wx
        try:
            self._bound()
            if self.busy or self.frame.working:
                raise ValueError('Wait or cancel the current operation before selecting another source.')
            dialog = wx.FileDialog(self, 'Import a selected large UTF-8 text',
                wildcard='UTF-8 text (*.txt)|*.txt', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                path = dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()
            if path:
                self.import_streamed_text(path)
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
        from ..research.export_file import capture_export_destination, export_completion, write_export
        try:
            self._bound()
            if self.busy or self.frame.working:
                raise ValueError('Wait for the current operation before exporting its report.')
            if self.batch is None:
                raise ValueError('Select a saved import batch first.')
            library, workspace, batch_id = self.library, self.workspace, self.batch['batch_id']
            dialog = wx.FileDialog(self, 'Save import report', defaultFile='Import-report.txt',
                wildcard='Text report (*.txt)|*.txt', style=wx.FD_SAVE)
            try:
                path = dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()
            if not path:
                return
            self._bound()
            if self.library is not library or self.workspace != workspace or self.batch is None or self.batch['batch_id'] != batch_id:
                raise ValueError('The selected import batch changed. Choose its report again.')
            token = object()

            def cancelled():
                return (self.closed or getattr(self.frame, '_closing', False)
                    or self.frame.cancel_event.is_set() or self._job_token is not token
                    or self.library is not library or self.frame.library is not library
                    or self.workspace != workspace or (self.batch or {}).get('batch_id') != batch_id)

            def current():
                if cancelled():
                    raise InterruptedError('Import report export cancelled; the destination was not changed.')
                self._bound()

            def capture():
                current()
                library.import_batch(workspace, batch_id)
                return capture_export_destination(path, private_root=library.root, cancelled=cancelled)

            def captured(approval):
                current()
                if not approval.require_absent:
                    decision = wx.MessageBox(
                        f'Replace the existing export {Path(path).name}?', 'Replace import report',
                        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self)
                    if self.closed:
                        return
                    current()
                    if decision != wx.YES:
                        self.report('Import report export cancelled.')
                        return
                current()

                def publish():
                    current()
                    batch = library.import_batch(workspace, batch_id)
                    content = batch_report(batch)
                    current()
                    return write_export(path, content, private_root=library.root,
                                        cancelled=cancelled, approval=approval)

                self._job('Saving import report', publish,
                          lambda receipt: self.report(export_completion('Import report saved.', receipt)),
                          token=token)

            self._job('Checking report destination', capture, captured, token=token)
        except Exception as exc:
            if not self.closed:
                self.report(str(exc))

    def cancel_operation(self, event=None):
        if self.busy:
            self.frame.cancel_event.set()
            self.report('Stopping the source scan. A new page is not saved until the current operation finishes; '
                        'completed review rows and their exact continuation will be retained if admitted.'
                        if getattr(self, 'planning', False) else
                        'Stopping the current operation. Queued files will remain saved.')

    def close(self, event=None):
        if not self._ui_alive():
            return
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
    from ..extract.page_ocr import Config
    from .ocr_options import config_summary

    class ImportCenter(wx.Dialog, ImportCenterActions):
        def __init__(self):
            super().__init__(frame, title='Import Center', size=(920, 760),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.frame, self.library, self.workspace = frame, frame.library, workspace
            self.busy = self.closed = False
            self._native_alive = True
            self._native_id = self.GetId()
            self._cleanup_owner = None
            self.import_budget = None
            self.batch, self.summaries = None, ()
            self.mutable = []
            outer = wx.BoxSizer(wx.VERTICAL)
            self.form = ScrolledPanel(self, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
            self.form.SetName('Import Center scrollable form')
            self.form.SetMinSize((0, 200))
            layout = wx.BoxSizer(wx.VERTICAL)
            introduction = wx.StaticText(self.form, label='Prepare a file or folder import, review the plan, then start. '
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
            self.collection = wx.Choice(self.form, choices=collection_labels)
            self.collection.SetSelection(0)
            self.ocr_config = Config()
            self.languages = wx.TextCtrl(self.form, value=config_summary(self.ocr_config),
                                         style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 40))
            for label, control in [('Destination for new plans', self.collection),
                                   ('Text recognition languages for new plans', self.languages)]:
                control.SetName(label)
                options.Add(wx.StaticText(self.form, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                options.Add(control, 1, wx.EXPAND)
            self.mutable.append(self.collection)
            layout.Add(options, 0, wx.EXPAND | wx.ALL, 8)
            self.page_files = wx.SpinCtrl(self.form, min=1, max=5000, initial=1000)
            self.page_entries = wx.SpinCtrl(self.form, min=1, max=20000, initial=20000)
            self.page_seconds = wx.SpinCtrl(self.form, min=1, max=600, initial=60)
            limits = wx.FlexGridSizer(cols=3, hgap=8, vgap=4)
            controls = [('Files per scan page', self.page_files),
                        ('Traversal entries per scan page', self.page_entries),
                        ('Seconds per source scan page', self.page_seconds)]
            for index, (label, _control) in enumerate(controls):
                limits.AddGrowableCol(index)
                limits.Add(wx.StaticText(self.form, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            for label, control in controls:
                control.SetName(label)
                limits.Add(control, 1, wx.EXPAND)
                self.mutable.append(control)
            layout.Add(limits, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            from .import_resources import profile_summary
            resources = wx.Button(self.form, label='Review import &resource allowances…')
            resources.SetName('Review import resource allowances for new plans')
            resources.Bind(wx.EVT_BUTTON, self.choose_resources)
            self.mutable.append(resources)
            layout.Add(resources, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.resource_summary = wx.TextCtrl(self.form, value=profile_summary(),
                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 55))
            self.resource_summary.SetName('Resource allowances for future import plans')
            layout.Add(self.resource_summary, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            models = wx.Button(self.form, label='Choose installed or managed OCR &models...')
            models.SetName('Choose installed or managed OCR models for new plans')
            models.Bind(wx.EVT_BUTTON, self.choose_languages)
            self.mutable.append(models)
            layout.Add(models, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.recursive = wx.CheckBox(self.form, label='Include subfol&ders in new plans')
            self.recursive.SetValue(True)
            self.mutable.append(self.recursive)
            layout.Add(self.recursive, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            add = wx.BoxSizer(wx.HORIZONTAL)
            for label, handler in [('&Add files...', self.add_files), ('Add &folder...', self.add_folder),
                                   ('Continue source &scan', self.continue_scan),
                                   ('Refresh saved &queues', self.refresh)]:
                control = wx.Button(self.form, label=label)
                control.Bind(wx.EVT_BUTTON, handler)
                self.mutable.append(control)
                add.Add(control, 0, wx.RIGHT, 8)
            layout.Add(add, 0, wx.ALL, 8)
            streamed = wx.BoxSizer(wx.HORIZONTAL)
            self.stream_mib = wx.SpinCtrl(self.form, min=1, max=128, initial=128)
            self.stream_mib.SetName('Default streamed UTF-8 size in MiB; reviewed resource allowances take precedence')
            streamed.Add(wx.StaticText(self.form, label='Default streamed TXT limit (MiB; reviewed allowances take precedence)'),
                         0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            streamed.Add(self.stream_mib, 0, wx.RIGHT, 8)
            large_text = wx.Button(self.form, label='Import large UTF-8 te&xt...')
            large_text.SetName('Import selected UTF-8 text in bounded logical fragments')
            large_text.Bind(wx.EVT_BUTTON, self.choose_streamed_text)
            streamed.Add(large_text, 0)
            self.mutable.extend((self.stream_mib, large_text))
            layout.Add(streamed, 0, wx.ALL, 8)
            layout.Add(wx.StaticText(self.form, label='Saved import batches'), 0, wx.LEFT | wx.RIGHT, 8)
            self.batches = wx.ListBox(self.form, size=(-1, 65))
            self.batches.SetName('Saved import batches')
            self.batches.Bind(wx.EVT_LISTBOX, self.select_batch)
            self.mutable.append(self.batches)
            layout.Add(self.batches, 1, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(self.form, label='Files in the selected batch'), 0, wx.LEFT | wx.RIGHT, 8)
            self.rows = wx.ListBox(self.form, size=(-1, 100))
            self.rows.SetName('Files and import status')
            self.rows.Bind(wx.EVT_LISTBOX, self.select_row)
            layout.Add(self.rows, 2, wx.EXPAND | wx.ALL, 8)
            self.details = wx.TextCtrl(self.form, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 70))
            self.details.SetName('Selected file details')
            layout.Add(self.details, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            actions = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            actions.AddGrowableCol(0)
            actions.AddGrowableCol(1)
            for label, handler in [('&Start queued files', self.run),
                    ('&Retry failed or reviewed files', lambda event: self.run(event, retry=True)),
                    ('&Exclude or include selected file', self.toggle_row), ('Save re&port...', self.save_report)]:
                control = wx.Button(self.form, label=label)
                control.Bind(wx.EVT_BUTTON, handler)
                self.mutable.append(control)
                actions.Add(control, 0, wx.EXPAND)
            layout.Add(actions, 0, wx.EXPAND | wx.ALL, 8)
            self.form.SetSizer(layout)
            self.form.SetupScrolling(scroll_x=False, scroll_y=True, scrollIntoView=True)
            outer.Add(self.form, 1, wx.EXPAND)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 45))
            self.status.SetName('Import Center status')
            outer.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            bottom = wx.BoxSizer(wx.HORIZONTAL)
            self.cancel = wx.Button(self, label='&Cancel operation')
            self.cancel.Disable()
            self.cancel.Bind(wx.EVT_BUTTON, self.cancel_operation)
            bottom.Add(self.cancel, 0, wx.RIGHT, 8)
            close = wx.Button(self, wx.ID_CANCEL, 'Cl&ose')
            close.Bind(wx.EVT_BUTTON, self.close)
            bottom.Add(close)
            outer.Add(bottom, 0, wx.ALL, 8)
            self.SetSizer(outer)
            self.SetMinSize((800, 650))
            self.Bind(wx.EVT_CLOSE, self.close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
            self.refresh()
            self.batches.SetFocus() if self.summaries else self.collection.SetFocus()

        def _destroyed(self, event):
            if event.GetId() == self._native_id:
                self._destroy_observed = True
                self._dispose()
            event.Skip()

        def Destroy(self):
            self._dispose()
            return super().Destroy()

    dialog = ImportCenter()
    try:
        dialog.ShowModal()
    finally:
        dialog._dispose()
        if not getattr(dialog, '_destroy_observed', False):
            try:
                if not dialog.IsBeingDeleted():
                    dialog.Destroy()
            except RuntimeError:
                pass
