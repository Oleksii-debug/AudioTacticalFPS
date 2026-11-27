# Measured audiobook parts

Status: authored source, installed and Windows qualification; runtime qualification is NOT_RUN until its exact frozen candidate executes. The focused gate declares 227 cases in each interpreter (143 existing queue cases plus 84 new duration/copy/backup/CLI/UI cases) and 41 native checks (all 26 existing queue checks plus 15 measured-duration checks).

A completed book item in the current audio batch is the source. `AudioBatchService.review_duration` asks the existing `PublicationService` to verify its typed completed Studio plan, measured timeline, exact master and artifact hashes. It retains an independent PCM16 snapshot and immutable review inside that same batch. No voice is invoked again.

The review binds the original queue plan/item/source/receipt, accepted render identity, completion digest, measured PCM hash/rate/channels/frame count, selected existing output encoder, exact source sample intervals and all output filenames. Decimal seconds are rounded down to complete source frames; the review shows that effective integer interval. The last part holds the exact remaining frames. Names and intervals are recomputed during validation.

`publish_duration` requires the displayed review SHA256, uses the existing resource broker, trust admission, selected FFmpeg runtime and owned native-process settlement, and publishes all verified parts in one no-replace directory. WAV/FLAC/AIFF outputs must preserve exact frame counts. Lossy decoded frame counts are recorded separately from the exact source/PCM intervals; gapless concatenation of lossy files is not claimed. A cancelled attempt can explicitly repeat the same saved review. A completed receipt is byte-verified and reused without native processing. Originals, prior whole/chapter outputs and original batch schema remain unchanged.

Native Studio exposes review, reopening a saved review, publication and explicit export through the same queue-owned worker. Headless actions are `duration-review`, `duration-list`, `duration-show`, `duration-publish` and `duration-export`. Publication requires `--audio-duration-review` and `--audio-duration-approved`; exports use the existing captured absent destination and never replace a file.

Complete-library backup includes typed review metadata, the independent accepted PCM and completed part receipts/bytes through `audio_batch_backup`; it never follows an old external output path or executable. Unknown scratch and unclaimed members cause rejection. The completed batch remains complete when a derived part attempt is cancelled; the saved review remains available for explicit retry.

## Qualification and receipt-bound exports

The existing isolated source/installed audio acceptance engine, complete tracked-source inventory, installed-wheel byte map, integrated 56-pin runtime and pinned FFmpeg are reused. A new standalone workflow runs the exact 227-case Counters and 41-label native Counter, preserves raw JUnit, process logs and receipts on every outcome, and rejects skips/timeouts/missing phases. The native flow creates a synthetic completed audiobook, reviews its measured master, invalidates a changed duration approval, publishes actual WAV parts, compares their concatenated PCM with the accepted source, proves no resynthesis and replays exact completed bytes. Physical audio and NVDA are separate checks.

`audio.jobs.publish_new` now accepts an optional expected SHA256. It hashes the bytes actually copied and rejects a mismatch before any destination publication. Both current queue export and duration export pass their validated receipt hash; the existing callers without that option retain their API. Regressions reproduce a changed source between receipt verification and later identity capture and require that no destination is ever published.

Example headless sequence (replace IDs and paths with the returned exact values):

```text
python -m carr_library --data-dir LIBRARY --audio-batch BATCH --audio-batch-item ITEM --audio-batch-action duration-review --audio-duration-seconds 1800 --audio-duration-format mp3
python -m carr_library --data-dir LIBRARY --audio-batch BATCH --audio-batch-item ITEM --audio-batch-action duration-publish --audio-duration-review REVIEW --audio-duration-approved SHA256 --ffmpeg SELECTED_FFMPEG
python -m carr_library --data-dir LIBRARY --audio-batch BATCH --audio-batch-item ITEM --audio-batch-action duration-show --audio-duration-review REVIEW
```
