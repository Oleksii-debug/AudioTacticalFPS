# Retained playback queue

The native **Library → Saved playback queue** window keeps one explicit ordered queue,
selection and original PCM frame cursor for the current library. Audio Studio's Playback
page opens the same window and offers its current recordings for an explicit capture.
The original Studio preview playlist continues to use the same existing WavePlayer.

## Data and review

`playback-queue/queue.json` is bounded typed passive state. It contains relative
content-addressed media names, original byte SHA256, exact PCM dimensions, a display
label, order, selection, frame and cursor accuracy. It contains no external input path,
native device handle, engine configuration, credentials or automatic playback request.
Opening the window reads this bounded JSON only. **Review / reload saved recordings**
authenticates each owned retained WAV/RF64 before editing or playback; **Play** is separate
and retains verified input handles until the existing player has actually stopped.

**Add** reviews explicitly selected local PCM16 RIFF/RF64 and publishes independent copies
through the current audio publisher and ResourceBroker. Repeated identical inputs reuse
a verified copy. Hash collisions with different bytes are refused, originals are never
modified, and output publication cannot silently overwrite a different destination.
Completed retained copies survive cancellation even if the queue draft was not saved.

The namespace permits at most 256 retained distinct media files, 256 queue entries,
4 GiB per file and 32 GiB retained media. Each interactive review/capture phase has a
120-second ceiling and cancellation checkpoints. Resource acquisition is bounded by
60 seconds within that ceiling. Bounded complete-backup verification uses the caller's
own time and cancellation budget. A full namespace requires an explicit future archive
management action; Clear changes the queue and does not silently delete retained audio.

## Cursor, ownership and saving

The queue uses the existing WavePlayer/PCMPlayer. The only player API addition is an
optional `start_seconds` on `play_index`; the default queue and A–B behavior is preserved.
Playback starts at the saved original frame divided by the original sample rate.
Driver-reported and completed-buffer frame observations are retained with their accuracy
label. A fallback external player never gains a guessed elapsed-clock cursor. Output
device permission is session-local and is not restored from saved data.

Stop, Save and Close settle the existing player before releasing verified input readers.
A native stop/close failure retains the owner and handles for explicit retry.
Reordering, removing, manual seek and Clear stop playback; they do not automatically play
the newly selected recording.

Save uses the existing application writer, a namespace lock and compare-and-swap JSON
publication. It cannot overwrite a newer saved queue from another window. A post-commit
directory-flush error returns an explicitly unconfirmed save receipt, keeps the draft
dirty, and permits retry with the newly observed token. The native Save All participant
reports success only after the exact saved snapshot is confirmed and no newer draft
exists. Close waits for work and saving, then offers Save / Discard / Cancel for a dirty
draft. Restoring or reopening never starts playback.

## Complete backup

The existing complete-backup service includes this typed namespace. It holds the same
writer/namespace order, inventories retained PCM and the saved queue, validates their
hashes and frame references at the source, copied stage and verification/restore stages,
and rechecks inventory after the copy. Empty/no-queue states are representable without
inventing empty directories in the file-only manifest. Private incomplete capture stages
are not completed media and are omitted; completed retained copies remain included even
when no queue references them yet.

## Provenance and qualification

The passive retained-queue concepts were reviewed from PR152 commit
dc477add6db2fc0348637dd460d03b76d8e1c75f (`speech/playlist.py` blob
9ceeee4215442952af64d258413a0550689d4407). Its alternate speech/MCI player was not adopted.
This implementation keeps the current audio player, verified input reader, resource
broker, writer, atomic publication, SaveCoordinator and complete-backup families.

The first core checkpoint is 714f982102a070df807252aa2cd7d8d3585f1518 on integrated
aa89b6f2e11bf49dd1b1350b28e8c055f54e62ac. The focused qualification candidate is composed
on 48e038ef1993879605ecb83e0dfa17794382a851; all eleven backup hunks applied to its current
8e90842684094fc8d1270cdbff23a59c12c05f66 library-backup blob without conflict, preserving
the incoming workflow-template state. The player, desktop and Studio shared-file base
blobs were reread and their small hunks applied independently.

The dedicated workflow requires 141 exact source cases, the same 141 installed cases,
and 43 actual native-event/retained-data assertions. Function/parameter and native-label
Counters are committed in scripts/playback_queue_oracles.json. The existing 56-pin Windows
runtime and source-bound wheel are verified before and after isolated test execution;
complete tracked checkout, copied fixtures, installed Python bytes and native implementation
origins remain bound to the event source. Each test phase has a 1200-second child watchdog
and 22-minute outer step, the native probe has a 180-second watchdog, and the whole job is
bounded to 75 minutes. Every result uploads textual receipts and actual test output on
success or failure. No synthetic PCM, user data or model files are uploaded.

Source/installed/native focused qualification is pending. No physical audio-device,
screen-reader or whole-application PASS is claimed by source review.

The final submission boundary also covers an actual first-thread-start failure: work may
already be enqueued when ThreadPoolExecutor.submit raises. A one-operation admission gate
releases that queued callback as refused, so a later successful worker cannot perform an
unreported stale capture, save or held-reader preparation. The real thread-start regression
adds one exact case (141 per test lane); no previous case or native assertion is removed.
