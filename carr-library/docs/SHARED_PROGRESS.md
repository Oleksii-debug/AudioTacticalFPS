# Shared operation progress and optional local feedback

## Existing progress and job authorities

The native main window displays the current operation from `carr_library.progress.ProgressHub`. Its reporter is bound while `DesktopFrame.run_job` executes. Local imports, saved import batches, reviewed assistant jobs, translation jobs and Audio Studio feed their current services' results into this view. No second job store, synthesis registry or production pipeline is introduced.

The named Gauge shows measured work when the service supplies a denominator. An unknown total pulses and says “total unknown”. Known work stays below 100 until a verified successful service result returns. Failed, timed-out, cancelled, paused, quarantined and unknown explicit states cannot become successful completion. A late cancellation request does not erase an already committed typed success.

The import observer runs after each actual file outcome. It never consumes an iterator to estimate its size; single-file and unsized input stay indeterminate. Observer failure cannot retry or reclassify an import that already committed. An acknowledged cancellation stops the remaining files. Sources, catalogs and job permissions remain governed by their existing services.

Production render messages are phases with an unknown total. This UI does not derive a percentage from chapter-number text, elapsed time or a guessed duration. The production engine still owns its disk checkpoints.

## Native controls and callback lifetime

The main window includes one named Gauge, readable progress text, an operations/subtasks selector, query/settings/mute buttons and “Quiet now (this session)”. Automatic updates never move focus. The explicit query focuses the progress text so the user can read it with a screen reader. The controller is `OperationProgress`; the compatibility `frame.progress_view` name points to that same object.

Options contains Query current progress, Progress feedback settings and Mute or unmute extra progress feedback. The existing keyboard manager retains stable IDs `progress.query`, `options.progress` and `progress.mute`, with unassigned defaults to avoid conflicting with saved user shortcuts. These three actions remain available during a main-window worker; unrelated menu commands and scoped navigation keep their busy restrictions and previous enabled states.

A cancellation request is reported separately from the worker's acknowledgement. Closing an assistant dialog signals its worker without taking a competing journal lock. The shared view can receive the eventual durable paused checkpoint while the closed dialog ignores late widget updates.

The same hub is retained for the window lifetime so an active player or quarantined native handle keeps its media guard across a library change. Old-library events, voice selection and notices are cleared from the view without replacing that authority. Every queued view update checks the operation and library identity. A new operation, another library or a closed frame invalidates older callbacks and notices. A completed previous operation is not replayed as a cue for new work. Concurrent jobs keep distinct identities; the main Gauge presents the current operation rather than combining unrelated denominators.

## Explicit local audio selection

Progress audio defaults to Off. Modes are Off, Beep, Speech and Beep and speech. Interval, volume, mute, quiet, background permission and media suppression use the existing library progress preferences. Version 1 remains readable; version 2 adds the explicitly selected voice, notice language and SHA-256 witness of the voice/adapter/runtime identity. Complete backup, read-only verification and restore preserve these preferences.

The settings dialog discovers existing local voices in a bounded worker. The user chooses a voice and English, Ukrainian or Slovak notice language. There is no first-English-voice fallback. The existing engine registry verifies the voice and runtime again before synthesis. Missing or changed local resources stop progress speech with a readable reason; no model is downloaded and no provider is contacted.

Short notices use a separate temporary WAV and the existing local WavePlayer. Their samples never enter a rendered master, a reading derivative, an imported asset or a recording. Language availability is limited by the actual installed engine. This feature does not qualify 135-language synthesis, NVDA announcements or listening quality.

Quiet now changes only the current session. Persistent quiet mode remains in settings. Terminal notices suppressed by quiet/background/media policy are discarded instead of being replayed later without context.

## Playback handoff

Audio Studio registers its current player with the presenter. Observed playing, starting or unknown device state can suppress optional notices. Before content starts or resumes, an asynchronous handoff cancels this presenter's notice and waits for its actual player cleanup. The native event loop never waits on a device. Even when notices during media were explicitly allowed, a content-start handoff first releases the notification output.

Cancelled requests, changed libraries, closed dialogs and timeouts cannot start content later. Failed native cleanup remains conservatively busy, so content does not start over output that may still be audible. This code never stops a screen reader or another application's audio.

## Verification

Portable checks use real shared progress objects, local TXT imports, saved preferences and complete backup/restore. Deterministic event/device boundary doubles test callback and ownership rules; those cases are not native Windows device acceptance.

Required source and installed suites:

- `tests/ui/test_shared_progress.py`
- `tests/ui/test_operation_progress.py`
- `tests/ui/test_progress_view.py`
- `tests/e2e/test_import_progress.py`
- `tests/ui/test_desktop_handlers.py`
- `tests/ui/test_audio_studio.py`
- `tests/ui/test_audio_playback_controls.py`
- `tests/ui/test_import_center.py`
- `tests/assistant/test_dialog_lifecycle.py`
- `tests/ui/test_translation_dialog.py`

Run `scripts/windows_progress_smoke.py --native` and `scripts/windows_shared_progress_smoke.py` with the installed Windows Python. The probe drives actual wx focus, an explicitly configured current-progress shortcut, native cancel-button dispatch, a worker's fsynced synthetic output, measured 0/25/50/75/99/100 states, unknown totals and late cancellation. Installed module bytes are compared with the exact source under test. The shared-progress probe also queries actual Windows UIA read-only Gauge values at 50 and verified 100 percent, checks named settings controls and persists their chosen values. Both probes operate on the same controller. Linux returns 77 / NOT_RUN. Native Windows execution, NVDA speech and voice/device listening require their actual environment and remain NOT_RUN until performed.
