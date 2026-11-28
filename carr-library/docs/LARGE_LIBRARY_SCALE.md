# Large-library scale: actual corpus acceptance

## Evidence boundary

V3-07 in `UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md` requires actual library, import and retrieval evidence. Generating files, passing a small contract suite, or measuring only a semantic cache does not establish a supported library capacity.

The import and retrieval benchmark scripts use the existing product routes. `scripts/benchmark_library_scale.py` writes and independently verifies real synthetic originals. `scripts/benchmark_library_catalog.py` uses `LibraryApp.plan_import_page`, `continue_import_scan`, `run_import_batch` and `search_page`. It never seeds catalog, FTS, revision, review, checkpoint or receipt rows directly. Its worker measurement delegates the existing owned process runner and observes the actual worker exit.

The default import limits remain unchanged. Large sources use an explicit, frozen `ImportBudget`; its allowance does not grant source authority. Original identity, whole-source digest, sealed segments, publication attempts and source-backed quotations retain their current checks.

### Executed predecessor results, 28 November 2026

These are exact-run observations. No row below is a completed 12-tier acceptance.

| Exact run | Executed result | Remaining boundary |
| --- | --- | --- |
| [Foundation fixture contracts](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46077545623), head `993e` | Linux 26 source + 26 installed cases; Windows 24 + 24. Includes actual 65 MiB generation and descriptor-substitution controls. | Fixture correctness only. No populated-library throughput claim. |
| [Public catalog contracts](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46085884711), head `9dc9623e8bb77db319cc03a88bfe2509e3abad0b` | Linux 615 source + 615 installed cases. Windows 611 source cases in 771.39 seconds and 463 observed installed passes before the 25-minute job allowance expired. | Windows installed acceptance is incomplete. The reviewed next aggregate allowance is 35 minutes; all case bounds remain unchanged. |
| [Actual corpus predecessor](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46086678855), head `a758f0c2c8fd92d4d3cc8856edadd5338b44801c` | Linux 1,000 books / 40,000 segments completed public import, genuine post-commit interruption, durable resume, all-source catalog verification and retrieval. | The final identity harness failed because JSON lists were compared with Python tuples. The exact path/hash comparison is preserved by the reviewed list correction. This run is not labelled PASS. |
| Same corpus predecessor, Linux 65 MiB TXT | Actual 68,157,613-byte source, 17 segments: public import, interruption/resume and all-source catalog verification passed. | The last-segment Unicode oracle inspected only the first bounded source window. The reviewed adapter now follows issued continuations and still requires exactly one exact quote. |
| Same corpus predecessor, Linux 129 MiB TXT | Actual 135,266,477-byte source, 33 segments: public import, interruption/resume and catalog verification passed. | Retrieval was incomplete when the failed run was normally superseded by the diagnostic. |
| Same corpus predecessor, Linux 10,000 and 50,000 books | Actual source generation, public admission and the first committed publication followed by exit 73 passed. | Resume was incomplete at supersession. Full catalog verification and retrieval were not run. |
| Same corpus predecessor, Linux 2 GiB and all Windows tiers | No product phase executed before supersession. | NOT_RUN. Queued work is not evidence. |

The exact predecessor evidence is retained in Git blob `48150e415b63c03e61ba66d58245b85df1d62c63`, including job IDs, phase states, raw log blobs and artifact identities.

The first 1,000-book run spent approximately 381.826 seconds between starting resume of the remaining 999 books and starting the catalog-verification step. This includes the step transition; it is not a final API profile. A simple linear extrapolation would exceed the current 95-minute resume phase at 50,000 books. That observation triggered an actual parent/worker profile rather than an unsupported throughput claim.

The [separate import-cost diagnostic](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46088811012), head `d68c6b6a8709e7b7cae9632490fcfdfead455585`, completed successfully on Linux with one fresh 1,000 × 40 corpus in four bounded 250-row slices. All original/catalog checks and the 393-file installed-package, 36-module-origin and executed-script proofs passed. Product bytes, source checks, publication checks, resource accounting and process ownership were unchanged. `cProfile` overhead is included. This is DIAGNOSTIC_ONLY; it does not replace any of the twelve required tiers.

