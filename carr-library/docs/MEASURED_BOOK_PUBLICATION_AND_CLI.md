# Measured book parts and headless book batches

This extends the existing BookAudioService, BookBatchService and
PublicationService. It does not introduce another voice runtime, audio
renderer, job store, player or backup format.

## Exact duration after rendering

Open **Audio → Reviewed book batches**, select the saved book and enter its
completed part number. Enter a maximum duration in **milliseconds**, a filename
template and an explicit new ZIP path. **Preview measured PCM parts** rechecks
the current retained source, saved Book plan, managed Studio document, typed
production plan, completion receipt, every retained artifact and actual PCM
frames. It creates no output.

The review displays every frame interval, actual duration, exact member name
and an approval SHA-256. **Publish reviewed measured parts** is separate. Any
change to the selected book, completed part, duration, naming template or
destination withdraws the prior review. The worker repeats those checks at
publication and checks source/destination again after the final stage flush.
A completed archive is never overwritten.

The selected maximum is converted to an integer number of frames by flooring
milliseconds × sample rate / 1000. Every source frame appears exactly once,
in order; the final part can be shorter. Boundaries are not guessed from word
count or voice speed. A boundary inside a spoken line is marked as partial:
its text mapping retains the whole line and explicitly has **no character or
word alignment**. The original source and completed production remain unchanged.

The archive includes the entire **selected Book review text and metadata** in
source/book-plan.json, including other parts of that saved review. This is
shown in the preview notice. It also retains the exact typed production-plan
bytes and measured timeline. Those inert records let offline verification
rederive the Book and production fingerprints without reading old external
paths, opening a voice/model installation, or granting quotation authority.
They do not grant permission to run archived code or providers.

Current limits are 1–86,400,000 milliseconds per part, at most 256 resulting
parts, and the existing 32 GiB default publication/source-graph budget. The
current PublicationService still supports its explicit bounded embedding
budgets. The bounded Book review remains at most two million source characters.
Metadata and ZIP central-directory limits are checked independently of PCM size.

The current verified PCM16 RIFF/RF64 reader and writer are reused. Parts are
stored without lossy encoding, in at most 1 MiB blocks. Large parts receive an
RF64 header; ZIP64 covers large archives. No full PCM master is allocated in
memory, copied to another whole-master stage, resynthesized or decoded. The
shared local resource broker admits the publication work and staging bytes.

## Durable publication and recovery

The existing managed audio project holds publication-<32hex>.json, schema
resonance-publication/2, now with format pcm-parts. Its prepared receipt binds
the exact output bytes and original source proofs before the no-replace move.
Late cancellation cannot reclassify an already committed archive as a new
operation to retry.

A move followed by a failed directory flush retains the prepared receipt and
reports that explicit verification is needed. **Verify measured publication**
rehashes the selected archive, checks its bounded ZIP graph, exact plan and
timeline identities, every part's container/PCM checksum and the checksum of
all concatenated PCM frames. It then resynchronizes the output directory and
completes the same receipt. It never overwrites the archive.

Complete library backup retains the Book plan, current managed production and
publication receipt through their existing typed producers. External archives
are selected explicitly after relocation; historical absolute paths are not
automatically followed. Completed retained PCM remains exportable after moving
the library and removing the original speech/FFmpeg installation. That is
**retained completion reuse**, not a fresh engine or acoustic qualification.

## Command line

The packaged direct entrypoint works without the desktop:

    python -m carr_library.audio.book_cli --data-dir LIBRARY --workspace WORKSPACE COMMAND ...

The main application dispatch may expose the same entrypoint as:

    python -m carr_library books --data-dir LIBRARY --workspace WORKSPACE COMMAND ...

The top-level route is a thin integration hook; both use the same services.
An existing library and explicit workspace are required. Opening the library
does not create it or migrate its schema. Runtime discovery happens only when
an incomplete part actually needs the current renderer.

### Capture ordered retained sources

Create a UTF-8 JSON request of at most 128 KiB:

~~~json
{
  "schema": "carr-book-capture-request/1",
  "title": "My reviewed reading order",
  "books": [
    {
      "edition": "EXACT_EXISTING_EDITION",
      "options": {
        "mode": "document",
        "characters": 10000,
        "pattern": "",
        "ignore_case": false,
        "name_template": "{index:04d} - {book} - {part}"
      },
      "role": {
        "name": "Narrator",
        "voice": "EXPLICIT_CURRENT_LOCAL_VOICE_ID",
        "language": "uk",
        "pan": 0,
        "rate": 0,
        "pitch": 0,
        "volume": 1,
        "effects": []
      },
      "sample_rate": 48000,
      "format": "wav"
    }
  ]
}
~~~

