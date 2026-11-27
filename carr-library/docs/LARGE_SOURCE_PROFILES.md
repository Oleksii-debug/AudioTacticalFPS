# Explicit streamed UTF-8 originals

Resonance can import one selected UTF-8 TXT original through an explicit finite source profile. This reuses the current import request, owned Python worker, extraction journal, immutable revision publisher, source attestation and resource broker. It does not create a second catalog or import pipeline.

## Choosing the profile

In **Library → Import Center**, choose **Import large UTF-8 text...** after setting **Streamed UTF-8 source limit (MiB)**. The native limit control accepts 1–128 MiB. The chosen collection, source profile and OCR configuration are captured before the existing background job starts. This action accepts TXT only; it never starts automatically when a folder is scanned. Ordinary file and folder imports keep their existing 64 MiB limit and format rules.

The installed command has the same explicit boundary:

~~~text
python -I -B -m carr_library import-text --data-dir PRIVATE_LIBRARY --workspace WORKSPACE_ID --file ORIGINAL.txt --max-mib 128
python -I -B -m carr_library import-text --data-dir PRIVATE_LIBRARY --workspace WORKSPACE_ID --resume-request REQUEST_ID
~~~

The CLI requires an existing library. A resumed request keeps its original title, source identity and allowance. It cannot receive a replacement profile. The selected source can be removed after its original has been retained; resume then uses the hash-bound copy in that same library.

## Exact text and finite limits

| Boundary | Meaning |
| --- | --- |
| Ordinary source intake | Existing 64 MiB per-original limit remains. |
| Explicit streamed TXT | 1–128 MiB, chosen before execution and persisted in request version 2. |
| Extraction identity | strict-utf8-stream-v1; old strict-utf8-v1 revisions are unchanged. |
| Incremental read | 64 KiB decoder blocks with strict UTF-8 validation. |
| Logical fragment | At most 65,536 Unicode characters; at most 8,192 fragments per original. |
| Source representation | UTF-8 BOM is handled explicitly; source CRLF and form-feed boundaries are retained. |
| Provenance | Each fragment carries its original field and character offset; complete original SHA256 is checked. |
| Worker | Existing isolated interpreter and 0.1–600 second worker deadline; the CLI and native action default to 180 seconds. |
| Cooperative admission | One CPU slot, 512 MiB declared incremental memory, and 10 × selected maximum bytes + 128 MiB on the existing library volume. |
| Backup | Existing complete format 5 and catalog schema 6. Profile-bearing pending imports use the bounded version 3 component envelope. |

Logical fragments are unpaginated source locations. They do not establish printed page numbers, PDF pagination or a reviewed visual layout. Binary controls, invalid UTF-8 and files with incompatible executable, PDF, RTF or archive headers are rejected. Neither this profile nor a backup can authorize a larger PDF or the donor's later multi-gigabyte capacity profile.

The resource declaration is cooperative admission, not a measurement of peak RSS or an operating-system memory sandbox. Processing still checks cancellation, the exact original byte hash, native file identity and the saved request/attempt. A request or profile changed after admission cannot publish catalog rows.

## Completion, interruption and backup

The original is copied in bounded chunks into the existing retained-original store. Publication is create-only. Cleanup may release only this attempt's exact staging link; replaced targets or unaccounted extra links remain intact and are rejected. A late cancellation after a proven original-file commit does not delete that committed source.

Fragments enter the existing checkpoint journal one at a time. Checkpoints are candidates until the original is fully revalidated and the existing catalog transaction publishes the complete immutable revision. An interrupted import can keep its checked fragments without exposing a partial revision. Resume rescans the retained original and compares each surviving checkpoint, including its raw text, digest and field/offset provenance.

Complete backup includes both registered originals and the original of a prepared or interrupted streamed request. Pending profile declarations must match the actual typed requests and journal configurations. Restore reads only the copied originals; saved external paths are not opened. It preserves the source profile and checked content while discarding old worker attempts, publication candidates and execution acknowledgements. A later resume gets a fresh current attempt. A restored completed result remains historical data and must pass the existing sealed-revision and original-byte verification.

## Open original

The current **Open original** action creates a separate verified viewing copy. It never gives an external editor the retained original's inode. Edited viewing copies are preserved and a new copy is prepared. Large source verification and copying stay on the existing worker; only the final fresh selection check and native opener run on the event thread. The worker holds the verified file readers until the queued native action actually finishes. Cancellation or an expired queued action cannot launch later.

## Qualification

This source composition is **NOT_RUN** until the corresponding source, installed and native gates complete on its exact commit.

Prepared automated controls include actual files larger than 64 MiB, strict incremental decoding, bounded Python allocation measurement of the scanner, real owned-child import/cancellation/resume, independent source quotations, source/profile mutation during real catalog staging, exact retained-link fault controls, complete backup/restore/rebackup, real worker-to-event-thread handoff, and legacy source-view regressions.

The native Import Center probe exercises real named controls, focus/Tab navigation, captured profile, the current background worker and actual import child, source bytes, receipt and verified quotation. Its small synthetic fixture qualifies that UI/worker integration. The separate greater-than-64-MiB tests qualify capacity. Neither probe claims an operating-system file-picker result or manual NVDA acceptance.
