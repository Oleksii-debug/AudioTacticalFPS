# Operation progress and optional local sound

The main window, Import Center and Audio Studio use the same progress model and
named native Windows gauge. A read-only field beside the gauge gives the current
phase, percentage when the total is known, and completed units. Press
**Ctrl+Alt+P** or choose **Current progress** to move to that field. In the main
window the command is also under **Help** and appears in keyboard customization
and command search. Routine progress updates never move keyboard focus.

## What the numbers mean

Import Center counts files resolved during the current run. Existing completed
files and old failures do not create an artificial processing rate. A retry has
its own work total. Exact duplicates, explicitly excluded files and rejected
unsupported files are resolved with their actual status still visible in the
batch report. Failures and files awaiting independent page review leave the
operation marked **Needs attention**. Cancelled work remains **Cancelled**.

A work counter reaching its total displays at most **99 percent** until the
operation returns successfully and its authoritative result is validated. Only then
can it display **100 percent**. A failed submit, worker error, invalid result or
cancellation before publication cannot produce a success percentage. After durable
publication, a late Cancel or a display error does not relabel the saved result.

Audio duration, voice generation time, mixing and final validation cannot be
reliably inferred from a count of script segments. Audio rendering therefore
uses an indeterminate gauge with actual phase messages until its completed output
is accepted. General main-window jobs also stay indeterminate when they do not
provide an actual work counter. The application does not invent an estimate from
elapsed time alone.

When real work units provide a measured rate, the status can show an estimated
remaining time. Resumed cached work does not establish that rate. A changed total
is announced in the status and resets the estimate. Pausing/resuming also resets
the rate baseline. The current integrations expose cancellation; the shared
model's pause state does not add a renderer pause command.

Native views refresh at most four times a second during work. Import Center uses
a one-slot mailbox for worker list updates. Audio render phase messages update
the shared model directly, so thousands of chunks cannot flood the native event
queue. A terminal result or an explicit query can refresh immediately.

## Screen reader and application sound choices

Application sounds are **off by default**. NVDA users can choose their own progress
speech or beep settings in NVDA. This application does not change NVDA settings,
and no NVDA manual acceptance is claimed by automated tests. Optional application
speech uses an installed CPU voice that you explicitly select in Audio Studio
with **Use voice for progress**. The selection applies only to this session and
does not enable sound by itself. No engine is installed or downloaded. See
[Local progress voice](LOCAL_PROGRESS_VOICE.md) for supported engines/languages
and the independent execution boundaries.

**Progress sound settings** offers Off, Beep, Speech and Beep + Speech,
notification volume from 0 to 100, a minimum
interval from 1 to 60 seconds, mute, and permission to sound while the window is
in the background. The default volume is 20, interval five seconds, and
background sound is disabled. Saved choices are loaded before opening settings
and when an operation starts, including after the active library folder changes.

On Windows, short original PCM cues use asynchronous native playback. Known
progress raises the pitch; unknown progress uses a steady pitch. Completion,
failure, cancellation, pause and resume have distinct short cues. The queue
retains at most one waiting request and the backend uses at most two tiny
temporary WAV files. Changes to the current state or sound settings invalidate
queued stale cues. Volume zero is silent.

Application cues are suppressed before Audio Studio playback is admitted, and
remain suppressed until Stop playback, a new studio operation, an unsuccessful
playback start or closing the studio. A rejected new playback request does not
unmute a still-playing preview. This conservative gate also prevents a queued
progress sound from replacing the preview in Windows' shared playback API.
Tones and speech are independent notifications and are never inputs to rendered
audio. A speech notice uses a bounded private temporary PCM file and its own
player. Studio waits for confirmed notification silence without blocking the UI
thread. Failed native cleanup retains its owned buffers and prevents overlap.
On other operating systems the native gauge/status work, while application
tones are unavailable in this build.

## Local storage and recovery

The library root stores sound choices in `progress-settings.json`. The strict
versioned JSON contains only mode, volume, interval, mute and background settings;
version 2 adds speech/both while version 1 remains readable. Voice identity and
execution authority are not saved in this file or restored from backup. The file
has an 8192-byte budget and rejects unknown/duplicate fields. Saving uses the
shared library writer lock, a flushed temporary file and atomic replacement.
Malformed or linked settings are rejected, with tones kept off and an explanatory
status. Existing invalid files are not silently rewritten. Complete library
backups include this allowlisted file and validate it before restore. Restoring
Speech mode still requires a new explicit voice selection.

## Verification boundaries

`tests/test_progress.py` checks real-unit/unknown progress, accepted completion,
failure/cancellation, measured-rate resets, strict/atomic preferences, generated
PCM frequency/volume/duration, bounded asynchronous queuing and preview admission.
`tests/ui/test_progress_view.py` and the existing desktop/Import Center tests run
the actual handlers with queued futures, including errors after work returns,
retry counts, event coalescing, no unsolicited focus change and stale-tone
invalidation.

After installing the built wheel on Windows, run:

```powershell
python -I scripts/windows_progress_smoke.py
```

The probe checks actual native gauge values at 0, 25, 50, 75, 99 and accepted 100,
readable phases/units, unchanged editing focus, explicit query focus, unknown
duration, cancellation, and native settings save/reload. It fails if imports come
from a source checkout. Its output explicitly records NVDA and physical tone
listening as **NOT_RUN**. On non-Windows it returns 77 with a truthful **NOT_RUN**
record. Human listening and screen-reader qualification remain separate evidence.
