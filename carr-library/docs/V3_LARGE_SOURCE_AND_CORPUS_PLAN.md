# V3-07 large-source intake and corpus qualification plan

## Status and source authority

This is a design and acceptance map, not a statement that large-source intake
has been implemented or qualified. The inspected source is
`54e63c3dcadaabeb66fb5ec9a1334ecda352b94d`. The governing requirement is
[finalization section 7](UNIVERSAL_SCOPE_V3_FINALIZATION.md): safety limits must
be changed through measured streaming designs, and corpus capacity must include
declared hardware, p50/p95 import/search/open latency, memory/disk, cancellation
and recovery evidence.

Search continuation has a separate implementation chain:
`c91eed24dd83b6ac311838c4309127ad0ba8c2cd` →
`fe634ef5043f0f899f93a9d4097302456db0584f` →
`eac828371c319938ffa9cd90dca38a69eabd2632` →
`976634809b08ae608ce0feb715211dda68ed9470`.
The first backend acceptance executed 18 strict source-page cases and 9 strict
federation cases. The later two active-revision integrity cases, eight isolated
CLI cases, six desktop handler cases and native paging probe still require CI.
Those tests establish bounded behavior on their exact fixtures; they do not
establish a 10,000-book support profile.

The scanner is now integrated through `bffd715db3b316616b4f3a93ee94d1edc2096dd9`
and `5114a7453ec44ffd12737ca09585445c8605a664`. Bounded lexical pages have typed
saved successors, idempotent orphan recovery, explicit Import Center actions,
and backup/restore that retains prepared inputs without reactivating external
traversal authority. The actual 5,003-file fixture and all 42 new cases passed;
the combined installed wheel subsequently passed 404 reader/scanner/master and
backup cases. These are bounded fixture results, not a corpus-capacity claim.
Large-source streaming remains separate active implementation work. See the
[current composition evidence](V3_INTEGRATION_EVIDENCE_2026_11_27.md).

## Why increasing one constant is insufficient

| Existing boundary | Current behavior | Required coordinated change |
|---|---|---|
| [catalog/intake.py](../src/carr_library/catalog/intake.py), `AssetSnapshot`, `snapshot` | A complete selected original becomes an immutable `bytes` value; the public maximum is 64 MiB. Classification also receives the full byte value. | Add an explicit owned-file snapshot type with streamed retention and format-specific bounded classification. Keep the current byte API and its limit compatible. |
| [ingest/retained_assets.py](../src/carr_library/ingest/retained_assets.py), `retain`, `resolve` | Retention accepts complete bytes. Resolve independently verifies regular-file/link policy, size and complete SHA-256. | Add streamed retention and a resolver that checks the approved source-size profile. Preserve whole-file content verification and the existing accounted staging-recovery rules. |
| [ingest/pipeline.py](../src/carr_library/ingest/pipeline.py), `_plan`, `_validated` | TXT is decoded and split as a complete value; PDF uses `BytesIO`. At most 1,000 logical segments/pages are admitted; checkpoint validation materializes records. | Add explicit versioned planning over a retained file and bounded checkpoint iteration. Do not silently change the historical `strict-utf8-v1` interpretation. |
| [ingest/worker.py](../src/carr_library/ingest/worker.py) | Several retained-source/render paths read all bytes. Requests/replies are 64,000 bytes, diagnostics are bounded, and the current source checks assume 64 MiB. The existing owner settles the whole native family before releasing resources. | Pass a private owned-source descriptor, never original arbitrary paths. Keep small requests/replies; store large text pages/checkpoints on disk. Retain the same native process owner, resource admission, deadlines and cancellation. |
| [citations/text_attestation.py](../src/carr_library/citations/text_attestation.py) | Independent strict UTF-8 verification receives complete original bytes and checks all original SHA-256 plus the requested logical segment. The historical configuration is exact. | Dispatch by extraction configuration. Add an independent streaming attestor for the new version while preserving every old version's exact verification rules. |
| [application.py](../src/carr_library/application.py), `_retained_source`, `_asset_bytes`, `_loaders` | Search/action authorization binds the active catalog identity to retained originals; quoting can independently re-read the whole source. | Add a file-backed resolver only for approved streaming adapters. Keep catalog/revision/asset agreement, source-content verification, topic membership and action-specific authorization. |
| [pending_backup.py](../src/carr_library/pending_backup.py) and [library_backup.py](../src/carr_library/library_backup.py) | Typed pending inputs and old import configurations have explicit validation and byte budgets. | Add a versioned typed producer for the new owned source/checkpoint metadata, streaming hash/copy, aggregate budgets and cross-root rebinding. Old external paths remain inert. |

