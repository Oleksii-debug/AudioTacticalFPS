"""Native menu shortcut preferences, with stable action paths and safe conflicts.

These are application accelerators, never OS-wide hotkeys. The installer runs
after the frame's menu installers. Call ``frame.keyboard_settings.reload()``
after changing the data root. Native Windows/NVDA behavior requires human QA.
"""
from dataclasses import dataclass, replace
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


def register_command_identity(menu_bar, item, name):
    """Keep a renamed action on its retained menu bar, not a transient wx wrapper."""
    if (not isinstance(name, str) or not name or name != name.casefold().strip()
            or '/' in name or any(ord(char) < 32 for char in name)):
        raise ValueError('A menu command needs a stable, unambiguous action path.')
    known = getattr(menu_bar, '_carr_command_names', None)
    if known is None:
        known = {}
    if type(known) is not dict:
        raise ValueError('The retained menu command identities are invalid.')
    identity, value = item.GetId(), (name, _label(item.GetItemLabel()))
    if identity in known and known[identity] != value:
        raise ValueError('A native menu ID already identifies a different command.')
    known[identity] = value
    menu_bar._carr_command_names = known


def capture_commands(menu_bar, *, normalizer=None):
    """Capture final menu paths once, before preference labels are applied."""
    commands, known = [], set()
    names = getattr(menu_bar, '_carr_command_names', {})
    if type(names) is not dict:
        raise ValueError('The retained menu command identities are invalid.')
    def visit(menu, parents, display):
        for item in menu.GetMenuItems():
            if item.IsSeparator():
                continue
            label = item.GetItemLabel()
            # Installers may keep an existing command identity when its visible
            # label expands. Saved shortcuts continue to address that same action.
            part = getattr(item, 'carr_command_name', _part(label))
            if item.GetId() in names:
                recorded = names[item.GetId()]
                if (type(recorded) is not tuple or len(recorded) != 2
                        or recorded[1] != _label(label)
                        or getattr(item, 'carr_command_name', recorded[0]) != recorded[0]):
                    raise ValueError('The native menu command identity changed.')
                part = recorded[0]
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
                                         (normalizer(key) if normalizer is not None
                                          else normalize_shortcut(key, original=True)), label, item))
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
    from ..keyboard_preferences import _read
    raw = _read(Path(path).absolute(), limit=MAX_SETTINGS_BYTES)
    if raw is not None and not raw:
        raise ValueError('Keyboard shortcut settings exceed the supported size.')
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


class _LegacyKeyboardSettings:
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
        return write_export(destination, raw.decode('utf-8'), private_root=self.root,
                            require_absent=True, cancelled=check)

    def show(self, event=None):
        wx = self.wx
        if (self.frame.library is not self.library
                or local_path(self.frame.library.root, exists=True) != self.root):
            self.reload()
        self._guard()
        opened = (self.library, self.root, self.menu_bar, self.commands)
        alive, reviewed, undo = True, None, None
        class KeymapDialog(wx.Dialog):
            def __init__(self):
                super().__init__(self_frame, title='Keyboard shortcuts', size=(900, 680),
                                 style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
                self._native_id = self.GetId()
                self._destroy_requested = False
                self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)

            def on_destroy(self, event):
                nonlocal alive
                if event.GetId() == self._native_id:
                    alive = False
                    self._destroy_requested = True
                event.Skip()

            def Destroy(self):
                nonlocal alive
                alive = False
                if getattr(self, '_destroy_requested', False) or not self:
                    return False
                self._destroy_requested = True
                return super().Destroy()

        self_frame = self.frame
        dialog = KeymapDialog()
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
                if not alive or not dialog:
                    raise ValueError('The shortcut window closed. Reopen keyboard shortcuts.')
                self._guard()
                if (not alive or self.library is not opened[0] or self.root != opened[1]
                        or self.menu_bar is not opened[2] or self.commands is not opened[3]):
                    raise ValueError('The library or commands changed. Reopen keyboard shortcuts.')
                return False
            def report(message, *, focus=True):
                if alive and dialog:
                    status.ChangeValue(message)
                    if focus:
                        status.SetFocus()
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
                    report(f'Shortcut was not applied: {exc}')
                    return
                if alive:
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
                    report(f'Original shortcuts could not be saved: {exc}')
                if alive:
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
                    report(f'Shortcuts could not be restored: {exc}')
                if alive:
                    status.SetFocus()
            def file_choice(importing):
                guard()
                picker = wx.FileDialog(dialog, 'Preview keymap import' if importing else 'Export keymap to a new file',
                        wildcard='Keyboard shortcuts (*.json)|*.json',
                        style=(wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) if importing else wx.FD_SAVE)
                try:
                    answer = picker.ShowModal()
                    if not alive or not dialog or not picker:
                        return None
                    if answer != wx.ID_OK:
                        return None
                    destination = picker.GetPath()
                finally:
                    if picker:
                        picker.Destroy()
                if not alive or not dialog:
                    return None
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
                    report(f'Keymap was not imported: {exc}')
                if alive:
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
                    report(f'Keymap was not applied: {exc}')
                if alive:
                    status.SetFocus()
            def export_file(event=None):
                if not alive:
                    return
                try:
                    destination = file_choice(False)
                    if destination is None:
                        return
                    from ..research.export_file import export_completion
                    receipt = self.export_keymap(destination, cancelled=guard)
                    report(export_completion('Current saved keymap exported to a new file. Existing files are never replaced.', receipt))
                except (ValueError, OSError, RuntimeError) as exc:
                    report(f'Keymap was not exported: {exc}')
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
            if dialog:
                dialog.Destroy()



"""Native keyboard settings and searchable commands for the existing desktop.

Only application-local commands are installed. Menu events keep their original
handlers, authorization checks and dirty-note protection. The persisted file
contains stable IDs and shortcut text, never Python callbacks or wx IDs.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import weakref

from ..keyboard_preferences import KeymapStore, import_legacy_keymap, legacy_keymap_path
from ..research.export_file import write_export
from .export_destination import confirm_export_destination
from .builtin_keyboard_commands import MAIN_COMMANDS, builtin_commands, plain_caption
from .command_registry import CommandRegistry, CommandSpec, normalize_shortcut as _canonical_shortcut


CONTEXT_NAMES = {'main': 'Main window', 'reader': 'Reading Desk'}


@dataclass
class _MenuBinding:
    item: object
    caption: str
    top_index: int


def _menu_items(menu):
    for item in menu.GetMenuItems():
        if item.IsSeparator():
            continue
        submenu = item.GetSubMenu()
        if submenu is not None:
            yield from _menu_items(submenu)
        else:
            yield item


def accelerator_tuple(shortcut, identifier):
    """Convert validated portable shortcut text through wx's own parser."""
    import wx
    entry = wx.AcceleratorEntry()
    if not entry.FromString(shortcut):
        raise ValueError('This keyboard combination is unavailable: ' + shortcut)
    return entry.GetFlags(), entry.GetKeyCode(), int(identifier)


