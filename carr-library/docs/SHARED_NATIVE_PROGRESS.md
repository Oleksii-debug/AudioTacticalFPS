# Shared native progress

This view connects the current progress contract to the existing desktop.
It reuses ProgressHub, ProgressReporter, ProgressPreferences and
NotificationWorker. It does not introduce another job queue or progress tracker.

## User workflow

The main window contains a named native progress bar, a readable status field,
and a selector for operations and their subtasks. Automatic updates preserve
keyboard focus. Choose Query current progress to move focus to its full text.
Options contains stable, configurable commands:

| Command ID | Action |
|---|---|
| progress.query | Query current progress |
| options.progress | Progress feedback settings |
| progress.mute | Mute or unmute extra progress feedback |

These commands have no default key assignment, preserving existing saved
shortcuts. Use the current Keyboard shortcuts settings to assign them. Their
buttons and menu commands remain usable while an operation is running; other
actions retain the host's busy-state restrictions.

Known totals display actual counts and percentage. A completed count at the
known total remains at 99 until the existing service returns its verified result.
An unknown total uses the native indeterminate bar and explicit text; no time
estimate or percentage is invented. Pending review, partial, paused, failed and
cancelled outcomes stay distinct from verified completion. The original
operation owns validation and publication. Progress observations cannot retry or
undo an accepted result.

Extra sound is Off by default. Existing preferences support Off, Beep, Speech,
Beep and speech; interval, volume, mute, quiet mode, background permission and
suppression during app media playback. Speech uses the current optional local
English SAPI path; missing support is a visible status, not another-language or
network fallback. Muting takes effect immediately even if saving the preference
fails. Unmuting and other changes persist before activation. The current typed
backup already includes progress-preferences.json.

## Integration

DesktopFrame keeps its current executor, cancellation event, completion
callbacks and control guards. Each accepted run_job operation creates one
reporter and binds it while the existing operation executes. Operations that
already expose measured units can use current_progress. The transcription
dialog reuses that binding and does not create an additional reporter.
Uninstrumented operations are honestly indeterminate; this change does not
claim measured phase coverage for every existing service.

The view polls the same bounded hub from the wx thread. NotificationWorker
receives the shared snapshot, foreground state and that hub's media_activity
state. It owns no production audio samples. The current player's separate media
lifetime integration supplies actual playback suppression. No microphone
capture implementation or manual listening qualification is claimed by this
view. Notification shutdown remains nonblocking.

Native gauge/focus/menu/settings patterns were reviewed from earlier 80ab and
7bf contributions. Their alternate progress trackers, preference formats,
notification engines and job facades were not imported.

## Exact validation required

The initial source candidate was prepared during an executor outage. Source
preparation and independent review are not executable PASS evidence.

Required headless source and installed suites include:

- tests/ui/test_progress_view.py: 25 cases, including real background workers,
  actual file publication, late cancellation, observer failure, persisted
  settings, busy command availability, separate task selection and focus.
- tests/ui/test_shared_progress.py: retain the existing complete suite.
- tests/ui/test_desktop_handlers.py and tests/ui/test_keyboard_manager.py:
  retain existing host and keyboard assertions.
- tests/transcription/test_pr99_host.py: all 27 current cases, adapted to the
  single actual host reporter where previously the dialog created one.
- tests/transcription/test_supplied_worker_bridge.py and
  tests/storage/test_transcription_backup.py: retain the existing complete
  service/backup gates.
- The media-lifetime and preference-backup suites from the paired maintenance
  contribution remain separate required gates.

Run the exact installed-wheel native probe after the current test dependencies
and activation are ready, inside the existing Windows acceptance boundary:

    python -I -B -X utf8 scripts/windows_shared_progress_smoke.py

The probe requires exact installed module bytes, actual wx controls and real
Windows key dispatch, preservation of focus during measured updates, UI
Automation's read-only range/value at 50 and after verified 100, real queued
work, cooperative cancellation, a committed file surviving late cancellation,
persisted native settings and closed notification lifecycle. Synthetic work is
explicit. The probe reports manual NVDA and audio listening as NOT_RUN; these
human acceptance dimensions cannot be inferred from native control assertions.
