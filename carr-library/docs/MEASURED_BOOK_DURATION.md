# Measured audiobook parts

Status: source checkpoint; runtime qualification is NOT_RUN. The source and installed regression suites and native Windows review/publish flow are being completed before this feature is claimed qualified.

A completed book item in the current audio batch is the source. `AudioBatchService.review_duration` asks the existing `PublicationService` to verify its typed completed Studio plan, measured timeline, exact master and artifact hashes. It retains an independent PCM16 snapshot and immutable review inside that same batch. No voice is invoked again.

The review binds the original queue plan/item/source/receipt, accepted render identity, completion digest, measured PCM hash/rate/channels/frame count, selected existing output encoder, exact source sample intervals and all output filenames. Decimal seconds are rounded down to complete source frames; the review shows that effective integer interval. The last part holds the exact remaining frames. Names and intervals are recomputed during validation.

`publish_duration` requires the displayed review SHA256, uses the existing resource broker, trust admission, selected FFmpeg runtime and owned native-process settlement, and publishes all verified parts in one no-replace directory. WAV/FLAC/AIFF outputs must preserve exact frame counts. Lossy decoded frame counts are recorded separately from the exact source/PCM intervals; gapless concatenation of lossy files is not claimed. A cancelled attempt can explicitly repeat the same saved review. A completed receipt is byte-verified and reused without native processing. Originals, prior whole/chapter outputs and original batch schema remain unchanged.

Native Studio exposes review, reopening a saved review, publication and explicit export through the same queue-owned worker. Headless actions are `duration-review`, `duration-list`, `duration-show`, `duration-publish` and `duration-export`. Publication requires `--audio-duration-review` and `--audio-duration-approved`; exports use the existing captured absent destination and never replace a file.

Complete-library backup includes typed review metadata, the independent accepted PCM and completed part receipts/bytes through `audio_batch_backup`; it never follows an old external output path or executable. Unknown scratch and unclaimed members cause rejection. The completed batch remains complete when a derived part attempt is cancelled; the saved review remains available for explicit retry.
