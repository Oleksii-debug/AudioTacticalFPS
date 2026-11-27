"""Find and invoke existing native menu commands without a second registry."""
from dataclasses import dataclass

from .keyboard_settings import MenuCommand, capture_commands, effective_shortcuts, _part

PALETTE_IDENTITY = 'help/find a command'
PALETTE_SHORTCUT = 'Ctrl+Shift+P'
MAX_QUERY = 300


@dataclass(frozen=True)
class CommandChoice:
    command: MenuCommand
    menu_id: int
    shortcut: str
    enabled: bool

    @property
    def label(self):
        return (self.command.title + '; ' + (self.shortcut or 'No shortcut')
                + ('; available now' if self.enabled else '; unavailable now'))


class CommandPalette:
    """Read the current keyboard registry and recheck its native menu boundary."""
    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.frame = keyboard.frame
        self.menu_bar = self.frame.menu_bar
        self.library = self.frame.library
        self.active = self.frame.active
        self.root = keyboard.root

    def _guard(self):
        frame = self.frame
        if (not frame or getattr(frame, '_closing', False) or frame.IsBeingDeleted()
                or getattr(frame, 'working', False) or getattr(frame, 'busy', False)):
            raise ValueError('Wait for the current operation before running a command.')
        if (frame.keyboard_settings is not self.keyboard or frame.menu_bar is not self.menu_bar
                or frame.GetMenuBar() is not self.menu_bar
                or frame.library is not self.library or frame.active != self.active
                or self.keyboard.root != self.root or frame.library.root != self.root):
            raise ValueError('The library or command list changed. Open Find a command again.')

    def _live_items(self):
        """Include ancestor and top-menu enablement, not just leaf flags."""
        # Reuse the keyboard registry's exact stable-name/path and unique-ID
        # validation. Presence of the old object alone cannot authorize an ID
        # reused by another item or an action moved to a different menu path.
        current = {id(command.item): command for command in capture_commands(self.menu_bar)}
        states = {}

        def walk(menu, enabled):
            for item in menu.GetMenuItems():
                if item.IsSeparator():
                    continue
                available = enabled and item.IsEnabled()
                child = item.GetSubMenu()
                if child is None:
                    states[id(item)] = (item, available, current[id(item)].action)
                else:
                    walk(child, available)

        for index in range(self.menu_bar.GetMenuCount()):
            walk(self.menu_bar.GetMenu(index), self.menu_bar.IsEnabledTop(index))
        return states

    def matching(self, query):
        self._guard()
        if not isinstance(query, str) or len(query) > MAX_QUERY:
            raise ValueError(f'Use at most {MAX_QUERY} characters to find a command.')
        words = query.casefold().split()
        live, result = self._live_items(), []
        keys = effective_shortcuts(self.keyboard.commands, self.keyboard.overrides)
        for command in self.keyboard.commands:
            if command.action == PALETTE_IDENTITY:
                continue
            item = live.get(id(command.item))
            if (item is None or item[0] is not command.item or command.item.GetId() != command.menu_id
                    or item[2] != command.action):
                continue
            shortcut = keys[command.action]
            searchable = (command.title + ' ' + command.action + ' ' + shortcut).casefold()
            if all(word in searchable for word in words):
                result.append(CommandChoice(command, command.menu_id, shortcut, item[1]))
        return tuple(result)

    def validate(self, choice):
        self._guard()
        if (not isinstance(choice, CommandChoice)
                or not isinstance(choice.command, MenuCommand)
                or choice.command.action == PALETTE_IDENTITY
                or not any(command is choice.command for command in self.keyboard.commands)):
            raise ValueError('That command is no longer available. Find it again.')
        item = self._live_items().get(id(choice.command.item))
        if (item is None or item[0] is not choice.command.item
                or choice.command.menu_id != choice.menu_id
                or choice.command.item.GetId() != choice.menu_id
                or item[2] != choice.command.action):
            raise ValueError('That menu command changed. Open Find a command again.')
        if not item[1]:
            raise ValueError('That command is unavailable now. Wait or choose another command.')
        return choice.menu_id

    def dispatch(self, choice):
        """Call only after the palette closes; original handlers keep scope checks."""
        import wx
        menu_id = self.validate(choice)
        event = wx.CommandEvent(wx.EVT_MENU.typeId, menu_id)
        event.SetEventObject(self.frame)
        if not self.frame.GetEventHandler().ProcessEvent(event):
            raise ValueError('The command could not be started. Open Find a command again.')
        return True


