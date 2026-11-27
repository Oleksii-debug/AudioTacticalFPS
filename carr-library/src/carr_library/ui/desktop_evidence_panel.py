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

    def refresh(self,event=None,*,evidence_id=None,discard=False):
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
            self._clear()
            self.rows = rows
            for number,row in enumerate(rows,1):
                self.items.Append(f'{number}. {format_citation(row.anchor)}')
            if rows:
                index = next((i for i,row in enumerate(rows) if row.evidence_id == evidence_id),0)
                self.items.SetSelection(index)
                self._show(rows[index],editable)
            self.status.SetValue(f'{len(rows)} saved evidence items.' + ('' if editable else ' Read only.'))

        self.frame.run_job('Loading saved evidence',load,done,
            lambda error: self._failed(epoch,'Evidence unavailable. Check the library and original source.'))
        return True

    def save_note(self,event=None, *, completed=None):
        if completed is not None and not callable(completed):
            raise ValueError('Save completion must be callable.')
        reported = False
        def finish(saved, message=''):
            nonlocal reported
            if completed is not None and not reported:
                from carr_library.ui.save_all import SaveResult
                reported = True
                completed(SaveResult(saved, message))
        if self.frame._closing:
            finish(False, 'The library window closed; the note was not saved.')
            return
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            finish(False, 'A library operation is running; the note is retained.')
            return
        row = self._selected()
        if row is None or row != self.displayed:
            self.status.SetValue('Select the evidence whose note you want to save. Your draft is retained.')
            self.status.SetFocus()
            finish(False, 'Select the evidence whose note you want to save.')
            return
        note = self.note.GetValue()
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
                finish(False, 'The evidence view changed during saving; review its current note.')
                return
            self.rows = tuple(saved if current.evidence_id == saved.evidence_id else current for current in self.rows)
            self.displayed = saved
            self.status.SetValue('Note saved.' if self.note.GetValue() == note else
                                 'Note saved. Your newer unsaved edits are retained.')
            self.note.SetFocus()
            finish(True)

        def failed(error):
            self._failed(epoch,'Note not saved. Check the library and original source, then retry.')
            finish(False, str(error))

        try:
            self.frame.run_job('Saving research note',save,done,failed)
        except Exception as error:
            failed(error)
