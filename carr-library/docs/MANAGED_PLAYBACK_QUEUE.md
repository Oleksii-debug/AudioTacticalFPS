# Durable playback queue in the current Audio Studio

V3-05 and V3-06 use the current managed Audio Studio project, WavePlayer,
native PCM player, FFmpeg playback preparation and operation executor.
The new queue does not introduce another audio engine or device owner.

## User flow

Save a library Audio Studio project. Add WAV recordings retains independently
owned PCM copies and saves their order. Save queue and position, Save All and
the existing five-second autosave timer checkpoint the last observed native
sample position and the last successfully prepared independent playback
controls. Closing, changing projects and Save As also checkpoint dirty bounded metadata
before the controller leaves the original managed project.

Restore saved queue rechecks every retained WAV against its SHA256, byte count,
sample format and exact frame count and installs a stopped queue. Resume saved
position performs that same review, then explicitly starts the existing player
at the saved source-frame position on the currently selected output. No saved
output device, armed sleep timer or autoplay state exists. An external player
that cannot report a playhead persists an unknown position and requires an
explicit ordinary Play action.

Playback settings are restored into the existing Independent playback controls
dialog. Preparing a new private derivative and playing it remain explicit
actions. The source WAV and synthesis settings are unchanged.

## Data and ownership

Metadata: audio-projects/<project-id>/audio/playback-queue.json.
PCM: audio-projects/<project-id>/audio/assets/<sha256>.wav.
The typed schema binds project identity, relative retained filenames, PCM
receipts, native cursor evidence and four bounded playback controls. No
external source path is executable restored authority.

Limits are 256 items, 4 GiB per WAV, 32 GiB total, 2 MiB metadata and a
two-minute import/review allowance. These are configured bounds, not measured
hardware capacity. The current ResourceBroker admits the bounded copy.
Managed draft and native audio locks coordinate with current renders/backups.
Optimistic metadata digests prevent one Studio window from overwriting another.

Cursor saves check retained file identity/change metadata without rehashing
audio on each timer tick. On Windows, that pin includes native FileBasicInfo
ChangeTime rather than treating stat creation time as modification evidence.
The existing player rechecks the same pin before Play, Resume, Seek and repeat
restarts; stale recording bytes cannot become a new playback start. Explicit queue review and backup hash full retained
audio. Cancellation is checked after final flush before queue publication;
a pre-publication failure leaves the previous checkpoint authoritative.
An error after the owned JSON was published instead carries
QueueChangeCommittedError.review: the exact QueueReview, digest and asset pins
that were committed. This covers final directory flush and native guard-release
failures too. Equal-byte create-only competitors never become that receipt.
The UI adopts this baseline, reports unconfirmed final durability, stops automatic
save retries and requires an explicit restore/review. Save All completes with a
warning rather than waiting forever or replaying the write. An interrupted append
may leave a fully published, inert retained WAV, but does not replace the prior
committed queue. Owned recordings are not automatically deleted by Clear.

The existing project and complete-library backup validators include the queue
and recheck every referenced PCM receipt. Restore preserves inert relative
references across roots. Standalone project snapshots preserve the same typed
metadata; active managed playlist work uses a saved library project.

Historical canonical state/speech/playback-queue.json is never silently activated
as this managed project or treated as current device permission. An explicit
import adapter may supply exact reviewed hashes and labels (up to 256 characters)
to PlaylistStore.append. Its saved frame uses historical_cursor evidence,
requires a valid item/frame and is visibly distinguished from a newly measured
native position. Original controls/devices remain outside this activation path.
A session-only resource_broker constructor dependency lets a staged import reuse
the current library's admission policy without serializing or restoring a broker.

The accepted Studio Future always settles its Save All callback on the UI thread
after work ends, even when its window was closed, destroyed or superseded. That
path reports unsuccessful presentation, performs no native control updates and
does not clear a newer operation's busy flag. The saved file remains available
for ordinary reopening.

## Evidence

New source is NOT_RUN until exact-head required CI completes. Contract tests
use actual PCM bytes, real filesystem commits, current complete/project backup
and explicit device doubles only for the start-call boundary. The native probe
uses actual wx controls and the installed package. Physical audio and NVDA are
separate acceptance evidence.


The added regressions cover actual same-size PCM rewrites with restored mtime,
native-only ChangeTime changes, denied/invalid-parent optional lookup,
create-only competition, post-rename/guard-release faults, Save As ordering,
lost-window Save All settlement and inert historical cursor import. Source and
native execution for this corrective checkpoint remain NOT_RUN.