The database ordinal constraint alone is not the intake contract. Removing the
pipeline's page count limit while leaving its record lists, attestors, worker
request budgets, backups or UI unchanged would create a partially supported
source rather than a safe larger document.

## Smallest complete first implementation

Begin with strict UTF-8 TXT only. PDF, ZIP-based publications and native office
converters have independent parse/expansion/resource bounds and must not inherit
a blanket larger-file label.

1. **Capture the selected source once.** Under the existing application writer,
   inspect every trusted-root component and the selected regular file. Stream
   bounded chunks into an owned private staging file while calculating the full
   SHA-256, checking cancellation, permitted size and available disk. Compare
   native file identity and relevant stamps before/after the copy. Parse only
   the retained copy. Reuse existing no-overwrite publication and directory-sync
   recovery; preserve the source unchanged on refusal or interruption.

2. **Represent ownership explicitly.** An internal frozen descriptor should
   carry content digest, media type, byte count, owned relative name, stable
   opened-file identity and the selected resource profile. It is not a serialized
   capability to open a user-supplied absolute path. A restored record must
   rediscover and rehash its owned file in the new library before execution.

3. **Plan a new extraction configuration.** An incremental UTF-8 decoder must
   preserve the current leading-BOM treatment, every original CR/LF and form
   feed, and the rejection of binary control characters. Do not add Unicode or
   newline normalization. Record exact original byte intervals and the
   corresponding decoded text hashes for logical segments. A segment with no
   form feed can itself be huge: either reject it at an explicit per-segment
   budget or introduce a separately reviewed locator version for fragments.
   Never quietly label arbitrary fragments as original pages.

4. **Persist bounded checkpoints.** Write one bounded segment/checkpoint at a
   time into the existing ingestion journal/database. Reuse the immutable
   publication ownership receipt, not a second completion marker. Recovery
   authenticates ordered checkpoint hashes, retained source hash, configuration
   and attempt ownership before resuming. A killed job must not publish a
   partial active revision. Status counts use actual validated segments and
   distinguish unplanned input from a known total.

5. **Verify independently when evidence is used.** The streaming attestor
   must rehash the complete retained original with cancellation at the same
   action boundary where the current code verifies complete source bytes.
   It can then decode just the authenticated bounded interval needed for the
   requested segment, cross-checking the locator/configuration/hash.
   Within one already authorized operation, a checked-source cache may avoid
   duplicate hashing. Modification outside the displayed interval, including
   equal-size/equal-mtime changes, must still cause refusal. A stat-only or
   selected-chunk-only cache is not equivalent to current evidence integrity.

6. **Expose an explicit supported profile.** Keep the old default unchanged
   until the larger profile is measured. A user-selected larger byte/page budget
   must be admitted through the existing resource broker and disk checks. Show
   the real byte/page budget and cancellation state in the current Import
   Center/Tasks UI. Failed admission must leave previous accepted import
   publication and selection state intact.

7. **Round-trip the entire pending job.** Typed backup must stream the retained
   input, its extraction configuration and validated checkpoints; restore into
   a new directory, rebuild internal relative paths, and require review for
   external runtime execution. Backup and restore must cancel before publication
   without replacing an existing archive/destination. Resuming after deletion
   of the originally selected external file is a required acceptance case.

This scope requires engineering and test fixtures, not private models,
credentials, a paid provider or redistribution permission. Those external
inputs are not reasons to leave this code gap unimplemented. Large scanned-PDF
quality and third-party converter qualification remain separate.

## Required first-adapter regression evidence

The first source fixture must actually exceed 64 MiB; a fake catalog size or
repeated cache row does not count. Use deterministic text generated incrementally
in a temporary directory, then retain the original with the production pipeline.
Choose a fixture only slightly above the prior limit and avoid committing large
binary/text assets.

