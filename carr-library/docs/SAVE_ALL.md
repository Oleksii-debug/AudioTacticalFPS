# Save all open drafts

File → Save all open drafts saves open research notes and Audio Studio projects.
Its default main-window shortcut is Ctrl+Alt+S; Options → Keyboard shortcuts can
change it. F1 lists the current binding. Audio Studio provides the same named
button while its modal window is open. Ctrl+S and Save project as keep their
existing project-specific behavior.

The final report appears after each participant confirms completion. A research
note confirms only after its database transaction commits and its displayed
saved baseline updates on the UI thread. An audio project confirms only after
the existing project save and baseline refresh complete. Starting a worker or
returning a truthy value cannot announce a completed save.

Newer edits remain dirty and appear in the final report. Failed or cancelled
saves retain their drafts and do not prevent other available drafts from being
saved. Save As still uses the native chooser, including its normal cancellation
and overwrite checks. Closed/replaced views unregister their own participants.
The coordinator rechecks live drafts before claiming that everything is saved.
Unsaved dialog forms outside research notes and Audio Studio are not registered
and retain their existing explicit Apply/Save behavior.

The completion coordinator and its protocol tests are reused from AF5 source
commit `a3eb3d7ffe20ac23280c5fdc32c74bc6420f0288`. The desktop integration uses
the existing F749 command registry, library evidence transaction, Audio Studio
save implementation and recovery sidecars; it adds no alternate persistence
format or background scheduler.

Source tests exercise actual SQLite commits/rollbacks and actual project JSON
publication with headless control doubles. The separate native Windows probe
uses real widgets, menu events, the threaded library job, and real files. Linux
or missing wx returns NOT_RUN/77; it is not a native pass or an NVDA claim.
