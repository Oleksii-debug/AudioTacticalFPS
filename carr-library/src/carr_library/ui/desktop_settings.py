"""Native opt-in desktop controls; saved choices never start clipboard observation."""
from ..desktop_preferences import DesktopPreferences
from types import SimpleNamespace
from .desktop_integration import GLOBAL_COMMANDS


def show_desktop_settings(parent, controller):
    import wx
    if not controller._live():
        return
    root = controller.scope()
    dialog = wx.Dialog(parent, title='Desktop integration', size=(780, 640))
    alive, destroyed = True, False
    owner_id = dialog.GetId()

    def owner_destroyed(event):
        nonlocal alive, destroyed
        if event.GetId() == owner_id:
            alive, destroyed = False, True
        event.Skip()

    dialog.Bind(wx.EVT_WINDOW_DESTROY, owner_destroyed)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label=
        'Clipboard watching and global shortcuts start off in every new window and after changing libraries. '
        'Watching keeps only the latest text in memory for explicit review. It never starts speech. '
        'Close exits normally; Hide to tray is a separate action.'), 0, wx.EXPAND | wx.ALL, 8)
    tray = wx.CheckBox(dialog, label='Show the optional system tray icon')
    tray.SetName('Show the optional system tray icon')
    layout.Add(tray, 0, wx.ALL, 6)
    rows = []
    for identity in GLOBAL_COMMANDS:
        try:
            command = controller.commands.command(identity)
        except ValueError:
            continue
        rows.append(SimpleNamespace(command_id=identity, label=command.label))
    present = {row.command_id for row in rows}
    rows.extend(SimpleNamespace(command_id=identity, label='Unavailable saved action: ' + identity)
                for identity in controller.preferences.global_shortcuts if identity not in present)
    layout.Add(wx.StaticText(dialog, label='Global action'), 0, wx.LEFT | wx.TOP, 6)
    action = wx.Choice(dialog, choices=[row.label for row in rows])
    action.SetName('Global action')
    layout.Add(action, 0, wx.EXPAND | wx.ALL, 6)
    layout.Add(wx.StaticText(dialog, label='Global shortcut: Ctrl+Shift+letter, digit or F1–F11; Alt optional'),
               0, wx.LEFT | wx.TOP, 6)
    shortcut = wx.TextCtrl(dialog)
    shortcut.SetName('Global shortcut')
    layout.Add(shortcut, 0, wx.EXPAND | wx.ALL, 6)
    buttons = {}
    for choices in ((('assign', 'Set assignment'), ('clear', 'Clear assignment'),
                     ('save', 'Save choices'), ('reload', 'Reload saved choices')),
                    (('enable', 'Enable saved global shortcuts'), ('disable', 'Disable global shortcuts')),
                    (('watch', 'Start clipboard watching'), ('stop', 'Stop clipboard watching'),
                     ('close', 'Close'))):
        row = wx.BoxSizer(wx.HORIZONTAL)
        for key, label in choices:
            button = wx.Button(dialog, wx.ID_CANCEL if key == 'close' else wx.ID_ANY, label)
            button.SetName(label)
            buttons[key] = button
            row.Add(button, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
    status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
    status.SetName('Desktop integration status')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 6)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    draft = dict(controller.preferences.global_shortcuts)
    tray.SetValue(controller.preferences.tray_icon)
    if rows:
        action.SetSelection(0)

    def guard():
        if not alive or not controller._live() or controller.scope() != root:
            raise ValueError('The desktop or library selection changed. Reopen these settings.')

    def current():
        index = action.GetSelection()
        if not 0 <= index < len(rows):
            raise ValueError('Select a current global action.')
        return rows[index].command_id

    def refresh(message=''):
        registered = bool(controller.hotkeys._registered)
        for control in (tray, action, shortcut, buttons['assign'], buttons['clear'],
                        buttons['save'], buttons['reload']):
            control.Enable(not registered)
        buttons['enable'].Enable(not registered and controller.hotkeys.platform == 'win32')
        buttons['disable'].Enable(registered)
        buttons['watch'].Enable(not controller.watch.enabled)
        buttons['stop'].Enable(controller.watch.enabled)
        state = ('Global shortcuts: ' + ('on' if controller.hotkeys.enabled else 'off')
                 + '. Clipboard watching: ' + ('on' if controller.watch.enabled else 'off') + '.')
        if controller.hotkeys.platform != 'win32':
            state += ' Global shortcuts are unavailable on this platform.'
        lines = [state, message, 'Saved assignments activate only after explicit Enable in this window.']
        for identity, value in draft.items():
            try:
                label = controller.commands.command(identity).label
            except ValueError:
                label = 'Unavailable: ' + identity
            lines.append(label + ': ' + value)
        status.ChangeValue('\n'.join(line for line in lines if line))

    def choose(event=None):
        shortcut.ChangeValue(draft.get(current(), ''))

    def assign():
        updated = dict(draft, **{current(): shortcut.GetValue()})
        reviewed = DesktopPreferences(tray.GetValue(), updated)
        controller.hotkeys.validate(reviewed.global_shortcuts)
        draft.clear(); draft.update(reviewed.global_shortcuts)

    def clear():
        draft.pop(current(), None)
        shortcut.ChangeValue('')

    def save():
        if rows and shortcut.GetValue() != draft.get(current(), ''):
            assign()
        controller.save_preferences(DesktopPreferences(tray.GetValue(), draft))
        return 'Choices saved. Enable global shortcuts separately when ready.'

    def reload():
        controller.preferences = controller.store.load()
        tray.SetValue(controller.preferences.tray_icon)
        draft.clear(); draft.update(controller.preferences.global_shortcuts)
        choose()

    def enable():
        candidate = DesktopPreferences(tray.GetValue(), draft)
        if (candidate != controller.preferences
                or rows and shortcut.GetValue() != draft.get(current(), '')):
            raise ValueError('Save or reload the reviewed choices before enabling global shortcuts.')
        controller.hotkeys.enable(controller.preferences.global_shortcuts)
        if not controller.timer.Start(1000):
            controller.hotkeys.disable()
            raise RuntimeError('The desktop scope timer could not be started.')

    def run(callback):
        def event_handler(event):
            try:
                guard()
                message = callback()
                refresh(message if isinstance(message, str) else '')
            except Exception as exc:
                if alive and controller._live():
                    refresh(str(exc))
        return event_handler

    for key, callback in (('assign', assign), ('clear', clear), ('save', save), ('reload', reload),
                          ('enable', enable), ('disable', controller.disable_hotkeys),
                          ('watch', lambda: controller.watch_clipboard(True)),
                          ('stop', lambda: controller.watch_clipboard(False))):
        buttons[key].Bind(wx.EVT_BUTTON, run(callback))
    action.Bind(wx.EVT_CHOICE, run(choose))
    try:
        if rows:
            choose()
        refresh()
        action.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        if not destroyed:
            dialog.Destroy()