class KeyboardManager:
    def __init__(self, frame=None, *, store=None, registry=None, wx_module=None):
        self.frame = frame
        self.wx_module = wx_module
        self.registry = registry or CommandRegistry(builtin_commands())
        self.store = store
        self.overrides = {}
        self.load_notice = ''
        self.bindings = {}
        self._adapters = weakref.WeakSet()
        self._unmanaged = {}
        self._pending_saved = None
        self._load()

    def _load(self):
        self.overrides = {}
        self.load_notice = ''
        self._pending_saved = None
        self._load_failed = False
        if self.store is not None:
            try:
                loaded = self.store.load()
                self._pending_saved = loaded
                if isinstance(self.registry, MenuCommandRegistry):
                    self.registry.legacy = bool(getattr(self.store, 'legacy', False))
                self.overrides = self.registry.validate(loaded)
                self._pending_saved = None
            except (ValueError, OSError) as exc:
                self._load_failed = True
                if isinstance(self.registry, MenuCommandRegistry):
                    self.registry.legacy = bool(getattr(self.store, 'legacy', False))
                self.load_notice = ('Keyboard settings could not be loaded. Default shortcuts are active. '
                                    'The existing file was kept. Open Options, Keyboard shortcuts to repair it. '
                                    + str(exc))

        if isinstance(self.store, LibraryKeymapStore) and self.store.legacy:
            self.load_notice = (self.load_notice + ' ' + self.store.warning).strip()
        elif self.store is not None:
            root = getattr(self.store, 'library_root', self.store.path.parent)
            try:
                earlier = (not self.store.path.exists() and legacy_keymap_path(root).exists())
            except (ValueError, OSError) as exc:
                self.load_notice = (self.load_notice + ' Earlier keyboard settings need review; '
                    'their file was kept. ' + str(exc)).strip()
            else:
                if earlier:
                    self.load_notice = (self.load_notice + ' An earlier keyboard map is preserved. '
                        'Open Keyboard shortcuts, choose Import earlier keymap, then review and Save. '
                        'It has not been applied automatically.').strip()

    def change_root(self, root):
        if hasattr(self, 'legacy_commands'):
            self.store = LibraryKeymapStore(
                root, self.legacy_commands, self.action_ids,
                default_legacy=getattr(self.store, 'default_legacy', False))
        else:
            self.store = KeymapStore(Path(root) / 'keyboard-shortcuts.json')
        self._load()
        self.activate_loaded()
        facade = getattr(self.frame, 'keyboard_settings', None)
        if isinstance(facade, KeyboardSettings) and facade.manager is self:
            facade._synchronize()
        if self.load_notice and self.frame is not None:
            import wx
            wx.CallAfter(lambda: self.frame.report(self.load_notice)
                         if self.load_notice and not getattr(self.frame, '_closing', False) else None)

    def register_menu_command(self, spec, item, *, fallback_unassigned=False):
        """Register an explicit stable ID for a newly installed native module."""
        if spec.context != 'main':
            raise ValueError('A main-window menu command must use the main context.')
        top_index = self._top_index(item)
        known = {command.command_id: command for command in self.registry.commands()}
        proposed = (MenuCommandRegistry(self.registry.commands(), legacy=self.registry.legacy)
                    if isinstance(self.registry, MenuCommandRegistry)
                    else CommandRegistry(self.registry.commands()))
        if spec.command_id not in known:
            proposed.register(spec)
        elif any(getattr(known[spec.command_id], field) != getattr(spec, field)
                 for field in ('command_id', 'label', 'context', 'default_shortcut')):
            raise ValueError('The command is already registered with different metadata.')
        old_registry, old_bindings, old_unmanaged = self.registry, self.bindings, self._unmanaged
        old_overrides = self.overrides
        old_notice = self.load_notice
        old_label = item.GetItemLabel()
        self.registry, self.bindings = proposed, dict(self.bindings)
        self.bindings[spec.command_id] = _MenuBinding(item, item.GetItemLabel().split('\t', 1)[0], top_index)
        try:
            self._discover()
            if self._pending_saved is not None:
                try:
                    recovered = self.validate(self._pending_saved)
                except ValueError:
                    pass  # Other modules may still be registering their IDs.
                else:
                    self.overrides = recovered
            try:
                self.validate(self.overrides)
            except ValueError:
                if not fallback_unassigned or spec.command_id in known:
                    raise
                fallback = dict(self.overrides)
                fallback[spec.command_id] = ''
                self.validate(fallback)
                self.overrides = fallback
                self.load_notice = (f'The new command "{spec.label}" has no shortcut because its default '
                                    'conflicts with an existing command. Your saved keymap was kept. '
                                    'Use Keyboard shortcuts to choose an assignment.')
            self._render()
        except Exception:
            self.registry, self.bindings, self._unmanaged = old_registry, old_bindings, old_unmanaged
            self.overrides = old_overrides
            self.load_notice = old_notice
            item.SetItemLabel(old_label)
            self._render()
            raise
        if self._pending_saved is not None and self.overrides == self._pending_saved:
            self._pending_saved = None
            self._load_failed = False
            self.load_notice = ''
        if hasattr(self, '_captured'):
            self._refresh_capture()

    def _top_index(self, wanted):
        menu_bar = self.frame.GetMenuBar()
        for index in range(menu_bar.GetMenuCount()):
            if any(item.GetId() == wanted.GetId() for item in _menu_items(menu_bar.GetMenu(index))):
                return index
        raise ValueError('The command is not attached to the current menu bar.')

    def _discover(self):
        if self.frame is None:
            return
        bar = self.frame.GetMenuBar()
        known = {(plain_caption(menu), plain_caption(label)): identity
                 for identity, menu, label, _ in MAIN_COMMANDS}
        current = {}
        present = {}
        for index in range(bar.GetMenuCount()):
            menu_caption = plain_caption(bar.GetMenuLabel(index))
            for item in _menu_items(bar.GetMenu(index)):
                present[item.GetId()] = index
                label = item.GetItemLabel()
                identity = known.get((menu_caption, plain_caption(label)))
                if identity:
                    previous = current.get(identity)
                    if previous is not None and previous.item.GetId() != item.GetId():
                        raise ValueError('Duplicate native command: ' + label.split('\t', 1)[0])
                    current[identity] = _MenuBinding(item, label.split('\t', 1)[0], index)
        # Explicit registrations survive discovery; removed menu objects do not.
        for identity, binding in self.bindings.items():
            if binding.item.GetId() in present and identity not in current:
                current[identity] = _MenuBinding(binding.item, binding.caption, present[binding.item.GetId()])
        self.bindings = current
        managed = {binding.item.GetId() for binding in current.values()}
        self._unmanaged = {}
        for index in range(bar.GetMenuCount()):
            for item in _menu_items(bar.GetMenu(index)):
                label = item.GetItemLabel()
                if item.GetId() in managed or '\t' not in label:
                    continue
                try:
                    shortcut = _canonical_shortcut(label.split('\t', 1)[1])
                except ValueError:
                    # OS-owned Exit/Alt+F4 stays with the native window manager.
                    continue
                if shortcut:
                    self._unmanaged[shortcut] = label.split('\t', 1)[0].replace('&', '')

    def guard_native(self):
        if self.frame is None or not self.frame or getattr(self.frame, '_closing', False):
            raise ValueError('The application is closing; reopen command settings.')
        if hasattr(self.frame, 'IsBeingDeleted') and self.frame.IsBeingDeleted():
            raise ValueError('The application is closing; reopen command settings.')
        if not hasattr(self, '_captured'):
            return
        current = _capture_shared_commands(self.frame.GetMenuBar())
        expected = {(row.menu_id, row.action, _label(row.original_label)) for row in self._captured}
        observed = {(row.menu_id, row.action, _label(row.original_label)) for row in current}
        if observed != expected:
            raise ValueError('The menu commands changed. Reopen the application before editing shortcuts.')
        keys = self.registry.effective(self.overrides)
        if any(not binding.item for binding in self.bindings.values()):
            raise ValueError('A retained native menu item was destroyed. Reopen keyboard settings.')
        by_id = {binding.item.GetId(): identity for identity, binding in self.bindings.items()}
        if any(row.menu_id not in by_id or row.default != keys[by_id[row.menu_id]] for row in current):
            raise ValueError('The native shortcut bindings changed. Reopen keyboard settings.')

    def _refresh_capture(self):
        current = _capture_shared_commands(self.frame.GetMenuBar())
        previous = {row.menu_id: row for row in self._captured}
        by_id = {binding.item.GetId(): identity for identity, binding in self.bindings.items()}
        captured, commands, identities = [], [], {}
        for row in current:
            identity = by_id.get(row.menu_id)
            if identity is None:
                raise ValueError('A new native command must register its semantic identity.')
            if row.menu_id in previous:
                original = previous[row.menu_id]
                if row.action != original.action or _label(row.original_label) != _label(original.original_label):
                    raise ValueError('A registered native command changed its action path.')
                row = replace(row, default=original.default, original_label=original.original_label)
            else:
                key = self.registry.get(identity).default_shortcut
                row = replace(row, default=key,
                              original_label=row.original_label.split('\t', 1)[0] + ('\t' + key if key else ''))
            captured.append(row)
            action = _LEGACY_ACTIONS.get(identity, row.action)
            commands.append(replace(row, action=action))
            identities[action] = identity
        self._captured = tuple(captured)
        self.legacy_commands, self.action_ids = tuple(commands), identities
        if isinstance(self.store, LibraryKeymapStore):
            self.store.commands, self.store.identities = self.legacy_commands, dict(identities)
        facade = getattr(self.frame, 'keyboard_settings', None)
        if isinstance(facade, KeyboardSettings) and facade.manager is self:
            facade.commands = self.legacy_commands
            facade._synchronize()

    def legacy_action_paths(self):
        self.guard_native()
        paths, bindings = {}, {}
        effective = self.registry.effective(self.overrides)
        for identity, binding in self.bindings.items():
            native_id = binding.item.GetId()
            if native_id in bindings:
                raise ValueError('Two registered commands share one native menu identity.')
            bindings[native_id] = identity, binding
        for row in _capture_shared_commands(self.frame.GetMenuBar()):
            bound = bindings.get(row.menu_id)
            if bound is None:
                paths.setdefault(row.action, []).append(None)
                continue
            identity, binding = bound
            if (row.default != effective[identity]
                    or _label(row.original_label) != _label(binding.caption)):
                raise ValueError('The native command binding changed. Reopen keyboard settings.')
            paths.setdefault(row.action, []).append(identity)
        for identity, action in _LEGACY_ACTIONS.items():
            if identity in self.bindings and any(values == [identity] for values in paths.values()):
                aliases = paths.setdefault(action, [])
                if identity not in aliases:
                    aliases.append(identity)
        return paths

    def earlier_keymap_draft(self):
        if self.store is None or self.frame is None or getattr(self.frame, '_closing', False):
            raise ValueError('Open this library in the main window before importing its earlier shortcuts.')
        root = getattr(self.store, 'library_root', self.store.path.parent)
        if local_path(self.frame.library.root, exists=True) != local_path(root, exists=True):
            raise ValueError('The active library changed. Reopen keyboard settings.')
        proposed, unassigned = import_legacy_keymap(
            legacy_keymap_path(root), self.registry, self.legacy_action_paths())
        return self.validate(proposed), unassigned

    def validate(self, overrides):
        normalized = self.registry.validate(overrides)
        effective = self.registry.effective(normalized)
        for command in self.registry.commands('main'):
            shortcut = effective[command.command_id]
            if shortcut and shortcut in self._unmanaged:
                raise ValueError(f'{shortcut} is used by {self._unmanaged[shortcut]}. '
                                 'Choose a different shortcut.')
        return normalized

    def refresh(self):
        if self.frame is not None and getattr(self.frame, '_closing', False):
            return
        self._discover()
        self.validate(self.overrides)
        self._render()

    def _convert_shortcut(self, shortcut, identifier):
        if self.wx_module is None:
            return accelerator_tuple(shortcut, identifier)
        key = normalize_shortcut(shortcut, original=True)
        row = MenuCommand('compatibility', 'Compatibility command', identifier, key, '', None)
        return accelerator_entries((row,), {row.action: key}, self.wx_module)[0]

    def _render(self):
        effective = self.registry.effective(self.overrides)
        for identity, binding in self.bindings.items():
            shortcut = effective[identity]
            # Updating the native label removes the old implicit accelerator too.
            binding.item.SetItemLabel(binding.caption + ('\t' + shortcut if shortcut else ''))
        if self.frame is not None and hasattr(self, 'legacy_commands'):
            if self.wx_module is None:
                import wx
            else:
                wx = self.wx_module
            entries = []
            for identity, binding in self.bindings.items():
                shortcut = effective[identity]
                if shortcut:
                    entries.append(self._convert_shortcut(shortcut, binding.item.GetId()))
            self.frame.SetAcceleratorTable(wx.AcceleratorTable(entries))
        for adapter in tuple(self._adapters):
            adapter.refresh()

    def activate_loaded(self):
        """An incompatible saved map must not prevent opening the application."""
        try:
            self.refresh()
        except ValueError as exc:
            self.overrides = {}
            defaults = self.registry.effective({})
            for command in self.registry.commands('main'):
                if defaults[command.command_id] in self._unmanaged:
                    self.overrides[command.command_id] = ''
            self.refresh()
            self.load_notice = ('Some keyboard settings conflict with newly installed commands. '
                                'Safe defaults are active; conflicting defaults are unassigned. '
                                'The saved file was kept. Review Options, Keyboard shortcuts. ' + str(exc))

    def apply_overrides(self, overrides):
        if self.frame is not None:
            self.guard_native()
        normalized = self.validate(overrides)
        # Validate native key conversion before writing or changing any window.
        effective = self.registry.effective(normalized)
        for identity, shortcut in effective.items():
            if shortcut and (self.wx_module is None or identity in self.bindings):
                self._convert_shortcut(shortcut, 1)
        previous = dict(self.overrides)
        self.overrides = normalized
        try:
            self.refresh()
            if self.store is not None:
                normalized = self.store.save(normalized, self.registry)
                self.overrides = normalized
        except Exception:
            self.overrides = previous
            self._render()
            raise
        self.load_notice = getattr(self.store, 'warning', '')
        self._pending_saved = None
        return dict(self.overrides)

    def convert_overrides(self, overrides, *, expected_token):
        if not isinstance(self.store, LibraryKeymapStore):
            raise ValueError('There is no earlier library keymap to convert.')
        self.guard_native()
        normalized = self.store.conversion_draft(overrides, self.registry)
        for identity, shortcut in self.registry.effective(normalized).items():
            if shortcut and (self.wx_module is None or identity in self.bindings):
                self._convert_shortcut(shortcut, 1)
        previous = dict(self.overrides)
        self.overrides = normalized
        try:
            self._render()
            self.overrides = self.store.convert(normalized, self.registry, expected_token=expected_token)
        except Exception:
            self.overrides = previous
            self._render()
            raise
        if isinstance(self.registry, MenuCommandRegistry):
            self.registry.legacy = False
        self.load_notice = self.store.warning
        facade = getattr(self.frame, 'keyboard_settings', None)
        if isinstance(facade, KeyboardSettings):
            facade._synchronize()
        return dict(self.overrides)

    def shortcut(self, command_id):
        return self.registry.shortcut(command_id, self.overrides)

    def native_state(self):
        states = {}
        if self.frame is None or not self.frame or getattr(self.frame, '_closing', False):
            return states
        bar = self.frame.GetMenuBar()
        def walk(menu, enabled, path):
            for item in menu.GetMenuItems():
                if item.IsSeparator():
                    continue
                identity = item.GetId()
                if identity in states:
                    raise ValueError('Native menu IDs must identify separate actions.')
                active = enabled and item.IsEnabled()
                current = (*path, _part(item.GetItemLabel()))
                states[identity] = item, active, current
                child = item.GetSubMenu()
                if child is not None:
                    walk(child, active, current)
        for index in range(bar.GetMenuCount()):
            walk(bar.GetMenu(index), self.frame.IsEnabled() and bar.IsEnabledTop(index),
                 (_part(bar.GetMenuLabel(index)),))
        return states

    def available(self, command_id):
        binding = self.bindings.get(command_id)
        if binding is None or self.frame is None or getattr(self.frame, '_closing', False):
            return False
        try:
            self.guard_native()
            current = self.native_state().get(binding.item.GetId())
            return current is not None and current[1]
        except (ValueError, RuntimeError):
            return False

    def invoke(self, command_id):
        if not self.available(command_id):
            return False
        import wx
        event = wx.CommandEvent(wx.EVT_MENU.typeId, self.bindings[command_id].item.GetId())
        event.SetEventObject(self.frame)
        return bool(self.frame.GetEventHandler().ProcessEvent(event))

    def help_text(self):
        lines = [
            'Alt opens the native menus. Alt+O opens Options. Tab and Shift+Tab move between controls.',
            'Use Options, Find a command to search commands by name and run the selected action.',
            'Options, Keyboard shortcuts edits separate Main window and Reading Desk bindings.',
            'These shortcuts work inside this application. Your usual text-editing and screen-reader keys remain available.',
            '', 'Current Main window shortcuts:',
        ]
        for command in self.registry.commands('main'):
            if command.command_id in self.bindings:
                lines.append(f'{self.shortcut(command.command_id) or "Not assigned"}: {command.label}')
        lines.extend(('',
            'Within the Reading Desk, open Reader keyboard guide for its current contextual bindings.',
            'Choose a library or research topic before running scoped actions. The original handlers still check access and selections.',
            'Library, Import Center manages files, folders and resumable import queues.',
            'Academic library manages references, reading progress, tags and notes. Bibliography exports references.',
            'Research Search Center searches only the libraries you explicitly select.',
            'Original source text stays read-only. Put your commentary in Evidence and Notes.',
            'Reading or bookmarks do not approve quotations. Unknown printed pagination remains unknown.'))
        return '\n'.join(lines)


