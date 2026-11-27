# Explicit clipboard reading

Use **Voice → Read clipboard text...** to capture the current clipboard text
once into an independent Voice reader. Opening the copy starts no speech. Choose
the existing Read action if you want it spoken. The original retained books,
verified source locators and unrelated manual reading recovery are unchanged.

The normal reader may save this derivative only through its existing recovery
and Save controls. The one-shot copy uses its own `clipboard` recovery slot.
A watched copy uses the separate `clipboard-watch` slot. Neither receives a
retained-book source anchor or quotation authority because its text happens to
match a book.

**Options → Desktop integration → Start clipboard watching** is optional and
explicit for this window and library selection. It retains only the latest
bounded valid text in memory and pauses while a modal or foreground operation is
active. It reports that new text is available without moving keyboard focus.
**Open watched clipboard** is a separate action; watching never opens a reader,
starts speech, writes a clipboard history, uploads text, or runs an assistant.

Stop watching, closing the window, changing the library/topic or replacing its
directory clears the pending text. A root or selection change during the native
read also prevents retaining that result. The operating-system clipboard
transfer is not claimed to be a streaming or interruptible process. Normal
reader admission retains the existing Unicode and 2,000,000-character bound.

The system tray and global shortcuts are described in
[Desktop integration](DESKTOP_INTEGRATION.md). Native OS registration is explicit
and may be refused if another application owns a combination.

This composition remains **DRAFT_NOT_RUN**. The Windows acceptance probe uses
synthetic text with real wx/Win32 clipboard, tray, keyboard events and existing
reader controls; actual execution is required before any native PASS claim.
Speech quality and manual NVDA testing remain separate.
