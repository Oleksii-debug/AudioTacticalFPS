# Ordered audio effects and saved presets

Open **Convert audio and apply effects** from the Audio Studio menu and choose a local source. The existing numeric conversion controls remain available. Choose **Edit ordered effect chain and presets** to review a sequence of effects supported by the selected local audio runtime.

This editor applies to an offline conversion of the selected source. It does not install an engine, download a model, or change the live reader's playback settings.

## Review a chain

The list runs from top to bottom. Select a row to edit its numeric controls, move it up or down, remove it, or toggle bypass. Bypass retains the effect's settings without processing that row. All numeric values are still checked, including values in bypassed rows.

**Undo chain change** and **Redo chain change** affect this editing session. **Apply effect chain** returns the reviewed chain to the conversion window; it does not start encoding. **Cancel** keeps the previous conversion settings.

The available kinds come from the existing typed DSP implementation and the installed FFmpeg capability inventory. The editor accepts known numeric controls, not arbitrary filter graphs or executable arguments. A chain has at most 32 effects. Frequency controls must remain below half the selected sample rate.

The sample rate shown in the chain window is the conversion rate at which you opened it. The final conversion validates the chain again against the current output rate. If a later rate change makes a filter invalid, correct that value or choose a suitable rate before starting the conversion.

## Basic controls and ordered controls

When a reviewed ordered chain is active, the basic gain, playback speed, target loudness and cleanup controls are inactive. The ordered list determines those operations, so the same gain or cleanup is not applied a second time.

Choose **Use basic effect controls** to return to the original numeric controls and their retained values. The output format, rate, channels, bit depth, bitrate and trim controls continue to use the existing conversion rules.

Choose a new output filename to start conversion. An existing output is rejected. The captured destination and the existing atomic publication path preserve a new or changed target that appears while work is running. The selected source is not overwritten. Completion warnings about cleanup do not turn an already checked output into a failed export.

## Save and load presets

**Load effect preset** offers the existing Dry audio, Narration cleanup and Podcast loudness presets, followed by presets saved in this library. Loading changes the editable chain; applying or cancelling the chain remains a separate decision.

**Save chain as a new preset** writes the reviewed names, sample rate, effect order, numeric parameters and bypass states to this library. Saving is explicit and independent of the editing window's Cancel button. A saved preset therefore remains available even if you cancel the current conversion afterward. A new save creates a new identity rather than silently replacing a preset with the same name.

Each saved preset is a bounded typed JSON record: at most 64 KiB, with a lowercase 32-character identity. A library may contain up to 256 preset records. Invalid fields, out-of-range values, oversized files, unsafe file links and mismatched identities are rejected. A conflicting update preserves the current record. If publication completed but a later synchronization step could not be confirmed, the store reports that distinction.

Complete library backups include the exact validated preset records through the existing common backup inventory. Restoration does not launch an audio process or grant a preset any output-path authority. Unknown files in the preset namespace are rejected rather than silently omitted.

## Work and cancellation

Capability inspection runs on the existing background worker and uses the shared resource broker before launching the local audio tools. The conversion itself keeps the existing admission, cancellation, output verification and publication checks. Closing an effect review cancels its own pending inspection and prevents its late callback from reopening a deleted window. It does not cancel a different host operation.

Resource reservations are cooperative limits for the participating application operations. They are not operating-system memory enforcement or measurements of audio quality. If the saved resource policy cannot admit an operation, review that policy or wait for competing work; the editor does not silently alter the runtime settings.

## Verification scope

The portable regression suites exercise ordered typed chains, bypass and numeric validation, preset conflicts and bounds, backup/restore, actual FFmpeg conversion and cancellation, and native-handler lifetime boundaries.

The Windows acceptance command is:

    python -I -B -X utf8 scripts/windows_effect_chain_smoke.py --audio-evidence PATH_TO_EXTERNAL_TEST_AUDIO_BINDING_JSON

The installed package must match the reviewed source, and the explicitly prepared external FFmpeg/ffprobe files must match the binding report. The probe uses real wx windows, keyboard events and the current worker. It measures a real converted PCM result, checks the original bytes, and delivers an actual completed capability result after direct and parent-driven native deletion.

Only the source/output file-picker answers are supplied by the synthetic fixture. Physical devices, audible quality, manual screen-reader use and production voice quality are not qualified by this probe. The script reports its own execution result; its presence in the repository is not a claim that a particular build has passed.
