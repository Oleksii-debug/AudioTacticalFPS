# Reviewed audio conversion batches

Audio Studio → **Convert a batch of audio files…** accepts one to 64 explicitly
selected local audio or video files. It uses the same installed AudioRuntime,
FFmpeg, effect-chain controls and cooperative resource broker as individual
conversion. It does not create another renderer or background queue.

## Review and output

Choose shared output format, sample rate, channels, PCM bit depth or encoded
bitrate, optional source interval and ordered effects. Then choose an existing
parent directory and a batch folder name. Inspection runs on the existing
desktop worker. A separate native review lists the exact originals, measured
input properties, output names and any invalid inputs. **Cancel** is the default;
only **Convert reviewed files** starts conversion.

Each output name has a stable selection ordinal, such as 001-chapter.wav.
Equal basenames and repeated selections remain separate items. The folder must
be outside the library data directory. A fresh conversion requires a new folder.
The existing publisher never overwrites an output.

The selected folder contains **conversion-manifest.json**. Its reviewed plan binds
the original bytes and file identities, tool bytes, shared settings, parent
directory identity and exact output names. Each item records its own state,
error and, after encoding, measured output metadata and SHA-256. Failed media
does not discard earlier successful outputs or prevent later valid items from
being attempted. Cancellation retains completed outputs.

The native result reports completed and failed counts, the captured folder and
any receipt-recovery warning. A library change before confirmation or publication
refuses that work. A library change after durable publication does not turn a
completed write into an alleged failure.

## Explicit continuation

Select the same originals in the same order, choose the same settings and folder,
and check **Resume this exact saved batch; keep completed matching files**.
Review again. Matching completed outputs are fully verified and kept without
encoding them again. Changed originals, changed tools, changed settings, a
replaced folder, foreign output collisions or changed completed outputs refuse
continuation.

A candidate receipt is saved before the existing create-only publisher links the
validated encoded file into the chosen output folder. If execution stops after
that link but before the final receipt, continuation may accept only the exact
candidate bytes. If the interrupted publisher left its staging hardlink, recovery
requires the exact two regular names, recorded inode/device/size/time identity
and actual SHA-256 before removing only that owned staging alias. It does not
relax the ordinary no-link reader or delete unrelated files. An unconfirmed
publication remains visible as requiring attention.

Receipts are external output artifacts. They are not an automatic execution
capability, a new library backup type or an instruction to open stored paths.
Continuation reconstructs the reviewed plan from the user's newly selected
inputs and current configured runtime before it reads the prior manifest.

## Command line

The existing Studio entrypoint has an additive **convert-batch** command. Its
**convert** alias uses the same folder-and-receipt contract even for one input.

    python -m carr_library.studio.cli --data-dir LIBRARY convert-batch first.wav second.flac --output OUTPUT_PARENT/reviewed --format wav --channels 1 --preview
    python -m carr_library.studio.cli --data-dir LIBRARY convert-batch first.wav second.flac --output OUTPUT_PARENT/reviewed --format wav --channels 1
    python -m carr_library.studio.cli --data-dir LIBRARY convert-batch first.wav second.flac --output OUTPUT_PARENT/reviewed --format wav --channels 1 --resume

**--preview** prints a JSON plan and creates no output folder. The execution command
is the explicit approval of the listed sources, destination and options.
Progress goes to standard error; the result and per-item receipts are JSON on
standard output. Exit code 0 denotes a completed batch or successful preview;
2 denotes a partial, cancelled, timed-out or attention result; 1 denotes refusal
or other failure. Keyboard interruption keeps accepted outputs and is reported
as cancellation, or exit 130 if no saved batch result is yet available.

Options include --sample-rate, --channels, --bit-depth, --bitrate,
--start, --duration, --gain-db, --tempo, --loudness, --denoise,
--timeout, and --max-bytes. **--preset** selects an existing local saved preset
ID and cannot be combined with different basic effect controls. No filter
expression, executable argument list, remote service or model download is
accepted through these options.

## Bounds and verification

A batch accepts at most 64 inputs, 8 GiB per input and 32 GiB in total. Each
manifest is bounded to 4 MiB. Output and temporary staging are constrained by the
runtime byte budget and admitted through the existing resource broker. The
limits are declared cooperative allowances, not claims about available hardware
or a memory sandbox. Explicit format support remains the installed FFmpeg
runtime's responsibility.

The source regression set uses real short PCM files, actual FFmpeg conversion,
real subprocess death after publication, lease ownership, cancellation, source
and output mutation, and final-receipt failure. CLI children import the package
selected by their parent test process. The existing Windows ordered-effects
probe retains its prior assertions and adds actual native batch review,
two-file conversion and explicit completed-output reuse. Source delivery alone
does not claim those new cases have passed: the exact composed source and
installed-package gates must execute them. Physical listening quality and NVDA
acceptance remain separate checks.