| Actual diagnostic block | Worker wall time | `ImportSession.check` cumulative time | Full `Storage._check` cumulative time |
| --- | ---: | ---: | ---: |
| Books 1–250 | 123.588 s | 76.793 s / 52,002 calls | 11.894 s / 253 calls |
| Books 251–500 | 141.897 s | 76.685 s / 52,002 calls | 37.025 s / 253 calls |
| Books 501–750 | 167.437 s | 77.036 s / 52,002 calls | 62.557 s / 253 calls |
| Books 751–1,000 | 195.601 s | 76.354 s / 52,002 calls | 88.137 s / 253 calls |

Total profiled parent wall time was 650.723 seconds, including 628.960 seconds inside the existing worker invocations. The function columns are cumulative; nested values must not be summed as independent costs. Targeted revision validation took approximately 0.082 seconds for each 250-book block, and the actual query plans used the existing revision and integer-primary-key indexes. The measured growing full-check fallback and repeated path accounting are the optimization targets. Validation of unknown writes and every actual per-check cancellation, source identity, database allocation and available-space observation remain required.

The same diagnostic read the prior 1,000-book artifact only after verifying its fixed artifact ID, byte size and SHA-256. Its uninstrumented reports contain these component observations:

| Prior actual component | Measurement |
| --- | --- |
| Public source planning | 1.497199 s |
| First commit plus owned closure and authored interruption | 0.422108 s |
| Resume of the remaining 999 originals | 381.617112 s |
| Independent full catalog verification | 1.498569 s |
| Complete retrieval sequence | 99.325628 s |
| Literal result pages | 400 calls; P50 0.208641 s; P95 0.211594 s |
| Exact-title anchor queries | 50 calls; P50 0.007992 s; P95 0.008333 s |
| First complete-scope thematic call | 7.000122 s; 40,000 indexed rows |
| Same-source thematic reopen | 6.721593 s; zero updated rows |
| Scoped index size | 28,667,904 bytes |
| Maximum observed individual import worker peak | 48,615,424 bytes; simultaneous family peak not measured |

These are observations from the predecessor with the separate failed final identity oracle, not a passing capacity claim. Diagnostic evidence, selected function records, query plans, source identities and prior-artifact measurements are retained in Git blob `0cb886f419fab353d25bf7ba8a38adeee7253709`.

### Measured correction component, historical checkpoint 01:33 UTC

The [correction component](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46092363367), exact head `5c35cb0fa139e8009452a9b24dcbb00f2203ab30`, executed the same actual 1,000-book / 40,000-segment route after the receipt-read and repeated-path accounting corrections. The Linux job completed successfully, including the final exact checked-out commit, unchanged whole-package/script inventories, independent originals, durable restart and all search oracles.

| Linux component measurement | Actual result |
| --- | --- |
| Public source planning | 1.557163 s |
| First commit, owned closure and exit 73 | 0.313156 s |
| Remaining 999 sources | 102.876920 s |
| Total measured import calls | 103.190076 s |
| Full catalog verification | 1.364407 s |
| Complete retrieval sequence | 91.084270 s |
| Literal result pages | 400 calls; P50 0.194789 s; P95 0.197675 s |
| Exact-title anchor queries | 50 calls; P50 0.006307 s; P95 0.006693 s |
| First complete-scope thematic call / same-source reopen | 5.682827 s / 5.567812 s; zero updated rows on reopen |
| Maximum observed individual import worker peak | 48,500,736 bytes; simultaneous family peak not measured |

The same-head Windows job failed during resume when its real worker removed a private scratch directory while the parent enumerated it; Windows returned `PermissionError [WinError 5]`. Generation, admission and the genuine first committed interruption passed. The failed approximately 269-second resume is **not** a completed throughput measurement; catalog and search did not execute.

The same-head [source/installed contract gate](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46092363225) reported 628 passes and six failures in each Linux context. Those six cases reached a legitimate POSIX directory-link-count change and were refused as root replacement before their intended oracle. At the recorded 01:33 UTC checkpoint, the Windows source step had passed and its installed step was still running. Subsequent results belong to their exact recorded runs; this paragraph is a historical observation, not a live status banner.

