"""Native evidence/notes candidate backed by authorized SavedEvidenceStore transactions."""
from uuid import uuid4

import wx
from ..citations.anchors import format_citation


class EvidencePanel(wx.Panel):
    """One fixed workspace/topic. Destroy on scope switch; never retarget in place."""
    def __init__(self, parent, workspace, topic, transaction):
        super().__init__(parent)
        self.workspace, self.topic, self.transaction = workspace, topic, transaction
        self.rows = ()
        self.displayed = None
        self.items = wx.ListBox(self, style=wx.LB_SINGLE)
        self.items.SetName('Saved evidence')
        self.quote = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.citation = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.note = wx.TextCtrl(self, style=wx.TE_MULTILINE)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.items, 1, wx.EXPAND)
        for label, control in [('Exact quotation (read only)', self.quote),
                               ('Source citation (read only)', self.citation),
                               ('Research note (editable)', self.note),
                               ('Evidence operation status', self.status)]:
            control.SetName(label)
            layout.Add(wx.StaticText(self, label=label))
            layout.Add(control, 1 if control is not self.status else 0, wx.EXPAND)
        for label, handler in [('Save note', self.save_note),
                               ('Refresh evidence (discard unsaved note)', self.refresh)]:
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            layout.Add(button)
            if handler == self.save_note:
                self.save_button = button
        self.items.Bind(wx.EVT_LISTBOX, self.select)
        self.SetSizer(layout)
        self.refresh()

    def _selected(self):
        i = self.items.GetSelection()
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _clear_details(self):
        self.displayed = None
        self.quote.SetValue('')
        self.citation.SetValue('')
        self.note.SetValue('')
        self.note.Enable(False)
        self.save_button.Enable(False)

    def _clear(self):
        self.rows = ()
        self.items.Clear()
        self._clear_details()

    def _live(self, service):
        if service.workspace != self.workspace:
            raise ValueError('workspace changed')
        rows = tuple(service.list_saved(self.topic))
        if any(r.topic_id != self.topic or r.anchor.identity.workspace != self.workspace for r in rows):
            raise ValueError('scope mismatch')
        if len({r.evidence_id for r in rows}) != len(rows):
            raise ValueError('duplicate identity')
        editable = any(t.id == self.topic for t in service.tree.list_topics())
        return rows, editable

    def _show(self, row, editable):
        self._clear_details()
        self.displayed = row
        self.quote.SetValue(row.anchor.quote)
        self.citation.SetValue(format_citation(row.anchor))
        self.note.SetValue(row.note)
        self.note.Enable(editable)
        self.save_button.Enable(editable)

    def refresh(self, event=None, *, evidence_id=None):
        if evidence_id is None:
            selected = self._selected()
            evidence_id = selected.evidence_id if selected else None
        self._clear()
        try:
            with self.transaction() as service:
                rows, editable = self._live(service)
            self.rows = rows
            for i, row in enumerate(rows, 1):
                self.items.Append(f'{i}. {format_citation(row.anchor)}')
            if rows:
                i = next((i for i, r in enumerate(rows) if r.evidence_id == evidence_id), 0)
                self.items.SetSelection(i)
                self._show(rows[i], editable)
            self.status.SetValue(f'{len(rows)} saved evidence items.' + ('' if editable else ' Read only.'))
        except Exception:
            self._clear()
            self.status.SetValue('Evidence unavailable. Check the active library and source.')

    def select(self, event=None):
        row = self._selected()
        previous = self.displayed
        if previous is not None and row != previous and self.note.GetValue() != previous.note:
            if wx.MessageBox('Discard the unsaved note?', 'Unsaved note',
                             wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                if previous in self.rows:
                    self.items.SetSelection(self.rows.index(previous))
                    self.note.SetFocus()
                    return
                self._clear()
                self.status.SetValue('Selection unavailable. Refresh evidence.')
                return
        self.refresh(evidence_id=row.evidence_id if row else None)

    def save_note(self, event=None):
        row = self._selected()
        try:
            if row is None or row != self.displayed:
                raise ValueError('no current evidence selected')
            note = self.note.GetValue()
            with self.transaction() as service:
                rows, editable = self._live(service)
                if not editable or row not in rows:
                    raise ValueError('archived or stale evidence')
                service.edit_note(self.topic, row.evidence_id, note)
        except Exception:
            self.refresh()
            self.status.SetValue('Note not saved. Evidence changed or is unavailable.')
            return
        self.refresh(evidence_id=row.evidence_id)
        if self.displayed is not None and self.displayed.evidence_id == row.evidence_id:
            self.status.SetValue('Note saved.')
            self.note.SetFocus()

    def save_anchor(self, anchor, note=''):
        """Trusted search controller supplies an anchor; store revalidates before saving."""
        evidence_id = uuid4().hex
        try:
            with self.transaction() as service:
                _, editable = self._live(service)
                if not editable or anchor.identity.workspace != self.workspace:
                    raise ValueError('invalid scope or archived topic')
                service.save(self.topic, evidence_id, anchor, note)
        except Exception:
            self.refresh()
            self.status.SetValue('Evidence not saved. Check the source and active topic.')
            return None
        self.refresh(evidence_id=evidence_id)
        return evidence_id
