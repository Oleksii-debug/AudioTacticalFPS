"""Candidate wx panel; host binds approved scoped search and reader services."""
import wx
from .search_model import SearchModel
from ._vendor.wx_grid.grid import AccessibleGrid


class SearchPanel(wx.Panel):
    """search(workspace, query, retrieval_mode, result_mode, limit) -> Hit iterable.
    more(workspace, ref, revision) -> verified context string (or raises).
    verified_copy(workspace, ref, revision, field) -> re-resolved exact string (or raises).
    No indexing, citation formatting, or workspace data access in this widget.
    """
    def __init__(self, parent, search, more, *, verified_copy, save_evidence=None, preview_search=None):
        super().__init__(parent)
        self.search_service, self.more_service = search, more
        if not callable(verified_copy):
            raise TypeError("verified_copy service required")
        self.verified_copy_service = verified_copy
        if save_evidence is not None and not callable(save_evidence):
            raise TypeError('save evidence service must be callable')
        self.save_evidence_service = save_evidence
        if preview_search is not None and not callable(preview_search):
            raise TypeError('preview search service must be callable')
        self.preview_search_service = preview_search
        self.model = SearchModel()
        layout = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(layout)

        def add(label, ctrl):
            layout.Add(wx.StaticText(self, label=label), 0, wx.ALL, 3)
            ctrl.SetName(label)
            layout.Add(ctrl, 0, wx.EXPAND | wx.ALL, 3)
            return ctrl

        self.query = add('Search words or phrase', wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER))
        self.retrieval = add('Search mode', wx.Choice(self, choices=['Literal', 'Thematic']))
        self.retrieval.SetSelection(0)
        self.result_mode = add('Result mode', wx.Choice(self, choices=['Short quotations', 'Thematic excerpts']))
        self.result_mode.SetSelection(0)
        self.source_mode = None
        if preview_search is not None:
            self.source_mode = add('Sources', wx.Choice(self, choices=[
                'Verified sources', 'Include unverified PDF previews']))
            self.source_mode.SetSelection(0)
            self.source_mode.Bind(wx.EVT_CHOICE, self.on_source_mode)
        self.go = wx.Button(self, label='&Search')
        layout.Add(self.go, 0, wx.ALL, 3)
        self.grid = AccessibleGrid(self, self.model, label='Search results')
        layout.Add(self.grid.control, 1, wx.EXPAND | wx.ALL, 3)
        self.details = add('Selected result details', wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY))
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.verified_actions = []
        self.more_button = None
        for label, handler, verified in [('&More', self.on_more, False), ('Copy &quote', self.on_quote, True),
                                         ('Copy &citation', self.on_citation, True)]:
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.ALL, 3)
            if verified:
                self.verified_actions.append(button)
            else:
                self.more_button = button
        if save_evidence is not None:
            button = wx.Button(self, label='&Save to selected topic')
            button.Bind(wx.EVT_BUTTON, self.on_save_evidence)
            buttons.Add(button, 0, wx.ALL, 3)
            self.verified_actions.append(button)
        layout.Add(buttons, 0, wx.EXPAND)
        self.status = add('Search status', wx.TextCtrl(self, style=wx.TE_READONLY))
        self.go.Bind(wx.EVT_BUTTON, self.on_search)
        self.query.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        import wx.dataview as dv
        self.grid.control.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self.on_selection)
        self.grid.control.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.on_more)
        self.set_workspace(None)

    def set_workspace(self, workspace):
        self.model.set_workspace(workspace)
        self.grid.refresh()
        self.details.ChangeValue('')
        self.status.ChangeValue('Ready to search' if workspace else 'Select a library first')
        self.go.Enable(bool(workspace))
        self._set_result_actions()

    def selected(self):
        rows = self.grid.selected_rows()
        if len(rows) != 1:
            raise ValueError('Select exactly one result')
        return self.model.hit(rows[0])

    def _refresh_details(self):
        """Keep the readable details in sync even without a native selection event."""
        try:
            hit = self.selected()
            self.details.ChangeValue(
                f'Title: {hit.title}\nAuthor: {hit.author}\nEdition: {hit.edition}\nPage: {hit.page}'
                f'\nVerification: {hit.verification_status}\nCitation: {hit.citation}\n\nExcerpt:\n{hit.quote}')
            self._set_result_actions(hit)
        except ValueError:
            self.details.ChangeValue('')
            self._set_result_actions()

    def _set_result_actions(self, hit=None):
        """A preview remains readable but cannot become verified evidence through the UI."""
        selected = hit is not None and hit.workspace == self.model.workspace
        verified = selected and hit.citation_verified is True
        for button in getattr(self, 'verified_actions', ()):
            button.Enable(verified)
        more = getattr(self, 'more_button', None)
        if more is not None:
            more.Enable(selected)

    def on_selection(self, event):
        self._refresh_details()
        event.Skip()

    def on_source_mode(self, event):
        self.model.clear()
        self.grid.refresh()
        self.details.ChangeValue('')
        self._set_result_actions()
        self.status.ChangeValue('Source filter changed. Run the search again.')

    def on_search(self, event):
        scope = self.model.workspace
        self.model.clear()
        self.grid.refresh()
        self.details.ChangeValue('')
        self._set_result_actions()
        query = self.query.GetValue().strip()
        if not scope or not query:
            self.status.ChangeValue('Select a library and enter a query')
            self.query.SetFocus()
            return
        try:
            service = self.search_service
            source_mode = getattr(self, 'source_mode', None)
            if source_mode is not None:
                selection = source_mode.GetSelection()
                if selection not in (0, 1):
                    raise ValueError('invalid source filter')
                if selection == 1:
                    service = self.preview_search_service
            if not callable(service):
                raise ValueError('selected search service unavailable')
            hits = service(scope, query,
                ('literal', 'thematic')[self.retrieval.GetSelection()],
                ('short', 'thematic')[self.result_mode.GetSelection()], 100)
            self.model.replace(scope, hits)
            self.grid.refresh()
        except Exception:
            self.model.clear()
            self.grid.refresh()
            self.status.ChangeValue('Search failed. No results retained.')
            self.status.SetFocus()
            return
        count = self.model.row_count()
        self.status.ChangeValue(f'{count} results' if count else 'No results. Import books or change the query.')
        if count:
            self.grid.select_rows([0])
            self.grid.focus_row(0)
            self._refresh_details()
        else:
            self.status.SetFocus()

    def on_more(self, event):
        try:
            hit = self.selected()
            row = self.grid.selected_rows()[0]
            context = self.more_service(hit.workspace, hit.ref, hit.revision)
            if not isinstance(context, str) or not context:
                raise ValueError('Missing source context')
        except Exception:
            self.status.ChangeValue('Context unavailable or selection invalid')
            self.status.SetFocus()
            return
        dialog = wx.Dialog(self, title='Original source context', size=(760, 550))
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            reader = wx.TextCtrl(dialog, value=hit.citation + '\n\n' + context,
                                 style=wx.TE_READONLY | wx.TE_MULTILINE)
            reader.SetName('Original source context')
            layout.Add(reader, 1, wx.EXPAND | wx.ALL, 8)
            back = wx.Button(dialog, wx.ID_CANCEL, '&Back')
            layout.Add(back, 0, wx.ALL, 8)
            dialog.SetSizer(layout)
            reader.SetInsertionPoint(0)
            reader.SetFocus()
            dialog.ShowModal()
        finally:
            dialog.Destroy()
            self.grid.select_rows([row])
            self.grid.focus_row(row)
            self._refresh_details()

    def copy(self, field):
        try:
            hit = self.selected()
            if field not in ('quote', 'citation') or not hit.workspace or hit.workspace != self.model.workspace:
                raise ValueError('Invalid copy scope or field')
            if hit.citation_verified is not True:
                self.status.ChangeValue('Unverified extraction preview. Review the source before copying a quotation or citation. Nothing copied.')
                self.status.SetFocus()
                return
            value = self.verified_copy_service(hit.workspace, hit.ref, hit.revision, field)
            if (not isinstance(value, str) or not value or value != getattr(hit, field) or
                    hit.workspace != self.model.workspace or self.selected() != hit):
                raise ValueError('Verified source or selection changed')
        except Exception:
            self.status.ChangeValue('Source verification failed. Search again or reopen the source. Nothing copied.')
            self.status.SetFocus()
            return
        try:
            if not wx.TheClipboard.Open():
                raise RuntimeError('Clipboard busy')
            try:
                if not wx.TheClipboard.SetData(wx.TextDataObject(value)):
                    raise RuntimeError('Clipboard rejected data')
                persisted = wx.TheClipboard.Flush()
            finally:
                wx.TheClipboard.Close()
            self.status.ChangeValue('Copied ' + field if persisted else
                                    'Copied ' + field + '; availability after exit not confirmed. Paste before closing.')
        except Exception:
            self.status.ChangeValue('Copy failed. Select one result and retry.')
        self.status.SetFocus()

    def on_quote(self, event):
        self.copy('quote')

    def on_citation(self, event):
        self.copy('citation')

    def on_save_evidence(self, event):
        try:
            hit = self.selected()
            if (not hit.workspace or hit.workspace != self.model.workspace
                    or not callable(self.save_evidence_service)):
                raise ValueError('select a result in a research topic')
            if hit.citation_verified is not True:
                self.status.ChangeValue('Unverified extraction preview. Review the source before saving verified evidence. Nothing saved.')
                self.status.SetFocus()
                return
            result = self.save_evidence_service(hit.workspace, hit.ref, hit.revision,
                                                hit.quote, hit.citation)
        except Exception:
            self.status.ChangeValue('Evidence not saved. Verify the source and selected topic, then retry.')
            self.status.SetFocus()
            return
        # The shell owns focus on success/cancellation, including unsaved notes.
        self.status.ChangeValue('Evidence saved to selected topic.' if result is not None else
                                'Evidence save cancelled.')
