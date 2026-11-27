# Independent listening speed and pitch

Playback controls can prepare a private listening copy at 0.25–4 times the
original speed, with a separate pitch adjustment from −12 to +12 semitones.
Changing listening speed does not change the voice's synthesis rate. A speed
change with pitch zero uses the existing typed tempo effect; pitch adjustment
uses the existing pitch effect and duration compensation. No new DSP engine,
player or public export path is introduced.

Apply stops the current transport, freezes the original path and identity and
prepares audio on the reader's existing worker. It never starts playback.
After verification, choose Play explicitly. Original rendered PCM, saved source
audio, voice settings and source quotations remain unchanged. A new rendered
recording resets listening controls to normal speed and pitch.

## Timelines and ownership

The public player path, duration, position, seek, A–B repeat and resume metadata
remain on the original recording's timeline. The backend reads the private
derivative. Every source/native time conversion uses the measured source and
output PCM frame counts at the same sample rate, rather than assuming the
requested speed is the exact achieved ratio. Source and output endpoints are
known from their actual frames. Intermediate correspondence is a linear
estimate: time stretching does not supply phoneme or word alignment.

Sleep remains a wall-clock timer. Apply clears repeat and sleep while retaining
the captured original resume position. Paused seek stays silent. Existing
quiet admission and explicit output identity checks still own every real Play,
Resume and repeat. Applying or selecting a derivative cannot bypass them.

A private snapshot is bounded, streamed, hashed and checked against the
original's immutable request identity. The existing FFmpeg converter renders
only that snapshot. The operation then checks the snapshot, original and output
again, including complete PCM lengths. It does not adopt a fresh filesystem
baseline after hashing. Stop, a new request, another original, a tab change or
owner closure revokes adoption.

At most one derivative is adopted by a player. Its private files cannot be
released while that player owns them. A failed native Close retains the alias,
prepared PCM and retry authority. Only acknowledged transport closure releases
those files. Reset to 1× / zero semitones uses an exact private copy of the
original and needs no optional engine.

Default preparation uses a 2 GiB source/output allowance, bounded block reads,
a disk reserve and a cooperative aggregate 300-second deadline. Synchronous
filesystem calls cannot be forcibly preempted. Native conversion and capability
children use the same cancellation token. These are private session artifacts,
not persistent library sidecars or execution authority restored by backup.

## Parent reader integration contract

The component extends the existing playback dialog with an optional callback:

    prepare_listening(request, completed, failed, *, active) -> cancel_callback

The dialog reads and validates numeric controls on the UI thread, captures the
original position, calls the existing Stop action, then freezes
player.listening_request(...). The parent must run:

    player.prepare_listening(
        request, audio_files.name,
        state_root=reader_root / 'state' if reader_root is not None else None,
        cancelled=stopped,
    )

on its existing serial worker. It captures the current playback/tab generation,
checks the same source/library scope before and after work, and includes
not active(), reader closure, generation change and ordinary cancellation in
stopped(). At completion, still on the native transport owner thread, it
rechecks those guards and calls player.apply_listening(prepared) before
completed(). That adoption does not play audio. Failed or stale results are
closed through the same worker-owned cleanup; they are never adopted simply
because conversion finished.

Keep an owned set of not-yet-adopted results in the parent. Register each result
before returning it from work. Remove it only after successful player adoption
or acknowledged discarded-result cleanup. Extend the existing ReaderResources
cleanup to close the remaining unadopted results before cleaning its temporary
audio directory. The existing retained-resource path on failed native Close
must remain intact. This closes the window where a worker result is ready but
the UI completion callback is dropped during parent shutdown.

Return a callable that sets the parent cancellation token. Closing the playback
controls invokes it while preparation is active; the callback's active guard
becomes false before widget destruction. No second executor or application
queue is required. The parent speech dialog is intentionally outside this
component's file ownership and must compose this small lifecycle bridge with
its current tabs/source guards.

## Verification gates

Required source/installed suites include tests/speech/test_listening.py,
tests/speech/test_player_controls.py, test_player_transport.py,
test_playback_devices.py and tests/ui/test_reader_playback.py. Actual FFmpeg
cases measure pitch independently from output duration, preserve original
hashes, refuse changed snapshots/sources, revoke completed results after Stop,
test post-hash mutation and retain files across native Close failure. The MCI
unit boundary is controlled and is not a Windows driver qualification.

The existing windows_reader_player_smoke.py keeps all prior real native
pause/seek/repeat/sleep/output/close assertions. Added controls use actual
FFmpeg plus the same actual MCI player, verify source/output timeline mapping,
independent pitch selection, silent Apply and exact reset. The probe now needs
the existing configured or sealed FFmpeg/FFprobe tools; explicit --ffmpeg and
--ffprobe options are available for the reviewed test runtime. The canonical
bundle route checks that those executables are the sealed tools.

The preserved draft has no completed final execution result: the executor
disconnected during the new actual-media selection. An earlier 101-case
existing-player preservation run completed successfully, but that is not
qualification of this complete component. Native Windows, acoustic quality
and screen-reader acceptance require their own actual results.

## Platform reference

The typed converter already chains atempo stages when needed, avoiding the
single-stage greater-than-two behavior documented by FFmpeg. Its pitch effect
uses asetrate, resampling and compensating tempo; the controls never accept
arbitrary filter graphs.

- [FFmpeg atempo](https://ffmpeg.org/ffmpeg-filters.html#atempo)
- [FFmpeg asetrate](https://ffmpeg.org/ffmpeg-filters.html#asetrate)
