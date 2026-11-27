# Primary desktop state in complete backups

The common version-4 backup preserves the primary reader recovery documents,
provider profiles, provider usage reservations, Studio projects and revisions,
Studio job and control checkpoints, assistant plans and checkpoints, local
engine declarations, and progress notification preferences. Each registered
format has a bounded validator. Unknown state fails with its filename; it is
not silently dropped or treated as arbitrary JSON.

Reader recovery includes its edit history and original-source digest. This is
an editable reading copy and does not grant verified quotation status. Provider
profiles contain only opaque Credential Manager references; credential values
remain in the Windows vault. Backup does not open the vault, activate local
engines, contact remote services, or resolve stored external paths.

Studio job metadata and generated UTF-8 transcripts are preserved. The default
backup omits reproducible audio caches and sets `generated_audio_included` to
false. Explicit `include_generated=True` also retains completed primary-Studio
cache media and their exact producer receipts. Only registered cache filenames,
receipt schemas, media-container signatures and matching SHA-256 values are
accepted. Unreceipted or damaged files fail visibly. Transient locks and pending
writes are excluded. No decoder or voice engine runs during this operation.

Generated media use a separate 32 GiB budget and stream in 1 MiB blocks; metadata
retain their 512 MiB aggregate budget and format-specific limits. An explicit
whole-backup byte budget includes both. All source bytes are rechecked after
copying, including same-size changes concealed by coarse filesystem timestamps.
Container-signature validation is not full codec decoding or audio-quality
qualification. External published output folders remain outside the private
library backup. Keep them with project files; explicit Studio resume verifies
their actual bytes and recomputes missing cache nodes through installed engines.

Assistant checkpoints retain their exact original data directory and approved
paths. Restoring to another location does not rewrite those permissions or
authorize execution. Review and resubmit a plan when its original scope is no
longer available. Uncertain provider charge reservations remain recorded and
are not turned into permission for automatic retry.

Known transient producer locks and incomplete temporary writes are not backup
records. Unknown private state, damaged digests, changed project identity,
unrecognized tools, invalid control actions and mismatched library identities
are rejected before a completed restore destination is published.

The primary-producer tests use real saved model objects and actual SQLite
backup/restore, with engine and credential access forbidden during validation.
The additional local acceptance rendered installed FFmpeg Flite speech,
backed up the completed Studio checkpoint and all six metadata sidecars,
restored unchanged bytes, and verified all six original output artifacts on
reopen. This is Linux execution, not native Windows or manual NVDA acceptance.

The generated-cache acceptance also preserved the completed Flite fixture's
five PCM files and five receipts alongside its six metadata sidecars (16 entries,
1,474,004 bytes), verified the backup and restored every digest unchanged with
process launch forbidden. Four strict suites passed 104 tests without skips.
Native Windows and manual NVDA acceptance remain separate gates.

Translation checkpoints are also registered: exact original text and chunk
boundaries, accepted translation receipts, their approved request scope and
cost limits, provider-declared language inventories and provider usage records.
The validators preserve paused work, old approvals and recorded charge states
without opening a gateway, credential store or connection. They do not renew
expired approvals or grant execution permission after restore. Current provider
profile fingerprints and Studio music-continuity notices are retained alongside
compatible older checkpoint formats. Ten translation-backup regressions and the
35 canonical synthetic transport/authorization tests passed without skips.


## Transcription recovery and explicit exclusions

The typed ASR producer retains runtime declarations (not the runtimes),
job/control metadata, retained source audio, accepted transcript receipts,
TXT/JSON/SRT/VTT exports and separate edit history. Required source audio is
included even when generated media are omitted. Original external paths are
inert provenance. The ordinary backup does not copy external Python/worker
executables, model weights, credential values, external publication folders
or operation locks. External copies and licensed model packs require their
own authorized recovery procedure.

The existing `generated_audio_included` flag also governs optional ASR
recognition-cache files under current and archived worker workspaces. The
manifest validator rejects a cache entry that contradicts a false (or absent)
flag, including legacy state-list shapes. Accepted receipts, retained inputs,
edits and inert non-cache worker results do not require that flag. Restored
runtime declarations lose execution trust and unfinished jobs require review;
absolute worker launch configuration and transient control flags are removed
during staging. Completed accepted results remain readable without an engine.

ASR inputs are streamed separately from the metadata budget: 32 GiB per
source, 64 GiB aggregate, at most 512 sources per job, 4096 jobs, 1024 runtime
profiles and 1024 worker-history directories per job. ASR metadata/cache
files are limited to 32 MiB each and count against the shared 512 MiB metadata
budget; parsed metadata may consume more memory than its serialized size.
The common 100,000-sidecar inventory, 8 MiB manifest, database/originals and
optional whole-backup limits still apply. These are safety limits, not
measured throughput or supported maximum corpus claims. Backup is a stable
private/cooperative-writer snapshot; if state changes, pause its producer
and retry. Restore always requires a fresh destination and an independently
retained manifest digest. This does not establish encrypted backups,
incremental/multi-device migration or recovery of external credentials,
engines or published files.

## Cancellation and pending-import validation boundaries

The same cancellation callback is checked during current and legacy manifest
normalization, pending file/source binding validation, journal inventory and
record/checkpoint validation, and immediately before restore publication.
Malformed non-string membership fields in pending manifests are rejected as
validation errors before dictionary lookup. This does not change the format,
retained source identity, allowed filenames or existing aggregate budgets.
Pending capture scans committed and known transient names incrementally;
restore copies retained journals by a single pass through the validated file
inventory. The existing aggregate file/byte limits still apply. JSON decoding,
individual SQLite queries, bounded source reads and synchronous filesystem
calls are not preempted; cancellation is cooperative at the next boundary.
These are correctness and algorithmic bounds, not a measured Windows latency
or complete crash-recovery qualification.
