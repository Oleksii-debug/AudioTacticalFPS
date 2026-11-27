"""Native save and keyboard actions on the existing desktop."""
from dataclasses import dataclass
from pathlib import Path
import json
import os
import weakref

from ..keybindings import (Command, KeybindingStore, normalize_shortcut,
                           validate_bindings, read_preferences, encode_preferences)
from ..research.export_file import write_binary_export, capture_export_destination
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


def _migrate_command_aliases(commands, overrides):
    """Translate reviewed renamed menu identities without rewriting preferences."""
    known = {command.identity for command in commands}
    aliases = {
        'Academic/DOI lookup and open-access sources':
            'Academic/DOI and ISBN lookup and open-access sources',
    }
    candidate, changed = dict(overrides), False
    for previous, current in aliases.items():
        if previous not in candidate or previous in known or current not in known:
            continue
        value = normalize_shortcut(candidate[previous])
        if current in candidate and normalize_shortcut(candidate[current]) != value:
            raise ValueError('Conflicting saved shortcuts for the renamed command: ' + current + '.')
        candidate.pop(previous)
        candidate[current] = value
        changed = True
    return candidate, changed


def _preserve_added_defaults(commands, overrides):
    """A new palette default cannot displace an already saved assignment."""
    candidate = dict(overrides)
    palette = next((command for command in commands
                    if command.identity == 'Help/Find a command'), None)
    notice = ''
    if palette is not None and candidate.get(palette.identity, '') == '':
        known = {command.identity for command in commands if command.context == palette.context}
        claimed = any(identity in known and identity != palette.identity
                      and normalize_shortcut(shortcut) == palette.shortcut
                      for identity, shortcut in candidate.items())
        if claimed:
            candidate[palette.identity] = ''
            notice = ('Find a command remains unassigned to preserve your saved choices. '
                      'Open Keyboard shortcuts to choose its shortcut.')
    return candidate, notice


def compatible_shortcuts(root, commands):
    """Read the earlier strict schema without rewriting or dropping commands."""
    from .keyboard_settings import _bytes, _path, _unique
    raw = _bytes(_path(root))
    if raw is None:
        return {}
    value = json.loads(raw, object_pairs_hook=_unique)
    if (type(value) is not dict or set(value) != {'schema', 'overrides', 'originals'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['overrides']) is not dict or type(value['originals']) is not dict
            or set(value['overrides']) != set(value['originals'])):
        raise ValueError('Unsupported previous shortcut settings; the original file is retained.')
    indexed = {command.identity.casefold(): command for command in commands}
    aliases = {'file/save': 'file/save current changes',
               'file/save all': 'file/save all changes', 'file/exit': 'library/exit',
               'help/keyboard shortcuts': 'options/keyboard shortcuts',
               'help/focus library and research topic': 'options/focus library and research topic',
               'help/focus operation status': 'options/focus operation status',
               'academic/doi lookup and open-access sources':
                   'academic/doi and isbn lookup and open-access sources'}
    result = {}
    for key, shortcut in value['overrides'].items():
        command = indexed.get(key) or indexed.get(aliases.get(key, key))
        if command is None:
            raise ValueError('A saved command is unavailable: ' + str(key) + '. The original file is retained.')
        original = value['originals'][key]
        approved_old_defaults = {'help/current operation progress': 'Ctrl+Alt+P', 'file/save all': ''}
        if original != command.shortcut and original != approved_old_defaults.get(key, object()):
            raise ValueError('The saved command default changed; review the original shortcut settings.')
        normalized = normalize_shortcut(shortcut)
        if command.identity in result and result[command.identity] != normalized:
            raise ValueError('Conflicting previous shortcut entries target the same command.')
        result[command.identity] = normalized
    result, _ = _preserve_added_defaults(commands, result)
    validate_bindings(commands, result)
    return result


