# Explicit streamed UTF-8 source import

This extension uses the existing library catalog, `imports.sqlite` checkpoint
journal, resource coordinator, owned parser process, immutable V5 publication
receipt, source reader and complete-backup service. It adds no second import
queue, catalog, executable profile or evidence authority.

## User action and finite capacity

Import Center exposes **Import large UTF-8 text...** and a named numeric source
allowance from 1 through 128 MiB. The action captures the chosen library,
collection and byte allowance before submitting to the existing frame worker.
On interruption, use **Library → Resume interrupted import**. The saved request
keeps its chosen allowance, title, source identity and extraction version.

The CLI is explicit too:

```sh
python -m carr_library import-text --data-dir LIBRARY --workspace ID --file SELECTED.txt --max-mib 96
python -m carr_library import-text --data-dir LIBRARY --workspace ID --resume-request REQUEST_ID
```

The library and workspace must already exist. The command prints a bounded JSON
result and returns nonzero for cancelled, timed-out or failed work. A resumed
request cannot select a new profile or title. The command uses the same native
supervisor as the desktop; it does not parse the source in a second CLI engine.

Ordinary import remains at its existing 64 MiB byte ceiling. This extension
accepts UTF-8 `.txt` only, with an optional leading UTF-8 BOM. It rejects binary
controls, malformed UTF-8, mismatched PDF/RTF/archive/executable headers,
empty/whitespace-only sources, aliased or reparse-point inputs, changed files
and sources above the explicit bound. It does not qualify large PDFs, Office
documents, archives or arbitrary encodings.

The 128 MiB source ceiling is finite and intentional: source text can require
twice its byte size in escaped checkpoint JSON, which must still fit the
existing 512 MiB pending-journal budget. The remaining catalog, total-backup,
disk and operation deadlines continue to apply. Declared resource admission
reserves at least ten times the selected source allowance plus 128 MiB on the
library volume, as well as the ordinary parser scratch allowance. This can
legitimately wait/refuse on a nearly full volume even for a small selected file.
No disk quota or process limit is silently disabled.

## Exact text and quotation meaning

The new configuration is `strict-utf8-stream-v1`. It retains CRLF, combining
characters, supplementary Unicode characters and all original bytes. Form
feeds delimit source fields. Within a field, long text is split at fixed
65,536-Unicode-character boundaries; at most 8,192 fragments are allowed.
Checkpoints retain each field number and exact character offset. Published
locations are **logical segments, unpaginated**. They are not invented physical
or printed page numbers. Historical `strict-utf8-v1` segmentation and evidence
verification remain unchanged.

Reading uses 64 KiB byte blocks and at most one unfinished bounded fragment.
The in-memory index stores only fragment hashes, character counts and offsets.
Every successful scan exhausts the source and verifies its complete original
SHA-256 and native file identity. A late edit outside a selected quote still
invalidates that quote. Checkpoints are durable candidates, not permission to
publish without rechecking the original.

The independent streamed attestor dispatches only on the exact new extraction
configuration. Each new read/copy/evidence action rehashes the complete original.
Within one action, its small hash index can be reused only while native change
time, file identity and link count remain unchanged. This uses Windows change
time rather than treating its creation-time stat field as write freshness.
The cache holds no source text and has finite entry/fragment budgets.

## Resume and backup

The worker request has an explicit version 2 source profile. The frozen launch
identity and immutable database publication receipt bind that profile; changing
it while a child runs cannot approve a different publication. Checkpoints use
the existing schema and are verified one at a time against freshly scanned
original fragment hashes. Failed publication rolls back the current catalog
transaction. A crash after catalog commit is reconciled from that exact sealed
revision, without publishing a second copy.

Complete backups remain the existing library format. Its pending component
uses version 3 only when streamed profiles are present; earlier pending versions
1 and 2 retain their exact contracts. The new component records a typed source
profile inventory, and verification independently derives it from saved jobs
and requests. Merely increasing a manifest allowance cannot authorize a larger
source. Prepared-before-launch and partially extracted inputs are included.

Restore rebinds only retained original locations, discards old native launch
attempt hints and requires a fresh supervised attempt. Imported profiles contain
numeric text limits only. Saved folder scan cursors remain inactive after
relocation, including backups that mix scan pages and streamed text requests.
Read-only verification streams original bytes and creates no authority to scan
old external roots or execute an archived program.

## Evidence and limits

Required portable suites cover incremental decoding and bounds, the actual
existing journal, independent attestation, real owned-child import/cancellation,
CLI and deferred Import Center handlers, and complete backup/restore. Synthetic
files really larger than 64 MiB are generated at test time, not checked in. One
case backs up a prepared large request, deletes its external original, restores
to another root, performs real extraction and exact quotation, then makes and
verifies a completed backup. A separate scan measures bounded Python allocations;
that measurement is not a claim about native-family RSS or a realistic corpus.

`tests/ui/probe_streamed_import_native.py` exercises actual named controls,
focus/Tab, the existing frame worker, durable completion and native close. It
supplies a synthetic path to the real handler and explicitly does **not** claim
OS file-picker or manual NVDA acceptance. Windows execution is a separate gate.
Realistic corpus throughput, multilingual quality and wider-format capacities
require their own declared profiles and measured evidence.

## Open original action

The current Open original action also uses a file-backed path for a sealed streamed
source above 64 MiB. It copies bounded chunks into a private viewing stage, hashes
the complete bytes, and publishes a separate file through the existing no-replace
publisher. The external viewer never receives the retained original inode. An
edited viewing copy is preserved; a later request receives a newly verified copy.
A post-move directory-sync failure is retried before an existing copy is accepted.
Fresh workspace/source authorization and complete copied-byte verification remain
required immediately before an explicit native launch.

The added test `tests/reader/test_streamed_source_view.py` includes one actual
source above 64 MiB and real source-view retry, copy, cancellation and changed-scope
checks. This follow-up was prepared while execution was unavailable; its source
and installed/native gates must run before reporting an executed result.

### Event-thread and shared resource boundary

Large-source viewing reserves one declared CPU slot, 16 MiB of incremental
working memory and, when copying, the full selected source size plus 16 MiB of
disk headroom through the existing ResourceBroker. These are cooperative
admission estimates, not measured physical memory or an operating-system sandbox.
A waiting action remains cancellable and rechecks its exact source after admission.

Copying and full original/view hashes execute on the current desktop worker.
The native viewer call runs on the wx event thread. Both exact payload readers
remain held until that native call settles; a delayed callback repeats fresh
workspace/topic/selection and native file-identity checks without rereading the
large source. Cancelled or timed-out queued callbacks are inert. An already
acknowledged viewer opening is not reported as undone by a later Stop.
The final logical-fragment authorization still checks its bounded stored text;
legacy byte-profile behavior and its earlier per-source bounds remain supported.

The additional source handoff and worker/event-thread policy tests are prepared
in `tests/ui/test_source_launch.py` and `tests/ui/test_source_open_handler.py`.
Their new source and installed/native outcomes remain **NOT_RUN** until CI or a
recovered executor executes this follow-up.
