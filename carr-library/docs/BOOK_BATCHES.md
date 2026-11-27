# Reviewed multiple-book audio

This adds an explicit ordered review of existing library books. Each book keeps its own source range, split, installed voice, supported rate and volume, output format and chosen output parent. The existing BookAudioService and Studio jobs perform all rendering; the batch is immutable selection metadata.

## Desktop workflow

Open **Audio Studio → Book audio: split and render…**, then **Multiple books…**. Choose up to 32 existing books from the active library. For each entry, choose **Review selected book**. The existing Book audio window shows its retained source revision and lets you choose the exact installed local voice, supported rate/volume, split and output folder.

Choose **Preview split and names**, then **Use reviewed book**. This captures review metadata and starts no speech. Return to the ordered list and use **Move selected book up/down** if needed. **Preview entire batch** rechecks every source and destination.

**Prepare batch items** asks for confirmation with No as the default. It saves the exact plans and existing ordinary Studio jobs; output folders remain empty. **Run or resume batch** requires a separate confirmation. A failure, Pause or Cancel stops this invocation before another item is started. Returned completed results and accepted Studio chunks remain available.

**Saved batches** reopens immutable references and stored checkpoints. It does not start or resume audio. Choose a new explicit Run to verify completed artifacts and continue existing jobs. A saved status alone does not prove that external output bytes are still intact.

Closing the child review or changing the active library invalidates its queued callbacks. The parent Book workspace retains its original service and the existing serial worker owns cleanup.

## CLI workflow

The existing Studio entry point has a `book` family:

```text
python -B -m carr_library studio book --data-dir LIBRARY --workspace WORKSPACE preview --request request.json --review review.json
python -B -m carr_library studio book --data-dir LIBRARY --workspace WORKSPACE prepare --review review.json --approval REVIEW_SHA256
python -B -m carr_library studio book --data-dir LIBRARY --workspace WORKSPACE run BATCH_ID --output PLAN_ID=EXISTING_DIRECTORY --approval PREPARED_SHA256
```

Repeat `--output` once for every saved plan; its order is resolved through the immutable batch. The SHA shown after Prepare is the current prepared approval and differs from the initial preview. The caller must explicitly review it before Run.

A request has the exact schema below. Replace identities and paths with the chosen existing library entries and installed voice. No named profile, inferred language or voice fallback is accepted by this request version.

```json
{
  "schema": "carr-book-batch-request/1",
  "title": "Reviewed reading order",
  "books": [
    {
      "edition": "EXISTING_EDITION_ID",
      "voice": {
        "language": "en-US",
        "voice_id": "EXACT_INSTALLED_VOICE_ID",
        "rate": 0,
        "volume": 100
      },
      "output_parent": "EXISTING_OUTPUT_DIRECTORY",
      "options": {
        "mode": "document"
      },
      "format": "wav"
    }
  ]
}
```

Each book may additionally specify the existing exact revision, reading position range, EPUB chapter, supported SplitOptions fields, and sample rate. Unknown fields fail visibly. `list`, `status`, `review`, `pause` and `cancel` use saved batch identities. An active stop signal is bound to its invocation token and the actual currently owned Studio part; it cannot cancel another batch merely because both reference a plan.

Review files use the existing source-aware, no-clobber exporter outside private library data. An existing review file is preserved. The source is re-attested through every publication checkpoint. JSON stdout is ASCII-escaped so redirected Windows code pages preserve Unicode values.

Exit 0 reports the completed command result, whose JSON status must also be read. Exit 1 is a refused or failed operation; an error may still include actual earlier returned Studio results. Exit 3 reports a visible result or saved selection that needs durability or cleanup attention. Exit 130 reports an interrupted invocation. A cleanup warning never erases a previously returned output receipt.

## Backup and restore

Saved selections live in the reserved `state/studio/book-batches/<workspace-hash>/<batch-id>.json` namespace. A record contains only the immutable batch and its fingerprint. It has no output permission, automatic-run flag, runtime token or private origin nonce.

Both full-library and standalone state backup validate actual copied bytes against their manifest, then bind every ordered item to the exact copied Book plan. The existing Book backup verifier separately binds plans to jobs and, for a full-library backup, to the copied database source anchors. Zero prepared jobs and interrupted partial Prepare remain valid recoverable states.

Runtime stop controls are outside state and are not transferred. Studio's local origin remains nonportable. After a full restore, choose and review a fresh output folder for each book, Prepare again, and inspect the fresh names before Run. Restored metadata never grants permission to reuse an old external output path.

## Bounds and qualification

A batch contains at most 32 plans, 256 total parts and eight million selected Unicode characters. Each saved selection is limited to 128 KiB; a workspace holds at most 4096 such records, with a 64 MiB total selection-inventory limit. Per-book source and Studio runtime limits remain unchanged. Immutable plan metadata and actual returned publication receipts are distinct.

This slice supports the current source split modes only. It does not add duration estimation, automatic chapter inference, saved voice profiles, cross-book loudness normalization, unattended scheduling, a second renderer, or automatic output adoption from matching filenames.

The new source regressions and installed native probe are **NOT_RUN** until the exact integrated head executes its required gates. Synthetic tones exercise process, receipt and ordering behavior without qualifying speech quality. The Windows probe requires the actual existing DesktopFrame, real wx controls and keyboard confirmations, a genuinely installed native SAPI voice, and the selected hash-pinned FFmpeg/FFprobe. It verifies file synthesis, source receipts, explicit resume and unchanged originals. Audible listening, manual keyboard testing, NVDA, physical devices and private-model inference remain separate unqualified checks.
