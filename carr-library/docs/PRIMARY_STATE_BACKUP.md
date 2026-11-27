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

Named assistant instruction templates are an optional exact producer at
`state/assistant/templates.json`. Backup retains bounded names and instruction
text only, using the same passive validator as the template store. It never
turns a template into a job or imports execution authority. After restore,
loading a template still clears the old plan/job selection and requires a new
Preview and Run under the current scope. Historical backups without templates
remain supported without a manifest-format change.
