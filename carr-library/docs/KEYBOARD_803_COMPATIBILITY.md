# 803 editors with the F749 keyboard manager

Authoritative base: PR105, commit
`bf14cb5ef791eb7622a1c73f0ebd1aa80d2c0bc8`.

The 803 speech/editor/settings modules import `CommandBinding` and
`bind_context_shortcuts`, and call `host.keyboard.bind_window(...)`. Without an
adapter, importing `speech_dialog` during desktop startup fails against F749;
opening the reading-copy editor has the same missing API boundary.

This additive shim keeps F749's command registry, conflict validation, keymap
store and native menu dispatch. `CommandBinding` records editor callbacks;
window adapters register those exact handlers under the existing semantic IDs.
The adapter returns idempotent cleanup, removes only its own events, restores
the prior window accelerator table and ignores child-destruction events.
Known editor IDs are declared before keymap loading, so an editor's saved
shortcut works after restart without first opening every other dialog.

Both `frame.keyboard_commands` and `frame.keyboard` refer to the same installed
manager. Compatibility names delegate directly: `notice` reads `load_notice`,
`retarget(root)` calls `change_root(root)`, and `show_settings()` opens F749's
existing settings dialog. No second settings file or command-ID scheme is added.

## Exact host hook

Apply `validation/keyboard-803-host-hook.patch` to the 803 host when including its
editors. It makes only three changes:

1. Remove the duplicate 803 Options/keyboard-settings menu row.
2. Import `install_keyboard_settings` in `_install_desktop_commands`.
3. Assign `self.keyboard = install_keyboard_settings(self)` after module menus
   have been installed, replacing direct `KeyboardManager(self)` construction.

The shared installer is idempotent. Direct construction would omit the existing
installer's store, menu registration and lifecycle wiring. The host patch was
checked against the actual current 803 file without modifying that owner's tree.

## Save All

The native `Library / Save All` item is discovered as `library.save_all`, in the
`main` context, with default `Ctrl+S`. Its original wx menu ID and handler remain
in place. `reading_copy.save` belongs to the `reading_copy` context and can use
the same shortcut independently.

The host/editor participant calls match the tested coordinator signature:
`register(key, label, *, dirty, save, focus=None)`. A real registration and durable
completion exercise against AF5's tested `save_all.py` completed successfully.

## Validation and integration boundary

Run the adapter, registry, manager and existing reader-dispatch suites:

```sh
python -m pytest -q tests/ui/test_context_shortcuts_compat.py \
  tests/ui/test_command_registry.py tests/ui/test_keyboard_manager.py \
  tests/ui/test_reader_command_dispatch.py
```

**165 tests passed in 0.65 seconds.** The adapter tests explicitly use wx event doubles; they do not claim Windows
or NVDA key-dispatch acceptance. They cover handler identity, live remapping,
separate windows and contexts, cleanup, disabled/closed windows, saved-map
reopening, invalid-registration rollback, and the original Save All native ID.

803's old `test_keyboard_settings.py` and `test_keyboard_commands.py` target its
superseded registry, including label-derived IDs and different persistence
methods. Port additional behavioral assertions to the authoritative manager;
do not reintroduce the second implementation to satisfy its private API tests.

The event-table operations follow the primary wxPython documentation for
[accelerator tables](https://docs.wxpython.org/wx.AcceleratorTable.html) and
[window event handling](https://docs.wxpython.org/wx.Window.html#event-handling).
