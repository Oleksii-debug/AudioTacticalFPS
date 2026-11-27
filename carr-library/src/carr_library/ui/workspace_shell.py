"""Candidate native shell composing existing panels; host owns authorization.

list_scopes() yields (workspace, topic_or_None, label) for authorized scopes.
bind_scope(workspace, topic) returns ScopeServices after fresh authorization.
Every bound service must independently enforce authorization on each operation.
No global search, database access, or new persistence contract here.
"""
import wx
from .paged_search_panel import PagedSearchPanel
from .catalog_panel import CatalogPanel
from .topic_panel import TopicPanel
from .evidence_panel import EvidencePanel
from .scope_bindings import ScopeServices, bind_scope_services, scope_labels


class WorkspaceFrame(wx.Frame):
    def __init__(self, parent, list_scopes, bind_scope):
        super().__init__(parent, title='Resonance', size=(900, 700))
        self.list_scopes, self.bind_scope = list_scopes, bind_scope
        self.active = self.book = self.evidence = None
        self.scopes = []
        self.busy = False
        self.host = wx.Panel(self)
        self.layout = wx.BoxSizer(wx.VERTICAL)
        self.host.SetSizer(self.layout)
        self.choice = wx.Choice(self.host)
        self.choice.SetName('Library and research topic')
        self.layout.Add(wx.StaticText(self.host, label='Library and research topic'))
        self.layout.Add(self.choice, 0, wx.EXPAND)
        for label, handler in [('Open selected scope', self.open_selected),
                               ('Refresh libraries and topics', self.refresh_scopes)]:
            button = wx.Button(self.host, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            self.layout.Add(button)
        self.status = wx.TextCtrl(self.host, style=wx.TE_READONLY)
        self.status.SetName('Library operation status')
        self.layout.Add(self.status, 0, wx.EXPAND)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.refresh_scopes()

    def _dirty(self):
        p = self.evidence
        return p is not None and p.displayed is not None and p.note.GetValue() != p.displayed.note

    def _confirm(self):
        return not self._dirty() or wx.MessageBox(
            'Discard the unsaved research note?', 'Unsaved note',
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES

    def _restore_note_focus(self):
        # A dirty note can belong to a hidden page. Reveal that exact page
        # without page-changing events before requesting keyboard focus.
        book, evidence = self.book, self.evidence
        try:
            if book is not None and evidence is not None:
                for index in range(book.GetPageCount()):
                    if book.GetPage(index) is evidence:
                        book.ChangeSelection(index)
                        evidence.note.SetFocus()
                        return
        except Exception:
            pass
        # Focus failure must not reach scope teardown and discard the draft.
        try:
            self.status.SetValue('Note retained. Open Evidence and Notes to resume editing.')
            self.status.SetFocus()
        except Exception:
            pass

    def _drop(self):
        old = self.book
        self.active = self.book = self.evidence = None
        if old is not None:
            old.Hide()
            self.layout.Detach(old)
            old.Destroy()
        self.host.Layout()

    def _load_scopes(self):
        rows = tuple(self.list_scopes())
        ids = []
        for workspace, topic, label in rows:
            if (not isinstance(workspace, str) or not workspace or
                    (topic is not None and (not isinstance(topic, str) or not topic)) or
                    not isinstance(label, str) or not label.strip()):
                raise ValueError('invalid authorized scope')
            ids.append((workspace, topic))
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate authorized scope')
        return rows, ids

    def refresh_scopes(self, event=None):
        if self.busy:
            return
        self.busy = True
        try:
            rows, ids = self._load_scopes()
            self.choice.Clear()
            self.scopes = ids
            for label in scope_labels(rows):
                self.choice.Append(label)
            if self.active is not None and self.active not in ids:
                self._drop()
                self.status.SetValue('Access changed. Previous view closed; unsaved edits not saved.')
            else:
                self.status.SetValue('Select a library or topic and choose Open.' if ids else 'No libraries available.')
            if ids:
                self.choice.SetSelection(ids.index(self.active) if self.active in ids else 0)
        except Exception:
            self.scopes = []
            self.choice.Clear()
            self._drop()
            self.status.SetValue('Authorization unavailable. View closed; unsaved edits not saved.')
        finally:
            self.busy = False

    def open_selected(self, event=None):
        if self.busy:
            return
        self.busy = True
        book = None
        try:
            i = self.choice.GetSelection()
            if not 0 <= i < len(self.scopes):
                self.status.SetValue('Choose a library or topic first.')
                return
            scope = self.scopes[i]
            _, live = self._load_scopes()
            if self.active is not None and self.active not in live:
                self._drop()
                self.status.SetValue('Access changed. Previous view closed; unsaved edits not saved.')
                return
            if scope not in live:
                raise ValueError('scope no longer authorized')
            if not self._confirm():
                self.choice.SetSelection(self.scopes.index(self.active))
                self._restore_note_focus()
                return
            services = self.bind_scope(*scope)
            if not isinstance(services, ScopeServices) or (services.workspace, services.topic) != scope:
                raise ValueError('bound scope mismatch')
            self._drop()
            book = wx.Notebook(self.host)
            book.SetName('Research library sections')
            search = self.make_search_panel(book, services)
            search.set_workspace(scope[0])
            pages = [(search, 'Search'),
                     (CatalogPanel(book, scope[0], services.catalog_tx, services.collections), 'Books and Collections'),
                     (TopicPanel(book, scope[0], services.topic_tx), 'Topics and Sections')]
            evidence = None
            if scope[1] is not None:
                evidence = self.make_evidence_panel(book, services)
                pages.append((evidence, 'Evidence and Notes'))
            for page, label in pages:
                if not book.AddPage(page, label):
                    raise RuntimeError('could not add page')
            self.layout.Add(book, 1, wx.EXPAND)
            self.book, self.evidence, self.active = book, evidence, scope
            book = None
            self.host.Layout()
            self.status.SetValue('Research library opened.')
            search.query.SetFocus()
        except Exception:
            if book is not None:
                book.Hide()
                self.layout.Detach(book)
                book.Destroy()
            self._drop()
            self.status.SetValue('Scope unavailable. View closed; unsaved edits not saved.')
            self.choice.SetFocus()
        finally:
            self.busy = False

    def on_close(self, event):
        if event.CanVeto():
            if self.busy:
                event.Veto()
                return
            self.busy = True
            try:
                if not self._confirm():
                    event.Veto()
                    self._restore_note_focus()
                    return
            finally:
                self.busy = False
        event.Skip()

    def make_search_panel(self, parent, services):
        """Allow the desktop host to add filters without replacing service boundaries."""
        return PagedSearchPanel(parent, services.search, services.load_context,
                                verified_copy=services.verified_copy)

    def make_evidence_panel(self,parent,services):
        return EvidencePanel(parent,services.workspace,services.topic,services.evidence_tx)
