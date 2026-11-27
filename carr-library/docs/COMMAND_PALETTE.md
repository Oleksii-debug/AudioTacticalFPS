# Find a command

Press **Ctrl+Shift+P** in the main window, or open **Help, Find a command**.
Type words from a command name or menu path, such as `reading copy`, `export
topic` or `library tools`. You can also search its current shortcut. Matching
ignores capitalization and keeps the native menu order.

The first matching command is selected. Press **Tab** to reach the native
results list, then use **Up** and **Down** to choose another command. Each row
contains its full menu path, current shortcut or “No shortcut”, and whether
it is unavailable. The read-only status control also reports the number of
matches and the selection. **Enter** in the filter or results list runs the
selected command. The **Run command** button provides the same action.
**Escape** or **Cancel** closes the palette without running anything and
returns focus to the control where you started, if it is still available.

The palette closes before the chosen command starts. Focus returns to the
originating control before execution, so commands such as **Edit, Select all**
act on that control. A chosen command can then move focus or open its normal
dialog. Disabled commands remain discoverable but cannot run. If an operation
starts, the selected library or topic changes, or the command is removed or
disabled before execution, the request stops with a readable message. The
original menu handler still checks its own source, selection and permissions.

## Commands and shortcuts

The palette reads the existing main-window menu and keyboard registry.
Commands installed before the final shortcut capture, including voice,
studio and transcription actions, are available through the same path.
Dialog-specific controls are outside this main-window inventory.

Changes made through **Help, Keyboard shortcuts** appear in the palette the
next time it is opened or filtered. **Find a command** itself has the default
shortcut **Ctrl+Shift+P** and can be remapped through those same settings.
If an existing saved assignment already uses **Ctrl+Shift+P**, it keeps that
key. **Find a command** remains available in Help without a shortcut; the
keyboard settings explain the conflict and let you choose another key.

Typing only filters existing commands. It does not run expressions, scripts,
shell commands or macros, and no command runs just because it matches a word
in a document. Search terms are not saved or sent to a service.

## Verification

`tests/ui/test_command_palette.py` checks filtering, current shortcuts,
selection and cancellation, shared-registry integration, preserved saved
shortcuts, scope changes, menu removal/replacement, disabled parent menus,
focus restoration and validation again after the modal closes.
`scripts/windows_command_palette_smoke.py` exercises installed Windows
controls and the original File Save and Edit Select all handlers with a
synthetic research note. The script checks focus, native Enter-event routing,
shortcut remapping, disabled commands and changes during modal closure.

Native execution evidence is separate from source tests. NVDA speech quality
and physical keyboard acceptance remain manual checks; a source test or
synthetic native event alone does not establish them.
