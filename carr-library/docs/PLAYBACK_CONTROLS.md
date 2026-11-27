# Independent playback controls

The local preparation API changes the playback derivative, leaving the selected recording and saved synthesis settings unchanged. It accepts speed (0.25–4), pitch (−12–12 semitones), volume (0–1, including mute), and stereo balance (−1–1). Mono recordings use equal-power placement; stereo recordings retain separate left/right channels and attenuate the opposite channel when balanced.

`audio.playback_processing.prepare_playback(source, PlaybackControls(...), library_root=..., budget=AudioBudget(...), ffmpeg=..., cancelled=...)` uses the existing reviewed FFmpeg admission, resource coordinator, owned process family, bounded diagnostics, output-byte allowance and deadline. It returns a private PCM derivative, already byte-verified and pinned. It never opens a playback device. The declared memory reservation is cooperative admission, not a measured process-memory sandbox.

Callers retain their own source authority and generation guard. On the operation worker they prepare the derivative (or call `pin` again after an explicit release). Immediately before a possibly delayed native action they check that authority and call `ready`. They pass the returned path to the existing WavePlayer. After that player has acknowledged `stop` or `close`, they call `release`; `close` then deletes the private derivative. `close` refuses to discard pinned PCM. Failed native cleanup must retain all these owners until it succeeds.

`source_to_audio` and `audio_to_source` map inclusive endpoints in whole sample frames. The latter rounds conservatively. Time stretching and pitch processing do not supply word alignment: `source_time_accuracy` is `estimated_after_time_stretch` if either temporal control differs from its default. Reader callers must retain their original source positions and show that qualification when using mapped playback positions. This API does not grant quotation authority or automatically advance a reading cursor.

The strict source suite uses actual FFmpeg to check independent frequency/duration, stereo balance/mute, very short PCM, mapping, cancellation, byte limits and reader/cleanup ownership. The exact new source has not yet been executed while the local executor is unavailable. Physical sound, device-specific real-time controls and screen-reader behavior are not claimed by these tests.

## Audio Studio

The Playback page opens these controls for the explicitly selected playlist recording. Production also provides a separate button for the current rendered master. Both dialogs borrow Studio's existing WavePlayer and executor; they do not register a second player with the progress coordinator.

Prepare first stops and snapshots the current queue on a worker, hashing the original files under their verified readers. Preparation is separate from Play. All six numeric settings freeze while a derivative exists; New settings stops its player and discards only that private copy. Original-time seeking uses the explicit mapping above and keeps unsupported backend position reporting visible.

Closing waits for the active operation. Only after the same player confirms Stop can its source and derivative readers close and private PCM be removed. The worker rechecks original queue hashes before restoring the prior selected item, segments, A–B interval, device and original sleep deadline. Expired timers are not renewed. Restoring settings never starts audio; changed originals leave an empty stopped queue with an explicit review warning. Closing the settings before Prepare does not change the existing queue.

The native smoke prepares real local PCM through named wx controls, exercises frozen settings, original-time bounds, discard and close-during-operation. It intentionally does not start a playback device. The existing native WavePlayer gate remains the separate device-owner acceptance test.

## Live source reading

The existing live reader exposes separate playback speed, pitch, volume and
balance, plus selected PCM-byte and preparation-time allowances. Voice-native
settings keep their existing meaning. Every bounded utterance is first rendered
by the chosen voice, then optionally processed by the same admitted FFmpeg owner.
Default playback controls do not require another runtime.

Both original utterance and controlled PCM remain byte-verified through delayed
native handoff. A failed Stop retains these owners. Only actual completed playback,
successful reader validation and released private files advance the saved exact
text cursor. Validation failure stays sticky, so a second completion attempt
cannot accept changed audio. Cleanup failures retain the exact object for retry.

Seeking takes original utterance seconds. Where-am-I includes observed playback
time and the qualified original-time mapping. The selected source stays an exact
utterance range: processing does not invent word timing. Through-book reading
uses the same explicitly chosen controls for each bounded utterance and source
location, without preparing the entire book in memory.

## Synchronized EPUB and DAISY reading

The recorded reader uses the same controls and current WavePlayer. Its selected
preparation byte allowance covers both the already decoded publication PCM and
the controlled derivative. Changing controls requires an acknowledged Stop; Pause
can resume the unchanged derivative. Replaying cached PCM verifies its exact bytes
again on the existing worker.

Native playback coordinates map back into original publication time before
interval navigation and saving. The saved position remains bound to the full
publication digest. After speed or pitch processing the UI identifies the
original-time position as estimated. Stops retain reusable private PCM but release
its readers; replacing a publication removes its original and controlled private
copies while keeping the shared player. Closing retains failed cleanup ownership.

The new real-FFmpeg reader suites cover source preservation, independent mapping,
completion, cancelled preparation, combined byte limits, failed native Stop,
delayed media approval, sticky validation and retryable cleanup. Native SAPI and
EPUB/DAISY probes now exercise these controls through actual wx actions. The
recorded probe deliberately holds the normal media approval callback before an
output device opens. These exact new tests are **NOT_RUN** until their current
source/installed CI execution completes; physical output and NVDA remain separate.
