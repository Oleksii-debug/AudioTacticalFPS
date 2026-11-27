"""Native save, reading-copy and keyboard actions on the existing desktop."""
from dataclasses import dataclass
from pathlib import Path

from ..keybindings import (Command, KeybindingStore, normalize_shortcut,
                           validate_bindings, read_preferences, encode_preferences)
from ..research.export_file import write_binary_export
from .export_destination import approve_export_destination
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


def install_save_coordinator(frame):
    """Bind existing draft stores to one completion-aware desktop coordinator."""
    from .save_all import SaveCoordinator, SaveResult
    coordinator = getattr(frame, '_save_coordinator', None)
    if coordinator is None:
        coordinator = frame._save_coordinator = SaveCoordinator()
    if 'research.note' not in coordinator.participants:
        def dirty():
            panel = getattr(frame, 'evidence', None)
            return panel is not None and panel._dirty()

        def save_note(completed):
            panel = getattr(frame, 'evidence', None)
            if getattr(frame, '_closing', False):
                completed(SaveResult(False, 'The main window is closing.'))
            elif panel is None or not panel._dirty():
                completed(SaveResult(True))
            else:
                panel.save_note(completed=completed)

        coordinator.register('research.note', 'Research note', dirty=dirty, save=save_note,
                             focus=lambda: frame._restore_note_focus())
    return coordinator


def save_all(frame, event=None):
    """Wait for every captured draft's existing durable save operation."""
    if getattr(frame, '_closing', False):
        return False
    coordinator = install_save_coordinator(frame)

    def completed(result):
        if not getattr(frame, '_closing', False):
            frame.report(result.message())

    started = coordinator.save_all(completed, cancelled=lambda: getattr(frame, '_closing', False))
    if not started:
        frame.report('Save All is already waiting for a durable save result.')
    return started



_EDIT_COMMANDS = (
    ('edit.undo', '&Undo', 'Undo'), ('edit.redo', '&Redo', 'Redo'),
    ('edit.cut', 'Cu&t', 'Cut'), ('edit.copy', '&Copy', 'Copy'),
    ('edit.paste', '&Paste', 'Paste'), ('edit.select_all', 'Select &all', 'SelectAll'),
)


def recover_current_note(frame, event=None):
    """Open the existing scoped recovery review; never commit a recovered draft."""
    if getattr(frame, '_closing', False):
        return False
    panel = getattr(frame, 'evidence', None)
    recover = getattr(panel, 'recover_draft', None)
    if not callable(recover):
        frame.report('Open the saved evidence whose research note you want to recover.')
        return False
    recover()
    return True


def edit_current_text(frame, action, event=None):
    """Route an explicit menu command to this window's focused native control."""
    if action not in {entry[2] for entry in _EDIT_COMMANDS}:
        raise ValueError('Choose an existing native text-editing action.')
    if getattr(frame, '_closing', False):
        return False
    import wx
    try:
        target = wx.Window.FindFocus()
        owner, seen = target, set()
        while owner is not None and owner is not frame and id(owner) not in seen:
            seen.add(id(owner))
            parent = getattr(owner, 'GetParent', None)
            owner = parent() if callable(parent) else None
        if owner is not frame:
            frame.report('Choose a text control in this window before using Edit.')
            return False
        method = getattr(target, action, None)
        if not callable(method):
            frame.report('This command is available in a text control that supports it.')
            return False
        if action in {'Undo', 'Redo', 'Cut', 'Paste'}:
            editable = getattr(target, 'IsEditable', None)
            if not callable(editable) or not editable():
                frame.report('The selected text is read only. Its contents were kept.')
                return False
        available = getattr(target, 'Can' + action, None)
        if callable(available) and not available():
            frame.report('This editing action is not available for the current selection.')
            return False
        method()
        return True
    except RuntimeError as error:
        # A destroyed native control cannot acquire another window's focus.
        frame.report('The selected text control is no longer available: ' + str(error))
        return False


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
            style=wx.FD_SAVE if save else (wx.FD_OPEN | wx.FD_FILE_MUST_EXIST))
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        try:
            if save:
                library = self.frame.library
                approval = approve_export_destination(self.frame, path, private_root=library.root)
                if approval is None:
                    return
                if self.frame.library is not library:
                    raise ValueError('The active library changed. Choose the export destination again.')
                write_binary_export(path, encode_preferences(self.overrides), private_root=library.root,
                                    approval=approval)
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
    """Install reading/save actions, then use the shared contextual registry.

    KeyboardActions above is retained only for validating older keymap exports.
    The running application has one settings store and one keyboard manager.
    """
    from .keyboard_settings import install_keyboard_settings
    install_save_coordinator(frame)
    if getattr(frame, 'narration_actions', None) is not None:
        return install_keyboard_settings(frame)
    import wx

    def append(menu, label, handler, *, identity=None):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
        if identity is not None:
            from .command_registry import CommandSpec
            from .keyboard_settings import register_menu_command
            register_menu_command(frame, CommandSpec(identity, label.replace('&', ''), 'main'), item)
        return item

    narration = NarrationActions(frame)
    file_menu = wx.Menu()
    append(file_menu, '&Save current changes\tCtrl+S', lambda event: save_current(frame, event))
    append(file_menu, 'Save &all changes\tCtrl+Shift+S', lambda event: save_all(frame, event))
    append(file_menu, 'Recover research-note draft...', lambda event: recover_current_note(frame, event),
           identity='file.recover_note')
    file_menu.AppendSeparator()
    append(file_menu, '&New reading copy...', narration.new)
    append(file_menu, '&Import text as a reading copy...', narration.import_text)
    append(file_menu, '&Open saved reading copy...', narration.reopen)
    frame.menu_bar.Insert(0, file_menu, '&File')
    edit_menu = wx.Menu()
    for identity, label, action in _EDIT_COMMANDS:
        append(edit_menu, label, lambda event, action=action: edit_current_text(frame, action, event),
               identity=identity)
    frame.menu_bar.Insert(1, edit_menu, '&Edit')
    reading = frame.menu_bar.FindMenu('Reading')
    if reading >= 0:
        menu = frame.menu_bar.GetMenu(reading)
        menu.AppendSeparator()
        append(menu, 'New editable reading copy...', narration.new)
        append(menu, 'Open saved reading copy...', narration.reopen)

    frame.narration_actions = narration
    return install_keyboard_settings(frame)
