"""File/Edit commands use the shared draft coordinator and evidence recovery."""
from .command_registry import CommandSpec
from .keyboard_settings import register_menu_command
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
        def completed(result):
            if result.saved:
                frame.report('Note saved. Newer unsaved edits are retained.' if frame._dirty()
                             else 'Research note saved. Completed library changes are already stored.')
            else:
                frame.report('Note not saved: ' + result.message)
        save_note(completed)

    def save_all(event=None):
        if not coordinator.save_all(lambda result: frame.report(result.message()),
                                    cancelled=lambda: frame._closing):
            frame.report('Save All is already waiting for a durable save result.')

    def recover(event=None):
        if frame.evidence is None:
            frame.report('Open the saved evidence whose research note you want to recover.')
        else:
            frame.evidence.recover_draft()

    for identity, title, shortcut, handler in [
            ('file.save', '&Save', 'Ctrl+S', save),
            ('file.save_all', 'Save &all', 'Ctrl+Shift+S', save_all),
            ('file.recover_note', 'Recover research-note draft...', '', recover)]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, CommandSpec(identity, title.replace('&', ''), 'main', shortcut), item)
    menu.AppendSeparator()
    item = menu.Append(wx.ID_EXIT, 'E&xit\tAlt+F4')
    frame.Bind(wx.EVT_MENU, lambda event: frame.Close(), item)
    frame.menu_bar.Insert(0, menu, '&File')
    owner_id = frame.GetId()
    timer = wx.Timer(frame)
    frame._draft_save_timer = timer
    active = True

    def autosave(event):
        if not active or frame._closing or not frame._dirty() or frame.working:
            return
        panel = frame.evidence
        if panel is None or panel.displayed is None or frame.active is None:
            return
        # The canonical evidence panel owns the exact source-bound draft.
        # The periodic frame callback flushes that same producer.
        panel._flush_recovery()

    def stop_autosave(event):
        nonlocal active
        if event.GetId() == owner_id:
            # wx can deliver destruction through a different Python wrapper.
            # Use the native ID captured while the owner was alive; the dying
            # frame must not be queried or unbound through an invalid wrapper.
            # Stopping the separate timer prevents native delivery, and active
            # makes an already queued Python callback inert.
            active = False
            timer.Stop()
        event.Skip()

    frame.Bind(wx.EVT_WINDOW_DESTROY, stop_autosave)
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
