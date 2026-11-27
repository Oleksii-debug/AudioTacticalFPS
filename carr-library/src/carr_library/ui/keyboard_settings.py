"""Native menu shortcut preferences, with stable action paths and safe conflicts.

These are application accelerators, never OS-wide hotkeys. The installer runs
after the frame's menu installers. Call ``frame.keyboard_settings.reload()``
after changing the data root. Native Windows/NVDA behavior requires human QA.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from ..durable_state import atomic_json, local_path, regular, state_lock

MAX_COMMANDS = 512
MAX_SETTINGS_BYTES = 128 * 1024
_MODIFIERS = {'ctrl': 'Ctrl', 'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift'}
_ORDER = ('Ctrl', 'Alt', 'Shift')
_EDITING = {'Ctrl+A', 'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+Z', 'Ctrl+Y',
            'Ctrl+Shift+A', 'Ctrl+Shift+C', 'Ctrl+Shift+V', 'Ctrl+Shift+X', 'Ctrl+Shift+Z',
            'Ctrl+F', 'Ctrl+H', 'Ctrl+W'}
_RESERVED = _EDITING | {'F6', 'F10', 'Shift+F10', 'Alt+F4', 'Ctrl+Alt+N'}


def normalize_shortcut(value, *, original=False):
    """Return a portable accelerator string; empty explicitly disables a key."""
    if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError('Enter one shortcut such as Ctrl+Shift+K or F9.')
    value = value.strip()
    if not value:
        return ''
    parts = [part.strip() for part in value.split('+')]
    if any(part.casefold().replace(' ', '') in ('insert', 'ins', 'capslock', 'caps', 'nvda') for part in parts):
        raise ValueError('Insert and Caps Lock belong to screen-reader commands and cannot be assigned.')
    modifiers = []
    for part in parts[:-1]:
        modifier = _MODIFIERS.get(part.casefold())
        if modifier is None or modifier in modifiers:
            raise ValueError('Use Ctrl, Alt and Shift once each, followed by one letter, number or function key.')
        modifiers.append(modifier)
    key = parts[-1].upper()
    if not re.fullmatch(r'[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])', key):
        raise ValueError('Navigation and editing keys stay available to controls. Choose a letter, number or F1–F24.')
    if len(key) == 1 and ('Ctrl' not in modifiers or modifiers == ['Shift']):
        raise ValueError('Letters and numbers require Ctrl. Plain typing and Alt menu access stay available.')
    if 'Alt' in modifiers and 'Ctrl' not in modifiers and len(key) == 1:
        raise ValueError('Alt with a letter is reserved for menu access.')
    result = '+'.join([*(modifier for modifier in _ORDER if modifier in modifiers), key])
    if result in _RESERVED and not original:
        raise ValueError('This shortcut is reserved for text editing, navigation, Windows or the screen reader.')
    return result


def _label(value):
    """Remove access-key markers without confusing an escaped literal ampersand."""
    text = value.split('\t', 1)[0].replace('&&', '\x00').replace('&', '').replace('\x00', '&').strip()
    return text


def _part(value):
    return _label(value).removesuffix('...').removesuffix('…').strip().casefold()


@dataclass(frozen=True)
class MenuCommand:
    action: str
    title: str
    menu_id: int
    default: str
    original_label: str
    item: object


def capture_commands(menu_bar):
    """Capture final menu paths once, before preference labels are applied."""
    commands, known = [], set()
    def visit(menu, parents, display):
        for item in menu.GetMenuItems():
            if item.IsSeparator():
                continue
            label = item.GetItemLabel()
            # Installers may keep an existing command identity when its visible
            # label expands. Saved shortcuts continue to address that same action.
            part = getattr(item, 'carr_command_name', _part(label))
            if (not isinstance(part, str) or not part or part != part.casefold().strip()
                    or '/' in part or any(ord(char) < 32 for char in part)):
                raise ValueError('A menu command needs a stable, unambiguous action path.')
            path, title = (*parents, part), (*display, _label(label))
            submenu = item.GetSubMenu()
            if submenu is not None:
                visit(submenu, path, title)
                continue
            identity = '/'.join(path)
            if identity in known or len(commands) >= MAX_COMMANDS:
                raise ValueError('Duplicate or excessive menu command paths.')
            known.add(identity)
            key = label.partition('\t')[2]
            commands.append(MenuCommand(identity, ' / '.join(title), item.GetId(),
                                         normalize_shortcut(key, original=True), label, item))
    for index in range(menu_bar.GetMenuCount()):
        label = menu_bar.GetMenuLabel(index)
        visit(menu_bar.GetMenu(index), (_part(label),), (_label(label),))
    if len({command.menu_id for command in commands}) != len(commands):
        raise ValueError('Menu command IDs must identify separate actions.')
    return tuple(commands)


def effective_shortcuts(commands, overrides):
    if not isinstance(overrides, dict) or len(overrides) > MAX_COMMANDS:
        raise ValueError('Invalid keyboard shortcut settings.')
    indexed = {command.action: command for command in commands}
    if set(overrides) - set(indexed):
        raise ValueError('A saved shortcut command is unavailable in this version. Reset the shortcuts to use the current menus.')
    result, used = {}, {}
    for command in commands:
        raw = overrides.get(command.action, command.default)
        key = normalize_shortcut(raw, original=True)
        if key and key in _RESERVED and key != command.default:
            normalize_shortcut(key)  # gives the accessible conflict explanation
        if key and key in used:
            raise ValueError(f'{key} is already assigned to both {used[key]} and {command.title}. Clear or change one assignment first.')
        if key:
            used[key] = command.title
        result[command.action] = key
    return result


def _path(root):
    root = local_path(root, exists=True)
    regular(root, directory=True)
    return local_path(root / 'state' / 'ui' / 'shortcuts.json')


def _bytes(path):
    if not os.path.lexists(path):
        return None
    path = local_path(path, exists=True)
    before = regular(path)
    if not 0 < before.st_size <= MAX_SETTINGS_BYTES:
        raise ValueError('Keyboard shortcut settings exceed the supported size.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_SETTINGS_BYTES + 1)
    after = regular(path)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if (len(raw) != before.st_size or any(getattr(before, key) != getattr(opened, key) or
                                         getattr(before, key) != getattr(after, key) for key in fields)):
        raise ValueError('Keyboard shortcut settings changed during read. Reopen this dialog.')
    return raw


def _token(raw):
    return sha256(raw).hexdigest() if raw is not None else None


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate keyboard shortcut setting.')
        value[key] = item
    return value


def _settings_value(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SETTINGS_BYTES:
        raise ValueError('Keyboard shortcut settings exceed the supported size.')
    try:
        # Preserve schema 1's existing BOM/UTF-16/UTF-32 read compatibility.
        # All new writes still use the canonical UTF-8 payload below.
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite settings.')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Keyboard shortcut settings could not be read.') from exc
    if (not isinstance(value, dict) or set(value) != {'schema', 'overrides', 'originals'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['overrides'], dict) or not isinstance(value['originals'], dict)
            or len(value['overrides']) > MAX_COMMANDS
            or set(value['originals']) != set(value['overrides'])
            or any(not isinstance(key, str) or not key or len(key) > 1024
                   or any(ord(char) < 32 for char in key) for key in value['overrides'])):
        raise ValueError('Invalid keyboard shortcut settings schema.')
    for mapping in (value['overrides'], value['originals']):
        for shortcut in mapping.values():
            normalize_shortcut(shortcut, original=True)
    return value


def _decode(raw, commands):
    if raw is None:
        return {}, ()
    value = _settings_value(raw)
    # Schema 1 records explicit choices and their original keys, not the whole
    # old command catalogue. Validate those choices independently of today's
    # implicit defaults; duplicate explicit owners and unknown actions still
    # fail. A newly introduced default must not disable a valid saved keymap.
    explicit = {command.action: '' for command in commands}
    explicit.update(value['overrides'])
    explicit_keys = effective_shortcuts(commands, explicit)
    defaults = {command.action: command.default for command in commands}
    if any(value['originals'][key] != defaults[key] for key in value['overrides']):
        raise ValueError('An original menu shortcut changed in this version. Reset shortcuts before editing them.')
    indexed = {command.action: command for command in commands}
    owners = {explicit_keys[action]: indexed[action].title for action in value['overrides']
              if explicit_keys[action]}
    restored, conflicts = dict(value['overrides']), []
    for command in commands:
        if command.action not in restored and command.default and command.default in owners:
            restored[command.action] = ''
            conflicts.append(f'{command.title}: {command.default} is used by {owners[command.default]}')
    effective_shortcuts(commands, restored)
    return restored, tuple(conflicts)


def load_shortcuts(root, commands):
    """Return overrides and a compare-before-write token; never retarget actions."""
    raw = _bytes(_path(root))
    overrides, _ = _decode(raw, commands)
    return overrides, _token(raw)


def _settings_payload(commands, overrides):
    keys = effective_shortcuts(commands, overrides)
    kept = {command.action: keys[command.action] for command in commands if keys[command.action] != command.default}
    originals = {command.action: command.default for command in commands if command.action in kept}
    value = {'schema': 1, 'overrides': kept, 'originals': originals}
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValueError('Keyboard shortcut settings exceed the supported size.')
    return value, raw


def save_shortcuts(root, commands, overrides, *, expected_token):
    value, raw = _settings_payload(commands, overrides)
    path = _path(root)
    with state_lock(path.parent, 'shortcuts'):
        if _token(_bytes(path)) != expected_token:
            raise ValueError('Another window changed the shortcuts. Reopen this dialog before applying changes.')
        atomic_json(path, value, replace=path.exists())
    return value['overrides'], _token(raw)


@dataclass(frozen=True, eq=False)
class ShortcutScope:
    root: Path
    library: object
    menu_bar: object
    commands: tuple
    token: str | None
    overrides: tuple

    def __eq__(self, other):
        if not isinstance(other, ShortcutScope):
            return NotImplemented
        return (self.library is other.library and self.menu_bar is other.menu_bar
                and self.commands is other.commands and self.root == other.root
                and self.token == other.token and self.overrides == other.overrides)


@dataclass(frozen=True)
class KeymapImport:
    source: Path
    source_token: str
    scope: ShortcutScope
    overrides: tuple
    recognized: tuple
    notices: tuple
    issues: tuple

    @property
    def description(self):
        rows = ['Import preview only; no shortcuts have changed.',
                'Only listed choices are merged. Other saved choices remain unchanged.']
        rows.extend(self.recognized or ('The file contains no explicit shortcut choices.',))
        rows.extend('Default made unassigned: ' + row for row in self.notices)
        rows.extend('Cannot apply: ' + row for row in self.issues)
        if not self.issues:
            rows.append('Review these changes, then choose Apply reviewed import.')
        return '\n'.join(rows)


@dataclass(frozen=True)
class ShortcutUndo:
    before: tuple
    after: ShortcutScope


def _merge_import(commands, current, value):
    """Merge explicit choices; never discard an unmentioned saved override."""
    indexed = {command.action: command for command in commands}
    previous = effective_shortcuts(commands, current)
    updated, recognized, notices, issues = dict(current), [], [], []
    for action, raw in value['overrides'].items():
        command = indexed.get(action)
        if command is None:
            issues.append(f'Unavailable command: {action}.')
            continue
        if value['originals'][action] != command.default:
            issues.append(f'The original shortcut changed for {command.title} [{action}].')
            continue
        try:
            key = normalize_shortcut(raw, original=True)
            if key in _RESERVED and key != command.default:
                normalize_shortcut(key)
        except ValueError as exc:
            issues.append(f'{command.title}: {exc}')
            continue
        updated[action] = key
        recognized.append(f'{command.title} [{action}]: {previous[action] or "Unassigned"}'
                          f' → {key or "Unassigned"}.')
    # Explicit owners, including existing saved choices, must not conflict.
    try:
        explicit = effective_shortcuts(commands, {**{row.action: '' for row in commands}, **updated})
        owners = {explicit[action]: indexed[action].title for action in updated if explicit[action]}
        for command in commands:
            if command.action not in updated and command.default and command.default in owners:
                updated[command.action] = ''
                notices.append(f'{command.title}: {command.default} is used by {owners[command.default]}.')
        effective_shortcuts(commands, updated)
    except ValueError as exc:
        issues.append(str(exc))
    return tuple(sorted(updated.items())), tuple(recognized), tuple(notices), tuple(issues)


def accelerator_entries(commands, keys, wx):
    flags = {'Ctrl': wx.ACCEL_CTRL, 'Alt': wx.ACCEL_ALT, 'Shift': wx.ACCEL_SHIFT}
    result = []
    for command in commands:
        value = keys[command.action]
        if not value:
            continue
        *modifiers, key = value.split('+')
        mask = 0
        for modifier in modifiers:
            mask |= flags[modifier]
        code = getattr(wx, 'WXK_' + key) if key.startswith('F') and len(key) > 1 else ord(key)
        result.append((mask, code, command.menu_id))
    return result


class KeyboardSettings:
    def __init__(self, frame, *, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        self.frame, self.wx = frame, wx_module
        self.menu_bar = frame.menu_bar
        self.commands = capture_commands(frame.menu_bar)
        self.overrides, self.token, self.root = {}, None, None
        self.load_notice = ''
        self.reload()

    def _render(self, overrides):
        keys = effective_shortcuts(self.commands, overrides)
        table = self.wx.AcceleratorTable(accelerator_entries(self.commands, keys, self.wx))
        for command in self.commands:
            label = command.original_label.split('\t', 1)[0]
            command.item.SetItemLabel(label + ('\t' + keys[command.action] if keys[command.action] else ''))
        self.frame.SetAcceleratorTable(table)

    def reload(self):
        self.root = local_path(self.frame.library.root, exists=True)
        self.library = self.frame.library
        raw = None
        try:
            raw = _bytes(_path(self.root))
            overrides, conflicts = _decode(raw, self.commands)
            self._render(overrides)
            self.overrides, self.token = overrides, _token(raw)
            self.load_notice = ('Saved keyboard shortcuts are active. These default shortcuts remain unassigned '
                'to preserve your saved choices: ' + '; '.join(conflicts) +
                '. Open Keyboard shortcuts to review the assignments.') if conflicts else ''
            if self.load_notice:
                self.frame.report(self.load_notice)
            return True
        except (ValueError, OSError) as exc:
            self._render({})
            self.overrides, self.token = {}, _token(raw)
            self.load_notice = f'Original keyboard shortcuts are active. Saved settings could not be applied: {exc}'
            self.frame.report(self.load_notice)
            return False

    def _guard(self):
        frame = self.frame
        if (not frame or getattr(frame, '_closing', False)
                or (hasattr(frame, 'IsBeingDeleted') and frame.IsBeingDeleted())):
            raise ValueError('The application is closing; reopen keyboard shortcuts.')
        if (frame.library is not self.library
                or local_path(frame.library.root, exists=True) != self.root):
            raise ValueError('The library folder changed. Reopen keyboard shortcuts.')
        if (frame.menu_bar is not self.menu_bar
                or (hasattr(frame, 'GetMenuBar') and frame.GetMenuBar() is not self.menu_bar)
                or getattr(frame, 'keyboard_settings', self) is not self):
            raise ValueError('The menu commands changed. Reopen the application before editing shortcuts.')
        current = capture_commands(self.menu_bar)
        identity = lambda rows: tuple((row.action, row.menu_id, id(row.item)) for row in rows)
        expected_keys = effective_shortcuts(self.commands, self.overrides)
        if (identity(current) != identity(self.commands)
                or any(row.default != expected_keys[row.action] for row in current)):
            raise ValueError('The menu commands changed. Reopen the application before editing shortcuts.')

    def _scope(self):
        return ShortcutScope(self.root, self.library, self.menu_bar, self.commands,
                             self.token, tuple(sorted(self.overrides.items())))

    def _current_scope(self):
        self._guard()
        if _token(_bytes(_path(self.root))) != self.token:
            raise ValueError('Another window changed the shortcuts. Reopen this dialog before applying changes.')
        return self._scope()

    def apply(self, overrides):
        self._guard()
        effective_shortcuts(self.commands, overrides)
        previous = dict(self.overrides)
        try:
            self._render(overrides)
            kept, token = save_shortcuts(self.root, self.commands, overrides, expected_token=self.token)
        except BaseException:
            self._render(previous)
            raise
        self.overrides, self.token = kept, token
        self.load_notice = ''

    def apply_with_undo(self, overrides):
        before = self._current_scope()
        self.apply(overrides)
        return ShortcutUndo(before.overrides, self._scope())

    def undo(self, change):
        if not isinstance(change, ShortcutUndo) or self._current_scope() != change.after:
            raise ValueError('Shortcuts changed since the last edit. Reopen this dialog before changing them.')
        self.apply(dict(change.before))

    def preview_import(self, source, *, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError('Keymap import cancelled.')
        scope = self._current_scope()
        regular(source)  # Reject a selected link or non-file before path traversal.
        source = local_path(source, exists=True)
        raw = _bytes(source)
        value = _settings_value(raw)
        merged = _merge_import(self.commands, self.overrides, value)
        if cancelled():
            raise InterruptedError('Keymap import cancelled.')
        if self._current_scope() != scope:
            raise ValueError('Shortcuts changed during import. Preview the file again.')
        return KeymapImport(source, _token(raw), scope, *merged)

    def apply_import(self, plan, *, cancelled=lambda: False):
        if not isinstance(plan, KeymapImport):
            raise ValueError('Preview a keymap file before applying it.')
        if self.preview_import(plan.source, cancelled=cancelled) != plan:
            raise ValueError('The file, library or shortcuts changed after review. Preview the import again.')
        if plan.issues:
            raise ValueError('Resolve every reported import conflict before applying this keymap.')
        if cancelled():
            raise InterruptedError('Keymap import cancelled.')
        return self.apply_with_undo(dict(plan.overrides))

    def export_keymap(self, destination, *, cancelled=lambda: False):
        from ..research.export_file import write_export
        scope = self._current_scope()
        _, raw = _settings_payload(self.commands, self.overrides)
        def check():
            if cancelled():
                return True
            if self._current_scope() != scope:
                raise ValueError('Shortcuts changed during export. Choose a new destination and try again.')
            return False
        write_export(destination, raw.decode('utf-8'), private_root=self.root,
                     require_absent=True, cancelled=check)

    def show(self, event=None):
        wx = self.wx
        if (self.frame.library is not self.library
                or local_path(self.frame.library.root, exists=True) != self.root):
            self.reload()
        self._guard()
        opened = (self.library, self.root, self.menu_bar, self.commands)
        alive, reviewed, undo = True, None, None
        dialog = wx.Dialog(self.frame, title='Keyboard shortcuts', size=(900, 680),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(dialog, label='Choose a command and type a shortcut, for example Ctrl+Shift+K or F9.\n'
                'An empty shortcut removes the assignment. Apply saves the selected shortcut.\n'
                'Import previews a merge before applying. Undo restores the entire preceding keymap in this window.\n'
                'Closing discards an unapplied preview; already applied changes remain saved.'), 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='&Command'), 0, wx.LEFT | wx.TOP, 8)
            choice = wx.Choice(dialog, choices=[command.title for command in self.commands])
            choice.SetName('Command to change')
            choice.SetSelection(0)
            layout.Add(choice, 0, wx.EXPAND | wx.ALL, 8)
            current = wx.TextCtrl(dialog, style=wx.TE_READONLY)
            current.SetName('Current and original shortcut')
            layout.Add(current, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='&Keyboard shortcut'), 0, wx.LEFT | wx.TOP, 8)
            key = wx.TextCtrl(dialog)
            key.SetName('Keyboard shortcut; empty means unassigned')
            layout.Add(key, 0, wx.EXPAND | wx.ALL, 8)
            status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
            status.SetName('Shortcut settings status')
            status.ChangeValue(self.load_notice)
            layout.Add(status, 1, wx.EXPAND | wx.ALL, 8)
            interchange = wx.BoxSizer(wx.HORIZONTAL)
            preview = wx.Button(dialog, label='&Preview keymap import...')
            import_apply = wx.Button(dialog, label='Apply reviewed &import')
            export = wx.Button(dialog, label='E&xport keymap...')
            for button in (preview, import_apply, export):
                button.SetName(_label(button.GetLabel()))
                interchange.Add(button, 0, wx.ALL, 6)
            layout.Add(interchange, 0, wx.EXPAND)
            import_apply.Enable(False)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            apply = wx.Button(dialog, wx.ID_APPLY, '&Apply')
            reset = wx.Button(dialog, label='&Reset all shortcuts to original keys')
            undo_button = wx.Button(dialog, label='&Undo last change')
            close = wx.Button(dialog, wx.ID_CANCEL, 'C&lose')
            for button in (apply, reset, undo_button, close):
                button.SetName(_label(button.GetLabel()))
                buttons.Add(button, 0, wx.ALL, 6)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            undo_button.Enable(False)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            def guard():
                self._guard()
                if (not alive or self.library is not opened[0] or self.root != opened[1]
                        or self.menu_bar is not opened[2] or self.commands is not opened[3]):
                    raise ValueError('The library or commands changed. Reopen keyboard shortcuts.')
                return False
            def clear_preview(event=None):
                nonlocal reviewed
                if not alive:
                    return
                if reviewed is not None:
                    status.ChangeValue('The editor changed; preview the keymap again before importing.')
                reviewed = None
                import_apply.Enable(False)
                if event is not None:
                    event.Skip()
            def selected():
                index = choice.GetSelection()
                if not 0 <= index < len(self.commands):
                    raise ValueError('Choose a menu command first.')
                return self.commands[index]
            def refresh(event=None):
                if not alive:
                    return
                if event is not None:
                    clear_preview()
                command = selected()
                value = effective_shortcuts(self.commands, self.overrides)[command.action]
                current.ChangeValue(f'Current: {value or "Unassigned"}. Original: {command.default or "Unassigned"}.')
                key.ChangeValue(value)
            def change(event=None):
                nonlocal undo
                if not alive:
                    return
                try:
                    guard()
                    command = selected()
                    updated = dict(self.overrides)
                    updated[command.action] = key.GetValue()
                    undo = self.apply_with_undo(updated)
                    undo_button.Enable(True)
                    clear_preview()
                    refresh()
                    status.ChangeValue(f'Applied shortcut for {command.title}.')
                except (ValueError, OSError, RuntimeError) as exc:
                    status.ChangeValue(f'Shortcut was not applied: {exc}')
                    status.SetFocus()
                    return
                key.SetFocus()
                key.SelectAll()
            def reset_all(event=None):
                nonlocal undo
                if not alive:
                    return
                try:
                    guard()
                    undo = self.apply_with_undo({})
                    undo_button.Enable(True)
                    clear_preview()
                    refresh()
                    status.ChangeValue('All original menu shortcuts restored and saved.')
                except (ValueError, OSError, RuntimeError) as exc:
                    status.ChangeValue(f'Original shortcuts could not be saved: {exc}')
                status.SetFocus()
            def undo_last(event=None):
                nonlocal undo
                if not alive:
                    return
                try:
                    guard()
                    self.undo(undo)
                    undo = None
                    undo_button.Enable(False)
                    clear_preview()
                    refresh()
                    status.ChangeValue('The complete keymap before the last applied change is restored and saved.')
                except (ValueError, OSError, RuntimeError) as exc:
                    status.ChangeValue(f'Shortcuts could not be restored: {exc}')
                status.SetFocus()
            def file_choice(importing):
                guard()
                with wx.FileDialog(dialog, 'Preview keymap import' if importing else 'Export keymap to a new file',
                        wildcard='Keyboard shortcuts (*.json)|*.json',
                        style=(wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) if importing else wx.FD_SAVE) as picker:
                    if picker.ShowModal() != wx.ID_OK:
                        return None
                    destination = picker.GetPath()
                guard()
                return destination
            def preview_file(event=None):
                nonlocal reviewed
                if not alive:
                    return
                try:
                    source = file_choice(True)
                    if source is None:
                        return
                    clear_preview()
                    reviewed = self.preview_import(source, cancelled=guard)
                    status.ChangeValue(reviewed.description)
                    import_apply.Enable(not reviewed.issues)
                except (ValueError, OSError, RuntimeError) as exc:
                    clear_preview()
                    status.ChangeValue(f'Keymap was not imported: {exc}')
                status.SetFocus()
            def import_file(event=None):
                nonlocal undo
                if not alive:
                    return
                try:
                    guard()
                    undo = self.apply_import(reviewed, cancelled=guard)
                    undo_button.Enable(True)
                    clear_preview()
                    refresh()
                    status.ChangeValue('The reviewed keymap was merged and saved. Other saved choices are preserved.')
                except (ValueError, OSError, RuntimeError) as exc:
                    clear_preview()
                    status.ChangeValue(f'Keymap was not applied: {exc}')
                status.SetFocus()
            def export_file(event=None):
                if not alive:
                    return
                try:
                    destination = file_choice(False)
                    if destination is None:
                        return
                    self.export_keymap(destination, cancelled=guard)
                    status.ChangeValue('Current saved keymap exported to a new file. Existing files are never replaced.')
                except (ValueError, OSError, RuntimeError) as exc:
                    status.ChangeValue(f'Keymap was not exported: {exc}')
                status.SetFocus()
            choice.Bind(wx.EVT_CHOICE, refresh)
            key.Bind(wx.EVT_TEXT, clear_preview)
            apply.Bind(wx.EVT_BUTTON, change)
            reset.Bind(wx.EVT_BUTTON, reset_all)
            undo_button.Bind(wx.EVT_BUTTON, undo_last)
            preview.Bind(wx.EVT_BUTTON, preview_file)
            import_apply.Bind(wx.EVT_BUTTON, import_file)
            export.Bind(wx.EVT_BUTTON, export_file)
            refresh()
            choice.SetFocus()
            dialog.ShowModal()
        finally:
            alive = False
            dialog.Destroy()


def install_keyboard_settings(frame):
    """Install after all menus exist; the returned controller owns live binding."""
    import wx
    if getattr(frame, 'keyboard_settings', None) is not None:
        frame.keyboard_settings.reload()
        return frame.keyboard_settings
    from .command_palette import install_command_palette
    install_command_palette(frame)
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if _part(frame.menu_bar.GetMenuLabel(index)) == 'help'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Help')
    settings = menu.Append(wx.ID_ANY, 'Keyboard shortcuts...\tCtrl+Alt+K')
    focus_library = menu.Append(wx.ID_ANY, 'Focus library and research topic\tCtrl+Alt+L')
    focus_status = menu.Append(wx.ID_ANY, 'Focus operation status\tCtrl+Alt+S')
    frame.Bind(wx.EVT_MENU, lambda event: frame.choice.SetFocus(), focus_library)
    frame.Bind(wx.EVT_MENU, lambda event: frame.status.SetFocus(), focus_status)
    controller = KeyboardSettings(frame)
    frame.keyboard_settings = controller
    frame.Bind(wx.EVT_MENU, controller.show, settings)
    return controller