class KeyboardActions:
    def __init__(self, frame, bindings, dialogs=None):
        self.frame, self.bindings = frame, tuple(bindings)
        from .keyboard_settings import reader_commands
        self.commands = (*tuple(binding.command for binding in self.bindings), *reader_commands())
        self._reader_adapters = weakref.WeakSet()
        self.dialogs = dialogs or NativeDialogs()
        self.overrides = {}
        self.notice = ''

    def _apply(self, overrides):
        values = validate_bindings(self.commands, overrides)
        old = [binding.item.GetItemLabel() for binding in self.bindings]
        previous_keys = validate_bindings(self.commands, self.overrides)
        try:
            for binding in self.bindings:
                shortcut = values[binding.command.identity]
                binding.item.SetItemLabel(binding.label + ('\t' + shortcut if shortcut else ''))
            for adapter in tuple(self._reader_adapters):
                adapter.refresh(values)
        except Exception:
            for binding, label in zip(self.bindings, old):
                binding.item.SetItemLabel(label)
            for adapter in tuple(self._reader_adapters):
                adapter.refresh(previous_keys)
            raise
        self.overrides = dict(overrides)
        return values

    def reload(self):
        self.notice = ''
        try:
            store = KeybindingStore(self.frame.library.root)
            if os.path.lexists(store.path):
                overrides = store.load()
            else:
                overrides = compatible_shortcuts(self.frame.library.root, self.commands)
                if overrides:
                    self.notice = 'Your previous shortcut preferences are active. Editing them saves the current format; the original file is retained.'
            overrides, renamed = _migrate_command_aliases(self.commands, overrides)
            if renamed:
                self.notice = (self.notice + ' Saved shortcuts for renamed commands are active; '
                               'the preference file stays unchanged until you save.').strip()
            overrides, added_notice = _preserve_added_defaults(self.commands, overrides)
            self._apply(overrides)
            if added_notice:
                self.notice = (self.notice + ' ' + added_notice).strip()
        except (OSError, ValueError) as exc:
            self._apply({})
            self.notice = f'Default keyboard shortcuts are active because saved preferences could not be used: {exc}'
        if os.path.lexists(Path(self.frame.library.root) / 'keyboard-shortcuts.json'):
            self.notice = (self.notice + ' Earlier canonical keyboard preferences are available for review. '
                'Use Options, Import keyboard shortcuts and select keyboard-shortcuts.json; '
                'the original file and current assignments are preserved until an approved import.').strip()
        return self.notice

    def shortcut(self, identity):
        return validate_bindings(self.commands, self.overrides)[identity]

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
            f'[{command.context}] {command.label}: {values[command.identity] or "No shortcut"}' for command in self.commands) +
            '\n\nMain and Reading Desk contexts share these saved preferences. '
            'Ordinary text editing keys and Alt-menu letters remain reserved.')

    def change(self, event=None, *, context='main', parent=None, report=None):
        parent = self.frame if parent is None else parent
        report = self.frame.report if report is None else report
        library, root = self.frame.library, Path(self.frame.library.root)
        def current():
            if self.frame.library is not library or Path(self.frame.library.root) != root:
                raise ValueError('The library changed. Reopen keyboard shortcuts.')
        try:
            if context not in ('main', 'reader'):
                raise ValueError('Unknown keyboard command context.')
            commands = tuple(command for command in self.commands
                             if command.configurable and command.context == context)
            values = validate_bindings(self.commands, self.overrides)
            labels = [f'{command.label}; {values[command.identity] or "No shortcut"}' for command in commands]
            if context == 'reader':
                labels.insert(0, 'Restore all Reader shortcuts to their original keys')
            index = self.dialogs.choose(parent, 'Keyboard shortcuts', 'Choose a command to change.', labels)
            current()
            if index is None:
                return
            if type(index) is not int or not 0 <= index < len(labels):
                raise ValueError('Choose one command.')
            if context == 'reader':
                if index == 0:
                    import wx
                    if wx.MessageBox('Restore every Reader shortcut to its original key? Main-window assignments are preserved.',
                        'Reset Reader keyboard shortcuts', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, parent) != wx.YES:
                        return
                    current()
                    reader_ids = {command.identity for command in commands}
                    self.save({key: value for key, value in self.overrides.items() if key not in reader_ids})
                    report('Original Reader shortcuts restored and saved.')
                    return
                index -= 1
            command = commands[index]
            value = self.dialogs.text(parent, 'Change keyboard shortcut',
                command.label + '\nUse Ctrl+Shift+S, Alt+F8 or F6. Leave empty to remove the shortcut. '
                + ('Reader defaults can be restored from the first choice in this dialog.'
                   if context == 'reader' else 'Main-window defaults can be restored from Options.'),
                values[command.identity])
            current()
            if value is None:
                return
            candidate = {**self.overrides, command.identity: normalize_shortcut(value)}
            self.save(candidate)
            report(f'Keyboard shortcut saved for {command.label}: {candidate[command.identity] or "none"}.')
        except Exception as exc:
            report(f'Keyboard shortcut was not changed: {exc}')

    def reset(self, event=None):
        import wx
        if wx.MessageBox('Restore every main-window shortcut to its default?', 'Reset keyboard shortcuts',
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self.frame) != wx.YES:
            return
        try:
            main_ids = {command.identity for command in self.commands if command.context == 'main'}
            self.save({key: value for key, value in self.overrides.items() if key not in main_ids})
            self.frame.report('Default main-window keyboard shortcuts restored and saved.')
        except Exception as exc:
            self.frame.report(f'Keyboard defaults could not be saved: {exc}')

    def _export_preferences(self, path, context):
        """Hash on the shared worker, confirm on the UI thread, then publish."""
        import wx
        frame = self.frame
        library, root, active, overrides = context
        payload = encode_preferences(overrides)
        cancelled_event = frame.cancel_event

        def current():
            return (not getattr(frame, '_closing', False)
                    and frame.library is library and Path(frame.library.root) == root
                    and getattr(frame, 'active', None) == active
                    and getattr(frame, 'keyboard_actions', self) is self
                    and self.overrides == overrides)

        def check():
            if cancelled_event.is_set():
                raise InterruptedError('Keyboard export cancelled; the destination was not changed.')
            if not current():
                raise ValueError('The library or keyboard preferences changed. Choose the export again.')
            return False

        def capture():
            check()
            return capture_export_destination(path, private_root=root, cancelled=check)

        def captured(approval):
            check()
            if not approval.require_absent:
                answer = wx.MessageBox(
                    'Replace this existing file with the reviewed keyboard preferences?\n\n' + str(approval.target),
                    'Export keyboard shortcuts', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, frame)
                # The next run_job clears its cancellation event, so retain the
                # user's stop request and scope decision across this modal gap.
                check()
                if answer != wx.YES:
                    frame.report('Keyboard export cancelled; the existing destination is unchanged.')
                    return

            def write():
                check()
                write_binary_export(path, payload, private_root=root, approval=approval, cancelled=check)

            def complete(value):
                if current():
                    frame.report('Keyboard shortcuts exported.')

            check()
            frame.run_job('Exporting keyboard shortcuts', write, complete)

        check()
        frame.run_job('Checking keyboard export destination', capture, captured)

    def transfer(self, *, save):
        import wx
        if save and (self.frame.working or getattr(self.frame, 'busy', False)):
            self.frame.report('Wait for the current operation before exporting keyboard shortcuts.')
            return
        if save and getattr(self.frame, '_closing', False):
            return
        context = (self.frame.library, Path(self.frame.library.root),
                   getattr(self.frame, 'active', None), dict(self.overrides)) if save else None
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
                self._export_preferences(path, context)
            else:
                from .keyboard_settings import read_shortcut_import
                candidate = read_shortcut_import(path, self.commands, self.overrides)
                candidate, _ = _migrate_command_aliases(self.commands, candidate)
                candidate, _ = _preserve_added_defaults(self.commands, candidate)
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


