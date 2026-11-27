"""App-wide save and recoverable research-note drafts, without changing source text."""
from pathlib import Path

from ..durable_state import atomic_json, read_json


def install_save_actions(frame):
    import wx
    menu = wx.Menu()

    def save(event=None):
        if frame.working:
            frame.report('A library operation is running. Your editable draft remains available; save when it completes.')
            return
        if frame._dirty():
            frame.evidence.save_note()
        else:
            frame.report('All committed library changes are saved. Reading and studio windows save their own editable projects with Ctrl+S.')

    for title, handler in [('&Save\tCtrl+S', save), ('Save &all', save)]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
    menu.AppendSeparator()
    item = menu.Append(wx.ID_EXIT, 'E&xit\tAlt+F4')
    frame.Bind(wx.EVT_MENU, lambda event: frame.Close(), item)
    frame.menu_bar.Insert(0, menu, '&File')
    install_research_recovery(frame, menu)


def install_research_recovery(frame, menu):
    """Share draft recovery and standard edit commands with one Save menu."""
    import wx
    if getattr(frame, '_draft_save_timer', None) is not None:
        return

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

    item = menu.Append(wx.ID_ANY, 'Recover research-note draft...')
    frame.Bind(wx.EVT_MENU, recover, item)
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