The independently reviewed follow-ups distinguish directory identity from its mutable count of child-directory links, while preserving regular-file single-link checks. A Windows scratch read error may be treated as disappearance only when a fresh same-path metadata call independently proves the path absent; a present, replaced, denied or unknown object retains the original failure. The corresponding regression and performance runs remain separate evidence from this earlier successful Linux component. Detailed results and original logs are retained in Git blob `c6afd0273264ba80a3bd5e8dc1e1816e066e814e`.

These observations justify measuring scheduling allowances, not extrapolating a passing 50,000-book result. For example, 20,000 literal pages at the observed Linux P95 would already take approximately 3,954 seconds before complete-scope ranking. The final workflow must accommodate the actual larger traversal without removing pages or extending individual source, query, child-process or cancellation limits.

The maintainable final workflow is `.github/workflows/carr-library-scale.yml`. It runs all twelve tiers for relevant pull requests or an explicit workflow dispatch, binding its report to the checked-out commit. Only a completed run on the coherent final candidate can establish final corpus acceptance. The exact PR-head checks and their bound artifacts are the final verdict authority; these dated component observations do not predeclare or override that verdict.

## Actual corpus tiers

Every corpus book contains 40 original form-feed-delimited segments. Each segment has an independent book/segment token, Ukrainian and Slovak text, a combining accent, exact CRLF bytes and repeated search terms. The first original includes a real UTF-8 BOM. No source is sparse, truncated to a nominal length or represented by a fabricated manifest size.

| Books | Original segments | Actual original bytes |
| ---: | ---: | ---: |
| 1,000 | 40,000 | 10,279,003 |
| 10,000 | 400,000 | 102,790,003 |
| 50,000 | 2,000,000 | 513,950,003 |

The 50,000-book corpus contains approximately 490 MiB of originals. Large-single-source acceptance is separate:

| Strict TXT tier | Actual source bytes | Original segments |
| --- | ---: | ---: |
| Above 64 MiB | 68,157,613 | 17 |
| Above 128 MiB | 135,266,477 | 33 |
| 2 GiB | 2,147,483,648 | 1,000 |

Both operating systems execute all six tiers, each in a fresh profile. A successful TXT tier does not qualify multi-gigabyte PDF, Office, EPUB or other parser routes.

The fixture manifest records original SHA-256 and size, segment SHA-256 and character/byte count, original offsets and each independent expected token. Its parser reads the same safely opened regular descriptor whose accumulated bytes must match the manifest digest. Cancellation remains live during these full passes. The initial BOM consumes three original bytes in the first record's admission allowance; decoding removes only that initial BOM from the selected text.

## Reproducible commands

Run from `carr-library` with the selected application installed. Each fixture destination must be a new owned directory. On Windows use the corresponding explicit local path.

```console
python -I -B -X utf8 scripts/benchmark_library_scale.py --output-dir /owned/scale-1000 --books 1000 --segments 40
python -I -B -X utf8 scripts/benchmark_library_scale.py --output-dir /owned/scale-10000 --books 10000 --segments 40
python -I -B -X utf8 scripts/benchmark_library_scale.py --output-dir /owned/scale-50000 --books 50000 --segments 40
python -I -B -X utf8 scripts/benchmark_library_scale.py --output-dir /owned/scale-txt-65m --large-bytes 68157613 --segments 17
python -I -B -X utf8 scripts/benchmark_library_scale.py --output-dir /owned/scale-txt-129m --large-bytes 135266477 --segments 33
python -I -B -X utf8 scripts/benchmark_library_scale.py --output-dir /owned/scale-txt-2g --large-bytes 2147483648 --segments 1000
```

Generation returns `FIXTURE_GENERATED_NOT_BENCHMARKED`. Run the real application route separately for each selected fixture:

