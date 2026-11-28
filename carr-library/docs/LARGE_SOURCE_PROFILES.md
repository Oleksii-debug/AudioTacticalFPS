# Explicit streamed UTF-8 originals

Resonance can import one selected UTF-8 TXT original through an explicit finite source profile. This reuses the current import request, owned Python worker, extraction journal, immutable revision publisher, source attestation and resource broker. It does not create a second catalog or import pipeline.

## Choosing the profile

In **Library → Import Center**, the separate **Import large UTF-8 text...** action keeps its default streamed TXT control of 1–128 MiB. For larger or differently bounded work, first choose **Review import resource allowances for new plans** and explicitly accept a typed policy. That reviewed policy takes precedence for the new TXT action and new scan pages. The chosen collection, profile and OCR configuration are captured before the existing background job starts. Reviewing a folder saves a page; it does not start extraction. Without an explicit resource policy, ordinary imports keep their existing 64 MiB limit and format rules.

The installed command has the same explicit boundary:

~~~text
python -I -B -m carr_library import-text --data-dir PRIVATE_LIBRARY --workspace WORKSPACE_ID --file ORIGINAL.txt --max-mib 128
python -I -B -m carr_library import-text --data-dir PRIVATE_LIBRARY --workspace WORKSPACE_ID --resume-request REQUEST_ID
~~~

The CLI requires an existing library. A resumed request keeps its original title, source identity and allowance. It cannot receive a replacement profile. The selected source can be removed after its original has been retained; resume then uses the hash-bound copy in that same library.

For a new source, `--budget-file REVIEWED.json` accepts one bounded, strict
`carr-import-budget/1` document instead of `--max-mib`. The profile is saved in
the request; resume uses that saved value and does not reread the original policy
file. Duplicate fields, nonfinite values, unknown fields and inconsistent resource
allowances are refused. The CLI and native dialogs expose the same typed policy.

## Exact text and finite limits

| Boundary | Meaning |
| --- | --- |
| Ordinary source intake | Existing 64 MiB per-original limit remains. |
| Legacy explicit streamed TXT | 1–128 MiB in source profile schema 1; its existing bytes and identity remain unchanged. |
| Reviewed capacity policy | Finite original bytes, record bytes, fragment count, steps, native time, memory, library growth and temporary space in source profile schema 2. The containing import request remains version 2. |
| Extraction identity | strict-utf8-stream-v1; old strict-utf8-v1 revisions are unchanged. |
| Incremental read | 64 KiB decoder blocks with strict UTF-8 validation. |
| Logical fragment | At most 65,536 Unicode characters. Schema 1 keeps 8,192 fragments; schema 2 uses the explicitly chosen fragment and per-record byte allowances. |
| Source representation | UTF-8 BOM is handled explicitly; source CRLF and form-feed boundaries are retained. |
| Provenance | Each fragment carries its original field and character offset; complete original SHA256 is checked. |
| Worker | Existing isolated interpreter. Schema 1 keeps its 0.1–600 second allowance and 180-second CLI/native default. Schema 2 uses its finite selected native allowance, at most 86,400 seconds; an explicit shorter run cannot exceed the saved policy. |
| Cooperative admission | Schema 1 keeps one CPU slot, 512 MiB declared memory, and 10 × selected maximum bytes + 128 MiB of library disk reservation. Schema 2 reserves one CPU slot, its declared memory, library growth plus one-write headroom, and its temporary-volume allowance through the same broker. |
| Backup | Existing complete format 5 and catalog schema 6. Profile-bearing pending imports use the bounded version 3 component envelope. |

Logical fragments are unpaginated source locations. They do not establish printed page numbers, PDF pagination or a reviewed visual layout. Binary controls, invalid UTF-8 and files with incompatible executable, PDF, RTF or archive headers are rejected. A larger explicit byte allowance applies only to this strict TXT adapter. Neither an import budget nor a backup manifest authorizes a larger PDF or another whole-buffer format. A numerical allowance is not evidence that a multi-gigabyte source or a million-fragment corpus has met performance acceptance.

The resource declaration is cooperative admission, not a measurement of peak RSS or an operating-system memory sandbox. Processing still checks cancellation, the exact original byte hash, native file identity and the saved request/attempt. A request or profile changed after admission cannot publish catalog rows.

## Completion, interruption and backup

The original is copied in bounded chunks into the existing retained-original store. Publication is create-only. Cleanup may release only this attempt's exact staging link; replaced targets or unaccounted extra links remain intact and are rejected. A late cancellation after a proven original-file commit does not delete that committed source.

Fragments enter the existing checkpoint journal one at a time. Checkpoints are candidates until the original is fully revalidated and the existing catalog transaction publishes the complete immutable revision. An interrupted import can keep its checked fragments without exposing a partial revision. Resume rescans the retained original and compares each surviving checkpoint, including its raw text, digest and field/offset provenance.

Complete backup includes both registered originals and the original of a prepared or interrupted streamed request. Pending profile declarations must match the actual typed requests and journal configurations. Restore reads only the copied originals; saved external paths are not opened. It preserves the source profile and checked content while discarding old worker attempts, publication candidates and execution acknowledgements. A later resume gets a fresh current attempt. A restored completed result remains historical data and must pass the existing sealed-revision and original-byte verification.

For schema 2, checkpoint and publication steps are explicitly bounded. Reaching
the selected step count preserves resumable candidates; a later explicit run uses
the same request and revision identity. Measured growth includes the current
SQLite databases, WAL/journal sidecars, unflushed allocated pages and retained
originals. The last receipt write is checked while the catalog transaction can
still roll back. Existing source, checkpoint, seal and publication verification
remains mandatory even when the selected allowance is larger.

Folder scans use separate bounded durable pages; see [Import Center](IMPORT_CENTER.md).
Restoring those pages marks their external traversal cursors as needing fresh
selection. Already pinned rows can still resume from the restored originals.

## Open original

The current **Open original** action creates a separate verified viewing copy. It never gives an external editor the retained original's inode. Edited viewing copies are preserved and a new copy is prepared. Large source verification and copying stay on the existing worker; only the final fresh selection check and native opener run on the event thread. The worker holds the verified file readers until the queued native action actually finishes. Cancellation or an expired queued action cannot launch later.

## Qualification

This source composition is **NOT_RUN** until the corresponding source, installed and native gates complete on its exact commit.

Prepared capacity controls include an actual source above 128 MiB, 5,001 real selected files continued through bounded pages, an actual 8,193-fragment journal, strict incremental decoding, real owned-child import/cancellation/resume, independent source quotations, source/profile mutation during real catalog staging, final pre-COMMIT resource refusal, retained request-lock/broker/stage ownership, complete backup/restore/rebackup, CLI continuation and typed native-control contracts. These remain planned checks until executed on the composed receiving head; earlier successful profiles and hosts do not qualify the new policy.

The native Import Center probe exercises real named controls, focus/Tab navigation, captured profile, the current background worker and actual import child, source bytes, receipt and verified quotation. Its small synthetic fixture qualifies that UI/worker integration. The separate greater-than-64-MiB tests qualify capacity. Neither probe claims an operating-system file-picker result or manual NVDA acceptance.
