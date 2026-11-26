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
        self.page_choice = wx.Choice(self)
        self.page_choice.SetName('Evidence source page or segment')
        self.quote = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.citation = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.note = wx.TextCtrl(self, style=wx.TE_MULTILINE)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.items, 1, wx.EXPAND)
        layout.Add(wx.StaticText(self, label='Evidence source page or segment'))
        layout.Add(self.page_choice, 0, wx.EXPAND)
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
        self.page_choice.Bind(wx.EVT_CHOICE, self.show_page)
        self.SetSizer(layout)
        self.refresh()

    def _selected(self):
        i = self.items.GetSelection()
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _dirty(self):
        return self.displayed is not None and self.note.GetValue() != self.displayed.note

    def _clear_source(self):
        self.page_choice.Clear()
        self.page_choice.Enable(False)
        self.quote.SetValue('')
        self.citation.SetValue('')

    def _clear_details(self):
        self.displayed = None
        self._clear_source()
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
        anchors = getattr(row, 'anchors', (row.anchor,))
        for position, anchor in enumerate(anchors, 1):
            self.page_choice.Append(f'Source part {position} of {len(anchors)}; {anchor.locator.display()}')
        self.page_choice.SetSelection(0)
        self.page_choice.Enable(len(anchors) > 1)
        self.show_page()
        self.note.SetValue(row.note)
        self.note.Enable(editable)
        self.save_button.Enable(editable)

    def show_page(self, event=None):
        """Keep source slices separate; changing page leaves the shared draft intact."""
        row = self.displayed
        anchors = getattr(row, 'anchors', (row.anchor,)) if row is not None else ()
        index = self.page_choice.GetSelection()
        if not 0 <= index < len(anchors):
            self.quote.SetValue('')
            self.citation.SetValue('')
            return
        anchor = anchors[index]
        self.quote.SetValue(anchor.quote)
        self.citation.SetValue(format_citation(anchor))

    def refresh(self, event=None, *, evidence_id=None, discard=False):
        if self._dirty() and not discard:
            if event is None or wx.MessageBox('Discard the unsaved note and load the saved evidence?',
                    'Unsaved note', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                self.status.SetValue('Unsaved note retained. Save it before refreshing, or choose Refresh and confirm discard.')
                self.note.SetFocus()
                return False
        if evidence_id is None:
            selected = self._selected()
            evidence_id = selected.evidence_id if selected else None
        try:
            with self.transaction() as service:
                rows, editable = self._live(service)
            # Commit/authorization may fail on context-manager exit. Replace the
            # visible note only after the entire fresh read has succeeded.
            self._clear()
            self.rows = rows
            for i, row in enumerate(rows, 1):
                self.items.Append(f'{i}. {format_citation(row.anchor)}')
            if rows:
                i = next((i for i, r in enumerate(rows) if r.evidence_id == evidence_id), 0)
                self.items.SetSelection(i)
                self._show(rows[i], editable)
            self.status.SetValue(f'{len(rows)} saved evidence items.' + ('' if editable else ' Read only.'))
            return True
        except Exception:
            if self._dirty():
                self._clear_source()
                self.status.SetValue('Evidence unavailable. Your unsaved note is retained. Check the library and source, then retry.')
                self.status.SetFocus()
            else:
                self._clear()
                self.status.SetValue('Evidence unavailable. Check the active library and source.')
            return False

    def select(self, event=None):
        row = self._selected()
        previous = self.displayed
        if row == previous and self._dirty():
            self.note.SetFocus()
            return
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
        self.refresh(evidence_id=row.evidence_id if row else None, discard=True)

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
            self._clear_source()
            self.status.SetValue('Note not saved. Your draft is retained. Shift+Tab returns to the draft. Retry, or copy it before refreshing the saved note.')
            self.status.SetFocus()
            return
        if self.refresh(evidence_id=row.evidence_id, discard=True):
            self.status.SetValue('Note saved.')
            self.note.SetFocus()
        else:
            self.status.SetValue('Note saved, but the evidence could not be refreshed. The displayed draft is retained.')

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
            self.status.SetValue('Evidence not saved. Check the source and active topic.')
            return None
        if self._dirty():
            self.status.SetValue('Quotation saved. Your current unsaved note is retained; save it before refreshing evidence.')
            self.note.SetFocus()
        else:
            self.refresh(evidence_id=evidence_id)
        return evidence_id
