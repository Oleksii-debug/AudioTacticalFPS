"""Desktop evidence reads and writes, with native control work on the UI thread."""
from dataclasses import replace

import wx

from ..citations.anchors import format_citation
from .evidence_panel import EvidencePanel


class DesktopEvidencePanel(EvidencePanel):
    def __init__(self,parent,frame,workspace,topic,transaction):
        self.frame = frame
        self._evidence_epoch = 0
        self._desktop_ready = False
        super().__init__(parent,workspace,topic,transaction)
        self._desktop_ready = True
        from ..evidence_drafts import EvidenceDrafts
        self._recovery_store = EvidenceDrafts(frame.library.root)
        self._recovery_owned = self._recovery_pending = None
        self._recovery_seen = set()
        self._recovery_loading = self._recovery_disposed = False
        self._recovery_discarded = False
        self._recovery_error = ''
        self._recovery_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._flush_recovery, self._recovery_timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._recovery_destroy)
        self.note.Bind(wx.EVT_TEXT, self._recovery_edited)
        recover = wx.Button(self, label='Recover research note draft...')
        recover.SetName('Recover or discard a locally saved research note draft')
        recover.Bind(wx.EVT_BUTTON, self.recover_draft)
        self.GetSizer().Add(recover, 0, wx.ALL, 3)

    def _recovery_edited(self, event):
        event.Skip()
        if (getattr(self, '_recovery_loading', True)
                or getattr(self, '_recovery_disposed', False) or self.displayed is None):
            return
        self._recovery_discarded = False
        self._recovery_pending = self.displayed, self.note.GetValue()
        if self._recovery_owned is None and self._dirty():
            # The first edit has a durable checkpoint immediately. Subsequent
            # edits are coalesced for at most one second, not indefinitely while
            # the user keeps typing. The final cached edit is flushed on destroy.
            self._flush_recovery()
        if not self._recovery_timer.IsRunning():
            self._recovery_timer.StartOnce(1000)

    def _flush_recovery(self, event=None, *, final=False):
        store = getattr(self, '_recovery_store', None)
        pending = getattr(self, '_recovery_pending', None)
        if store is None or pending is None:
            return True
        if getattr(self, '_recovery_discarded', False):
            return True
        row, note = pending
        owned = self._recovery_owned
        if owned is None and note == row.note:
            return True
        if owned is not None and owned.text == note and owned.matches(row):
            return True
        try:
            self._recovery_owned = store.checkpoint(row, note,
                expected=owned.revision if owned is not None else None)
        except Exception as error:
            message = ('Recovery copy could not be saved. Your editor text is retained. '
                       'Save the note or copy it before closing. ' + str(error))
            if not final and message != self._recovery_error:
                self.status.SetValue(message)
            self._recovery_error = message
            from carr_library.ingest.writer_guard import WriterBusy
            if not final and isinstance(error, WriterBusy) and not self._recovery_timer.IsRunning():
                self._recovery_timer.StartOnce(1000)
            return False
        warning = store.last_warning
        if warning and not final:
            self.status.SetValue(warning)
        elif self._recovery_error and not final:
            self.status.SetValue('Recovery copy saved locally. Use Save note to commit your research note.')
        self._recovery_error = warning
        return True

    def _capture_recovery(self):
        if getattr(self, '_recovery_store', None) is not None and self.displayed is not None:
            self._recovery_pending = self.displayed, self.note.GetValue()
            return self._flush_recovery()
        return True

    def _forget_recovery(self, draft):
        if draft is None or getattr(self, '_recovery_store', None) is None:
            return True
        try:
            removed = self._recovery_store.discard(draft)
        except Exception as error:
            self._recovery_error = 'The saved note is safe, but its recovery copy could not be removed. ' + str(error)
            return False
        if removed and self._recovery_owned == draft:
            self._recovery_owned = None
        elif not removed:
            if self._recovery_owned == draft:
                self._recovery_owned = None
            self._recovery_error = 'A newer recovery copy was retained; use Recover research note draft to review it.'
        if removed and self._recovery_store.last_warning:
            self._recovery_error = self._recovery_store.last_warning
        return removed

    def discard_recovery(self):
        """Called only after the user explicitly confirms discarding this note."""
        owned = getattr(self, '_recovery_owned', None)
        if owned is not None and not self._forget_recovery(owned):
            self.status.SetValue('The recovery copy could not be discarded. Your note is retained. '
                                 + self._recovery_error)
            return False
        self._recovery_pending = None
        self._recovery_discarded = True
        timer = getattr(self, '_recovery_timer', None)
        if timer is not None:
            timer.Stop()
        return True

    def _recovery_destroy(self, event):
        if event.GetEventObject() is self:
            self._recovery_timer.Stop()
            # No access to already destroyed child controls: EVT_TEXT captured
            # the exact pending note. Preserve it even after scope revocation;
            # recovery still needs fresh authorization before it is displayed.
            self._flush_recovery(final=True)
            self._recovery_disposed = True
        event.Skip()

    def _show(self, row, editable):
        if getattr(self, '_recovery_store', None) is None:
            return super()._show(row, editable)
        self._recovery_loading = True
        try:
            super()._show(row, editable)
            self._recovery_owned = self._recovery_pending = None
            self._recovery_discarded = False
        finally:
            self._recovery_loading = False
        if editable:
            wx.CallAfter(lambda: self.recover_draft(automatic=True, expected=row))

    def _authorized_recovery_row(self):
        if (not self._current(self._evidence_epoch) or self.frame.working
                or getattr(self, '_recovery_disposed', False)):
            raise ValueError('Open the current library and wait for its operation to finish.')
        row = self.displayed
        if row is None or self._selected() != row:
            raise ValueError('Select the evidence whose research note you want to recover.')
        with self.transaction() as service:
            if service.workspace != self.workspace:
                raise ValueError('The active library changed.')
            service.tree._visible(self.topic)
            current = service.get_saved(self.topic, row.evidence_id)
        if current != row or not self._current(self._evidence_epoch):
            raise ValueError('The saved note or source changed. Refresh evidence before recovering its draft.')
        return current

    def _review_recovery_copy(self, draft):
        dialog = wx.Dialog(self, title='Recovery copy from an earlier saved note', size=(760, 520))
        layout = wx.BoxSizer(wx.VERTICAL)
        message = wx.StaticText(dialog, label='This copy is not attached to the current evidence. Copy any text you want to keep.')
        layout.Add(message, 0, wx.EXPAND | wx.ALL, 8)
        text = wx.TextCtrl(dialog, value=draft.text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        text.SetName('Recovered research note, read only; current saved note is unchanged')
        layout.Add(text, 1, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.Button(dialog, wx.ID_CANCEL, 'Close recovery copy'), 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        dialog.SetSizer(layout)
        dialog.SetEscapeId(wx.ID_CANCEL)
        text.SetFocus()
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def recover_draft(self, event=None, *, automatic=False, expected=None):
        store = getattr(self, '_recovery_store', None)
        if (store is None or getattr(self, '_recovery_disposed', False)
                or not self._current(self._evidence_epoch)
                or (expected is not None and expected != self.displayed)):
            return
        try:
            row = self._authorized_recovery_row()
            draft = store.get(self.workspace, self.topic, row.evidence_id)
            if draft is None:
                if not automatic:
                    self.status.SetValue('No recovery draft exists for this research note.')
                return
            if automatic and draft.revision in self._recovery_seen:
                return
            if draft.source_matches(row) and draft.text == row.note:
                # A prior successful commit may have crashed before cleanup.
                self._forget_recovery(draft)
                return
            if self._dirty():
                if not automatic:
                    self.status.SetValue('Save or copy your current unsaved note before opening its recovery copy.')
                return
            matching = draft.matches(row)
            message = ('A local recovery copy is available. Recovering opens an unsaved note; use Save note to commit it.'
                       if matching else
                       'The saved note or source changed after this recovery copy was recorded. '
                       'Review and copy it without replacing the current saved note.')
            options = [('Recover unsaved draft' if matching else 'Review earlier recovery copy (read only)'),
                       'Discard this recovery copy', 'Keep recovery copy for later']
            chooser = wx.SingleChoiceDialog(self, message, 'Recover research note', options)
            chooser.SetSelection(2)
            before = self.note.GetValue()
            try:
                choice = chooser.GetSelection() if chooser.ShowModal() == wx.ID_OK else 2
            finally:
                chooser.Destroy()
            # A modal dialog is not authorization. Recheck scope, source, saved
            # baseline and compare-and-save revision after the user's decision.
            current = self._authorized_recovery_row()
            latest = store.get(self.workspace, self.topic, row.evidence_id)
            if current != row or latest != draft or self.note.GetValue() != before:
                raise ValueError('The note or recovery copy changed. Nothing was replaced; try again.')
            self._recovery_seen.add(draft.revision)
            if choice == 0 and matching:
                self._recovery_loading = True
                try:
                    self.note.ChangeValue(draft.text)
                finally:
                    self._recovery_loading = False
                self._recovery_owned = draft
                self._recovery_pending = row, draft.text
                self.status.SetValue('Research note draft recovered. Review it and choose Save note. Source quotations are unchanged.')
                self.note.SetFocus()
            elif choice == 0:
                self._review_recovery_copy(draft)
                self.status.SetValue('Earlier recovery copy retained. The current saved note is unchanged.')
            elif choice == 1:
                if not store.discard(draft):
                    raise ValueError('A newer recovery draft was retained. Try again.')
                self._recovery_owned = self._recovery_pending = None
                self.status.SetValue('Recovery copy discarded. The current saved note is unchanged.')
            else:
                self.status.SetValue('Recovery copy kept. Use Recover research note draft to return to it.')
        except Exception as error:
            self.status.SetValue('Recovery unavailable; your note and recovery copy were retained. ' + str(error))
            if not automatic:
                self.status.SetFocus()

    def _initial_refresh(self):
        # WorkspaceFrame finishes attaching the notebook before this callback.
        if self._current(self._evidence_epoch):
            self.refresh()

    def _current(self,epoch):
        return (not self.frame._closing and self._evidence_epoch == epoch and
                self.frame.evidence is self and self.frame.active == (self.workspace,self.topic))

    def _failed(self,epoch,message):
        if not self._current(epoch):
            return
        if self._dirty():
            if self.displayed in self.rows:
                self.items.SetSelection(self.rows.index(self.displayed))
            self._clear_source()
            self.status.SetValue(message + ' Your unsaved note is retained. Shift+Tab returns to the draft.')
        else:
            self._clear()
            self.status.SetValue(message)
        self.status.SetFocus()

    def refresh(self,event=None,*,evidence_id=None,discard=False,focus=False):
        if not self._desktop_ready:
            self._clear_details()
            wx.CallAfter(self._initial_refresh)
            return False
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return False
        if self._dirty() and not discard:
            if event is None or wx.MessageBox('Discard the unsaved note and load the saved evidence?',
                    'Unsaved note',wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,self) != wx.YES:
                self.status.SetValue('Unsaved note retained. Save it before refreshing, or choose Refresh and confirm discard.')
                self.note.SetFocus()
                return False
            discard = True
        self._capture_recovery()
        discarded_draft = getattr(self, '_recovery_owned', None) if discard else None
        if evidence_id is None:
            selected = self._selected()
            evidence_id = selected.evidence_id if selected else None
        draft = self.note.GetValue()
        self._evidence_epoch += 1
        epoch = self._evidence_epoch

        def load():
            if self.frame.cancel_event.is_set():
                raise TimeoutError('Evidence refresh cancelled.')
            with self.transaction() as service:
                snapshot = self._live(service)
            if self.frame.cancel_event.is_set():
                raise TimeoutError('Evidence refresh cancelled.')
            return snapshot

        def done(snapshot):
            if not self._current(epoch):
                return
            if self.note.GetValue() != draft:
                self._clear_source()
                self.status.SetValue('A newer unsaved note is retained. Save it before refreshing evidence.')
                self.note.SetFocus()
                return
            rows,editable = snapshot
            if discarded_draft is not None:
                if not self._forget_recovery(discarded_draft):
                    self.status.SetValue('The recovery copy changed or could not be removed. Your unsaved note is retained.')
                    self.note.SetFocus()
                    return
                self._recovery_pending = None
            self._clear()
            self.rows = rows
            for number,row in enumerate(rows,1):
                self.items.Append(f'{number}. {format_citation(row.anchor)}')
            if rows:
                index = next((i for i,row in enumerate(rows) if row.evidence_id == evidence_id),0)
                self.items.SetSelection(index)
                self._show(rows[index],editable)
            self.status.SetValue(f'{len(rows)} saved evidence items.' + ('' if editable else ' Read only.'))
            if focus:
                (self.items if rows else self.status).SetFocus()

        self.frame.run_job('Loading saved evidence',load,done,
            lambda error: self._failed(epoch,'Evidence unavailable. Check the library and original source.'))
        return True

    def save_note(self,event=None):
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        row = self._selected()
        if row is None or row != self.displayed:
            self.status.SetValue('Select the evidence whose note you want to save. Your draft is retained.')
            self.status.SetFocus()
            return
        note = self.note.GetValue()
        self._capture_recovery()
        saving_draft = getattr(self, '_recovery_owned', None)
        self._evidence_epoch += 1
        epoch = self._evidence_epoch

        def save():
            if self.frame.cancel_event.is_set():
                raise TimeoutError('Saving note cancelled.')
            with self.transaction() as service:
                rows,editable = self._live(service)
                if not editable or row not in rows:
                    raise ValueError('Archived topic or stale evidence.')
                if self.frame.cancel_event.is_set():
                    raise TimeoutError('Saving note cancelled.')
                service.edit_note(self.topic,row.evidence_id,note)
            return replace(row,note=note)

        def done(saved):
            if not self._current(epoch):
                return
            self.rows = tuple(saved if current.evidence_id == saved.evidence_id else current for current in self.rows)
            self.displayed = saved
            owned = getattr(self, '_recovery_owned', None)
            if owned is not None and owned.text == note:
                self._forget_recovery(owned)
            elif saving_draft is not None:
                self._forget_recovery(saving_draft)
            if self.note.GetValue() != note:
                self._capture_recovery()
            elif getattr(self, '_recovery_store', None) is not None:
                self._recovery_pending = None
            self.status.SetValue('Note saved.' if self.note.GetValue() == note else
                                 'Note saved. Your newer unsaved edits are retained.')
            if getattr(self, '_recovery_error', ''):
                self.status.SetValue(self.status.GetValue() + ' ' + self._recovery_error)
            self.note.SetFocus()

        self.frame.run_job('Saving research note',save,done,
            lambda error: self._failed(epoch,'Note not saved. Check the library and original source, then retry.'))
