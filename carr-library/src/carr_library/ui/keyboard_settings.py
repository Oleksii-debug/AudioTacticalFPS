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
            part = _part(label)
            if not part or '/' in part:
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


def _decode(raw, commands):
    if raw is None:
        return {}
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite settings.')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Keyboard shortcut settings could not be read.') from exc
    if (not isinstance(value, dict) or set(value) != {'schema', 'overrides', 'originals'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['overrides'], dict) or not isinstance(value['originals'], dict)
            or set(value['originals']) != set(value['overrides'])):
        raise ValueError('Invalid keyboard shortcut settings schema.')
    effective_shortcuts(commands, value['overrides'])
    defaults = {command.action: command.default for command in commands}
    if any(value['originals'][key] != defaults[key] for key in value['overrides']):
        raise ValueError('An original menu shortcut changed in this version. Reset shortcuts before editing them.')
    return dict(value['overrides'])


def load_shortcuts(root, commands):
    """Return overrides and a compare-before-write token; never retarget actions."""
    raw = _bytes(_path(root))
    return _decode(raw, commands), _token(raw)


def save_shortcuts(root, commands, overrides, *, expected_token):
    keys = effective_shortcuts(commands, overrides)
    kept = {command.action: keys[command.action] for command in commands if keys[command.action] != command.default}
    originals = {command.action: command.default for command in commands if command.action in kept}
    value = {'schema': 1, 'overrides': kept, 'originals': originals}
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValueError('Keyboard shortcut settings exceed the supported size.')
    path = _path(root)
    with state_lock(path.parent, 'shortcuts'):
        if _token(_bytes(path)) != expected_token:
            raise ValueError('Another window changed the shortcuts. Reopen this dialog before applying changes.')
        atomic_json(path, value, replace=path.exists())
    return kept, _token(raw)


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
        self.commands = capture_commands(frame.menu_bar)
        self.overrides, self.token, self.root = {}, None, None
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
        raw = None
        try:
            raw = _bytes(_path(self.root))
            overrides = _decode(raw, self.commands)
            self._render(overrides)
            self.overrides, self.token = overrides, _token(raw)
            return True
        except (ValueError, OSError) as exc:
            self._render({})
            self.overrides, self.token = {}, _token(raw)
            self.frame.report(f'Original keyboard shortcuts are active. Saved settings could not be applied: {exc}')
            return False

    def apply(self, overrides):
        if local_path(self.frame.library.root, exists=True) != self.root:
            raise ValueError('The library folder changed. Reopen keyboard shortcuts.')
        effective_shortcuts(self.commands, overrides)
        previous = dict(self.overrides)
        try:
            self._render(overrides)
            kept, token = save_shortcuts(self.root, self.commands, overrides, expected_token=self.token)
        except BaseException:
            self._render(previous)
            raise
        self.overrides, self.token = kept, token

    def show(self, event=None):
        wx = self.wx
        if local_path(self.frame.library.root, exists=True) != self.root:
            self.reload()
        dialog = wx.Dialog(self.frame, title='Keyboard shortcuts', size=(780, 540),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(dialog, label='Choose a command and type a shortcut, for example Ctrl+Shift+K or F9.\n'
                'An empty shortcut removes the assignment. Apply saves the selected shortcut.'), 0, wx.EXPAND | wx.ALL, 8)
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
            layout.Add(status, 1, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            apply = wx.Button(dialog, wx.ID_APPLY, '&Apply')
            reset = wx.Button(dialog, label='&Reset all shortcuts to original keys')
            close = wx.Button(dialog, wx.ID_CANCEL, '&Cancel')
            for button in (apply, reset, close):
                buttons.Add(button, 0, wx.ALL, 6)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            def selected():
                index = choice.GetSelection()
                if not 0 <= index < len(self.commands):
                    raise ValueError('Choose a menu command first.')
                return self.commands[index]
            def refresh(event=None):
                command = selected()
                value = effective_shortcuts(self.commands, self.overrides)[command.action]
                current.ChangeValue(f'Current: {value or "Unassigned"}. Original: {command.default or "Unassigned"}.')
                key.ChangeValue(value)
            def change(event=None):
                try:
                    command = selected()
                    updated = dict(self.overrides)
                    updated[command.action] = key.GetValue()
                    self.apply(updated)
                    refresh()
                    status.ChangeValue(f'Applied shortcut for {command.title}.')
                except (ValueError, OSError, RuntimeError) as exc:
                    status.ChangeValue(f'Shortcut was not applied: {exc}')
                    status.SetFocus()
                    return
                key.SetFocus()
                key.SelectAll()
            def reset_all(event=None):
                try:
                    self.apply({})
                    refresh()
                    status.ChangeValue('All original menu shortcuts restored and saved.')
                except (ValueError, OSError, RuntimeError) as exc:
                    status.ChangeValue(f'Original shortcuts could not be saved: {exc}')
                status.SetFocus()
            choice.Bind(wx.EVT_CHOICE, refresh)
            apply.Bind(wx.EVT_BUTTON, change)
            reset.Bind(wx.EVT_BUTTON, reset_all)
            refresh()
            choice.SetFocus()
            dialog.ShowModal()
        finally:
            dialog.Destroy()


def install_keyboard_settings(frame):
    """Install after all menus exist; the returned controller owns live binding."""
    import wx
    if getattr(frame, 'keyboard_settings', None) is not None:
        frame.keyboard_settings.reload()
        return frame.keyboard_settings
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


# Reading Desk shares the active KeyboardActions registry and keybindings.json.
# The earlier KeyboardSettings APIs above remain only for legacy-file validation.
_READER_ROWS = (
    ('reader.previous', 'Previous source location', 'Ctrl+PageUp'),
    ('reader.next', 'Next source location', 'Ctrl+PageDown'),
    ('reader.goto', 'Go to reading position', 'Ctrl+G'),
    ('reader.first', 'Start at first source location', ''),
    ('reader.back', 'Back in reading session', 'Alt+Left'),
    ('reader.forward', 'Forward in reading session', 'Alt+Right'),
    ('reader.where', 'Where am I?', 'Ctrl+Shift+L'),
    ('reader.bookmark_add', 'Add named page bookmark', 'Ctrl+B'),
    ('reader.bookmarks', 'Manage page bookmarks', 'Ctrl+Shift+B'),
    ('reader.help', 'Reader keyboard guide', 'F1'),
    ('reader.commands', 'Find a reader command', 'Ctrl+Shift+P'),
    ('reader.settings', 'Reader keyboard shortcuts', 'Ctrl+Alt+K'),
    ('reader.narration', 'Prepare narration of this source location', ''),
)


def reader_commands():
    from ..keybindings import Command
    return tuple(Command(identity, label, key, context='reader')
                 for identity, label, key in _READER_ROWS)


def voice_reader_commands():
    """Actual Voice reader controls, stored in the same active preference file."""
    from ..keybindings import Command
    rows = (
        ('voice_reader.read', 'Read selection / all', 'F6'),
        ('voice_reader.pause', 'Pause / resume speech', 'F7'),
        ('voice_reader.stop', 'Stop speech or current reader operation', 'F8'),
        ('voice_reader.save', 'Save reading copy', 'Ctrl+S'),
        ('voice_reader.save_as', 'Save reading copy as', 'Ctrl+Shift+S'),
        ('voice_reader.find', 'Find in reading copy', 'Ctrl+F'),
        ('voice_reader.replace', 'Find and replace in reading copy', 'Ctrl+H'),
        ('voice_reader.pronunciation', 'Reading pronunciation dictionary', 'Ctrl+Shift+D'),
        ('voice_reader.settings', 'Voice reader keyboard shortcuts', 'Ctrl+Alt+K'),
    )
    return tuple(Command(identity, label, key, context='voice_reader')
                 for identity, label, key in rows)


# Reviewed canonical966 IDs map only to actual current menu paths. Unknown or
# absent module commands are never guessed, dropped or activated from JSON.
_CANONICAL_MAIN_PATHS = {
    "library.new": "Library/New library",
    "library.open": "Library/Open existing library folder",
    "library.rename": "Library/Rename or describe library",
    "library.archive": "Library/Archive library",
    "library.restore": "Library/Restore archived library",
    "library.import": "Library/Import books",
    "library.import_center": "Library/Import Center: files, folders and queues",
    "library.resume_import": "Library/Resume interrupted import",
    "library.review_import": "Library/Review pending imported page",
    "library.add_text": "Library/Add original text",
    "library.metadata": "Library/Edit selected book metadata",
    "library.tools": "Library/Library tools",
    "library.backup": "Library/Back up all libraries",
    "library.restore_backup": "Library/Restore backup to a new folder",
    "library.epub_reader": "Library/Read EPUB book",
    "library.epub_contents": "Library/Read electronic book contents",
    "research.export_topic": "Research/Export active topic",
    "research.search_notes": "Research/Search saved research",
    "research.edit_tags": "Research/Edit tags for selected evidence",
    "research.view_tags": "Research/View tags for selected evidence",
    "research.find_tag": "Research/Find evidence by tag",
    "research.save_query": "Research/Save a topic search",
    "research.run_query": "Research/Run a saved topic search",
    "research.manage_queries": "Research/Manage saved topic searches",
    "research.refresh": "Research/Refresh libraries and topics",
    "research.export_outline": "Research/Export topic and subtopics",
    "research.overview": "Research/Library overview",
    "research.checklist": "Research/Source metadata and indexing checklist",
    "research.comparison": "Research/Export source comparison matrix",
    "research.search_center": "Research/Research Search Center",
    "academic.open": "Academic/Academic library",
    "academic.link_book": "Academic/Add selected book to academic library",
    "bibliography.review_metadata": "Bibliography/Review reference metadata for selected book",
    "bibliography.export": "Bibliography/Export library or collection bibliography",
    "source.review": "Source/Review selected PDF page",
    "source.revoke": "Source/Revoke selected PDF page review",
    "reading.open": "Reading/Read selected book",
    "reading.export_positions": "Reading/Export reading positions and bookmarks",
    "reading.import_positions": "Reading/Import reading positions and bookmarks",
    "help.keyboard": "Help/Keyboard guide",
    "help.installation": "Help/Check this installation",
    "commands.find": "Help/Find a command",
    "options.keyboard": "Options/Keyboard shortcuts"
}


def migrate_canonical_shortcuts(raw, commands, current=None):
    """Review a canonical keymap without rewriting its file or current choices."""
    from ..keybindings import normalize_shortcut as normalize, validate_bindings
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 64 * 1024:
        raise ValueError('Canonical keyboard preferences must be a bounded UTF-8 JSON file.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid canonical keyboard preferences.') from exc
    if (type(value) is not dict or set(value) != {'version', 'bindings'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['bindings']) is not dict or len(value['bindings']) > 512):
        raise ValueError('Unsupported canonical keyboard preferences.')
    commands = tuple(commands)
    indexed = {command.identity: command for command in commands}
    paths = {command.identity.casefold(): command.identity
             for command in commands if command.context == 'main'}
    candidate = dict(current or {})
    for identity, raw_key in value['bindings'].items():
        if (not isinstance(identity, str) or len(identity) > 100
                or re.fullmatch(r'[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*', identity) is None):
            raise ValueError('Invalid canonical keyboard command identity.')
        if identity.startswith('reader.'):
            target = identity if identity in indexed and indexed[identity].context == 'reader' else None
        else:
            path = _CANONICAL_MAIN_PATHS.get(identity)
            target = paths.get(path.casefold()) if path is not None else None
        if target is None:
            raise ValueError('Canonical command is unavailable for reviewed migration: ' + identity
                             + '. Its original file is retained.')
        key = normalize(raw_key)
        if target in candidate and normalize(candidate[target]) != key:
            raise ValueError('Conflicting current and canonical shortcuts for ' + target
                             + '. Review the existing assignment before importing.')
        candidate[target] = key
    validate_bindings(commands, candidate)
    return candidate


def read_shortcut_import(path, commands, current):
    """Accept the active format or explicitly review the canonical legacy map."""
    from ..keybindings import decode_preferences
    raw = _bytes(path)
    if raw is None:
        raise ValueError('The selected keyboard preference file is missing.')
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid keyboard preferences JSON.') from exc
    if type(value) is dict and set(value) == {'version', 'bindings'}:
        return migrate_canonical_shortcuts(raw, commands, current)
    return decode_preferences(raw)


def reader_accelerator(shortcut, identifier, wx, *, context='reader'):
    from ..keybindings import normalize_context_shortcut
    shortcut = normalize_context_shortcut(shortcut, context)
    *modifiers, key = shortcut.split('+')
    mask = 0
    for modifier in modifiers:
        mask |= {'Ctrl': wx.ACCEL_CTRL, 'Alt': wx.ACCEL_ALT, 'Shift': wx.ACCEL_SHIFT}[modifier]
    names = {'PageUp': 'WXK_PAGEUP', 'PageDown': 'WXK_PAGEDOWN',
             'Left': 'WXK_LEFT', 'Right': 'WXK_RIGHT', 'Up': 'WXK_UP', 'Down': 'WXK_DOWN',
             'Home': 'WXK_HOME', 'End': 'WXK_END', 'Space': 'WXK_SPACE',
             'Delete': 'WXK_DELETE', 'Backspace': 'WXK_BACK', 'Enter': 'WXK_RETURN'}
    code = ord(key) if len(key) == 1 else getattr(wx, names.get(key, 'WXK_' + key))
    return mask, code, int(identifier)


class ReaderKeyboardAdapter:
    """Context dispatch and native tables borrowed from one active key registry."""
    def __init__(self, reader, manager=None):
        import wx
        self.reader = reader
        self.manager = manager if manager is not None else getattr(reader.parent, 'keyboard_actions', None)
        self._root = Path(reader.controller.desk.library.root)
        self._library = reader.controller.desk.library
        self._ids, self._disposed = {}, False
        self._commands = tuple(command for command in reader_commands()
                               if command.identity in reader.command_buttons)
        for command in self._commands:
            identifier = wx.NewIdRef()
            self._ids[command.identity] = identifier
            reader.dialog.Bind(wx.EVT_MENU,
                lambda event, identity=command.identity: self.invoke(identity), id=int(identifier))
        if self.manager is not None:
            self.manager._reader_adapters.add(self)
        self.refresh()

    def _current(self):
        if Path(self.reader.controller.desk.library.root) != self._root:
            return False
        if self.manager is None:
            return True
        frame = self.manager.frame
        return (frame.library is self._library and Path(frame.library.root) == self._root
                and getattr(frame, 'keyboard_actions', None) is self.manager
                and not getattr(frame, '_closing', False))

    def available(self, identity):
        if self._disposed or self.reader.closed or self.reader.busy or not self._current():
            return False
        if identity == 'reader.settings' and self.manager is None:
            return False
        pair = self.reader.command_buttons.get(identity)
        if pair is None:
            return False
        try:
            return pair[0].IsEnabled()
        except RuntimeError:
            return False

    def invoke(self, identity):
        if not self.available(identity):
            return False
        self.reader.command_buttons[identity][1](None)
        return True

    def _keys(self):
        from ..keybindings import validate_bindings
        if self.manager is None:
            return validate_bindings(self._commands, {})
        return validate_bindings(self.manager.commands, self.manager.overrides)

    def refresh(self, values=None):
        if self._disposed or self.reader.closed or not self._current():
            return
        import wx
        keys, entries = self._keys() if values is None else values, []
        for command in self._commands:
            shortcut = keys[command.identity]
            if shortcut:
                entries.append(reader_accelerator(shortcut, self._ids[command.identity], wx))
            button = self.reader.command_buttons[command.identity][0]
            button.SetLabel(button.GetName() + (f' ({shortcut})' if shortcut else ''))
            if command.identity == 'reader.settings' and self.manager is None:
                button.Disable()
        self.reader.accelerators = entries
        self.reader.dialog.SetAcceleratorTable(wx.AcceleratorTable(entries))

    def apply_overrides(self, overrides):
        """Replace only this Reader context, preserving all main-window choices."""
        if self._disposed or self.reader.closed or not self._current() or self.manager is None:
            raise ValueError('Open this Reader from the current main window to change its shortcuts.')
        if type(overrides) is not dict:
            raise ValueError('Reader shortcuts require a command-to-key map.')
        identities = {command.identity for command in self._commands}
        if set(overrides) - identities:
            raise ValueError('Only currently available Reader commands can be changed here.')
        current = {key: value for key, value in self.manager.overrides.items() if key not in identities}
        self.manager.save({**current, **overrides})

    def shortcut(self, identity):
        return self._keys()[identity]

    def guide(self):
        return '\n'.join(f'{self.shortcut(command.identity) or "Not assigned"}: {command.label}.'
                         for command in self._commands)

    def settings(self, event=None):
        if not self.available('reader.settings'):
            return
        self.manager.change(context='reader', parent=self.reader.dialog, report=self.reader._error)

    def commands(self, event=None):
        if not self.available('reader.commands'):
            return
        rows = tuple(command for command in self._commands if command.identity != 'reader.commands')
        index = self.reader.dialogs.choose(self.reader.dialog, 'Reader commands',
            'Choose a command for this Reading Desk.',
            [f'{command.label}; {self.shortcut(command.identity) or "No shortcut"}'
             + ('' if self.available(command.identity) else '; unavailable now') for command in rows])
        if index is not None:
            if type(index) is not int or not 0 <= index < len(rows):
                raise ValueError('Choose one Reader command.')
            self.invoke(rows[index].identity)

    def close(self):
        if self._disposed:
            return
        self._disposed = True
        if self.manager is not None:
            self.manager._reader_adapters.discard(self)
        import wx
        for identifier in self._ids.values():
            try:
                self.reader.dialog.Unbind(wx.EVT_MENU, id=int(identifier))
            except RuntimeError:
                break  # The native dialog has already destroyed its bindings.


class VoiceReaderKeyboardAdapter:
    """Borrow the shared registry for the speech reader's actual controls."""
    def __init__(self, dialog, parent, controls, *, guard, alive, report, wx=None):
        if wx is None:
            import wx
        self.wx, self.dialog, self.parent = wx, dialog, parent
        self.controls, self.guard, self.alive, self.report = controls, guard, alive, report
        self.manager = getattr(parent, 'keyboard_actions', None)
        self.library = getattr(parent, 'library', None)
        self.root = getattr(self.library, 'root', None)
        self.closed = False
        self.commands = tuple(command for command in voice_reader_commands() if command.identity in controls)
        self.identifiers = {command.identity: wx.NewIdRef() for command in self.commands}
        for identity, identifier in self.identifiers.items():
            dialog.Bind(wx.EVT_MENU, lambda event, key=identity: self.invoke(key), id=int(identifier))
        if self.manager is not None:
            self.manager._reader_adapters.add(self)
        self.refresh()

    def _current(self):
        return (not self.closed and self.alive()
                and getattr(self.parent, 'library', None) is self.library
                and getattr(self.library, 'root', None) == self.root
                and not getattr(self.parent, '_closing', False)
                and (self.manager is None or getattr(self.parent, 'keyboard_actions', None) is self.manager))

    def invoke(self, identity):
        if not self._current() or identity not in self.controls:
            return False
        control, handler = self.controls[identity]
        if not control.IsEnabled():
            return False
        try:
            # Stop must remain reachable during a source revocation or active
            # operation; every other action requires the current source scope.
            if identity != 'voice_reader.stop':
                self.guard()
            handler(None)
            return True
        except Exception as exc:
            self.report(str(exc), focus=True)
            return False

    def refresh(self, values=None):
        if not self._current():
            return
        from ..keybindings import validate_bindings
        if values is None:
            values = (validate_bindings(self.manager.commands, self.manager.overrides)
                      if self.manager is not None else validate_bindings(self.commands, {}))
        entries = []
        labels = {'voice_reader.read': 'Read &selection / all',
                  'voice_reader.pause': '&Pause / resume', 'voice_reader.stop': 'S&top'}
        for command in self.commands:
            shortcut = values[command.identity]
            if shortcut:
                entries.append(reader_accelerator(shortcut, self.identifiers[command.identity],
                                                  self.wx, context='voice_reader'))
            control = self.controls[command.identity][0]
            control.SetToolTip(command.label + '. Shortcut: ' + (shortcut or 'not assigned'))
            if command.identity in labels:
                control.SetLabel(labels[command.identity] + (f' ({shortcut})' if shortcut else ''))
            if command.identity == 'voice_reader.settings' and self.manager is None:
                control.Disable()
        self.dialog.SetAcceleratorTable(self.wx.AcceleratorTable(entries))

    def settings(self, event=None):
        if self._current() and self.manager is not None:
            self.manager.change(context='voice_reader', parent=self.dialog,
                                report=lambda message: self.report(message, focus=True))

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.manager is not None:
            self.manager._reader_adapters.discard(self)
        for identifier in self.identifiers.values():
            try:
                self.dialog.Unbind(self.wx.EVT_MENU, id=int(identifier))
            except RuntimeError:
                break