class CommandPaletteActions:
    """Selection handlers kept separate for deterministic boundary tests."""
    def report(self, message, *, focus=False):
        self.status.SetValue(message)
        if focus:
            self.status.SetFocus()

    def refresh(self, event=None):
        previous = self.commands.GetSelection()
        selected = (self.rows[previous].command if 0 <= previous < len(self.rows) else None)
        try:
            self.rows = self.controller.matching(self.filter.GetValue())
        except (ValueError, RuntimeError) as exc:
            self.rows = ()
            self.commands.SetItems([])
            self.run.Disable()
            self.report(str(exc))
            return
        self.commands.SetItems([choice.label for choice in self.rows])
        index = next((i for i, choice in enumerate(self.rows) if choice.command is selected), 0)
        self.commands.SetSelection(index if self.rows else -1)
        self.selection_changed()

    def selection_changed(self, event=None):
        index = self.commands.GetSelection()
        if not 0 <= index < len(self.rows):
            self.run.Disable()
            self.report('No matching commands. Change the filter or press Escape to close.')
            return
        choice = self.rows[index]
        self.run.Enable(choice.enabled)
        self.report(f'{len(self.rows)} matching commands. ' + choice.label
                    + '. Tab to the list and use Up or Down. Enter runs the selected command.')

    def accept(self, event=None):
        index = self.commands.GetSelection()
        try:
            if not 0 <= index < len(self.rows):
                raise ValueError('Select a matching command first.')
            choice = self.rows[index]
            self.controller.validate(choice)
        except (ValueError, RuntimeError) as exc:
            self.refresh()
            self.report(str(exc), focus=True)
            return
        import wx
        self.result = choice
        self.EndModal(wx.ID_OK)

    def list_key(self, event):
        import wx
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.accept()
        else:
            event.Skip()


def create_command_palette(keyboard):
    """Create native controls; ShowModal/Destroy remain owned by the adapter."""
    import wx

    class PaletteDialog(wx.Dialog, CommandPaletteActions):
        def __init__(self):
            super().__init__(keyboard.frame, title='Find a command', size=(830, 540),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.controller = CommandPalette(keyboard)
            self.rows, self.result = (), None
            layout = wx.BoxSizer(wx.VERTICAL)
            introduction = wx.StaticText(self, label='Find a main-window command by menu path, name or current shortcut. '
                'Tab moves to the results. Enter runs the selected command; Escape closes this window.')
            introduction.Wrap(760)
            layout.Add(introduction, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(self, label='&Find a command'), 0, wx.LEFT | wx.RIGHT, 8)
            self.filter = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
            self.filter.SetName('Find a command by name, menu path or shortcut')
            self.filter.SetMaxLength(MAX_QUERY)
            self.filter.Bind(wx.EVT_TEXT, self.refresh)
            self.filter.Bind(wx.EVT_TEXT_ENTER, self.accept)
            layout.Add(self.filter, 0, wx.EXPAND | wx.ALL, 8)
            self.commands = wx.ListBox(self, style=wx.LB_SINGLE | wx.LB_HSCROLL)
            self.commands.SetName('Matching commands with current shortcuts and availability')
            self.commands.Bind(wx.EVT_LISTBOX, self.selection_changed)
            self.commands.Bind(wx.EVT_LISTBOX_DCLICK, self.accept)
            self.commands.Bind(wx.EVT_KEY_DOWN, self.list_key)
            layout.Add(self.commands, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80))
            self.status.SetName('Command search status')
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            buttons.AddStretchSpacer(1)
            self.run = wx.Button(self, wx.ID_OK, '&Run command')
            self.run.Bind(wx.EVT_BUTTON, self.accept)
            self.run.SetDefault()
            buttons.Add(self.run, 0, wx.RIGHT, 8)
            buttons.Add(wx.Button(self, wx.ID_CANCEL, 'Cancel'))
            layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
            self.SetSizer(layout)
            self.SetMinSize((620, 430))
            self.SetEscapeId(wx.ID_CANCEL)
            self.refresh()
            self.filter.SetFocus()

    return PaletteDialog()


def show_command_palette(keyboard, *, on_ready=None):
    """Close the native palette before dispatching the original menu command."""
    import wx
    frame = keyboard.frame
    previous_focus = wx.Window.FindFocus() if hasattr(wx, 'Window') else None
    try:
        CommandPalette(keyboard)._guard()
        dialog = create_command_palette(keyboard)
        controller = dialog.controller
        choice = None
        try:
            if on_ready is not None:
                wx.CallAfter(on_ready, dialog)
            if dialog.ShowModal() == wx.ID_OK:
                choice = dialog.result
        finally:
            dialog.Destroy()
        # Edit handlers use the focused native control. Restore it before
        # dispatch, while allowing the chosen handler to set its own focus.
        try:
            controller._guard()
            if (previous_focus is not None and not previous_focus.IsBeingDeleted()
                    and previous_focus.IsEnabled() and previous_focus.IsShown()):
                previous_focus.SetFocus()
        except (ValueError, RuntimeError):
            pass
        if choice is None:
            return False
        return controller.dispatch(choice)
    except (ValueError, RuntimeError) as exc:
        if frame and not getattr(frame, '_closing', False) and not frame.IsBeingDeleted():
            frame.report('Command was not run: ' + str(exc))
        return False


def install_command_palette(frame):
    """Add one native action before KeyboardSettings captures the final menus."""
    import wx
    existing = getattr(frame, 'command_palette_item', None)
    if existing is not None:
        return existing
    if getattr(frame, 'keyboard_settings', None) is not None:
        raise ValueError('Install command search before capturing keyboard shortcuts.')
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if _part(frame.menu_bar.GetMenuLabel(index)) == 'help'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Help')
    item = menu.Append(wx.ID_ANY, 'Find a command...\t' + PALETTE_SHORTCUT)
    frame.Bind(wx.EVT_MENU, lambda event: show_command_palette(frame.keyboard_settings), item)
    frame.command_palette_item = item
    return item
