"""UI-only scoped view model; citations and raw text originate in services."""
from dataclasses import dataclass
from ._vendor.wx_grid.model import Column, GridModel


@dataclass(frozen=True)
class Hit:
    workspace: str
    ref: str
    revision: str
    title: str
    author: str
    edition: str
    page: str  # already formatted by citation service, never inferred by UI
    quote: str
    citation: str


class SearchModel(GridModel):
    def __init__(self):
        self.workspace = None
        self.hits = ()

    def set_workspace(self, workspace):
        self.clear()
        self.workspace = workspace

    def clear(self):
        self.hits = ()

    def replace(self, workspace, hits):
        self.clear()
        if not workspace or workspace != self.workspace:
            raise ValueError('Search scope changed')
        rows = tuple(hits)
        if any(not isinstance(h, Hit) or h.workspace != workspace or
               not h.ref or not h.revision or not h.citation or not h.page
               for h in rows):
            raise ValueError('Invalid or out-of-scope result')
        self.hits = rows

    def columns(self):
        return [Column('title', 'Title', True, 'wide'), Column('author', 'Author'),
                Column('edition', 'Edition'), Column('page', 'Page'),
                Column('quote', 'Quotation', width_hint='wide')]

    def row_count(self):
        return len(self.hits)

    def hit(self, row):
        if not 0 <= row < len(self.hits):
            raise ValueError('Select a result first')
        return self.hits[row]

    def cell_text(self, row, column):
        if column not in self.column_names():
            raise ValueError('Unknown column')
        return getattr(self.hit(row), column)
