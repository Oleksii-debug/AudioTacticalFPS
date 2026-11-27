# Reading tabs and optional paced reading

The manual **Voice reader and reading copy** window can keep up to sixteen independent reading copies in one native notebook. Use **New reading tab**, **Close reading tab**, **Rename reading tab**, or the notebook's native **Ctrl+Tab / Ctrl+Shift+Tab** navigation. Changing tabs stops the current transport. It never starts speech or restores a running timer.

Each copy retains its original text, edited text, undo/redo history, text cursor, and selected local voice settings. A missing saved voice stays visibly unselected; another voice is not chosen for it. A saved numeric audio position is passive information and does not recreate an audio file or authorize playback.

**Ctrl+S** saves the current reading copy for recovery. **Save As** writes its separate reading project. **Save All open drafts** uses the application's existing save coordinator and includes intentionally empty edits. An empty result after deleting all text is still an unsaved change. On an exact-byte conflict with another window, the newer durable draft is retained and this window keeps its unsaved copy for review or Save As. Closing a tab does not delete already saved reading projects or recovery files.

When explicit incoming text is opened beside an existing manual recovery collection, it becomes a separate tab. Its first successful recovery save publishes both the new draft and its membership. Recovery does not silently replace an earlier copy with the incoming text.

Readers opened from a retained book or chapter remain single-source windows. Their existing source authorization and source-position checks remain in force. This change does not add simultaneous book tabs, transfer quotation authority between tabs, or turn edited reading copies into quotation evidence. If the owning library or source is revoked, saving remains blocked; the reader can still be explicitly closed without changing its existing recovery files.

## Reading focus

Choose **Reading focus and audio follow…** to prepare a bounded view of the current reading copy on the existing reader worker. It opens paused. **Start visual/follow** starts visual pacing at a chosen rate of 40–1000 word units per minute. **Pause**, **Previous word**, **Next word**, and **Use cursor and close** work without editing the reading copy. Preparation accepts at most 100,000 word units. Unicode letters, numbers, combining marks, and internal apostrophes/hyphens supply deterministic navigation boundaries; these are not language-specific linguistic analysis.

**Audio follow — approximate** is available only for audio rendered from the exact current reading document and selection. Its highlight estimates position from that recording's measured duration. It is not engine-provided word timing. Switching tabs, editing the document, or changing the current recording invalidates the old binding. Opening focus, selecting a mode, and changing a tab never start audio. The audio Pause and Stop controls borrow the existing reader keyboard preferences, including unassigned or remapped shortcuts.

Reading appearance follows the active editor using the already applied presentation settings. Switching editors does not rewrite preferences, text, or selection. Paced reading is a separate optional view; the source and editable reading copy remain unchanged.

## Retained state and bounded recovery

The passive tab index is limited to 16 KiB and sixteen entries. The complete referenced draft collection is limited to 16 MiB of captured draft bytes and 16 million retained characters, counting original text and every replayed history state. All referenced sizes are checked before draft materialization. Cancellation, missing drafts, invalid records, and stale exact-byte tokens do not authorize a new tab index or overwrite another window's recovery bytes.

Native and foreign-format backups validate the same referenced collection and retain its original spelling and bytes. Restoring tabs grants no voice, device, model, network, or playback authority.

## Reuse and verification scope

The passive tab/focus design reuses reviewed PR152 source from `55a0436aba29b5ac4750110f8c8fdc566922d520`; the corresponding relevant blobs are unchanged at `dc477add6db2fc0348637dd460d03b76d8e1c75f`. Integration retains the current ReaderEditHost, ReadingDocument, SaveCoordinator, coherent state reader, keyboard registry, single reader executor, and native player.

The dedicated native probe is `scripts/windows_reading_tabs_smoke.py`. It verifies real Windows controls, OS key dispatch, recovery/reopen, independent undo, empty-copy Save All, current-source isolation, and optional visual pacing with an explicitly empty voice inventory. That probe does not qualify speech inference, acoustic quality, exact word alignment, audio-follow playback, or manual NVDA use.
