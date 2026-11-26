"""Bind existing search and verified page dialog candidates without new data services."""
from .search_panel import SearchPanel
from .page_context_dialog import show_page_context


class PagedSearchPanel(SearchPanel):
    """Host supplies fresh authorized Context through PR43's load_context callback.

    Reuses the native search panel and grid; never calls legacy string More.
    This provisional UI binding does not change the source/storage contract.
    """
    def __init__(self, parent, search, load_context, *, verified_copy):
        if not callable(load_context):
            raise TypeError('verified page context loader required')
        self._context_epoch = 0
        self._load_context = load_context
        super().__init__(parent, search, load_context, verified_copy=verified_copy)

    def set_workspace(self, workspace):
        self._context_epoch += 1
        super().set_workspace(workspace)

    def on_search(self, event):
        self._context_epoch += 1
        super().on_search(event)

    def on_more(self, event):
        epoch = self._context_epoch
        try:
            hit = self.selected()
            if not hit.workspace or hit.workspace != self.model.workspace:
                raise ValueError('Invalid selection scope')
        except Exception:
            self.status.ChangeValue('Select one result in the active library')
            self.status.SetFocus()
            return
        identity = (hit.workspace, hit.ref, hit.revision)

        def current_selection():
            if self._context_epoch != epoch or self.model.workspace != hit.workspace:
                return None
            try:
                return identity if self.selected() == hit else None
            except Exception:
                return None

        if current_selection() != identity:
            return
        try:
            show_page_context(self, identity, current_selection, self._load_context)
        except Exception:
            if current_selection() == identity:
                self.status.ChangeValue('Context unavailable. Search again or reopen the source.')
                self.status.SetFocus()
            return
        # ShowModal runs a nested event loop. Never restore an old row into a new search.
        if current_selection() == identity:
            rows = self.grid.selected_rows()
            if len(rows) == 1 and current_selection() == identity:
                self.grid.focus_row(rows[0])
                self._refresh_details()