def install_productivity_actions(frame):
    """Install after old menu installers so their positional lookups stay valid."""
    existing = getattr(frame, 'keyboard_actions', None)
    if existing is not None:
        return existing
    import wx

    def append(menu, label, handler):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)

    file_menu = wx.Menu()
    append(file_menu, '&Save current changes\tCtrl+S', lambda event: save_current(frame, event))
    from .save_actions import save_all_open
    append(file_menu, 'Save &all changes\tCtrl+Shift+S', lambda event: save_all_open(frame, event))
    frame.menu_bar.Insert(0, file_menu, '&File')
    from .save_actions import install_research_recovery
    install_research_recovery(frame, file_menu)

    # Add all commands before collecting defaults; closures resolve the action
    # object only when invoked after construction has completed.
    options = wx.Menu()
    append(options, '&Keyboard shortcuts...\tCtrl+Alt+K', lambda event: keyboard.change(event))
    append(options, '&List current keyboard shortcuts...', lambda event: keyboard.show_list(event))
    append(options, '&Restore default keyboard shortcuts...', lambda event: keyboard.reset(event))
    append(options, '&Export keyboard shortcuts...', lambda event: keyboard.transfer(save=True))
    append(options, '&Import keyboard shortcuts...', lambda event: keyboard.transfer(save=False))
    append(options, 'Focus library and research topic\tCtrl+Alt+L', lambda event: frame.choice.SetFocus())
    append(options, 'Focus operation status\tCtrl+Alt+S', lambda event: frame.status.SetFocus())
    frame.menu_bar.Append(options, '&Options')
    keyboard = KeyboardActions(frame, collect_commands(frame.menu_bar))
    frame.keyboard_actions = keyboard
    keyboard.reload()
    return keyboard