```console
python -I -B -X utf8 scripts/benchmark_library_catalog.py init /owned/scale-1000 --memory-mib 1024 --search-disk-mib 4096 --reserve-mib 1024 --page-files 1000 --slice-items 1000 --slice-seconds 600 --steps 1001 --foreign-libraries 21
python -I -B -X utf8 scripts/benchmark_library_catalog.py import /owned/scale-1000 --seconds 1800 --crash-after-first-commit
python -I -B -X utf8 scripts/benchmark_library_catalog.py import /owned/scale-1000 --seconds 5400
python -I -B -X utf8 scripts/benchmark_library_catalog.py verify /owned/scale-1000 --seconds 1800
python -I -B -X utf8 scripts/benchmark_library_catalog.py search /owned/scale-1000 --seconds 3600 --query-seconds 1800 --query-samples 50
```

The first import command must exit with code **73** after one real publication, actual worker-family closure and the durable batch save. Run the next command as a new process. The resume reuses the original request and edition identities; a duplicate first publication is an acceptance failure.

Application progress can emit multiple JSON objects. Capture application stdout as JSON Lines, validate every nonempty line as a bounded JSON object, and use the final object for the phase outcome. Saved `state.json`, `catalog-verification.json` and `search-measurements.json` are ordinary single JSON documents. A progress line is not a terminal success result.

For the 2 GiB tier the workflow selects an 8 GiB search-disk allowance; the other tiers select 4 GiB. Fixture generation and application admission both check actual capacity. Raising an allowance after creating a profile does not silently change its frozen import policy.

An insufficient measured resource allowance yields `NOT_RUN` with a nonzero exit before the admitted operation. An incomplete or failed profile preserves its actual state for diagnosis. Neither outcome is a passing tier.

## Resource accounting

Before writing sources, record actual free disk and available memory. Linux observes `MemAvailable` and the unified cgroup limit when present; Windows observes `GlobalMemoryStatusEx`. Missing memory observations are not treated as unlimited capacity.

Admission estimates account for the external original, retained copy, fixture manifest, catalog/index and journal growth, bounded search spool and free-space reserve. Estimates are labelled as estimates. The actual phase reports then measure:

- External-original and retained-original tree bytes.
- Whole application-profile bytes.
- `library.sqlite` and `imports.sqlite`, with their current WAL, SHM and journal sidecars.
- Benchmark evidence and per-search index bytes.
- Coordinator own-process peak and actual isolated worker own-process peak.

The whole application profile includes retained originals and SQLite files. These categories overlap and must not be summed as disjoint storage. The reports are checkpoints, not a continuous maximum of every temporary allocation.

The worker peak recorder runs inside the existing isolated worker and delegates its actual owned runner. The maximum individual worker peak is not the simultaneous family peak. `simultaneous_family_peak` remains `NOT_MEASURED`; a cooperative byte allowance is not an operating-system RAM limit.

Each large-source tier uses a fresh profile. A completed tier may be removed only after retaining its exact measurements, manifest, selected module identities and outcome, and only inside its explicitly owned directory. Failed or incomplete profiles remain available unless their owner explicitly selects cleanup.

## Import and restart acceptance

The public planner creates bounded durable pages and the current worker executes their saved rows. A source with a partial checkpoint remains queued and retains the same job/request identity. Continue planning and execution until both the saved scan cursor and every selected row reach the required terminal state.

The harness records every batch page and execution slice without building a second full list of source records. It reopens the catalog and checks every expected original, segment, locator and digest. It must find exactly the generated books and segments, with the expected final segment and no unreviewed publication.

The ordinary public-contract suite includes a real **5,001-source** scan continuation. This is separate from the corpus throughput denominator. The twelve-tier workflow does not claim a separate executed 20,001-entry ignored-tree benchmark.

The importer, durable checkpoint, publication-session, source-attestation, process ownership and cancellation suites remain required component checks. They establish rejection and recovery behavior; they do not replace the actual corpus runs.

## Retrieval and correctness

The literal traversal uses `search_page` with the exact collection and source window. It exhausts results within each issued opaque cursor before moving to `next_source_cursor` at offset zero. It verifies every returned segment against the independent fixture, tracks omissions or duplicates in a bounded bitset, and copies the first and last result through the application's current source authority.

The exact-title anchor sample spans the corpus from its first to last book. Every sampled result must be unique and its copied quotation must match the original. A separate Unicode query for the first and last book's final segment preserves the combining accent, Ukrainian/Slovak text and CRLF sequence. That query also traverses all required source windows; an empty first window is not an absent-source conclusion.