def _owned_keyboard_dialog(parent, *, title, size, detach):
    """A retained native owner; direct and parent deletion share the same latch."""
    import wx
    class OwnedDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title=title, size=size)
            self._native_id = self.GetId()
            self._destroy_requested = False
            self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        def _destroyed(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                detach()
            event.Skip()
        def Destroy(self):
            detach()
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()
    return OwnedDialog()


class KeyboardSettingsDialog:
    """Edit a local draft; only Save applies and persists the entire valid map."""
    def __init__(self, parent, manager, *, context='main'):
        import wx
        self.wx, self.manager = wx, manager
        self.closed = False
        self.conversion = None
        self.original_store = manager.store
        self.draft = dict(manager.overrides)
        self.dialog = _owned_keyboard_dialog(parent, title='Keyboard shortcuts', size=(760, 670), detach=self.close)
        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self.dialog, label=(
            'Choose a context and command. Type a combination such as Ctrl+Shift+J; leave it empty to remove it. '
            'Changes take effect only when you choose Save.'))
        explanation.Wrap(710)
        layout.Add(explanation, 0, wx.ALL | wx.EXPAND, 8)
        self.contexts = list(dict.fromkeys(command.context for command in manager.registry.commands()))
        layout.Add(wx.StaticText(self.dialog, label='Command context'), 0, wx.LEFT | wx.TOP, 8)
        self.context = wx.Choice(self.dialog, choices=[CONTEXT_NAMES.get(value, value) for value in self.contexts])
        self.context.SetName('Command context')
        self.context.SetSelection(self.contexts.index(context) if context in self.contexts else 0)
        layout.Add(self.context, 0, wx.ALL | wx.EXPAND, 8)
        layout.Add(wx.StaticText(self.dialog, label='Filter commands'), 0, wx.LEFT, 8)
        self.search = wx.TextCtrl(self.dialog)
        self.search.SetName('Filter keyboard commands')
        layout.Add(self.search, 0, wx.ALL | wx.EXPAND, 8)
        self.commands = wx.ListBox(self.dialog)
        self.commands.SetName('Commands and their current shortcuts')
        layout.Add(self.commands, 1, wx.ALL | wx.EXPAND, 8)
        layout.Add(wx.StaticText(self.dialog, label='Shortcut for selected command'), 0, wx.LEFT, 8)
        self.shortcut = wx.TextCtrl(self.dialog)
        self.shortcut.SetName('Shortcut for selected command; leave empty to unassign')
        layout.Add(self.shortcut, 0, wx.ALL | wx.EXPAND, 8)
        actions = wx.WrapSizer(wx.HORIZONTAL)
        for label, handler in (
                ('&Assign shortcut', self.assign), ('Reset &selected', self.reset_selected),
                ('Reset &all defaults', self.reset_all), ('&Import keymap...', self.import_map),
                ('Import earlier &keymap', self.import_earlier_map),
                ('Edit earlier map and &undo...', self.edit_earlier),
                ('Review canonical &conversion', self.review_conversion),
                ('&Export keymap...', self.export_map)):
            button = wx.Button(self.dialog, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            actions.Add(button, 0, wx.ALL, 4)
        layout.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Keyboard settings status')
        self.status.SetMinSize((-1, 75))
        layout.Add(self.status, 0, wx.ALL | wx.EXPAND, 8)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        save = wx.Button(self.dialog, wx.ID_OK, '&Save')
        save.Bind(wx.EVT_BUTTON, self.save)
        footer.Add(save, 0, wx.ALL, 4)
        footer.Add(wx.Button(self.dialog, wx.ID_CANCEL, 'Cancel'), 0, wx.ALL, 4)
        layout.Add(footer, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.context.Bind(wx.EVT_CHOICE, self.rebuild)
        self.search.Bind(wx.EVT_TEXT, self.rebuild)
        self.commands.Bind(wx.EVT_LISTBOX, self.selected)
        self.rows = []
        self.rebuild()
        if manager.load_notice:
            self.status.ChangeValue(manager.load_notice)
        self.search.SetFocus()

    def close(self):
        self.closed = True

    def _alive(self):
        return (not self.closed and bool(self.dialog)
                and not getattr(self.manager.frame, '_closing', False))

    def _message(self, message):
        if not self._alive():
            return
        self.status.ChangeValue(message)
        self.status.SetFocus()

    def rebuild(self, event=None, *, selected=None):
        if not self._alive():
            return
        if selected is None and self.rows and self.commands.GetSelection() >= 0:
            selected = self.rows[self.commands.GetSelection()].command_id
        context = self.contexts[self.context.GetSelection()]
        needle = self.search.GetValue().strip().casefold()
        self.rows = [command for command in self.manager.registry.commands(context)
                     if needle in command.label.casefold()
                     or needle in self.manager.registry.shortcut(command.command_id, self.draft).casefold()]
        self.commands.Set([f'{command.label} — '
                           f'{self.manager.registry.shortcut(command.command_id, self.draft) or "Not assigned"}'
                           for command in self.rows])
        if self.rows:
            index = next((i for i, row in enumerate(self.rows) if row.command_id == selected), 0)
            self.commands.SetSelection(index)
        self.selected()

    def _selected(self):
        if not self._alive():
            raise ValueError('The shortcut window is closed.')
        index = self.commands.GetSelection()
        if not 0 <= index < len(self.rows):
            raise ValueError('Choose a command first.')
        return self.rows[index]

    def selected(self, event=None):
        if not self._alive():
            return
        try:
            command = self._selected()
        except ValueError:
            self.shortcut.ChangeValue('')
            self.shortcut.Disable()
            return
        self.shortcut.Enable()
        self.shortcut.ChangeValue(self.manager.registry.shortcut(command.command_id, self.draft))

    def assign(self, event=None):
        if not self._alive():
            return
        try:
            command = self._selected()
            proposed = dict(self.draft)
            proposed[command.command_id] = self.shortcut.GetValue()
            self.draft = self.manager.validate(proposed)
            self.rebuild(selected=command.command_id)
            self._message('Shortcut assigned in this draft. Choose Save to apply the changes.')
        except (ValueError, OSError) as exc:
            self._message(str(exc))

    def reset_selected(self, event=None):
        if not self._alive():
            return
        try:
            command = self._selected()
            proposed = dict(self.draft)
            proposed.pop(command.command_id, None)
            self.draft = self.manager.validate(proposed)
            self.rebuild(selected=command.command_id)
            self._message('Default restored for this command in the draft. Choose Save to apply.')
        except (ValueError, OSError) as exc:
            self._message(str(exc))

    def reset_all(self, event=None):
        if not self._alive():
            return
        try:
            self.draft = self.manager.validate({})
            self.rebuild()
            self._message('All default shortcuts are selected in this draft. Choose Save to apply.')
        except ValueError as exc:
            self._message(str(exc))

    def import_map(self, event=None):
        if not self._alive():
            return
        wx = self.wx
        chooser = wx.FileDialog(self.dialog, 'Import keyboard shortcuts', wildcard='Keymap JSON (*.json)|*.json',
                                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            answer = chooser.ShowModal()
            if not self._alive() or not chooser or answer != wx.ID_OK:
                return
            path = Path(chooser.GetPath())
        finally:
            if chooser:
                chooser.Destroy()
        if not self._alive():
            return
        try:
            proposed = self.manager.validate(KeymapStore(path).load())
            self.draft = proposed
            self.rebuild()
            self._message('Keymap imported into the draft. Review it and choose Save to apply.')
        except (ValueError, OSError) as exc:
            self._message('Keymap not imported; the current draft was kept. ' + str(exc))

    def import_earlier_map(self, event=None):
        if not self._alive():
            return
        if self.original_store is not self.manager.store:
            self._message('The active library folder changed. Close and reopen keyboard settings.')
            return
        try:
            proposed, unassigned = self.manager.earlier_keymap_draft()
            self.draft = proposed
            self.rebuild()
            message = ('Earlier shortcuts imported into the review draft only. '
                       'The original file and active shortcuts are unchanged. Choose Save to apply.')
            if unassigned:
                message += (' Newly conflicting defaults are unassigned in this draft: '
                            + '; '.join(unassigned) + '.')
            self._message(message)
        except (ValueError, OSError, RuntimeError) as exc:
            self._message('Earlier shortcuts were not imported; the current draft and files were kept. ' + str(exc))

    def edit_earlier(self, event=None):
        if not self._alive():
            return
        facade = getattr(self.manager.frame, 'keyboard_settings', None)
        if not isinstance(facade, KeyboardSettings) or not getattr(self.manager.store, 'legacy', False):
            self._message('This action applies only to the preserved earlier keymap format.')
            return
        _LegacyKeyboardSettings.show(facade, event)
        if not self._alive():
            return
        self.draft = dict(self.manager.overrides)
        self.conversion = None
        self.rebuild()
        self._message('Earlier-format edits are saved through their original producer. Review the current map again.')

    def review_conversion(self, event=None):
        if not self._alive():
            return
        try:
            if self.original_store is not self.manager.store or not isinstance(self.manager.store, LibraryKeymapStore):
                raise ValueError('The library folder changed. Reopen keyboard settings.')
            if self.rows and self.commands.GetSelection() >= 0:
                selected = self._selected()
                self.draft[selected.command_id] = self.shortcut.GetValue()
            proposed = self.manager.store.conversion_draft(self.draft, self.manager.registry)
            self.conversion = (self.manager.store, self.manager.store.token, tuple(sorted(proposed.items())))
            self.draft = proposed
            rows = ['Conversion review only. Save writes canonical contextual shortcuts and keeps '
                    'the complete earlier file unchanged. No changes are active yet.']
            rows.extend(self.manager.registry.get(identity).label + ': ' + (value or 'Unassigned')
                        for identity, value in sorted(proposed.items()))
            self._message('\n'.join(rows))
        except (ValueError, OSError) as exc:
            self.conversion = None
            self._message('Conversion was not prepared. ' + str(exc))

    def export_map(self, event=None):
        if not self._alive():
            return
        wx = self.wx
        chooser = wx.FileDialog(self.dialog, 'Export keyboard shortcuts', defaultFile='keyboard-shortcuts.json',
                                wildcard='Keymap JSON (*.json)|*.json', style=wx.FD_SAVE)
        try:
            answer = chooser.ShowModal()
            if not self._alive() or not chooser or answer != wx.ID_OK:
                return
            path = Path(chooser.GetPath())
        finally:
            if chooser:
                chooser.Destroy()
        if not self._alive():
            return
        try:
            root = (getattr(self.manager.store, 'library_root', self.manager.store.path.parent)
                    if self.manager.store is not None else self.manager.private_root)
            approval = confirm_export_destination(self.dialog, path, private_root=root, wx_module=wx)
            if not self._alive():
                return
            if approval is None:
                self._message('Keymap export cancelled. No file was written.')
                return
            normalized = self.manager.validate(self.draft)
            content = json.dumps({'version': 1, 'bindings': normalized}, ensure_ascii=False, indent=2) + '\n'
            from ..research.export_file import export_completion
            receipt = write_export(path, content, private_root=root, approval=approval)
            self._message(export_completion('The draft keymap was exported. Application shortcuts change only after Save.', receipt))
        except (ValueError, OSError, AttributeError) as exc:
            self._message('Keymap not exported. ' + str(exc))

    def save(self, event=None):
        if not self._alive():
            return
        if self.original_store is not self.manager.store:
            self._message('The active library folder changed. Close and reopen keyboard settings.')
            return
        try:
            if self.rows and self.commands.GetSelection() >= 0:
                command = self._selected()
                proposed = dict(self.draft)
                proposed[command.command_id] = self.shortcut.GetValue()
                self.draft = self.manager.validate(proposed)
            if self.conversion is not None:
                store, token, reviewed = self.conversion
                if store is not self.manager.store or tuple(sorted(self.draft.items())) != reviewed:
                    raise ValueError('The conversion draft changed. Review it again before Save.')
                self.manager.convert_overrides(self.draft, expected_token=token)
            else:
                self.manager.apply_overrides(self.draft)
        except (ValueError, OSError) as exc:
            self._message('Shortcuts were not saved. ' + str(exc))
            return
        if self._alive():
            self.dialog.EndModal(self.wx.ID_OK)

    def show(self):
        try:
            return self.dialog.ShowModal() == self.wx.ID_OK and self._alive()
        finally:
            self.close()
            if self.dialog:
                self.dialog.Destroy()


def show_keyboard_settings(parent, manager, *, context='main'):
    import wx
    focus = wx.Window.FindFocus()
    try:
        return KeyboardSettingsDialog(parent, manager, context=context).show()
    finally:
        if focus:
            try:
                focus.SetFocus()
            except RuntimeError:
                pass


def show_command_launcher(parent, manager, *, context='main', invoke=None, available=None):
    if context == 'main':
        from .command_palette import show_command_palette
        return show_command_palette(manager)
    import wx
    invoke = invoke or manager.invoke
    available = available or manager.available
    focus = wx.Window.FindFocus()
    closed = False
    def detach():
        nonlocal closed
        closed = True
    dialog = _owned_keyboard_dialog(parent, title='Find a command', size=(720, 560), detach=detach)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label='Search commands. Use Tab to choose a result, then Run command.'),
               0, wx.ALL | wx.EXPAND, 8)
    search = wx.TextCtrl(dialog)
    search.SetName('Search commands by name')
    layout.Add(search, 0, wx.ALL | wx.EXPAND, 8)
    results = wx.ListBox(dialog)
    results.SetName('Matching commands and shortcuts')
    layout.Add(results, 1, wx.ALL | wx.EXPAND, 8)
    status = wx.TextCtrl(dialog, style=wx.TE_READONLY)
    status.SetName('Command availability')
    layout.Add(status, 0, wx.ALL | wx.EXPAND, 8)
    footer = wx.BoxSizer(wx.HORIZONTAL)
    run = wx.Button(dialog, wx.ID_OK, '&Run command')
    footer.Add(run, 0, wx.ALL, 4)
    footer.Add(wx.Button(dialog, wx.ID_CANCEL, 'Cancel'), 0, wx.ALL, 4)
    layout.Add(footer, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    # Capture enablement before ShowModal disables the parent window.
    enabled = {command.command_id: available(command.command_id)
               for command in manager.registry.commands(context)}
    rows = []
    choice = []

    def selected(event=None):
        if closed or not dialog:
            return
        index = results.GetSelection()
        can_run = 0 <= index < len(rows) and enabled.get(rows[index].command_id, False)
        run.Enable(can_run)
        status.ChangeValue('Ready to run.' if can_run else 'This command is unavailable in the current view.')

    def rebuild(event=None):
        if closed or not dialog:
            return
        needle = search.GetValue().strip().casefold()
        rows[:] = [command for command in manager.registry.commands(context)
                   if needle in command.label.casefold()]
        results.Set([f'{command.label} — {manager.shortcut(command.command_id) or "Not assigned"}'
                     + ('' if enabled.get(command.command_id) else ' — unavailable') for command in rows])
        if rows:
            results.SetSelection(0)
        selected()

    def accept(event=None):
        if closed or not dialog:
            return
        index = results.GetSelection()
        if 0 <= index < len(rows) and enabled.get(rows[index].command_id, False):
            choice[:] = [rows[index].command_id]
            dialog.EndModal(wx.ID_OK)

    search.Bind(wx.EVT_TEXT, rebuild)
    results.Bind(wx.EVT_LISTBOX, selected)
    results.Bind(wx.EVT_LISTBOX_DCLICK, accept)
    run.Bind(wx.EVT_BUTTON, accept)
    rebuild()
    search.SetFocus()
    try:
        accepted = dialog.ShowModal() == wx.ID_OK and not closed and bool(dialog)
    finally:
        detach()
        if dialog:
            dialog.Destroy()
        if focus:
            try:
                focus.SetFocus()
            except RuntimeError:
                pass
    if accepted and choice:
        return invoke(choice[0])  # Rechecks the live command state after closing.
    return False


def register_menu_command(frame, spec, item, *, fallback_unassigned=False):
    """Register before or after the shared menu manager has been installed."""
    manager = getattr(frame, 'keyboard_commands', None)
    if manager is not None:
        return manager.register_menu_command(spec, item, fallback_unassigned=fallback_unassigned)
    if fallback_unassigned:
        raise ValueError('Register conflict-tolerant optional shortcuts after the shared keyboard manager is installed.')
    if spec.context != 'main':
        raise ValueError('Native main-window menus require the main context.')
    pending = getattr(frame, '_pending_keyboard_commands', [])
    if any(previous.command_id == spec.command_id for previous, _ in pending):
        raise ValueError('This command is already waiting for keyboard registration.')
    frame._pending_keyboard_commands = [*pending, (spec, item)]




class _NativeMenuSpec(CommandSpec):
    """A checked original native key, including historical protected defaults."""
    def __post_init__(self):
        try:
            original = _canonical_shortcut(self.default_shortcut)
        except ValueError:
            original = normalize_shortcut(self.default_shortcut, original=True)
        object.__setattr__(self, 'default_shortcut', '')
        super().__post_init__()
        object.__setattr__(self, 'default_shortcut', original)


class MenuCommandRegistry(CommandRegistry):
    """One command registry with a checked, explicitly selected legacy dialect."""
    def __init__(self, specs=(), *, legacy=False):
        self.legacy = legacy
        super().__init__(specs)

    def validate(self, overrides):
        if not isinstance(overrides, dict) or len(overrides) > MAX_COMMANDS:
            raise ValueError('Shortcut bindings must be a bounded command-to-shortcut object.')
        normalized = {}
        for command_id, value in overrides.items():
            spec = self.get(command_id)
            if self.legacy and isinstance(spec, _NativeMenuSpec) and spec.context == 'main':
                key = normalize_shortcut(value, original=True)
                if key in _RESERVED and key != spec.default_shortcut:
                    normalize_shortcut(key)
            else:
                key = _canonical_shortcut(value)
            normalized[command_id] = key
        occupied = {}
        for spec in self.commands():
            key = normalized.get(spec.command_id, spec.default_shortcut)
            if key:
                identity = spec.context, key
                if identity in occupied:
                    raise ValueError(self._conflict(key, spec, occupied[identity]))
                occupied[identity] = spec
        return normalized


class LibraryKeymapStore:
    """Keep the selected published producer; conversion is a separate action."""
    def __init__(self, root, commands, identities, *, default_legacy=False):
        self.library_root = local_path(root, exists=True)
        self.commands, self.identities = commands, dict(identities)
        self.canonical = KeymapStore(self.library_root / 'keyboard-shortcuts.json')
        self.legacy_path = _path(self.library_root)
        self.default_legacy = default_legacy
        self.legacy = False
        self.path = self.canonical.path
        self.token = None
        self.warning = ''

    def load(self):
        self.warning = ''
        canonical_exists = os.path.lexists(self.canonical.path)
        self.legacy = (not canonical_exists
                       and (os.path.lexists(self.legacy_path) or self.default_legacy))
        self.path = self.legacy_path if self.legacy else self.canonical.path
        if not self.legacy:
            self.token = _token(_bytes(self.path))
            return self.canonical.load()
        raw = _bytes(self.legacy_path)
        self.token = _token(raw)
        choices, conflicts = _decode(raw, self.commands)
        self.warning = ''
        if conflicts:
            self.warning = ('Saved keyboard shortcuts are active. These default shortcuts remain '
                'unassigned to preserve your saved choices: ' + '; '.join(conflicts) +
                '. The earlier saved keymap remains active in its original format; '
                'it is not rewritten or converted automatically.')
        return {self.identities[action]: value for action, value in choices.items()}

    def check_current(self):
        if self.legacy and os.path.lexists(self.canonical.path):
            raise ValueError('Canonical shortcuts appeared in another window. Reload keyboard settings.')
        if _token(_bytes(self.path)) != self.token:
            raise ValueError('Another window changed the shortcuts. Reopen this dialog before applying changes.')

    def save(self, overrides, registry):
        self.check_current()
        normalized = registry.validate(overrides)
        if self.legacy:
            reverse = {identity: action for action, identity in self.identities.items()}
            if any(identity not in reverse for identity in normalized):
                raise ValueError('These contextual bindings need the canonical format. '
                                 'Choose Review conversion, then explicitly Save the converted map.')
            kept, token = save_shortcuts(self.library_root, self.commands,
                                        {reverse[key]: value for key, value in normalized.items()},
                                        expected_token=self.token)
            self.token = token
            self.warning = ''
            return {self.identities[action]: value for action, value in kept.items()}
        from ..keyboard_preferences import validate_keymap
        # A legacy-compatible in-memory registry cannot broaden persisted v1.
        validate_keymap(json.dumps({'version': 1, 'bindings': normalized}).encode('utf-8'))
        value = self.canonical.save(normalized, registry)
        self.token = _token(_bytes(self.path))
        self.warning = getattr(self.canonical, 'warning', '')
        return value

    def conversion_draft(self, overrides, registry):
        from ..keyboard_preferences import validate_keymap
        self.check_current()
        if not self.legacy:
            raise ValueError('This library already uses the canonical shortcut format.')
        normalized = validate_keymap(json.dumps({'version': 1, 'bindings': overrides}).encode('utf-8'))
        return registry.validate(normalized)

    def convert(self, overrides, registry, *, expected_token):
        if not self.legacy or expected_token != self.token:
            raise ValueError('The keymap changed after conversion review. Review it again.')
        normalized = self.conversion_draft(overrides, registry)
        # Preserve the selected legacy snapshot across publication of the new
        # producer, using the same lock as its existing cooperative writer.
        with state_lock(self.legacy_path.parent, 'shortcuts'):
            if self.canonical.load():
                raise ValueError('Canonical shortcuts appeared after review. Reload keyboard settings.')
            if os.path.lexists(self.canonical.path):
                raise ValueError('Canonical shortcuts appeared after review. Reload keyboard settings.')
            self.check_current()
            value = self.canonical.save(normalized, registry)
        self.legacy = False
        self.path = self.canonical.path
        self.token = _token(_bytes(self.path))
        self.warning = 'Canonical shortcuts saved. The earlier keymap file remains unchanged.'
        return value


def _menu_registration(frame, *, default_legacy=False, wx_module=None):
    """Bind every actual native leaf to one semantic identity before loading."""
    captured = _capture_shared_commands(frame.GetMenuBar())
    known = {(plain_caption(menu), plain_caption(label)): identity
             for identity, menu, label, _ in MAIN_COMMANDS}
    explicit = {item.GetId(): spec for spec, item in getattr(frame, '_pending_keyboard_commands', ())}
    specs, bindings, commands, identities = [], {}, [], {}
    legacy_aliases = _LEGACY_ACTIONS
    from .builtin_keyboard_commands import READER_COMMANDS
    for row in captured:
        semantic = known.get((row.action.split('/', 1)[0], _part(row.original_label)))
        if semantic is None:
            semantic = next((identity for identity, action in legacy_aliases.items()
                             if row.action == action), None)
        registered = explicit.get(row.menu_id)
        if registered is not None:
            identity = registered.command_id
            spec = _NativeMenuSpec(identity, registered.label, registered.context,
                                   registered.default_shortcut)
        else:
            identity = semantic or ('menu.' + sha256(row.action.encode('utf-8')).hexdigest()[:32])
            spec = _NativeMenuSpec(identity, row.title, 'main', row.default)
        if identity in bindings:
            raise ValueError('Two native actions share one semantic command identity.')
        specs.append(spec)
        bindings[identity] = row
        action = legacy_aliases.get(identity, row.action)
        commands.append(replace(row, action=action))
        identities[action] = identity
    registry = MenuCommandRegistry((*specs, *READER_COMMANDS))
    commands = tuple(commands)
    store = LibraryKeymapStore(frame.library.root, commands, identities, default_legacy=default_legacy)
    manager = KeyboardManager(frame, registry=registry, store=store, wx_module=wx_module)
    manager.legacy_commands, manager.action_ids = commands, identities
    manager._captured = captured
    for identity, row in bindings.items():
        manager.bindings[identity] = _MenuBinding(
            row.item, row.original_label.split('\t', 1)[0], manager._top_index(row.item))
    manager.activate_loaded()
    return manager


class KeyboardSettings(_LegacyKeyboardSettings):
    """Preserve the old review/undo API over the shared native command manager."""
    def __init__(self, frame, *, wx_module=None, manager=None):
        if wx_module is None:
            import wx as wx_module
        self.frame, self.wx = frame, wx_module
        self.menu_bar = frame.GetMenuBar() if hasattr(frame, 'GetMenuBar') else frame.menu_bar
        existing = getattr(frame, 'keyboard_commands', None)
        self.manager = manager or existing or _menu_registration(frame, default_legacy=True, wx_module=wx_module)
        if existing is not None and existing is not self.manager:
            raise ValueError('The frame already owns a different keyboard manager.')
        frame.keyboard_commands = self.manager
        self.commands = self.manager.legacy_commands
        self.root = self.manager.store.library_root
        self.library = frame.library
        self.overrides, self.token, self.load_notice = {}, None, ''
        self._synchronize()
        if self.load_notice and self.manager.store.legacy:
            self.frame.report(self.load_notice)

    def _synchronize(self):
        self.root = self.manager.store.library_root
        self.library = self.frame.library
        reverse = {identity: action for action, identity in self.manager.action_ids.items()}
        self.overrides = {reverse[key]: value for key, value in self.manager.overrides.items() if key in reverse}
        self.token = self.manager.store.token
        self.load_notice = self.manager.load_notice or self.manager.store.warning
        if self.manager._load_failed and self.manager.store.legacy:
            self.load_notice = ('Original keyboard shortcuts are active. Saved settings could not be applied: '
                                + self.load_notice)

    def reload(self):
        if local_path(self.frame.library.root, exists=True) == self.manager.store.library_root:
            self.manager._load()
            self.manager.activate_loaded()
        else:
            self.manager.change_root(self.frame.library.root)
        self._synchronize()
        if self.load_notice and self.manager.store.legacy:
            self.frame.report(self.load_notice)
        return not self.manager._load_failed

    def _render(self, overrides):
        translated = {self.manager.action_ids[action]: value for action, value in overrides.items()}
        self.manager.overrides = self.manager.validate(translated)
        self.manager._render()

    def _guard(self):
        frame = self.frame
        if (not frame or getattr(frame, '_closing', False)
                or hasattr(frame, 'IsBeingDeleted') and frame.IsBeingDeleted()):
            raise ValueError('The application is closing; reopen keyboard shortcuts.')
        if frame.library is not self.library or local_path(frame.library.root, exists=True) != self.root:
            raise ValueError('The library folder changed. Reopen keyboard shortcuts.')
        if (frame.menu_bar is not self.menu_bar
                or hasattr(frame, 'GetMenuBar') and frame.GetMenuBar() is not self.menu_bar
                or getattr(frame, 'keyboard_settings', self) is not self):
            raise ValueError('The menu commands changed. Reopen the application before editing shortcuts.')
        self.manager.guard_native()

    def _current_scope(self):
        self._guard()
        self.manager.store.check_current()
        self._synchronize()
        return self._scope()

    def apply(self, overrides):
        self._guard()
        effective_shortcuts(self.commands, overrides)
        main_ids = set(self.manager.action_ids.values())
        translated = {key: value for key, value in self.manager.overrides.items() if key not in main_ids}
        translated.update({self.manager.action_ids[action]: value for action, value in overrides.items()})
        self.manager.apply_overrides(translated)
        self._synchronize()

    def show(self, event=None):
        if self.manager.store.legacy:
            return super().show(event)
        return show_keyboard_settings(self.frame, self.manager)




_LEGACY_ACTIONS = {
    'commands.find': 'help/find a command',
    'options.keyboard': 'help/keyboard shortcuts',
    'focus.library': 'help/focus library and research topic',
    'focus.status': 'help/focus operation status',
}


def _capture_shared_commands(menu_bar):
    def native_key(value):
        try:
            return _canonical_shortcut(value)
        except ValueError:
            return normalize_shortcut(value, original=True)
    return capture_commands(menu_bar, normalizer=native_key)


def install_keyboard_settings(frame):
    """Install one semantic manager and its historical API facade."""
    import wx
    existing = getattr(frame, 'keyboard_commands', None)
    if existing is not None:
        existing.refresh()
        if getattr(frame, 'keyboard_settings', None) is None:
            frame.keyboard_settings = KeyboardSettings(frame, manager=existing)
        return existing
    bar = frame.GetMenuBar()
    menu = next((bar.GetMenu(index) for index in range(bar.GetMenuCount())
                 if _part(bar.GetMenuLabel(index)) == 'options'), None)
    if menu is None:
        menu = wx.Menu()
        bar.Append(menu, '&Options')
    occupied = {row.default for row in _capture_shared_commands(bar) if row.default}
    unassigned = []
    def append_action(label, key):
        if key in occupied:
            unassigned.append(_label(label))
            key = ''
        elif key:
            occupied.add(key)
        return menu.Append(wx.ID_ANY, label + ('\t' + key if key else ''))
    commands = append_action('&Find a command...', 'Ctrl+Shift+P')
    settings = append_action('&Keyboard shortcuts...', 'Ctrl+Alt+K')
    focus_library = append_action('Focus library and research topic', 'Ctrl+Alt+L')
    focus_status = append_action('Focus operation status', 'Ctrl+Alt+S')
    manager = _menu_registration(frame)
    frame.keyboard_commands = manager
    if unassigned:
        manager.load_notice = (manager.load_notice + ' Existing native shortcuts were retained. '
            'These new commands remain unassigned: ' + '; '.join(unassigned) + '.').strip()
    facade = KeyboardSettings(frame, manager=manager)
    frame.keyboard_settings = facade
    frame._pending_keyboard_commands = []
    frame.command_palette_item = commands
    frame.Bind(wx.EVT_MENU, lambda event: show_command_launcher(frame, manager), commands)
    frame.Bind(wx.EVT_MENU, lambda event: show_keyboard_settings(frame, manager), settings)

    def focus_owned(name):
        if not frame or getattr(frame, '_closing', False):
            return
        try:
            if not hasattr(frame, 'IsBeingDeleted') or not frame.IsBeingDeleted():
                getattr(frame, name).SetFocus()
        except RuntimeError:
            pass
    frame.Bind(wx.EVT_MENU, lambda event: focus_owned('choice'), focus_library)
    frame.Bind(wx.EVT_MENU, lambda event: focus_owned('status'), focus_status)
    if manager.load_notice:
        wx.CallAfter(lambda: frame.report(manager.load_notice)
                     if manager.load_notice and frame and not getattr(frame, '_closing', False) else None)
    return manager
