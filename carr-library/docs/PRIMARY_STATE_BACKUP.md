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


## Transcription recovery and optional recognition caches

The single workspace-bound transcription producer preserves runtime declarations,
job/control metadata, retained source audio, accepted receipts, TXT/JSON/SRT/VTT
results, V5 manifests and separate edit history. Source audio is required recovery
data even when generated media are omitted. V5 result metadata may describe a
recognition cache identity; that does not make the result, manifest or receipt an
optional cache.

A default backup omits only the exact job-owned directories
`state/transcription/<workspace-sha256>/jobs/<job-id>/logs/cache` and
`logs/cache_v5`. Their directory safety checks still run before omission;
similarly named, foreign-layout or unrecognized state is not silently excluded.
A current or supported legacy manifest that lists an optional cache file while
`generated_audio_included` is false or absent is rejected before reading that
payload. Accepted inputs, receipts, results, manifests and edits never depend on
this optional flag.

The current flat supplied-worker adapter has no typed admission format for
recognition-cache payloads. Consequently, `include_generated=True` does not grant
permission to copy arbitrary ASR cache JSON or bytes: when such cache files are
present, the API fails with an explicit unsupported-cache message. Use the default
backup to preserve retained inputs and verified results without those optional
caches. The existing typed primary-Studio generated-audio inclusion remains
available. This limitation does not discard accepted transcription output and
does not claim compatibility with another producer's worker-history layout.

Restore validates all recorded relationships before revoking execution trust.
Runtime/model selectors and the typed `job_config.json` remain passive historical
data; configured Python, worker files and model directories are never opened or
copied. Restored controls are paused, profiles lose execution trust and jobs
require a fresh local review before execution. Completed accepted results remain
readable without an engine. Locks, transient stop/pause files, credentials and
external publication folders do not acquire restored execution authority.

The current producer permits at most 64 retained sources per job, 8 GiB per
source and 32 GiB per job. The transcription backup adapter also caps aggregate
retained source media at 32 GiB, with at most 10,000 jobs per library scope and
1,024 passive runtime profiles. Metadata keep their existing per-kind limits
(16 KiB controls, 4 MiB worker configuration/status, 32 MiB job/profile/receipt/edit
records, and up to 512 MiB for a result), together with the shared 512 MiB
metadata and 100,000-sidecar inventory bounds. These are validation ceilings,
not measured throughput or model-quality claims. The complete backup retains its
manifest, database, original-source and optional whole-backup byte limits.
