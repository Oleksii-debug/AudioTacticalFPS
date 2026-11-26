"""Native save, reading-copy and keyboard actions on the existing desktop."""
from dataclasses import dataclass
from pathlib import Path

from ..keybindings import (Command, KeybindingStore, normalize_shortcut,
                           validate_bindings, read_preferences, encode_preferences)
from ..research.export_file import write_binary_export
from .research_actions import NativeDialogs


def save_current(frame, event=None):
    """Commit the only modeless draft through its existing scoped transaction.

    Metadata and narration editors own their contextual Save commands. The
    main desktop never reports all-saved while a note write is still pending.
    """
    if frame.working or getattr(frame, 'busy', False):
        frame.report('An operation is running. Wait for completion or cancel it before saving.')
        return False
    panel = getattr(frame, 'evidence', None)
    if panel is not None and panel._dirty():
        frame._restore_note_focus()
        panel.save_note()
        return True
    frame.report('All changes in the main window are already saved. Open editors have their own Save command.')
    return True


@dataclass
class MenuBinding:
    command: Command
    item: object
    label: str


def _plain(label):
    return label.split('\t', 1)[0].replace('&', '').rstrip('.').strip()


def collect_commands(menu_bar):
    """Snapshot default labels after all existing product menus are installed."""
    result = []

    def walk(menu, prefix):
        for item in menu.GetMenuItems():
            if item.IsSeparator():
                continue
            label = item.GetItemLabel()
            name = _plain(label)
            path = (*prefix, name)
            child = item.GetSubMenu()
            if child is not None:
                walk(child, path)
                continue
            shortcut = label.split('\t', 1)[1] if '\t' in label else ''
            configurable = shortcut.casefold() != 'alt+f4'
            command = Command('/'.join(path), ' — '.join(path), shortcut, configurable)
            result.append(MenuBinding(command, item, label.split('\t', 1)[0]))

    for index in range(menu_bar.GetMenuCount()):
        walk(menu_bar.GetMenu(index), (_plain(menu_bar.GetMenuLabel(index)),))
    validate_bindings([binding.command for binding in result], {})
    return tuple(result)


class KeyboardActions:
    def __init__(self, frame, bindings, dialogs=None):
        self.frame, self.bindings = frame, tuple(bindings)
        self.commands = tuple(binding.command for binding in self.bindings)
        self.dialogs = dialogs or NativeDialogs()
        self.overrides = {}
        self.notice = ''

    def _apply(self, overrides):
        values = validate_bindings(self.commands, overrides)
        old = [binding.item.GetItemLabel() for binding in self.bindings]
        try:
            for binding in self.bindings:
                shortcut = values[binding.command.identity]
                binding.item.SetItemLabel(binding.label + ('\t' + shortcut if shortcut else ''))
        except Exception:
            for binding, label in zip(self.bindings, old):
                binding.item.SetItemLabel(label)
            raise
        self.overrides = dict(overrides)
        return values

    def reload(self):
        self.notice = ''
        try:
            self._apply(KeybindingStore(self.frame.library.root).load())
        except (OSError, ValueError) as exc:
            self._apply({})
            self.notice = f'Default keyboard shortcuts are active because saved preferences could not be used: {exc}'
        return self.notice

    def save(self, overrides):
        validate_bindings(self.commands, overrides)
        previous = dict(self.overrides)
        self._apply(overrides)
        try:
            KeybindingStore(self.frame.library.root).save(self.commands, overrides)
        except Exception:
            self._apply(previous)
            raise

    def show_list(self, event=None):
        values = validate_bindings(self.commands, self.overrides)
        self.dialogs.show(self.frame, 'Current keyboard shortcuts', '\n'.join(
            f'{command.label}: {values[command.identity] or "No shortcut"}' for command in self.commands) +
            '\n\nThese are main-window commands. Modal readers and editors keep their contextual shortcuts. '
            'Ordinary text editing keys and Alt-menu letters remain reserved.')

    def find_command(self, event=None):
        from .command_palette import show_command_palette
        return show_command_palette(self)

    def change(self, event=None):
        try:
            commands = tuple(command for command in self.commands if command.configurable)
            values = validate_bindings(self.commands, self.overrides)
            labels = [f'{command.label}; {values[command.identity] or "No shortcut"}' for command in commands]
            index = self.dialogs.choose(self.frame, 'Keyboard shortcuts', 'Choose a command to change.', labels)
            if index is None:
                return
            if type(index) is not int or not 0 <= index < len(commands):
                raise ValueError('Choose one command.')
            command = commands[index]
            value = self.dialogs.text(self.frame, 'Change keyboard shortcut',
                command.label + '\nUse Ctrl+Shift+S, Alt+F8 or F6. Leave empty to remove the shortcut. '
                'Defaults can be restored from Options.', values[command.identity])
            if value is None:
                return
            candidate = {**self.overrides, command.identity: normalize_shortcut(value)}
            self.save(candidate)
            self.frame.report(f'Keyboard shortcut saved for {command.label}: {candidate[command.identity] or "none"}.')
        except Exception as exc:
            self.frame.report(f'Keyboard shortcut was not changed: {exc}')

    def reset(self, event=None):
        import wx
        if wx.MessageBox('Restore every main-window shortcut to its default?', 'Reset keyboard shortcuts',
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self.frame) != wx.YES:
            return
        try:
            self.save({})
            self.frame.report('Default main-window keyboard shortcuts restored and saved.')
        except Exception as exc:
            self.frame.report(f'Keyboard defaults could not be saved: {exc}')

    def transfer(self, *, save):
        import wx
        dialog = wx.FileDialog(self.frame, 'Export keyboard shortcuts' if save else 'Import keyboard shortcuts',
            defaultFile='research-keybindings.json' if save else '', wildcard='Keyboard preferences (*.json)|*.json',
            style=(wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) if save else (wx.FD_OPEN | wx.FD_FILE_MUST_EXIST))
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        try:
            if save:
                write_binary_export(path, encode_preferences(self.overrides), private_root=self.frame.library.root)
                self.frame.report('Keyboard shortcuts exported.')
            else:
                candidate = read_preferences(path)
                values = validate_bindings(self.commands, candidate)
                changed = [f'{command.label}: {values[command.identity] or "No shortcut"}'
                           for command in self.commands if values[command.identity] != command.shortcut]
                self.dialogs.show(self.frame, 'Keyboard import preview', '\n'.join(changed) or 'Default shortcuts.')
                if wx.MessageBox('Apply these keyboard shortcuts?', 'Import keyboard shortcuts',
                                 wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self.frame) != wx.YES:
                    return
                self.save(candidate)
                self.frame.report('Keyboard shortcuts imported and saved.')
        except Exception as exc:
            self.frame.report(f'Keyboard preferences were not transferred: {exc}')


