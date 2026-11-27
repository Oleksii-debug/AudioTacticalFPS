# Optional desktop integration

**Voice → Read clipboard text...** opens one bounded, independent reading copy. It does
not start speech. Existing manual drafts, retained books and source quotation
anchors remain separate. See [Clipboard reading](CLIPBOARD_READING.md).

**Options → Desktop integration** controls the system tray and global shortcuts.
All commands also remain available in native menus and the existing searchable
command launcher. No default global shortcut is assigned. Use Ctrl+Shift with a
letter, digit or F1–F11; Alt is optional. Protected text-editing/screen-reader keys,
F12, duplicate assignments and collisions with current application commands are
refused. Windows can refuse a combination already owned by another application.
Partial registration releases this window's newly acquired keys, and an
unconfirmed release remains visible and retryable. Native MOD_NOREPEAT avoids
repeated pause/stop actions from holding a key.

Saving assignments does not activate them. **Enable saved global shortcuts** is
an explicit per-window action; startup, restore and changing library folders do
not grant that authority. Global actions call the same semantic menu commands.
Pause and Stop use the current Voice reader's actual buttons and handlers,
including its existing source, quiet, cancellation and owned cleanup checks.
Closed, replaced or differently scoped readers cannot receive those events.
This adapter currently supports Windows only; other platforms show unavailable.

The tray icon is optional and must be supported by the active desktop session.
**Hide window to tray** is a separate explicit action; Close keeps its ordinary
exit and unsaved-work behavior. Show restores the window. Removing a tray icon
first restores a hidden window. Its menu uses the existing semantic actions and
their current availability. Native icon callbacks, global registrations and the
clipboard timer are released on close. An unconfirmed native release keeps its
exact owner reachable and makes an ordinary close wait for an explicit retry.
Forced destruction revokes dispatch first and retains any unresolved cleanup;
it cannot be reported as a confirmed release. There is no startup registration, process
persistence, keyboard hook, hidden service or replacement player.

## Clipboard watching

Watching is off for every new window and after any library or topic selection
change. **Start clipboard watching** authorizes one observation per second while
this desktop is not blocked by a modal dialog or a foreground operation. At most
the newest nonempty valid Unicode text is retained in memory, within the existing
2,000,000-character admission bound. The operating-system transfer itself is not
a streaming or interruptible-process guarantee. No history, text log, network
request, focus change or speech is produced. A status message contains no copied
text. Use **Open watched clipboard** to create a reading copy explicitly; only
that normal reader's recovery workflow can then save its separate draft. Stop,
close and a changed scope clear the pending snapshot. Later clipboard updates
never alter an already opened reading copy or its source anchor.

`desktop-preferences.json` is a bounded typed sidecar containing only the tray
choice and semantic key assignments. It reuses the guarded settings reader,
existing state lock and atomic publisher, detects concurrent saves, and has a
passive complete-backup validator. Its captured library object and directory
identity are checked before admission and after reads; a replaced root cannot
reuse a previous store. If a write becomes visible but directory durability
fails, Save reports that distinction and retains the observed compare-and-swap
token for a real explicit retry. Matching visible bytes alone are not treated as
a confirmed save. Clipboard contents, observation permission,
live OS key registration and native handles are never stored or backed up.
Unknown future command IDs can be preserved as inert data and removed explicitly;
they cannot acquire an installed command merely by appearing in saved JSON.

## Evidence and provenance

This adapter reuses the existing PR152 desktop implementation pinned at
`c8cbc5014905ed693b47cbfa40b480605a4bbdc9`, adapting its host, native-command,
guarded-reader and cleanup contracts to the current application. It adds no
third-party runtime or proprietary application code. It does not introduce the
donor's separate assistant-player bridge.

Current composition status: **DRAFT_NOT_RUN**. Source readback and independent
static review are separate from execution. No Windows, operating-system
clipboard, hotkey, tray, speech-quality or manual NVDA pass is claimed by the
existence of this implementation.

Portable tests cover exact text admission, no initial read, deduplication and
bounded coalescing, stale scope/close, key conflicts and partial cleanup,
unconfirmed release, native button ownership, actual settings CAS and real
backup/restore. Native-control stand-ins establish those program boundaries;
they are not Windows or NVDA acceptance.

Run the actual native probe on the interactive Windows acceptance desktop:

```text
python scripts/windows_desktop_integration_smoke.py
python scripts/windows_desktop_integration_smoke.py --installed
```

The installed invocation requires the sealed package origin. It uses synthetic
clipboard text and an actual temporary library, native dialog buttons, the real
Windows tray, an independently held conflicting OS shortcut, global keys from
another foreground window, and actual Ctrl+S into separate recovery slots, two current reader lifetimes, native
HWND invalidation and actual frame/worker cleanup. A 180-second watchdog also
covers cleanup. Source and installed-package probes both use the actual native
DLL activation handle. Native Windows and
human NVDA remain NOT_RUN until corresponding evidence is recorded. The probe
does not synthesize or qualify speech.

API references: [wx clipboard](https://docs.wxpython.org/wx.Clipboard.html),
[wx tray ownership](https://docs.wxpython.org/wx.adv.TaskBarIcon.html),
[RegisterHotKey](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey),
and [wxWidgets 3.2.8 native wrapper](https://github.com/wxWidgets/wxWidgets/blob/v3.2.8/src/msw/window.cpp).
The fixed Win32 registration retains MOD_NOREPEAT, which that wx wrapper omits;
native events still use the existing wx loop. No raw key stream is collected.

Official API documentation and the pinned wxWidgets wrapper were retrieved on 2026-11-27.
