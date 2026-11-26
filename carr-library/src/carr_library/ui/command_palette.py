"""Find and invoke existing native menu commands without a second registry."""
from dataclasses import dataclass

PALETTE_IDENTITY = 'Options/Find a command'
PALETTE_SHORTCUT = 'Ctrl+Shift+P'
MAX_QUERY = 300


@dataclass(frozen=True)
class CommandChoice:
    binding: object
    menu_id: int
    shortcut: str
    enabled: bool

    @property
    def label(self):
        return (self.binding.command.label + '; ' + (self.shortcut or 'No shortcut')
                + ('' if self.enabled else '; unavailable now'))


class CommandPalette:
    """Read the current keyboard registry and recheck its native menu boundary."""
    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.frame = keyboard.frame
        self.menu_bar = self.frame.menu_bar
        self.library = self.frame.library
        self.active = self.frame.active

    def _guard(self):
        frame = self.frame
        if (getattr(frame, '_closing', False) or frame.IsBeingDeleted()
                or getattr(frame, 'working', False) or getattr(frame, 'busy', False)):
            raise ValueError('Wait for the current operation before running a command.')
        if (frame.keyboard_actions is not self.keyboard or frame.menu_bar is not self.menu_bar
                or frame.GetMenuBar() is not self.menu_bar
                or frame.library is not self.library or frame.active != self.active):
            raise ValueError('The library or command list changed. Open Find a command again.')

    def _live_items(self):
        """Include ancestor and top-menu enablement, not just leaf flags."""
        states = {}

        def walk(menu, enabled):
            for item in menu.GetMenuItems():
                if item.IsSeparator():
                    continue
                available = enabled and item.IsEnabled()
                child = item.GetSubMenu()
                if child is None:
                    states[id(item)] = (item, available)
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
        for binding in self.keyboard.bindings:
            if binding.command.identity == PALETTE_IDENTITY:
                continue
            item = live.get(id(binding.item))
            if item is None or item[0] is not binding.item:
                continue
            label = binding.item.GetItemLabel()
            shortcut = label.split('\t', 1)[1] if '\t' in label else ''
            searchable = (binding.command.label + ' ' + binding.command.identity + ' ' + shortcut).casefold()
            if all(word in searchable for word in words):
                result.append(CommandChoice(binding, binding.item.GetId(), shortcut, item[1]))
        return tuple(result)

    def validate(self, choice):
        self._guard()
        if (not isinstance(choice, CommandChoice)
                or choice.binding.command.identity == PALETTE_IDENTITY
                or not any(binding is choice.binding for binding in self.keyboard.bindings)):
            raise ValueError('That command is no longer available. Find it again.')
        item = self._live_items().get(id(choice.binding.item))
        if (item is None or item[0] is not choice.binding.item
                or choice.binding.item.GetId() != choice.menu_id):
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
        selected = (self.rows[previous].binding if 0 <= previous < len(self.rows) else None)
        try:
            self.rows = self.controller.matching(self.filter.GetValue())
        except (ValueError, RuntimeError) as exc:
            self.rows = ()
            self.commands.SetItems([])
            self.run.Disable()
            self.report(str(exc))
            return
        self.commands.SetItems([choice.label for choice in self.rows])
        index = next((i for i, choice in enumerate(self.rows) if choice.binding is selected), 0)
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
        if choice is None:
            return False
        return controller.dispatch(choice)
    except (ValueError, RuntimeError) as exc:
        if not getattr(frame, '_closing', False):
            frame.report('Command was not run: ' + str(exc))
        return False
