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

## Change a contextual shortcut

1. Press **Alt+O** to open **Options** and choose **Keyboard shortcuts**.
2. Choose **Main window** or **Reading Desk**, then find the command by name or
   its current shortcut.
3. Enter a key such as **Ctrl+Alt+S**, **Ctrl+Shift+F8**, or **F6**. An empty value
   removes the assignment.
4. Choose **Assign**, review the draft and choose **Save**. The new assignment appears in the real menu and is saved
   for the current library data folder.

Conflicts name the command that already uses the key. Remove its assignment
before moving that key to another command. Ordinary editing keys such as
Ctrl+C/Ctrl+V/Ctrl+Z, operating-system controls and Alt-menu letters remain
reserved. System-wide shortcuts are not installed. Main-window and Reading Desk
commands share one registry, with independent contextual assignments. A key may
perform different actions in these two windows. Reading-copy editors keep their
own Save and Save As commands.

**F1** opens a readable guide containing the current assignments.
**Options, Find a command**, **Ctrl+Shift+P**, searches available commands and
runs the selected action through its existing authorization checks. The keyboard
settings dialog provides default resets and JSON import/export. Imports remain
a draft until Save. Unknown command identities, duplicate JSON fields, malformed
keys and conflicts cannot become active assignments.

Research Search Center uses **Ctrl+Alt+F**, while the advanced query builder uses
**Ctrl+Alt+Q**. Academic comparison and reading-copy actions register their stable
identities in the same settings dialog. Legacy `keybindings.json` files remain
validated backup data; active settings use `keyboard-shortcuts.json`.

Preferences belong to the chosen library data folder, not the removable
program folder, and are included in complete library backups. Opening another
data folder loads its preferences. An invalid settings file activates defaults
with a readable notice while preserving the invalid file for recovery.

Native Windows event checks and manual NVDA acceptance are separate. Automated
tests do not establish screen-reader speech quality or physical keyboard
acceptance.
