# Playback queues in Audio Studio

Audio Studio can keep an ordered queue of reviewed local PCM WAV recordings. The queue uses the same playback device, navigation controller and Studio worker as rendered-output listening.

Open **Audio Studio → Script and audio production** (default shortcut **Ctrl+Alt+A**), then choose **Playback queue**. Opening the controls does not start audio.

## Add and review recordings

Choose **Add and review WAV files** and select the files in the intended order. Studio checks each file and retains an exact copy under the current library. The source files are not changed. Repeated rows are allowed and keep their position in the queue.

A queue holds at most 256 rows. Each selected recording must be an ordinary local PCM WAV file with one or two channels and a supported PCM sample width. The review admits up to 4 GiB per file and 32 GiB across all rows, counting repeated rows. A single review has a two-minute cooperative allowance. If a selection exceeds that allowance, choose fewer files. Other audio formats can first be converted through the existing Audio conversion action.

The controls are in a scrollable window. Tab moves between controls, arrow keys select a queue row, and Space activates the focused button. Escape closes the queue controls.

## Play and change the order

**Play queue** starts the selected item, or the first item if none is selected. A naturally completed recording advances to the next row through the same quiet-start and playback owner. Reaching the end of the queue stops it.

**Pause queue** pauses the current recording and withdraws automatic advancement. Choose **Play queue** to resume it explicitly. **Stop queue** stops playback and withdraws pending transitions.

**Previous queue item**, **Next queue item** and **Select item silently** change the selection without starting audio. **Move item up**, **Move item down** and **Remove selected item** also leave playback stopped. Save after changing the order if you want to keep the new order for another session.

The existing playback navigation controls continue to own A–B repeat and the sleep timer. Active A–B repeat takes precedence over a queue transition. An existing sleep deadline stays in effect across a natural transition and stops playback when it expires. Speed, pitch and per-player levels remain the existing playback settings; no second player or system-wide mixer is introduced.

## Save and reopen

**Save playback queue** writes the reviewed row order, selected row and exact original-frame position. Pause before saving when you want a deliberate resume point. Saving a queue does not move or rewrite the original audio.

A reopened queue is passive data. It can display the saved order and position, but it cannot play until you choose **Review saved queue**. That review verifies the retained copies in the current library, so it does not require the old external source paths.

By default, review keeps the current playback controls. **Restore saved playback controls as well as the queue** is an explicit option. A saved output device must still match the current device identity; an old device number alone is not enough. Historical queue files whose output identity belongs to the older format must be reviewed with current controls. Saved unequal skip intervals also require current controls because the current navigation host uses one interval.

**Reload saved queue as data** rereads the saved document without starting audio. If another window or process changed the saved queue, the stale view cannot overwrite it; reload and review the current document first.

## Closing, cancellation and backup

**Cancel current review** requests cancellation of the admitted Studio job. The existing worker owns that operation until it finishes; retry only after it has returned. Closing the queue controls withdraws their pending review and late playback action. A save that already completed remains saved even if the controls close before the result is delivered.

Closing Audio Studio stops its playback and closes its existing navigation timer. The queue window borrows that owner and does not create another worker or player.

Full library backup preserves the saved queue and its exact retained audio references. Restore validates them as passive data and does not open a device or start audio. Explicit review is still required before playback.

## Qualification boundaries

The focused queue gate checks the actual source and isolated installed package, retained PCM copies, saved-state conflict handling, cancellation and window lifetime. Its native probe uses actual Windows controls and the actual Studio executor, with authored file-picker answers and a clearly controlled MCI command boundary for hosted machines. This does not qualify physical speakers, listening quality, native file-picker interaction or NVDA use.

The initial queue composition is under qualification. Read the final release evidence for the exact tested commit and completed native result; source review alone is not a runtime pass.