| Case | Required observation |
|---|---|
| More than 64 MiB, Unicode spanning a read boundary, CRLF and leading BOM | Complete successful import, exact saved quote and context after process restart; original bytes unchanged. |
| Source over selected byte/segment budgets | Early bounded refusal, no active revision or misleading complete state, prior data unchanged. |
| Long logical segment without form feeds | Explicit budget refusal or separately qualified locator version; no invented physical page. |
| Late-byte source edit outside the selected quote | Search/copy/export refuses even when selected text and metadata still match. |
| Same-size edit with restored mtime | Complete content hashing still detects the change. |
| Cancel during copy, plan, extraction, verification and backup | Bounded acknowledgement after owned children/files settle; no partial final publication. |
| Kill after staged source, checkpoint and publication-intent boundaries | Reopen authenticates only its own files/attempt; reuse valid work; edited or replaced files require attention. |
| Restore to a different root after removing the external original | Retained pending input is sufficient; no old absolute path is read or executed. |
| Hardlink/reparse/source replacement | Existing containment and no-external-alias policies still reject the mutation. |
| Peak memory measurement | Record the full process family and source size; show the bound does not grow with total original bytes for the streaming phases. |

The exact practical byte/page ceilings must follow measured memory, disk and
latency on the declared hardware. This document deliberately does not invent a
numerical throughput promise.

## Corpus-profile benchmark contract

A capacity report must use actual imported books and retained originals, state
how those books were produced, and separate cold from warm operations. Existing
semantic-cache profiles with 1,000/10,000/50,000 vector records are cache tests;
they are not book counts, source sizes, import throughput or corpus-scale
qualification.

A bounded smoke profile belongs in CI. Larger profiles should be explicitly
invoked and disk-budgeted, not silently built during normal tests.

| Profile dimension | Report fields and control |
|---|---|
| Hardware/runtime | OS and architecture; exact Python/product commit; CPU/RAM; disk type/free space; optional GPU/model/runtime digest. Missing measurements are null with a reason, never a fabricated zero. |
| Corpus | Actual edition/library count, format counts, language mix, retained byte totals, per-book size and segment distribution, scanned versus embedded text, and synthetic versus authorized real samples. |
| Intake | Per-book prepare/worker/commit latency and total throughput with p50/p95; failures/quarantine must remain in the denominator. Direct database fixture seeding is reported as seeding, not ingestion throughput. |
| Search | Literal/word/advanced/thematic/semantic separately; selective, common, absent and high-frequency queries; first page latency, later page latency and full traversal time; scoped and multi-library cases. |
| Read/action | Original open, requested source segment/context, independently verified copy/export and saved-evidence reopen. Report whole-source verification costs. |
| Resources | Parent and complete native-family memory peaks, CPU, disk high-water mark, retained/index/checkpoint growth, and measured GPU memory only when a GPU workflow actually runs. |
| Cancellation | Time from real user/event request to durable paused/cancelled state and full child-family quiescence, during cold/warm search and each intake phase. |
| Recovery | Actual interruption, process restart and cross-root restore; reachable complete output counts, rejected edited files and retained valid checkpoints. |
| Selection integrity | No foreign library/source leakage; exact original hashes and quotations; continuation invalidates after scope/revision changes. |

Use several declared corpus sizes to expose growth, not a single favorable
sample. Include at least one real catalog over 500 editions, one selected
federation over 20 libraries, a source with more than 100 matches and a folder
over 5,000 files once durable scan continuation is integrated. An inexpensive
synthetic text corpus can exercise those cardinalities, but it cannot establish
OCR, complex PDF, dictionary, multilingual neural quality or audiobook capacity.

For statistical reporting, retain each raw timing sample with its operation and
outcome. Declare the percentile method and repetition count. Do not include
warm-up samples in a cold cohort, drop failed operations from the report, mix
different corpus profiles, or relabel a timeout as an empty result. A report
without enough samples still records its measurements, explicitly identifying
the insufficient percentile evidence.

## Remaining search and scanner acceptance

The continuation patch keeps existing bounded one-shot APIs available. New
pages have their own explicit source and library budgets, exact query/scope
binding, stale-cursor refusal and per-window rank wording. Ranking within a
bounded source window is not advertised as globally ranked corpus relevance.

The current workflow candidate adds these strict suites in source, portable
Linux and isolated installed-wheel contexts:

- `tests/search/test_source_pagination.py`
- `tests/search/test_federated_pagination.py`
- `tests/e2e/test_search_pages_cli.py`
- `tests/ui/test_search_paging.py`

`tests/ui/probe_search_pages_native.py` exercises actual worker jobs,
Previous/Next, all 205 retained logical segments, native focus and exact clipboard
copy. It is a native automation gate; manual NVDA acceptance remains distinct.

The integrated scanner now implements typed durable batch succession, bound
root/cursor identities, child-first publication and idempotent orphan recovery.
Backup/rebackup retains all prepared inputs while external traversal authority
stays inert after restore. Its domain, CLI/service and controller boundaries
have executed; native controls and the larger declared corpus profiles still
require their own exact-source results.
