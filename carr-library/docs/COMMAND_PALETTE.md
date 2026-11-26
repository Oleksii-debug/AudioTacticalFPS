# Find a command

Press **Ctrl+Shift+P** in the main window, or open **Options, Find a command**.
Type words from a command name or menu path, such as `reading copy`, `export
topic` or `library tools`. You can also search its current shortcut. Matching
ignores capitalization and keeps the native menu order.

The first matching command is selected. Press **Tab** to reach the native
results list, then use **Up** and **Down** to choose another command. Each row
contains its full menu path, current shortcut or “No shortcut”, and whether
it is unavailable. The read-only status control also reports the number of
matches and the selection. **Enter** in the filter or results list runs the
selected command. The **Run command** button provides the same action.
**Escape** or **Cancel** closes the palette without running anything.

The palette closes before the chosen command starts. Commands that open
another dialog therefore retain their normal keyboard workflow. Disabled
commands remain discoverable but cannot run. If an operation starts, the
selected library or topic changes, or the command is removed or disabled
before execution, the request stops with a readable message. The original
menu handler still checks its own source, selection and permissions.

## Shared commands and shortcuts

The palette reads the existing main-window menu and keyboard registry. It
does not maintain a separate command catalog. Commands added before the
desktop registry is collected, including future voice and audio menus, are
available through the same path. Dialog-specific controls are outside this
main-window inventory.

Changes made through **Options, Keyboard shortcuts** appear in the palette
the next time it is opened or filtered. **Find a command** itself has the
default shortcut **Ctrl+Shift+P** and can be remapped through those same
settings. Existing shortcut validation rejects conflicting assignments.

Typing only filters existing commands. It does not run expressions, scripts,
shell commands or macros, and no command runs just because it matches a word
in a document. Search terms are not saved or sent to a service.

## Verification

`tests/ui/test_command_palette.py` checks filtering, current shortcuts,
selection and cancellation, shared-registry integration, scope changes,
menu removal/replacement, disabled parent menus, and validation again after
the modal closes. `scripts/windows_command_palette_smoke.py` exercises the
installed Windows controls and original File Save handler with a synthetic
research note. The script verifies focus, native Enter-event routing,
shortcut remapping, disabled commands and changes during modal closure.

Native execution evidence is separate from source tests. NVDA speech quality
and physical keyboard acceptance remain manual checks; a source test or
synthetic native event alone does not establish them.
