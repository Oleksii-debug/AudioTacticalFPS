"""Native, read-only library reports and checked exports.

Application services own the authorized snapshots and report/export limits.
This dialog captures one library and workspace, runs work through the desktop
job runner, and never publishes a partial report or a stale-scope export.
"""
from pathlib import Path

import wx

from ..research.export_file import write_export


REPORT_MAX_CHARACTERS = 500_000
BIBLIOGRAPHY_FORMATS = (
    ('ris', 'RIS', '.ris'),
    ('bibtex', 'BibTeX', '.bib'),
    ('csl-json', 'CSL JSON', '.json'),
)


def _workspace(frame):
    current = getattr(frame, 'current_workspace', None)
    identity = (current() if callable(current) else current) if current is not None else frame._workspace()
    if not isinstance(identity, str) or not identity:
        raise ValueError('Open a library first.')
    return identity


def _collection_choices(rows):
    choices = tuple(rows)
    identifiers = set()
    for row in choices:
        if (not isinstance(row, (tuple, list)) or len(row) != 2 or
                not all(isinstance(value, str) and value.strip() for value in row) or
                row[0] in identifiers):
            raise ValueError('Collection choices changed. Refresh the library and try again.')
        identifiers.add(row[0])
    return choices


class LibraryToolsDialog(wx.Dialog):
    def __init__(self, frame, workspace, collections, *, collection=None):
        super().__init__(frame, title='Library tools', size=(800, 760))
        self.frame, self.library, self.workspace = frame, frame.library, workspace
        self.private_root = Path(self.library.root)
        self.pending = False
        self._closed = False
        self._close_when_idle = False
        self._request = 0
        rows = _collection_choices(collections)
        self.scope_ids = [None, *(row[0] for row in rows)]
        if collection not in self.scope_ids:
            raise ValueError('The selected collection is no longer active. Open Library tools again.')
        self._collection = collection

        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self, label=(
            'Review metadata and indexing readiness for this library or one collection.\n'
            'Bibliography and inventory exports describe sources. The review matrix contains\n'
            'verified saved evidence and separate research notes. Duplicate warnings do not merge books.'))
        layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label='Report and export scope'), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.scope = wx.Choice(self, choices=['Entire current library', *(f'Collection: {name}' for _, name in rows)])
        self.scope.SetName('Report and export scope')
        self.scope.SetSelection(self.scope_ids.index(collection))
        self.scope.Bind(wx.EVT_CHOICE, self.on_scope_changed)
        layout.Add(self.scope, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.report = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.report.SetName('Library overview and quality report')
        self.report.SetMaxLength(REPORT_MAX_CHARACTERS)
        layout.Add(self.report, 1, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label='Bibliography export format'), 0, wx.LEFT | wx.RIGHT, 8)
        self.format = wx.Choice(self, choices=[f'{name} ({extension})' for _, name, extension in BIBLIOGRAPHY_FORMATS])
        self.format.SetName('Bibliography export format')
        self.format.SetSelection(2)
        layout.Add(self.format, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.refresh_button = wx.Button(self, label='&Refresh report')
        self.bibliography_button = wx.Button(self, label='Export &bibliography…')
        self.inventory_button = wx.Button(self, label='Export source &inventory…')
        self.matrix_button = wx.Button(self, label='Export review &matrix…')
        actions = ((self.refresh_button, self.refresh_report),
                   (self.bibliography_button, self.export_bibliography),
                   (self.inventory_button, self.export_inventory),
                   (self.matrix_button, self.export_matrix))
        for button, handler in actions:
            button.Bind(wx.EVT_BUTTON, handler)
            layout.Add(button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.status = wx.TextCtrl(self, value='Choose a scope, then refresh the report or export.',
                                  style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Library tools status')
        self.status.SetMinSize((-1, 64))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.cancel_button = wx.Button(self, label='Cancel &operation')
        self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
        self.cancel_button.Bind(wx.EVT_BUTTON, self.cancel_operation)
        self.close_button.Bind(wx.EVT_BUTTON, self.close)
        buttons.Add(self.cancel_button, 0, wx.ALL, 8)
        buttons.Add(self.close_button, 0, wx.ALL, 8)
        layout.Add(buttons, 0, wx.ALIGN_RIGHT)
        self.SetSizer(layout)
        self.SetMinSize((650, 650))
        self.SetEscapeId(wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self._enabled(True)
        self.scope.SetFocus()

    def _same_host(self):
        try:
            return (self.frame.library is self.library and _workspace(self.frame) == self.workspace
                    and not getattr(self.frame, '_closing', False))
        except (AttributeError, ValueError, RuntimeError):
            return False

    def _selected_collection(self):
        index = self.scope.GetSelection()
        if not 0 <= index < len(self.scope_ids):
            raise ValueError('Choose the library or one active collection.')
        return self.scope_ids[index]

    def _enabled(self, enabled):
        for control in (self.scope, self.format, self.refresh_button, self.bibliography_button,
                        self.inventory_button, self.matrix_button, self.close_button):
            control.Enable(enabled)
        self.cancel_button.Enable(not enabled)

    def _message(self, text):
        self.status.SetValue(text)
        self.status.SetFocus()

    def _available(self):
        if self._closed:
            return False
        if self.pending or self.frame.working or getattr(self.frame, 'busy', False):
            self._message('An operation is already running. Wait or choose Cancel operation.')
            return False
        if not self._same_host():
            self.report.SetValue('')
            self._message('The selected library changed. Close Library tools and open it again.')
            return False
        return True

    def on_scope_changed(self, event=None):
        try:
            collection = self._selected_collection()
        except ValueError as exc:
            self._message(str(exc))
            return
        if collection != self._collection:
            self._collection = collection
            self.report.SetValue('')
            if self.pending:
                self.frame.cancel_event.set()
            self._message('Scope changed. Refresh the report to read this scope.')

    def _start(self, title, collection, operation, complete):
        if not self._available():
            return False
        self._request += 1
        request = self._request
        self._collection = collection
        self.pending = True
        self._enabled(False)
        self._message(title + '…')

        def cancelled():
            return (self._closed or self.frame.cancel_event.is_set() or not self._same_host()
                    or request != self._request or self._collection != collection)

        def work():
            if cancelled():
                raise TimeoutError('Operation cancelled before completion.')
            return operation(cancelled)

        def finish(result=None, error=None):
            if self._closed or request != self._request:
                return
            self.pending = False
            self._enabled(True)
            if not self._same_host() or self._collection != collection:
                self.report.SetValue('')
                self._message('The selected library or scope changed. Refresh before another operation.')
            elif error is not None:
                self._message(f'{title} was not completed: {error}')
            else:
                try:
                    complete(result)
                except Exception as exc:
                    self._message(f'{title} was not completed: {exc}')
            if self._close_when_idle:
                self._closed = True
                self.EndModal(wx.ID_CANCEL)

        try:
            self.frame.run_job(title, work, lambda value: finish(result=value),
                               lambda error: finish(error=error))
            if self.pending and not self.frame.working:
                finish(error=RuntimeError('The operation could not be started. Try again.'))
        except Exception as exc:
            finish(error=exc)
            return False
        return True

    def refresh_report(self, event=None):
        if not self._available():
            return False
        try:
            collection = self._selected_collection()
        except ValueError as exc:
            self._message(str(exc))
            return False

        def generate(cancelled):
            value = self.library.catalog_report(self.workspace, collection=collection, cancelled=cancelled)
            if cancelled():
                raise TimeoutError('Report cancelled.')
            if not isinstance(value, str):
                raise ValueError('The report service did not return readable text.')
            if len(value) > REPORT_MAX_CHARACTERS:
                raise ValueError('The complete report exceeds 500,000 characters. Choose a smaller collection.')
            return value

        def show(value):
            self.report.SetValue(value)
            if self.report.GetValue().replace('\r\n', '\n') != value.replace('\r\n', '\n'):
                self.report.SetValue('')
                raise ValueError('The complete report could not be displayed. Choose a smaller collection.')
            self.status.SetValue('Complete report loaded for the selected scope.')
            self.report.SetInsertionPoint(0)
            self.report.SetFocus()

        self.report.SetValue('')
        return self._start('Refreshing library report', collection, generate, show)

    def _export(self, kind, format, name, extension, button):
        if not self._available():
            return False
        dialog = None
        try:
            collection = self._selected_collection()
            self._collection = collection
            dialog = wx.FileDialog(self, f'Export {name}', defaultFile=f'{kind}{extension}',
                wildcard=f'{name} (*{extension})|*{extension}',
                style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
            if dialog.ShowModal() != wx.ID_OK:
                self.status.SetValue('Export cancelled. No file was written.')
                button.SetFocus()
                return False
            path = Path(dialog.GetPath())
        except Exception as exc:
            self._message(f'Export was not started: {exc}')
            return False
        finally:
            if dialog is not None:
                dialog.Destroy()
        try:
            same_scope = self._same_host() and self._selected_collection() == collection
        except ValueError:
            same_scope = False
        if not same_scope:
            self._message('The selected library or scope changed. Choose the export destination again.')
            return False

        def publish(cancelled):
            if kind == 'review-matrix':
                value = self.library.export_review_matrix(self.workspace, collection=collection, cancelled=cancelled)
            else:
                value = self.library.export_catalog(self.workspace, collection=collection,
                                                     format=format, cancelled=cancelled)
            if cancelled():
                raise TimeoutError('Export cancelled; the destination was not changed.')
            write_export(path, value, private_root=self.private_root, cancelled=cancelled)
            return path

        def exported(destination):
            detail = ('Verified quotations and research notes are in separate columns.' if kind == 'review-matrix'
                      else 'This file contains source metadata; it does not include saved quotations or notes.')
            self._message(f'{name} exported to {destination}.\n{detail}')

        return self._start(f'Exporting {name}', collection, publish, exported)

    def export_bibliography(self, event=None):
        index = self.format.GetSelection()
        if not 0 <= index < len(BIBLIOGRAPHY_FORMATS):
            self._message('Choose RIS, BibTeX or CSL JSON for the bibliography.')
            return False
        format, label, extension = BIBLIOGRAPHY_FORMATS[index]
        return self._export('source-bibliography', format, f'{label} bibliography', extension,
                            self.bibliography_button)

    def export_inventory(self, event=None):
        return self._export('source-inventory', 'csv', 'Source inventory', '.csv', self.inventory_button)

    def export_matrix(self, event=None):
        return self._export('review-matrix', 'csv', 'Literature-review matrix', '.csv', self.matrix_button)

    def cancel_operation(self, event=None):
        if self.pending:
            self.frame.cancel_event.set()
            self._message('Cancellation requested. Waiting for the running operation to finish.')

    def close(self, event=None):
        if self.pending:
            self._close_when_idle = True
            self.cancel_operation()
            return
        self._closed = True
        self.EndModal(wx.ID_CANCEL)

    def on_close(self, event):
        if self.pending and event.CanVeto():
            event.Veto()
            self.close()
            return
        self.dispose()
        event.Skip()

    def dispose(self):
        self._closed = True
        if self.pending:
            self.frame.cancel_event.set()


def show_catalog_tools(frame):
    """Open one workspace's tools with fresh active collection choices."""
    dialog = None
    library = workspace = None
    try:
        if frame.working or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
            raise ValueError('Wait for the current operation before opening Library tools.')
        workspace, library = _workspace(frame), frame.library
        collections = _collection_choices(library.collections(workspace))
        if frame.library is not library or _workspace(frame) != workspace:
            raise ValueError('The selected library changed. Open Library tools again.')
        collection = None
        if frame.book is not None:
            panel = frame.book.GetPage(1)
            if panel.workspace == workspace and panel.ready:
                selected = panel._selection()[0]
                if selected in {identity for identity, _ in collections}:
                    collection = selected
        dialog = LibraryToolsDialog(frame, workspace, collections, collection=collection)
        wx.CallAfter(dialog.refresh_report)
        dialog.ShowModal()
    except Exception as exc:
        frame.report(f'Library tools could not be opened: {exc}')
    finally:
        if dialog is not None:
            dialog.dispose()
            dialog.Destroy()
        if (library is not None and frame.library is library and not frame.working and
                not getattr(frame, '_closing', False)):
            try:
                if _workspace(frame) == workspace:
                    frame.status.SetFocus()
            except (AttributeError, ValueError, RuntimeError):
                pass
