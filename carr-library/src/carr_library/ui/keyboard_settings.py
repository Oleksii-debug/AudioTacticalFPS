"""Native keyboard settings and searchable commands for the existing desktop.

Only application-local commands are installed. Menu events keep their original
handlers, authorization checks and dirty-note protection. The persisted file
contains stable IDs and shortcut text, never Python callbacks or wx IDs.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import weakref

from ..keyboard_preferences import KeymapStore
from ..research.export_file import write_export
from .export_destination import confirm_export_destination
from .builtin_keyboard_commands import MAIN_COMMANDS, builtin_commands, plain_caption
from .command_registry import CommandRegistry, normalize_shortcut


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


def resolve_saved_bindings(registry, saved):
    """Keep explicit prior choices ahead of newly introduced default keys."""
    explicit = {command.command_id: '' for command in registry.commands()}
    if not isinstance(saved, dict):
        raise ValueError('Saved shortcuts must be a command-to-shortcut object.')
    explicit.update(saved)
    checked = registry.validate(explicit)  # unknown IDs and duplicate explicit owners refuse
    restored = {identity: checked[identity] for identity in saved}
    owners = {(registry.get(identity).context, shortcut): registry.get(identity)
              for identity, shortcut in restored.items() if shortcut}
    conflicts = []
    for command in registry.commands():
        owner = owners.get((command.context, command.default_shortcut))
        if command.command_id not in restored and command.default_shortcut and owner is not None:
            restored[command.command_id] = ''
            conflicts.append(f'{command.label}: {command.default_shortcut} is used by {owner.label}')
    registry.validate(restored)
    return restored, tuple(conflicts)


def _legacy_part(label):
    # This is the exact old menu-path spelling, only for matching an already
    # registered current native item. It never creates or guesses a command ID.
    return (label.split('\t', 1)[0].replace('&&', '\x00').replace('&', '')
            .replace('\x00', '&').strip().removesuffix('...').removesuffix('…')
            .strip().casefold())


class KeyboardManager:
    def __init__(self, frame=None, *, store=None, registry=None):
        self.frame = frame
        self.registry = registry or CommandRegistry(builtin_commands())
        self.store = store
        self.overrides = {}
        self.load_notice = ''
        self.bindings = {}
        self._adapters = weakref.WeakSet()
        self._unmanaged = {}
        self._pending_saved = None
        self._pending_legacy = None
        self._using_legacy = False
        self._load()

    def _load(self):
        self.overrides = {}
        self.load_notice = ''
        self._pending_saved = None
        self._pending_legacy = None
        self._using_legacy = False
        if self.store is not None:
            try:
                loaded = self.store.load()
                legacy_loader = getattr(self.store, 'load_legacy', None)
                self._pending_legacy = legacy_loader() if callable(legacy_loader) else None
                if self._pending_legacy is not None:
                    return  # Current menu bindings are attached before activation.
                self._pending_saved = loaded
                self.overrides, conflicts = resolve_saved_bindings(self.registry, loaded)
                self.load_notice = self._restored_notice(conflicts)
                self._pending_saved = None
            except (ValueError, OSError) as exc:
                self.load_notice = ('Keyboard settings could not be loaded. Default shortcuts are active. '
                                    'The existing file was kept. Open Options, Keyboard shortcuts to repair it. '
                                    + str(exc))

    def _restored_notice(self, conflicts):
        legacy = ('Earlier shortcuts were matched to the same registered commands. '
                  'Their original file is unchanged; Save writes the current keymap. '
                  if self._using_legacy else '')
        conflict = ('Saved keyboard shortcuts are active. These new defaults remain unassigned '
                    'to preserve your saved choices: ' + '; '.join(conflicts)
                    + '. Open Keyboard shortcuts to review the assignments.' if conflicts else '')
        return (legacy + conflict).strip()

    def _legacy_bindings(self, value):
        if self.frame is None:
            raise ValueError('Open the main window to review the earlier shortcut profile.')
        by_native = {binding.item.GetId(): identity for identity, binding in self.bindings.items()}
        if len(by_native) != len(self.bindings):
            raise ValueError('The earlier shortcut profile has ambiguous current menu bindings.')
        paths = {}
        def visit(menu, parents):
            for item in menu.GetMenuItems():
                if item.IsSeparator():
                    continue
                part = _legacy_part(item.GetItemLabel())
                if not part or '/' in part:
                    raise ValueError('The current menu has no unambiguous earlier command path.')
                path = (*parents, part)
                submenu = item.GetSubMenu()
                if submenu is not None:
                    visit(submenu, path)
                elif item.GetId() in by_native:
                    action = '/'.join(path)
                    if action in paths:
                        raise ValueError('The current menu repeats an earlier command path.')
                    paths[action] = by_native[item.GetId()]
        bar = self.frame.GetMenuBar()
        for index in range(bar.GetMenuCount()):
            visit(bar.GetMenu(index), (_legacy_part(bar.GetMenuLabel(index)),))
        result = {}
        for action, shortcut in value['overrides'].items():
            identity = paths.get(action)
            if identity is None:
                raise ValueError('An earlier shortcut command is unavailable: ' + action
                                 + '. Its original file was kept; no action was retargeted.')
            command = self.registry.get(identity)
            if command.context != 'main' or command.default_shortcut != value['originals'][action]:
                raise ValueError('An earlier command changed its original shortcut: ' + action
                                 + '. Review an explicit new assignment.')
            if identity in result:
                raise ValueError('Earlier shortcut paths resolve to the same command.')
            result[identity] = shortcut
        return result

    def _recover_pending(self):
        legacy = self._pending_legacy is not None
        loaded = self._legacy_bindings(self._pending_legacy) if legacy else self._pending_saved
        if loaded is None:
            return
        recovered, conflicts = resolve_saved_bindings(self.registry, loaded)
        self.validate(recovered)
        self.overrides = recovered
        self._using_legacy = self._using_legacy or legacy
        self.load_notice = self._restored_notice(conflicts)
        self._pending_saved = self._pending_legacy = None

    def change_root(self, root):
        self.store = KeymapStore(Path(root) / 'keyboard-shortcuts.json')
        self._load()
        self.activate_loaded()
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
        proposed = CommandRegistry(self.registry.commands())
        if spec.command_id not in known:
            proposed.register(spec)
        elif known[spec.command_id] != spec:
            raise ValueError('The command is already registered with different metadata.')
        old_registry, old_bindings, old_unmanaged = self.registry, self.bindings, self._unmanaged
        old_overrides = self.overrides
        old_notice = self.load_notice
        old_pending = self._pending_saved, self._pending_legacy, self._using_legacy
        old_label = item.GetItemLabel()
        self.registry, self.bindings = proposed, dict(self.bindings)
        self.bindings[spec.command_id] = _MenuBinding(item, item.GetItemLabel().split('\t', 1)[0], top_index)
        try:
            self._discover()
            if self._pending_saved is not None or self._pending_legacy is not None:
                try:
                    self._recover_pending()
                except ValueError:
                    pass  # Other modules may still be registering their IDs.
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
            self._pending_saved, self._pending_legacy, self._using_legacy = old_pending
            item.SetItemLabel(old_label)
            self._render()
            raise

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
                    shortcut = normalize_shortcut(label.split('\t', 1)[1])
                except ValueError:
                    # OS-owned Exit/Alt+F4 stays with the native window manager.
                    continue
                if shortcut:
                    self._unmanaged[shortcut] = label.split('\t', 1)[0].replace('&', '')

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
        self._discover()
        self.validate(self.overrides)
        self._render()

    def _render(self):
        effective = self.registry.effective(self.overrides)
        for identity, binding in self.bindings.items():
            shortcut = effective[identity]
            # Updating the native label removes the old implicit accelerator too.
            binding.item.SetItemLabel(binding.caption + ('\t' + shortcut if shortcut else ''))
        for adapter in tuple(self._adapters):
            adapter.refresh()

    def activate_loaded(self):
        """An incompatible saved map must not prevent opening the application."""
        try:
            self._discover()
            self._recover_pending()
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
        normalized = self.validate(overrides)
        # Validate native key conversion before writing or changing any window.
        for shortcut in self.registry.effective(normalized).values():
            if shortcut:
                accelerator_tuple(shortcut, 1)
        previous = dict(self.overrides)
        self.overrides = normalized
        try:
            self.refresh()
            if self.store is not None:
                self.store.save(normalized, self.registry)
        except Exception:
            self.overrides = previous
            self._render()
            raise
        self.load_notice = ''
        self._pending_saved = self._pending_legacy = None
        self._using_legacy = False
        return dict(self.overrides)

    def shortcut(self, command_id):
        return self.registry.shortcut(command_id, self.overrides)

    def available(self, command_id):
        binding = self.bindings.get(command_id)
        if binding is None or self.frame is None or getattr(self.frame, '_closing', False):
            return False
        try:
            return (self.frame.IsEnabled() and binding.item.IsEnabled()
                    and self.frame.GetMenuBar().IsEnabledTop(binding.top_index))
        except RuntimeError:
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


class KeyboardSettingsDialog:
    """Edit a local draft; only Save applies and persists the entire valid map."""
    def __init__(self, parent, manager, *, context='main'):
        import wx
        self.wx, self.manager = wx, manager
        self.original_store = manager.store
        self.draft = dict(manager.overrides)
        self.dialog = wx.Dialog(parent, title='Keyboard shortcuts', size=(760, 670))
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

    def _message(self, message):
        self.status.ChangeValue(message)
        self.status.SetFocus()

    def rebuild(self, event=None, *, selected=None):
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
        index = self.commands.GetSelection()
        if not 0 <= index < len(self.rows):
            raise ValueError('Choose a command first.')
        return self.rows[index]

    def selected(self, event=None):
        try:
            command = self._selected()
        except ValueError:
            self.shortcut.ChangeValue('')
            self.shortcut.Disable()
            return
        self.shortcut.Enable()
        self.shortcut.ChangeValue(self.manager.registry.shortcut(command.command_id, self.draft))

    def assign(self, event=None):
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
        try:
            self.draft = self.manager.validate({})
            self.rebuild()
            self._message('All default shortcuts are selected in this draft. Choose Save to apply.')
        except ValueError as exc:
            self._message(str(exc))

    def import_map(self, event=None):
        wx = self.wx
        chooser = wx.FileDialog(self.dialog, 'Import keyboard shortcuts', wildcard='Keymap JSON (*.json)|*.json',
                                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            path = Path(chooser.GetPath())
        finally:
            chooser.Destroy()
        try:
            proposed = self.manager.validate(KeymapStore(path).load())
            self.draft = proposed
            self.rebuild()
            self._message('Keymap imported into the draft. Review it and choose Save to apply.')
        except (ValueError, OSError) as exc:
            self._message('Keymap not imported; the current draft was kept. ' + str(exc))

    def export_map(self, event=None):
        wx = self.wx
        chooser = wx.FileDialog(self.dialog, 'Export keyboard shortcuts', defaultFile='keyboard-shortcuts.json',
                                wildcard='Keymap JSON (*.json)|*.json', style=wx.FD_SAVE)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            path = Path(chooser.GetPath())
        finally:
            chooser.Destroy()
        try:
            root = (self.manager.store.path.parent if self.manager.store is not None
                    else self.manager.private_root)
            approval = confirm_export_destination(self.dialog, path, private_root=root, wx_module=wx)
            if approval is None:
                self._message('Keymap export cancelled. No file was written.')
                return
            normalized = self.manager.validate(self.draft)
            content = json.dumps({'version': 1, 'bindings': normalized}, ensure_ascii=False, indent=2) + '\n'
            write_export(path, content, private_root=root, approval=approval)
            self._message('The draft keymap was exported. Application shortcuts change only after Save.')
        except (ValueError, OSError, AttributeError) as exc:
            self._message('Keymap not exported. ' + str(exc))

    def save(self, event=None):
        if self.original_store is not self.manager.store:
            self._message('The active library folder changed. Close and reopen keyboard settings.')
            return
        try:
            if self.rows and self.commands.GetSelection() >= 0:
                command = self._selected()
                proposed = dict(self.draft)
                proposed[command.command_id] = self.shortcut.GetValue()
                self.draft = self.manager.validate(proposed)
            self.manager.apply_overrides(self.draft)
        except (ValueError, OSError) as exc:
            self._message('Shortcuts were not saved. ' + str(exc))
            return
        self.dialog.EndModal(self.wx.ID_OK)

    def show(self):
        try:
            return self.dialog.ShowModal() == self.wx.ID_OK
        finally:
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
    import wx
    invoke = invoke or manager.invoke
    available = available or manager.available
    focus = wx.Window.FindFocus()
    dialog = wx.Dialog(parent, title='Find a command', size=(720, 560))
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label='Search commands. Use Tab to choose a result, then Run command.'),
               0, wx.ALL | wx.EXPAND, 8)
    search = wx.TextCtrl(dialog, style=wx.TE_PROCESS_ENTER)
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
        index = results.GetSelection()
        can_run = 0 <= index < len(rows) and enabled.get(rows[index].command_id, False)
        run.Enable(can_run)
        status.ChangeValue('Ready to run.' if can_run else 'This command is unavailable in the current view.')

    def rebuild(event=None):
        needle = search.GetValue().strip().casefold()
        rows[:] = [command for command in manager.registry.commands(context)
                   if needle in command.label.casefold()]
        results.Set([f'{command.label} — {manager.shortcut(command.command_id) or "Not assigned"}'
                     + ('' if enabled.get(command.command_id) else ' — unavailable') for command in rows])
        if rows:
            results.SetSelection(0)
        selected()

    def accept(event=None):
        index = results.GetSelection()
        if 0 <= index < len(rows) and enabled.get(rows[index].command_id, False):
            choice[:] = [rows[index].command_id]
            dialog.EndModal(wx.ID_OK)

    def enter(event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            accept()
        else:
            event.Skip()

    search.Bind(wx.EVT_TEXT, rebuild)
    search.Bind(wx.EVT_TEXT_ENTER, accept)
    results.Bind(wx.EVT_KEY_DOWN, enter)
    results.Bind(wx.EVT_LISTBOX, selected)
    results.Bind(wx.EVT_LISTBOX_DCLICK, accept)
    run.Bind(wx.EVT_BUTTON, accept)
    rebuild()
    search.SetFocus()
    try:
        accepted = dialog.ShowModal() == wx.ID_OK
    finally:
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


def install_keyboard_settings(frame):
    import wx
    existing = getattr(frame, 'keyboard_commands', None)
    if existing is not None:
        existing.refresh()
        return existing
    menu = wx.Menu()
    commands = menu.Append(wx.ID_ANY, '&Find a command...\tCtrl+Shift+P')
    settings = menu.Append(wx.ID_ANY, '&Keyboard shortcuts...\tCtrl+Alt+K')
    frame.GetMenuBar().Append(menu, '&Options')
    pending = getattr(frame, '_pending_keyboard_commands', [])
    registry = CommandRegistry((*builtin_commands(), *(spec for spec, _ in pending)))
    manager = KeyboardManager(frame, registry=registry,
                              store=KeymapStore(Path(frame.library.root) / 'keyboard-shortcuts.json'))
    for spec, item in pending:
        manager.bindings[spec.command_id] = _MenuBinding(
            item, item.GetItemLabel().split('\t', 1)[0], manager._top_index(item))
    frame._pending_keyboard_commands = []
    frame.keyboard_commands = manager
    frame.Bind(wx.EVT_MENU, lambda event: show_command_launcher(frame, manager), commands)
    frame.Bind(wx.EVT_MENU, lambda event: show_keyboard_settings(frame, manager), settings)
    manager.activate_loaded()
    if manager.load_notice:
        wx.CallAfter(lambda: frame.report(manager.load_notice)
                     if manager.load_notice and not getattr(frame, '_closing', False) else None)
    return manager