The 21 auxiliary libraries are explicitly selected and visited as 20 + 1 library windows. Their sources are excluded from the measured corpus denominator. Each federated quotation is copied through its own working application's handle; wrong-owner or wrong-scope use remains rejected. All active and retained search owners must confirm cleanup.

Complete-scope thematic ranking is measured separately using `ranking_scope='all'`. The first page must report the exact full corpus page/edition/result totals. The last global result page must be reachable and its final quotation must verify. A second same-source call must report zero updated index rows. Source-window ordering is never described as global ranking.

Finally, hash every external original again and require unchanged implementation identity. Equal-looking result text does not excuse a changed source or failed attestation.

## Backup and fresh-restore acceptance at the largest tiers

Both the 50,000-book tier and the exact 2 GiB TXT tier require the full existing common-format route on **both operating systems**: `LibraryApp.backup`, `verify_backup`, and `restore_backup` into a new private library directory. Every imported catalog row, raw segment, source digest and original byte must agree after restore; the archive and current source library must remain unchanged. The restored scan remains passive and requires renewed review before continuation. Verification or restoration never supplies permission to execute an old import or provider job.

The benchmark chooses an explicit caller-owned `BackupBudget` from its authored counts and measured current profile **before** creating the archive. The archive cannot raise that policy. The budget must account for catalog and import-journal sizes, originals, pending records, manifest decoding, verification/materialization work and the fresh restored profile. It retains the fixed decoder and per-record limits.

Before writing backup outputs, record the existing source/profile usage and the additional peak allocation on every affected temporary volume. The runner records both its known job-temp and Python-temp roots and uses an exclusively created private fixture parent; no preexisting fixture is adopted and no unrelated data is deleted. The backup implementation retains its separate native temporary-volume checks. Insufficient measured disk or memory produces `NOT_RUN` and a nonzero outcome; neither input size nor book count may be silently reduced.

Only an actual complete roundtrip with full source/archive/catalog/raw equality, exact source counts, unchanged implementation identity and measured resource results may be reported as `BACKUP_RESTORE_VERIFIED`. The other eight import/search jobs do not duplicate this expensive roundtrip. A small-corpus adapter contract or a prior backup component result does not establish 50,000-book or 2 GiB restore capacity.

## Measurements and limits of the report

The reports distinguish fixture generation, public scan planning, actual import slices including worker cleanup, reopened application startup, literal result-page latency, scoped anchor-query latency and complete-scope ranking. Coordinator downtime between commands is excluded from the sum of measured API calls and is not disguised as importer work.

The twelve-tier workflow requests **50** exact-title anchor queries. It also reports the latency distribution of all literal result-page calls, and two separately labelled complete-scope thematic calls. This is not 50 samples for every search mode. Absent-term distributions, corpus-scale semantic quality and exported-document performance are not claimed by this adapter.

Distribution fields contain sample count, minimum, maximum, median P50 and nearest-rank P95. Import/scan event files retain the individual API timings. Query reports contain their distributions and separately recorded complete-scope calls; they do not promise an exported list of every individual query sample.

A new interpreter is process-cold. Neither the operating-system page cache nor an existing persistent search cache is cleared. The first ranked call is labelled `first_complete_scope_call`, with `updated_index_rows` recorded, rather than being asserted to have a cold disk cache.

Every accepted tier must include the exact checked-out commit, full installed package inventory before and after, selected module origins, executed script hashes, Python/SQLite/platform identities, frozen resource profile, generation-manifest digest, exact source counts, phase outcomes and actual measurements. A source review or a copied report from another head does not supply that proof.

The current deterministic and actual pinned-model component tests independently cover full-scope semantic batching beyond 500 windows. This corpus adapter deliberately reports `semantic_model=NOT_RUN`; its BM25 or literal results do not imply semantic relevance quality.

A tier is complete only after the entire required route, exact counts, source checks and final installed-byte proof pass. Interrupted work is INCOMPLETE, capacity refusal is NOT_RUN and a failed assertion is FAILED. Physical listening, NVDA, signing and private-model quality remain separate qualification inputs.
