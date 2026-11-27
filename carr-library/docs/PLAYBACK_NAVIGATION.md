# Bounded reader playback navigation

The navigation controller in `ui/playback_navigation.py` borrows the reader's
existing `WavePlayer`. It adds a configurable backward/forward step (1–3600
seconds), percentage seek (0–100), explicitly enabled A–B repeat, and a sleep
timer (1 second–24 hours). A–B must span at least 0.25 seconds. Seeking preserves
the existing paused/playing intent. A seek outside an enabled repeat range
disables repeat. The sleep timer counts wall time, including paused time.

The reader host owns one 200 ms wx timer. Closing the controls window leaves an
explicitly enabled repeat or sleep timer active; stopping audio, loading another
recording, or closing the reader clears both. No navigation action starts stopped
audio. The only automatic replay follows the natural end of the same recording
when the user has explicitly enabled its current A–B range. A manual Stop is
distinct from natural completion and does not restart the player.

Open **Playback navigation...** in the voice reader after explicitly reading
text. Its named controls are available through the ordinary Tab/Space keyboard
flow. The existing **Seek to time** action uses this same controller. The panel
can also be opened before playback to inspect the controls; transport actions
remain disabled until the reader has started a current recording.

## Integration contract

Create one `PlaybackNavigationHost` for the reader window, supplying its current
playback-generation getter, window-alive predicate, existing `stop_audio` callback,
and existing progress controller. Call `bind()` only after an explicit successful
`player.play()`. Preserve this binding across pause/resume. Call `invalidate()` at
every reader Stop, new playback request, reading-source replacement, and any edit
that invalidates its rendered audio. Call `close()` before player or temporary
audio teardown. The controller never closes the borrowed player.

Each binding records the exact generation, local PCM path, duration and file
identity/stamp. Delayed seeks recheck the binding and use existing
`play_when_quiet`, including its finite deadline. Repeat queues at most one seek
at a time. Expired sleep wins over a queued seek and calls the reader's Stop so
that previously queued reader resumes also lose their generation. Failed native
Stop is reported as a failure, never a successful sleep stop. Device-query errors
clear repeat/sleep without retrying indefinitely or changing another recording.

## Precision and verification

The display retains the backend distinction: Windows MCI supplies a native
measured position; the existing POSIX ffplay backend supplies a clock estimate.
Repeat uses the 200 ms control interval and the backend's seek precision. It is
not sample-accurate editing. This feature adds no playback-speed conversion,
output-device selector, new synthesis engine, or automatic playback on opening
the controls.

The shared `tests/ui/test_playback_navigation.py` suite exercises finite bounds,
pause preservation, stale generations/files, delayed-start cancellation,
explicit/natural-end repeat, sleep priority, UI controls and owner cleanup. The
separate POSIX `tests/audio/test_playback_navigation_posix.py` gate uses actual
PCM WAV and ffplay with SDL's dummy sink to verify seek process replacement,
pause, child cleanup, unchanged input bytes, natural EOF and explicit Stop.
These are not physical-device, audible-quality or native Windows qualification.
`tests/speech/test_playback_reader_hook.py` composes the actual installed reader
closures with the controller, including sleep revoking deferred Resume, source
replacement revoking a pending Play, and Stop during the time-entry dialog.
Installed Windows keyboard and physical-device qualification remain separate.

`scripts/windows_playback_navigation_smoke.py` is an installed Windows UI and
protocol probe. It checks exact installed module hashes, runs the real reader
and navigation host in a normal wx MainLoop, and sends native Tab/Space/F6/F7/F8/
Escape events only after confirming this process owns foreground and focus.
The provider publishes actual synthetic PCM through the existing VoiceRegistry;
an explicitly substituted clocked transport emits no audio. The probe covers
disabled controls before explicit reading, paused seeks, marked and enabled A–B,
sleep after closing the controls, a fresh generation, explicit Stop and owner
cleanup. Its result labels MCI, physical output, voice quality and NVDA as
`NOT_RUN`. It does not replace the existing actual WavePlayer/EPUB audio gates.
Native Windows execution of this new probe is pending until its CI run completes.
