"""Find and invoke existing native menu commands without a second registry."""
from dataclasses import dataclass
from pathlib import Path

from .command_registry import CommandSpec, normalize_shortcut
from .builtin_keyboard_commands import plain_caption

PALETTE_IDENTITY = 'commands.find'
PALETTE_SHORTCUT = 'Ctrl+Shift+P'
MAX_QUERY = 300


@dataclass(frozen=True)
class CommandChoice:
    command: CommandSpec
    menu_id: int
    shortcut: str
    enabled: bool
    binding: object = None
    path: tuple[str, ...] = ()
    title: str = ''

    @property
    def label(self):
        return ((self.title or self.command.label) + '; ' + (self.shortcut or 'No shortcut')
                + ('; available now' if self.enabled else '; unavailable now'))


class CommandPalette:
    """Use the one canonical registry and recheck the retained native binding."""
    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.frame = keyboard.frame
        self.menu_bar = self.frame.menu_bar
        self.library = self.frame.library
        self.active = self.frame.active
        self.registry, self.store = keyboard.registry, keyboard.store
        self.root = Path(self.store.path.parent if self.store is not None else self.library.root)
        self._guard()
        live = self._live_items()
        self._paths = {identity: live[binding.item.GetId()][2]
                       for identity, binding in keyboard.bindings.items()
                       if binding.item.GetId() in live}

    def _guard(self):
        frame = self.frame
        if (not frame or getattr(frame, '_closing', False) or frame.IsBeingDeleted()
                or getattr(frame, 'working', False) or getattr(frame, 'busy', False)):
            raise ValueError('Wait for the current operation before running a command.')
        if (getattr(frame, 'keyboard_commands', None) is not self.keyboard
                or frame.menu_bar is not self.menu_bar or frame.GetMenuBar() is not self.menu_bar
                or frame.library is not self.library or frame.active != self.active
                or self.keyboard.registry is not self.registry or self.keyboard.store is not self.store
                or Path(self.library.root) != self.root
                or self.store is not None and self.store.path.parent != self.root):
            raise ValueError('The library or command list changed. Open Find a command again.')

    def _live_items(self):
        """Observe exact item, menu path and ancestor enablement without new IDs."""
        states, native_ids = {}, set()

        def caption(value):
            return value.split('\t', 1)[0].replace('&&', '\x00').replace('&', '').replace('\x00', '&').strip()

        def walk(menu, enabled, path, display):
            for item in menu.GetMenuItems():
                if item.IsSeparator():
                    continue
                native_id = item.GetId()
                if native_id in native_ids:
                    raise ValueError('Native menu IDs changed or are reused by more than one action.')
                native_ids.add(native_id)
                label = item.GetItemLabel()
                part = getattr(item, 'carr_command_name', plain_caption(label))
                if not isinstance(part, str) or not part:
                    raise ValueError('The native menu command path changed.')
                current_path = (*path, part)
                current_display = (*display, caption(label))
                available = enabled and item.IsEnabled()
                child = item.GetSubMenu()
                if child is None:
                    states[native_id] = (item, available, current_path, ' / '.join(current_display))
                else:
                    walk(child, available, current_path, current_display)

        for index in range(self.menu_bar.GetMenuCount()):
            label = self.menu_bar.GetMenuLabel(index)
            walk(self.menu_bar.GetMenu(index), self.menu_bar.IsEnabledTop(index),
                 (plain_caption(label),), (caption(label),))
        return states

    def matching(self, query):
        self._guard()
        if not isinstance(query, str) or len(query) > MAX_QUERY:
            raise ValueError(f'Use at most {MAX_QUERY} characters to find a command.')
        words = []
        for word in query.casefold().split():
            shortcut = None
            if '+' in word or (word.startswith('f') and word[1:].isdigit()):
                try:
                    shortcut = normalize_shortcut(word)
                except ValueError:
                    pass
            words.append((word, shortcut))
        live, result = self._live_items(), []
        keys = self.registry.effective(self.keyboard.overrides)
        for command in self.registry.commands('main'):
            if command.command_id == PALETTE_IDENTITY:
                continue
            binding = self.keyboard.bindings.get(command.command_id)
            if binding is None:
                continue
            native_id = binding.item.GetId()
            item = live.get(native_id)
            if (item is None or item[0] is not binding.item
                    or item[2] != self._paths.get(command.command_id)):
                continue
            shortcut = keys[command.command_id]
            searchable = (' '.join((item[3], '/'.join(item[2]),
                                   command.command_id, command.label, shortcut))).casefold()
            if all(shortcut == exact if exact is not None else word in searchable
                   for word, exact in words):
                result.append(CommandChoice(command, native_id, shortcut, item[1],
                                            binding, item[2], item[3]))
        return tuple(result)

    def validate(self, choice):
        self._guard()
        if (not isinstance(choice, CommandChoice)
                or not isinstance(choice.command, CommandSpec)
                or choice.command.command_id == PALETTE_IDENTITY
                or not any(command is choice.command for command in self.registry.commands('main'))):
            raise ValueError('That command is no longer available. Find it again.')
        binding = self.keyboard.bindings.get(choice.command.command_id)
        item = self._live_items().get(choice.menu_id)
        if (binding is None or binding is not choice.binding
                or item is None or item[0] is not binding.item
                or binding.item.GetId() != choice.menu_id
                or item[2] != choice.path or item[2] != self._paths.get(choice.command.command_id)):
            raise ValueError('That menu command changed. Open Find a command again.')
        if not item[1]:
            raise ValueError('That command is unavailable now. Wait or choose another command.')
        return choice.menu_id

    def dispatch(self, choice):
        """Dispatch after modal close through the original canonical manager."""
        self.validate(choice)
        if not self.keyboard.invoke(choice.command.command_id):
            raise ValueError('The command could not be started. Open Find a command again.')
        return True


class CommandPaletteActions:
    """Selection handlers kept separate for deterministic boundary tests."""
    alive = True

    def dispose(self):
        self.alive = False
        self.result = None

    def on_destroy(self, event):
        if event.GetId() == self._native_id:
            self.dispose()
        event.Skip()

    def report(self, message, *, focus=False):
        if not self.alive:
            return
        self.status.SetValue(message)
        if focus:
            self.status.SetFocus()

    def refresh(self, event=None):
        if not self.alive:
            return
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
        if not self.alive:
            return
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
        if not self.alive:
            return
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
        if not self.alive:
            return
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
            self.alive, self._destroy_requested = True, False
            self._native_id = self.GetId()
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
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

        def Destroy(self):
            self.dispose()
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

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
                def ready():
                    if dialog and dialog.alive and not dialog.IsBeingDeleted():
                        on_ready(dialog)
                wx.CallAfter(ready)
            if dialog.ShowModal() == wx.ID_OK:
                choice = dialog.result
        finally:
            if dialog:
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
    """Expose the existing canonical Find a command action exactly once."""
    from .keyboard_settings import install_keyboard_settings
    manager = install_keyboard_settings(frame)
    binding = manager.bindings.get(PALETTE_IDENTITY)
    if binding is None:
        raise ValueError('The canonical Find a command menu is unavailable.')
    frame.command_palette_item = binding.item
    return binding.item
