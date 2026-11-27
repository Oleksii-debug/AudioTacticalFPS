# Adjustable effects and named processed-copy presets

## Current workflow

Open **Audio Studio → Convert audio and apply effects...**. Select one existing local audio or video file, review its interval and output settings, and choose a **new** output filename outside the application's private library. Processing writes a separate copy. The selected original is read and checked again before publication.

Basic controls offer gain, processed-copy speed, processed-copy pitch, optional speech cleanup and measured loudness normalization. **Edit ordered effect chain and presets** checks the configured local FFmpeg capabilities on the existing cancellable desktop worker, then opens the ordered editor. Effects run from top to bottom. Select a row to edit its numeric controls, bypass it, move it, or remove it. Undo and redo each retain up to 32 in-memory changes. Apply returns the reviewed chain; it starts no audio. **Use basic effect controls** returns to the basic form.

All controls have accessible names and keyboard focus. The supplied Windows acceptance probe drives the real editor and preset dialogs with keyboard input; manual keyboard and NVDA qualification remain separate requirements.

## Available controls

The current DSP exposes 21 typed effect kinds. A configured FFmpeg build must actually support each enabled kind; the editor does not substitute a missing effect.

| Group | Effects and control purpose |
|---|---|
| Level and dynamics | Gain; gate; compressor; limiter; loudness normalization |
| Frequency shaping | Parametric equalizer; high-pass; low-pass; low shelf; high shelf; notch |
| Cleanup | Noise reduction; declick; declip; de-esser |
| Timing and space | Single delay; limited four-reflection reverb; chorus |
| Processed-copy transformation | Tempo; pitch; stereo width |

Numeric bounds and units come from the existing DSP definitions. Frequency controls must also fit the selected sample rate. Bypassed effects retain their settings and still undergo parameter validation. No raw filter graph, command, executable path or model is stored in a preset.

Reverb is an explicitly limited early-reflection effect, not convolution or a calibrated room model. Delay is one echo without recursive feedback. Noise removal and other repair controls can change wanted sound; their presence and synthetic regression results do not establish listening quality for a particular recording.

## Speed, pitch and synthesis are different controls

**Processed-copy speed without pitch change** changes the duration of the new audio file while retaining the intended pitch. **Processed-copy pitch in semitones** shifts the new file's pitch while retaining its planned duration. The renderer measures the produced file and checks the requested format and duration.

These controls do not alter an already playing file. They do not claim live playback speed or pitch support. Reader synthesis rate and pitch remain settings of the selected installed voice provider, with that provider's existing capability rules.

## Named presets

**Save chain as a new preset** stores the current typed chain under a new name in the active library. **Load effect preset** offers Dry audio, Narration cleanup, Podcast loudness and the saved user presets. Loading changes the editable chain only. A saved preset survives Cancel in the enclosing editor because Save was a separate explicit action.

Each saved record includes its identity, name, sample rate, ordered effects, bypass states and revision. There are at most 256 records, 32 effects per chain and 64 KiB per record. The store rejects malformed JSON, duplicate JSON keys, unknown fields, links, unexpected entries and stale same-path library owners. An existing preset update through the domain API requires the exact previously reviewed fingerprint.

If the preset file is already visible but its final directory flush fails, Save reports the existing preset identity as **saved, durability unconfirmed**. Use **Load effect preset** to review that record before another change; repeating Save is a separate new-preset action. A competing file for the same preset identity remains a conflict even if its bytes match.

Named presets are ordinary private settings. Their typed backup adapter must validate the exact record schema and filename identity; it grants no executable, model, source or quotation authority. They are retained independently of generated audio.

## Source, output and cancellation boundaries

The review binds the exact library object and directory identity, source file identity, selected output parent and destination state. Modal returns, queued work and late callbacks respect the current host scope. Replacing the library at the same path is a scope change.

The worker copies the explicitly selected source into bounded private staging, hashes those bytes, and uses the existing AudioRuntime and shared ResourceBroker. PCM output uses its measured frame allowance. Compressed output reserves the larger of PCM and selected encoded bitrate with a bounded variable-rate/container allowance; that same amount becomes the codec's enforced per-run output cap. It preserves the configured thread policy. An explicit policy that cannot admit that runtime fails visibly; it does not silently select another thread count.

Inspection and processing use existing owned native process cleanup. If a codec's CleanupOwner has not confirmed termination, staging and resource admission stay owned until it does. Closing a queued capability review cancels that review. Cancellation before final publication leaves no accepted output.

Publication uses the existing no-clobber file publisher after another source-byte and destination check. A competing file is a conflict even if its bytes match. Once the final link is visible, late cancellation does not change a saved result into a cancelled one. A later directory-flush failure is reported as **saved, durability unconfirmed**; retained temporary cleanup is reported separately with its existing cleanup owner.

## Acceptance and limits

The source suite contains actual FFmpeg tone tests for ordered effects, bypass, processed-copy tempo versus pitch, source preservation and no-clobber publication. Controlled real-child tests cover capability-probe cancellation and admission retained through delayed native cleanup. Control-double tests cover same-path LibraryApp replacement, root changes, closing, nested dialogs and late callbacks.

The native probe requires an installed wheel whose relevant source hashes equal the candidate checkout, a pinned explicit FFmpeg/ffprobe installation and actual Windows controls. It verifies saved presets, edit/bypass/reorder/undo/redo, real 48 kHz mono conversion and measured attenuation. Source/output picker results are provided by the fixture and are labelled separately.

This slice has no execution or native acceptance claim until those exact candidate gates run. Synthetic tones and protocol checks do not constitute acoustic quality, physical-device, manual accessibility or NVDA qualification.
