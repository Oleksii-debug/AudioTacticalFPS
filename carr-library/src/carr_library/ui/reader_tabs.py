"""Native tabs over the existing reading-copy and recovery contracts."""
from dataclasses import dataclass

from ..speech.reading import ReadingDocument
from ..speech.session import ReaderResume
from ..speech.tabs import MAX_TABS, ReadingTab, ReadingTabs, PreparedTabs, prepare_tabs


def _snapshot(resume):
    value = (resume.document._signature(), resume.voice_id, resume.rate, resume.volume,
             resume.pitch, resume.punctuation)
    pauses = (resume.sentence_pause_ms, resume.paragraph_pause_ms, resume.section_pause_ms)
    return value + pauses if any(pauses) else value


@dataclass
class TabState:
    entry: ReadingTab
    resume: ReaderResume
    store: object
    token: str | None
    page: object = None
    editor: object = None
    save_path: object = None  # Session choice only; never written to recovery.
    audio_signature: object = None
    reading_rules: tuple = ()

    def __post_init__(self):
        self.audio_signature = _snapshot(self.resume)


class NativeReaderTabs:
    def __init__(self, parent, library_root, initial, *, explicit_text=False,
                 prepared=None, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        self.wx, self.parent = wx_module, parent
        # The product supplies this snapshot from its existing reader worker.
        # Direct callers may prepare synchronously before showing their UI.
        prepared = prepared if prepared is not None else prepare_tabs(
            library_root, initial, explicit_text=explicit_text)
        if not isinstance(prepared, PreparedTabs):
            raise ValueError('Validated reading-tab recovery is required.')
        prepared.store.guard()
        self.store, self.token = prepared.store, prepared.token
        self.states = [TabState(row.entry, row.resume, row.store, row.token)
                       for row in prepared.tabs]
        self.active = prepared.active
        self.published = (ReadingTabs(tuple(state.entry for state in self.states),
                         self.states[self.active].entry.id)
                         if self.token is not None and all(state.token is not None for state in self.states)
                         else None)
        self.alive, self.bound, self.prepared = True, False, None
        self.updating = False
        wx = self.wx
        self.notebook = wx.Notebook(parent)
        self.notebook.SetName('Reading copies; Control+Tab changes reading tab')
        for state in self.states:
            self._page(state)
        self.notebook.ChangeSelection(self.active)
        self._names()
        self.buttons = {}
        for name, label in (('new', 'New reading tab'), ('close', 'Close reading tab'),
                            ('rename', 'Rename reading tab')):
            button = wx.Button(parent, label=label)
            button.SetName(label)
            self.buttons[name] = button

    @property
    def current(self):
        return self.states[self.active]

    def _page(self, state):
        wx = self.wx
        page = wx.Panel(self.notebook)
        try:
            editor = wx.TextCtrl(page, value=state.resume.document.text,
                                 style=wx.TE_MULTILINE | wx.TE_DONTWRAP)
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(editor, 1, wx.EXPAND)
            page.SetSizer(layout)
            if not self.notebook.AddPage(page, state.entry.label, select=False):
                raise RuntimeError('The native reading tab could not be created.')
        except Exception:
            page.Destroy()
            raise
        state.page, state.editor = page, editor

    def _names(self):
        for i, state in enumerate(self.states):
            state.editor.SetName('Reading copy (editable; Ctrl+S saves)' if i == self.active
                                 else 'Inactive reading copy: ' + state.entry.label)

    def bind(self, *, capture, activate, guard, stop, report, busy, confirm_close):
        self.capture, self.activate, self.guard = capture, activate, guard
        self.stop, self.report, self.busy, self.confirm_close = stop, report, busy, confirm_close
        wx = self.wx
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self.on_changing)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_changed)
        for key, handler in (('new', self.on_new), ('close', self.on_close_tab), ('rename', self.on_rename)):
            self.buttons[key].Bind(wx.EVT_BUTTON, handler)
        self.bound = True
        self.set_busy(False)
        owner_id = self.parent.GetId()

        def destroyed(event):
            if event.GetId() == owner_id:
                self.close()
            event.Skip()
        self.parent.Bind(wx.EVT_WINDOW_DESTROY, destroyed)

    def check(self, *, idle=True):
        if not self.alive or not self.bound:
            raise ValueError('This reading workspace has closed.')
        self.guard()
        self.store.guard()
        if idle and self.busy():
            raise ValueError('Stop the current reader operation before changing reading tabs.')

    def set_busy(self, busy):
        if self.alive:
            self.buttons['new'].Enable(not busy and len(self.states) < MAX_TABS)
            self.buttons['close'].Enable(not busy and len(self.states) > 1)
            self.buttons['rename'].Enable(not busy)

    def _publish(self, *, active=None, states=None):
        values = self.states if states is None else states
        current = self.active if active is None else active
        index = ReadingTabs(tuple(state.entry for state in values), values[current].entry.id)
        if index == self.published:
            # Ordinary autosave changes one existing draft, not membership.
            # Keep the index's CAS guard without replaying all other histories.
            if self.store.load()[1] != self.token:
                raise ValueError('Another window changed the reading tabs. Keep this window open or Save As.')
            return
        self.token = self.store.save(index, expected_token=self.token)
        self.published = index

    def save_resume(self, resume, *, save_path=None):
        self.check(idle=False)
        state = self.current
        state.token = state.store.save_with_token(resume, expected_token=state.token)
        state.resume, state.save_path = resume, save_path
        state.audio_signature = _snapshot(resume)
        self._publish()

    def saved_audio_position(self, document, *, synthesis=None):
        state = self.current
        requested = _snapshot(state.resume)[1:] if synthesis is None else synthesis
        return (state.resume.audio_seconds
                if (document._signature(), *requested) == state.audio_signature else 0)

    def save_current(self):
        value, path = self.capture()
        self.save_resume(value, save_path=path)

    def _activate(self, index):
        self.active = index
        self.updating = True
        try:
            self.notebook.ChangeSelection(index)
        finally:
            self.updating = False
        self._names()
        self.activate(self.current)
        self.set_busy(self.busy())

    def on_changing(self, event):
        if not self.alive:
            event.Veto()
            return
        if self.updating:
            event.Skip()
            return
        self.prepared = None
        try:
            self.check()
            selected = event.GetSelection()
            if type(selected) is not int or not 0 <= selected < len(self.states):
                raise ValueError('Choose an existing reading tab.')
            if selected != self.active:
                self.save_current()
                self.stop()
                self._publish(active=selected)
            self.prepared = selected
            event.Skip()
        except Exception as exc:
            event.Veto()
            self.report(str(exc), focus=True)

    def on_changed(self, event):
        if not self.alive:
            return
        if self.updating:
            event.Skip()
            return
        try:
            self.check()
            selected = event.GetSelection()
            if selected != self.prepared:
                raise ValueError('The reading-tab selection was not prepared. Choose it again.')
            self.prepared = None
            self._activate(selected)
            event.Skip()
        except Exception as exc:
            self.notebook.ChangeSelection(self.active)
            self.report(str(exc), focus=True)

    def on_new(self, event=None):
        added = published = False
        try:
            self.check()
            if len(self.states) >= MAX_TABS:
                raise ValueError('Close a reading tab before opening another; the limit is 16.')
            self.save_current()
            self.stop()
            entry = ReadingTab.new(f'Reading copy {len(self.states) + 1}')
            draft = self.store.draft(entry)
            previous = self.current.resume
            state = TabState(entry, ReaderResume(ReadingDocument('', title=entry.label),
                previous.voice_id, previous.rate, previous.volume,
                pitch=previous.pitch, punctuation=previous.punctuation,
                sentence_pause_ms=previous.sentence_pause_ms,
                paragraph_pause_ms=previous.paragraph_pause_ms,
                section_pause_ms=previous.section_pause_ms), draft, None)
            state.token = draft.save_with_token(state.resume, expected_token=None)
            # This exact newly created empty document was just saved. Later
            # erasure of an existing copy must never be treated as clean.
            state.resume.document._saved_signature = state.resume.document._signature()
            self._page(state)
            self.states.append(state)
            added = True
            self._publish(active=len(self.states) - 1)
            published = True
            self._activate(len(self.states) - 1)
            self.report('New reading tab. Its text, original, voice settings and undo history are independent.')
        except Exception as exc:
            if added and not published:
                self.states.pop()
                self.updating = True
                try:
                    self.notebook.DeletePage(self.notebook.GetPageCount() - 1)
                finally:
                    self.updating = False
            self.report(str(exc), focus=True)

    def on_close_tab(self, event=None):
        try:
            self.check()
            if len(self.states) == 1:
                raise ValueError('Keep one reading tab open, or close the reader window.')
            if not self.confirm_close():
                return
            self.check()
            self.stop()
            old = self.active
            remaining = [state for i, state in enumerate(self.states) if i != old]
            selected = min(old, len(remaining) - 1)
            self._publish(active=selected, states=remaining)
            self.states = remaining
            self.active = selected
            self.updating = True
            try:
                self.notebook.DeletePage(old)
            finally:
                self.updating = False
            self._activate(selected)
            self.report('Reading tab closed. Existing saved projects and recovery bytes were retained.')
        except Exception as exc:
            self.report(str(exc), focus=True)

    def on_rename(self, event=None):
        chooser = None
        destroyed = [False]
        try:
            self.check()
            chooser = self.wx.TextEntryDialog(self.parent, 'Reading-tab name, 1 to 80 characters',
                                              'Rename reading tab', self.current.entry.label)
            owner_id = chooser.GetId()
            def closed(event):
                if event.GetId() == owner_id:
                    destroyed[0] = True
                event.Skip()
            chooser.Bind(self.wx.EVT_WINDOW_DESTROY, closed)
            if chooser.ShowModal() != self.wx.ID_OK:
                return
            self.check()
            entry = ReadingTab(self.current.entry.id, chooser.GetValue())
            self.save_current()
            old = self.current.entry
            self.current.entry = entry
            try:
                self._publish()
            except Exception:
                self.current.entry = old
                raise
            self.notebook.SetPageText(self.active, entry.label)
            self._names()
            self.current.editor.SetFocus()
        except Exception as exc:
            self.report(str(exc), focus=True)
        finally:
            if chooser is not None and not destroyed[0]:
                chooser.Destroy()

    def confirm_all_close(self):
        # A worker completion can still target the active copy. Do not visit
        # another tab for save confirmation until that worker has finished.
        self.check()
        captured = self.current
        captured.resume, captured.save_path = self.capture()
        self.stop()
        for i, state in enumerate(self.states):
            if state.resume.document.dirty:
                self._activate(i)
                if not self.confirm_close():
                    return False
        # A clean copy can still have a new cursor, voice or playback position.
        # Flush its captured passive state only after every close confirmation
        # succeeds. Dirty copies retain their explicit Save/Discard decision.
        if not (captured.resume.document.dirty):
            self.check()
            captured.token = captured.store.save_with_token(captured.resume,
                                                            expected_token=captured.token)
            captured.audio_signature = _snapshot(captured.resume)
            # An explicitly discarded new copy may not have a saved draft.
            # Keep the prior membership instead of publishing a missing file.
            if all(state.token is not None for state in self.states):
                self._publish()
        return True

    def save_all(self, save_action):
        self.check()
        self.current.resume, self.current.save_path = self.capture()
        self.stop()
        original = self.active
        for index in range(len(self.states)):
            if not (self.states[index].resume.document.dirty):
                continue
            self._activate(index)
            if not save_action():
                return False
        self._activate(original)
        self._publish()
        return True

    def close(self):
        self.alive = False
