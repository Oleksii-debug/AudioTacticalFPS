# Operation progress

The main window shows a named native progress bar and a text description of the
current operation. The text includes its state, phase and measured unit counts.
Automatic updates leave keyboard focus where it is.

Use **Options → Query current progress**, **Ctrl+Shift+Q**, or the **Query current
progress** button to read the current status. This command remains available
while other commands are disabled by a running operation. Its shortcut can be
changed through the existing keyboard settings.

## What the numbers mean

An operation with a known total reports completed units such as files, pages or
bytes. Progress stays below 100% until the producer confirms successful durable
completion. Completing all measured units can therefore show 99% while final
publication is still pending. Failure and confirmed cancellation remain distinct
from success.

An operation without a trustworthy total displays **indeterminate** progress.
Time spent working does not manufacture a percentage. Existing background jobs
use this mode until their producers supply actual units. If the discovered total
changes, the status explains the change; the recalculated percentage can change
accordingly.

An estimated remaining time appears only after actual completed units and elapsed
time provide a throughput measurement. Pausing, changing phase or changing the
total resets that estimate. Merely viewing the status does not advance progress
or count down an unsupported estimate. Child operations report their own status;
they do not automatically invent an overall completion percentage.

## Optional feedback

Application sound is **Off by default**, allowing NVDA's own progress feedback to
operate without duplicate application tones. The native progress settings expose
only feedback supported by the current installation. Optional Windows tones use
a separate notification worker; they are never sent through the audiobook,
rendering or recording-output pipeline. Tones increase in pitch with measured
progress. Unknown-duration work and terminal states use distinct cues.

Preferences include the feedback interval, volume, mute, background behavior and
quiet behavior during media work. Speech requires an explicitly supplied approved
local speech adapter. This feature does not install a speech engine or promise
speech when no adapter is available. Producer integrations must report their
playback or recording activity for quiet-during-media suppression to be accurate.

The bounded `progress-preferences.json` file belongs to the active library data
folder. A full library backup includes and validates its exact bytes with the
other saved preferences. A library-scoped assistant backup omits this independent
global preference. Restoration preserves the user's settings without playing a
sound or acquiring a voice.

## Producer integration

`ProgressHub.start(...)` returns a thread-safe task. Report real units using
`update(completed=..., total=..., phase=...)` or `advance(...)`; use `pause()`,
`resume()` and `request_cancel()` for their corresponding states. A cancellation
request is not an acknowledgement that work has stopped. Call `cancelled(...)`
only after the worker has stopped, `fail(...)` after failure, and
`succeed(durable=True)` only after the successful operation has actually committed.

`DesktopFrame.run_job` binds its task to the worker using `bind_progress(task)`.
Existing producers can obtain it with `current_progress()` without changing
their user-facing callback signatures. Bindings are isolated between threads
and restored when nested operations finish. Known totals are optional; omit them
when they cannot be measured honestly.

For tasks created by `DesktopFrame.run_job`, the host owns final completion:
workers report units and return their actual result. They do not also call
`succeed()`. An acknowledged interruption uses `InterruptedError` or
`concurrent.futures.CancelledError`; a real I/O or publication error remains a
failure even if a cancellation was requested. Independent tasks created directly
through a hub own their own terminal transitions. A broken progress observer ends
as `unavailable`, preserving the separately reported operation result and never
turning committed work into a retryable operation failure.

Producers that return a domain status instead of raising an exception supply an
explicit `progress_result` adapter returning `ProgressOutcome`. Individual and
batch imports use this boundary: cancelled, timed-out, conflicted or quarantined
work cannot become 100 percent because its Python function returned normally.
The original detailed result still reaches its completion callback. A saved
batch reports accepted or intentionally skipped files against its fixed file
count; retrying an unfinished file does not make accepted counts go backwards.
Queue planning remains a separate operation from executing the queue.

Audio Studio uses this same task and native panel inside its own dialog. Voice
discovery finishes before the editor opens. Rendering counts the actual accepted
speech chunks, including verified cached chunks on resume; the unit label is
**speech chunks**, not an estimate of the complete audio production. Mixing,
music and finalizing the master are named phases. Complete speech counts remain
at 99% until the master, production manifest, captions and durable job result are
saved. A music-only project has indeterminate progress until completion.

The Studio Pause button first requests a checkpoint. The task becomes paused
only when the worker has retained that checkpoint and returned. Each resume
starts a fresh measured verification pass: unchanged chunks are reused, while
a changed voice engine cannot inherit an earlier run's accepted counts. The
previous paused job remains in Studio's saved history. Cancellation remains a
request until the worker stops. Ctrl+Shift+Q and
the native Query button expose this status without waiting for the render.
The form scrolls independently of the fixed progress, action and Close controls.

While Studio is open, notifications from its covered parent windows are
suspended. With quiet-during-media enabled, Studio cancels queued feedback and
yields to the event loop until the notification worker is quiet before starting
or resuming local playback. This wait can be cancelled with Stop playback or
closing the dialog. Paused playback also keeps extra progress cues quiet.

Recorder integrations should suspend notifications before opening the microphone
and wait for the feedback worker's `quiescent` property. A notification already
inside an OS playback call has a bounded duration; a timer noticing a recording
flag afterward cannot retroactively remove that sound. The native mute control
also remains available during long operations. If saving its preference fails,
muting still takes effect for the current session and the status explains that
it could not be saved. A failed unmute keeps feedback muted.

If progress measurement itself becomes unavailable, the operation's real result
is reported separately. A failed progress observer must not turn a committed
operation into a business failure or invoke its completion callback twice.

## Verification boundary

The source tests cover state transitions, unit validation, concurrent producers,
bounded history, throttling and feedback calculations. The native installed probe
checks real wx controls, focus preservation and busy-time keyboard querying.
Studio source tests additionally exercise the real Director, SQLite checkpoints
and FFmpeg using labelled synthetic PCM voices, including final-publication
failure, pause/resume and confirmed cancellation. Its native probe drives those
same controls and phases; synthetic PCM does not establish installed voice quality.
It reports `NOT_RUN` when native prerequisites are unavailable. Automated control
checks do not establish NVDA spoken output, audible pitch perception, real-device
mixing behavior or user listening acceptance; those require their own executed
Windows and NVDA checks.

Implementation references: [native wx gauge behavior](https://docs.wxpython.org/wx.Gauge.html),
[Python 3.11 Windows notification playback](https://docs.python.org/3.11/library/winsound.html),
and [NVDA progress-bar output](https://api.nvaccess.org/releases/stable/documentation/en/userGuide.html#ProgressBarOutput).