Each selection may additionally name its exact revision/topic, inclusive
first/last source positions, one EPUB chapter ordinal, or explicit bookmark
IDs for bookmark mode. The existing capture and split validators remain in
charge; logical segments are not relabelled as physical pages. Capturing a voice
identifier records the explicit review, not an availability or quality claim.

    ... capture --selection selected-books.json
    ... list
    ... list --batches
    ... show BOOK_PLAN_ID
    ... status BATCH_ID

Capture saves immutable source reviews and their exact order. It starts no
speech and stores no output permission. It acknowledges each saved plan ID
before later work; an interrupted/error result also retains candidate IDs for
explicit inspection of an ambiguous post-publication failure.

To reuse existing saved plans in another explicit order:

    ... batch --plan SECOND_PLAN --plan FIRST_PLAN --title "Reviewed order"

### Review, prepare and run

Supply exactly one existing external output parent per book, in saved order:

    ... review BATCH_ID --output-parent OUTPUT_A --output-parent OUTPUT_B
    ... prepare BATCH_ID --output-parent OUTPUT_A --output-parent OUTPUT_B --reviewed-sha256 REVIEW_SHA
    ... run BATCH_ID --output-parent OUTPUT_A --output-parent OUTPUT_B --reviewed-sha256 PREPARED_REVIEW_SHA

Prepare returns a new review fingerprint because its prepared state changed.
Use that returned fingerprint or perform another Review before Run. Every
destination/name/source/order change requires review again. The saved batch
never grants permission to an old output path.

Progress goes to stderr; JSON outcomes go to stdout. Ctrl+C requests a pause
of this invocation through the current service's cancellation callbacks. The
optional --cancel-on-interrupt changes the stopped invocation's reported
status to cancelled. Already accepted parts and current renderer checkpoints
remain retained. Stop cannot manufacture an engine result or erase a committed
export. Run returns exit 130 for paused/cancelled results and 0 only for complete.

For an incomplete live render, choose --ffmpeg as a global argument when the
current configured runtime needs an explicit executable. Completed verified
parts require no voice/FFmpeg discovery and report retained_completion=true.

### Measured PCM review, export and recovery

    ... duration-review PLAN_ID --part 1 --maximum-ms 600000 --output NEW.zip
    ... duration-publish PLAN_ID --part 1 --maximum-ms 600000 --output NEW.zip --reviewed-sha256 MEASURED_REVIEW_SHA
    ... publications PLAN_ID --part 1
    ... duration-verify PLAN_ID --part 1 --receipt PUBLICATION_ID --output EXISTING.zip

Pass the same --name-template to review/publish if using a nondefault name.
The review binds the current parent identity as well as requested/canonical
path, original Book source, completed audio and exact frame intervals.
Publications/status list recorded metadata; only explicit verification checks
current output bytes.

## Evidence and required gates

Adaptation authority: PR152 dc477add6db2fc0348637dd460d03b76d8e1c75f,
studio/measured_audio.py blob624e1daef35f0cfc2cc846b5c4923666612acf5e.
The current implementation replaces donor Project/runtime/state references
with current ProductionPlan, BookPlan, native project lock, resource broker
and publication receipts. It additionally accepts current RF64 masters and
bounds the ZIP central directory before the standard parser allocates it.

New source and tests were prepared during an executor outage and are **NOT_RUN**
until exact-source and isolated installed-package gates finish. Existing donor
or prior Book tests are not execution evidence for this extension.

Required strict suites:

- tests/audio/test_measured_book_audio.py — actual selected FFmpeg fixtures,
  concatenated PCM equality, small real RF64, real ZIP64 records without
  multi-GiB allocation, mutation/stop/recovery and actual backup/restore.
- tests/audio/test_book_cli.py — current-source capture/prepare/order/pins and
  an isolated fresh process bound to the same product/bootstrap origins.
- tests/audio/test_book_duration_cli.py — real rendered fixture, separate
  review/publish/verify processes and current completed-book re-export.
- tests/ui/test_measured_book_dialog.py plus existing test_book_batch_dialog.py —
  captured authority, stale callbacks and suppressed-result close settlement.
- Existing publication, production, Book backup and UI suites remain required.

The installed Windows windows_book_batch_smoke.py extension drives named
native measured controls through the current frame worker, verifies the actual
PCM archive and invalidated/reviewed settings, and preserves original-source
hash and close-settlement assertions. Synthetic tones and these native controls
do not establish real speech quality, device listening quality or manual NVDA
qualification.
