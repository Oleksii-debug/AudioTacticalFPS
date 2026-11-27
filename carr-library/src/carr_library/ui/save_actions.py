"""App-wide save and recoverable research-note drafts, without changing source text."""
from pathlib import Path

from ..durable_state import atomic_json, read_json
from .save_all import SaveCoordinator, SaveResult


def install_save_actions(frame):
    import wx
    menu = wx.Menu()
    coordinator = frame._save_coordinator = SaveCoordinator()

    def save_note(completed):
        panel = frame.evidence
        if panel is None or not frame._dirty():
            completed(SaveResult(True))
        else:
            panel.save_note(completed=completed)

    coordinator.register('research.note', 'Research note', dirty=frame._dirty,
                         save=save_note, focus=frame._restore_note_focus)

    def save(event=None):
        if frame._closing:
            return
        if frame.working:
            frame.report('A library operation is running. Your editable draft remains available; save when it completes.')
            return
        if frame._dirty():
            def completed(result):
                if not frame._closing:
                    frame.report(('Research note saved. Newer unsaved edits are retained.' if frame._dirty()
                                  else 'Research note saved.') if result.saved else
                                 'Note not saved: ' + result.message)
            save_note(completed)
        else:
            frame.report('All committed library changes are saved. Reading and studio windows save their own editable projects with Ctrl+S.')

    def save_all(event=None):
        if frame._closing:
            return False
        if frame.working:
            frame.report('A library operation is running. Your drafts are retained; save when it completes.')
            return False
        if coordinator.busy:
            frame.report('Save All is already waiting for a durable save result.')
            return False
        frame.cancel_event.clear()
        def completed(summary):
            if not frame._closing:
                frame.report(summary.message())
        return coordinator.save_all(completed,
            cancelled=lambda: frame._closing or frame.cancel_event.is_set())

    def recover(event=None):
        path = frame.library.root / 'state' / 'recovery' / 'research-note.json'
        try:
            if not path.exists():
                frame.report('There is no recovered research-note draft.')
                return
            data = read_json(path)
            from .desktop_app import show_text
            show_text(frame, 'Recovered research-note draft',
                'A saved draft is not an approved quotation. Open the matching library and evidence before applying it.\n'
                f'Library: {data["workspace"]}\nTopic: {data["topic"]}\nEvidence: {data["evidence"]}\n\n{data["note"]}')
        except Exception as exc:
            frame.report(f'Could not recover the draft: {exc}')

    for title, handler in [('&Save\tCtrl+S', save), ('Save &all', save_all),
                            ('Recover research-note draft...', recover)]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
    menu.AppendSeparator()
    item = menu.Append(wx.ID_EXIT, 'E&xit\tAlt+F4')
    frame.Bind(wx.EVT_MENU, lambda event: frame.Close(), item)
    frame.menu_bar.Insert(0, menu, '&File')
    timer = wx.Timer(frame)
    frame._draft_save_timer = timer
    previous = [None]

    def autosave(event):
        if frame._closing or not frame._dirty() or frame.working:
            return
        panel = frame.evidence
        if panel is None or panel.displayed is None or frame.active is None:
            return
        value = {'schema': 1, 'workspace': frame.active[0], 'topic': frame.active[1],
                 'evidence': panel.displayed.evidence_id, 'note': panel.note.GetValue()}
        identity = (str(frame.library.root), value)
        if previous[0] == identity:
            return
        try:
            path = frame.library.root / 'state' / 'recovery' / 'research-note.json'
            atomic_json(path, value, replace=path.exists())
            previous[0] = identity
        except Exception as exc:
            # An autosave failure must be announced, while retaining the live editor.
            frame.status.SetValue(f'Research draft could not be saved automatically: {exc}. Keep this window open and use Save.')

    frame.Bind(wx.EVT_TIMER, autosave, timer)

    def stop_autosave(event):
        if event.GetEventObject() is frame:
            timer.Stop()
        event.Skip()

    frame.Bind(wx.EVT_WINDOW_DESTROY, stop_autosave)
    timer.Start(3000)

    edit = wx.Menu()
    def focused(action):
        target = wx.Window.FindFocus()
        method = getattr(target, action, None)
        if callable(method):
            method()
        else:
            frame.report('This command is available in an editable text control.')
    for label, action in [('Undo', 'Undo'), ('Redo', 'Redo'), ('Cut', 'Cut'), ('Copy', 'Copy'),
                           ('Paste', 'Paste'), ('Select all', 'SelectAll')]:
        item = edit.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, action=action: focused(action), item)
    frame.menu_bar.Insert(1, edit, '&Edit')
