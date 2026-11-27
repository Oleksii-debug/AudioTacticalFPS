# Keyboard commands and recovery

## Use the application without a mouse

The main window keeps its native menus and existing commands. **Alt+O** opens
Options. **Ctrl+Shift+P** opens Find a command: type a command name, press Tab to
reach the results, select an action and choose Run command. Unavailable commands
are identified and cannot be run through the launcher.

**Ctrl+Alt+K** opens Keyboard shortcuts. Choose Main window or Reading Desk,
filter the command list, and enter a combination such as `Ctrl+Shift+J`.
Assign shortcut updates the draft. Save applies and saves it; Cancel keeps the
previous settings. An empty combination removes a shortcut. Reset selected and
Reset all restore the documented defaults. Imports are checked as a complete
map before they change the draft. Export writes the draft to a chosen JSON file
outside the library data folder.

The Main window and modal Reading Desk have separate contexts. For example,
the default `Ctrl+Shift+B` opens the Academic library in the Main window and
manages page bookmarks in the Reader. A combination cannot name two commands
within one context. Native text editing, focus navigation, menu mnemonics and
common screen-reader modifier keys cannot be reassigned by this settings panel.
These are application shortcuts, not system-wide media keys.

The Reading Desk includes named buttons for Find a reader command and Reader
keyboard shortcuts. Its guide and button labels show the current assignments.
Escape retains its native Close reader action. Busy, disabled and closed reader
commands do not run through a shortcut or command search.

File keeps Save current changes (`Ctrl+S`), Save all changes (`Ctrl+Shift+S`),
and the existing create, import and reopen reading-copy actions. The source
reader's Create editable reading copy action also has its own configurable
Reader command. Academic comparison, DOI lookup, advanced search, portable
snapshots and Audio Studio remain in their original menus and appear in the
same command launcher. Changing a shortcut keeps the existing action's scope
and authorization checks.

## Local profiles

Each library data folder has its own `keyboard-shortcuts.json`. Changing the
opened data folder loads that folder's profile. A missing profile uses defaults.
A damaged or incompatible profile produces a visible notice and safe defaults;
opening the application does not overwrite the damaged file. An explicit Save
from Keyboard shortcuts replaces it after validation. If another application
window changed the profile, saving is refused until the current profile is
reloaded by reopening the library folder or application.

The file contains a version and stable command-to-shortcut strings. It contains
no executable code, numerical window identifiers or private book contents.
Writes use a bounded temporary file, flush, lock and atomic replacement. Failed
writes retain the previous live keymap and saved file. Imports with unknown
commands, reserved combinations, duplicate JSON fields or contextual conflicts
are rejected together.

The earlier `keybindings.json` profile remains intact and is included in checked
backups. It is not silently applied or rewritten. To transfer it, choose Import
keymap in Keyboard shortcuts, select that file, review the proposed assignments
and choose Save. The import translates known menu paths to stable command IDs.
An unavailable command or unsupported key rejects the entire import; both files
and the active keymap remain unchanged. The new profile has a 64 KiB limit;
explicit imports of the older format retain its 128 KiB input limit.

## New application modules

Menu modules register an explicit stable identity after attaching their native
menu item. They keep their original event handlers and authorization checks:

```python
from carr_library.ui.command_registry import CommandSpec
from carr_library.ui.keyboard_settings import register_menu_command

register_menu_command(
    frame,
    CommandSpec('studio.render', 'Render audio project', 'main', ''),
    render_menu_item,
)
```

Use `frame.keyboard_commands.refresh()` after adding existing built-in menu
commands. Do not create a competing frame accelerator table. Shortcuts belonging
to unregistered native menu actions are reserved against keymap conflicts until
that module registers its stable command ID. Registration is transactional.
The helper can run before the final keyboard installer; it queues explicit IDs
so saved module bindings are recognized at startup. Late registration also
reactivates a valid saved profile once all its module IDs are available.

The public `keyboard_preferences.validate_keymap(raw_bytes)` validates bounded
backup bytes without executing or applying them. Runtime application additionally
checks the currently registered command set and context conflicts. The common
format4 backup includes both keyboard profile generations through their own
validators; restoring a profile does not execute commands or bypass current
registry validation.

## Validation boundary

Pure tests cover normalization, reserved keys, context collisions, atomic writes,
concurrent changes, corrupt imports, rollback, menu labels and Reader routing.
The installed-package native probes use actual wx controls and simulated system
key presses. Missing wx or a display returns `NOT_RUN` with exit code 77.
Automated native probes do not establish manual Windows 11/NVDA acceptance.