class NarrationActions:
    def __init__(self, frame):
        self.frame = frame

    def _scope(self):
        if self.frame.working or getattr(self.frame, 'busy', False) or getattr(self.frame, '_closing', False):
            raise ValueError('Wait for the current operation before opening a reading copy.')
        return self.frame.library, self.frame.active

    def _guard(self, library, active):
        if self.frame.library is not library or self.frame.active != active or getattr(self.frame, '_closing', False):
            raise ValueError('The library changed. Open the reading copy again.')
        if active is not None:
            from ..research.topic_tree import TopicTree
            with library.read(active[0]) as db:
                if active[1] is not None:
                    TopicTree(db, active[0])._visible(active[1])

    def new(self, event=None):
        try:
            from ..reader.narration import NarrationStore, new_project
            from .narration_editor import open_narration_editor
            library, active = self._scope()
            self._guard(library, active)
            store = NarrationStore(library.root)
            project = new_project('', title='Untitled reading copy')
            open_narration_editor(self.frame, store, project, guard=lambda: self._guard(library, active))
        except Exception as exc:
            self.frame.report(f'Reading copy was not opened: {exc}')

    def reopen(self, event=None):
        try:
            from ..reader.narration import NarrationStore
            from .narration_editor import show_narration_projects
            library, active = self._scope()
            self._guard(library, active)
            show_narration_projects(self.frame, NarrationStore(library.root),
                                    workspace=active[0] if active is not None else None,
                                    guard=lambda: self._guard(library, active))
        except Exception as exc:
            self.frame.report(f'Reading copies were not opened: {exc}')

    def import_text(self, event=None):
        try:
            from ..reader.narration import NarrationStore
            from .narration_editor import import_reading_text
            library, active = self._scope()
            self._guard(library, active)
            import_reading_text(self.frame, NarrationStore(library.root),
                                guard=lambda: self._guard(library, active))
        except Exception as exc:
            self.frame.report(f'Text was not imported: {exc}')


def install_productivity_actions(frame):
    """Install after old menu installers so their positional lookups stay valid."""
    existing = getattr(frame, 'keyboard_actions', None)
    if existing is not None:
        return existing
    import wx

    def append(menu, label, handler):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)

    narration = NarrationActions(frame)
    file_menu = wx.Menu()
    append(file_menu, '&Save current changes\tCtrl+S', lambda event: save_current(frame, event))
    append(file_menu, 'Save &all changes\tCtrl+Shift+S', lambda event: save_current(frame, event))
    file_menu.AppendSeparator()
    append(file_menu, '&New reading copy...', narration.new)
    append(file_menu, '&Import text as a reading copy...', narration.import_text)
    append(file_menu, '&Open saved reading copy...', narration.reopen)
    frame.menu_bar.Insert(0, file_menu, '&File')
    reading = frame.menu_bar.FindMenu('Reading')
    if reading >= 0:
        menu = frame.menu_bar.GetMenu(reading)
        menu.AppendSeparator()
        append(menu, 'New editable reading copy...', narration.new)
        append(menu, 'Open saved reading copy...', narration.reopen)

    # Add all commands before collecting defaults; closures resolve the action
    # object only when invoked after construction has completed.
    options = wx.Menu()
    from .command_palette import PALETTE_SHORTCUT
    append(options, '&Find a command...\t' + PALETTE_SHORTCUT, lambda event: keyboard.find_command(event))
    options.AppendSeparator()
    append(options, '&Keyboard shortcuts...', lambda event: keyboard.change(event))
    append(options, '&List current keyboard shortcuts...', lambda event: keyboard.show_list(event))
    append(options, '&Restore default keyboard shortcuts...', lambda event: keyboard.reset(event))
    append(options, '&Export keyboard shortcuts...', lambda event: keyboard.transfer(save=True))
    append(options, '&Import keyboard shortcuts...', lambda event: keyboard.transfer(save=False))
    from .language_catalog_dialog import show_language_catalog
    append(options, 'Language capability catalog...', lambda event: show_language_catalog(frame))
    frame.menu_bar.Append(options, '&Options')
    keyboard = KeyboardActions(frame, collect_commands(frame.menu_bar))
    frame.keyboard_actions, frame.narration_actions = keyboard, narration
    keyboard.reload()
    return keyboard
