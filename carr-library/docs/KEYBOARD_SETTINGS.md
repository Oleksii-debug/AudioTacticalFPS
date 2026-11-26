# Saving and keyboard preferences

The main desktop now starts with the **File** menu. Its **Save current changes**
command uses **Ctrl+S** and saves a pending research note through the existing
source-checked transaction. **Save all changes**, **Ctrl+Shift+S**, saves that
same modeless draft; other main-window operations already commit their changes
when accepted. The status says that an operation is still running when a save
cannot start. A note is reported saved only after its transaction succeeds.
An invalid or stale note remains in the editor for recovery.

Modal editors have contextual commands. In a reading-copy editor, **Ctrl+S**
saves the project and **Ctrl+Shift+S** creates a new copy. **Ctrl+Z/Ctrl+Y** move
through its saved editing revisions. Use the editor's named buttons for preview,
comparison and export. The exact source reader remains read-only.

## Find a command

Press **Ctrl+Shift+P**, or choose **Options, Find a command**. Type words from a
menu path, command name or current shortcut. Tab to the native results list;
Up and Down select a command, Enter runs it, and Escape closes the search.
Rows show the current assignment and clearly identify unavailable commands.
The palette closes before the original menu command runs and checks again
that the command and selected library/topic are still available. It uses the
same command inventory as keyboard preferences. See
[Find a command](COMMAND_PALETTE.md) for details.

## Change a main-window shortcut

1. Press **Alt+O** to open **Options** and choose **Keyboard shortcuts**.
2. Select the command by its menu path and current shortcut.
3. Enter a key such as **Ctrl+Alt+S**, **Ctrl+Shift+F8**, or **F6**. An empty value
   removes the assignment.
4. Confirm the dialog. The new assignment appears in the real menu and is saved
   for the current library data folder.

Conflicts name the command that already uses the key. Remove its assignment
before moving that key to another command. Ordinary editing keys such as
Ctrl+C/Ctrl+V/Ctrl+Z, operating-system controls and Alt-menu letters remain
reserved. System-wide shortcuts are not installed. Modal readers and editors
keep their contextual mappings.

**Options, List current keyboard shortcuts** shows a readable list of all
active assignments. The ordinary keyboard guide describes defaults. Use
**Restore default keyboard shortcuts** to reset, or **Export/Import keyboard
shortcuts** to transfer the versioned JSON preferences. Imports are validated
and previewed before explicit application. Unknown commands, duplicate JSON
fields, malformed keys and conflicts are rejected as a whole.

Preferences belong to the chosen library data folder, not the removable
program folder, and are included in complete library backups. Opening another
data folder loads its preferences. An invalid settings file activates defaults
with a readable notice while preserving the invalid file for recovery.

Native Windows event checks and manual NVDA acceptance are separate. Automated
tests do not establish screen-reader speech quality or physical keyboard
acceptance.
