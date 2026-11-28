# Large-library scale: actual corpus acceptance

## Evidence boundary

V3-07 in `UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md` requires actual library, import and retrieval evidence. Generating files, passing a small contract suite, or measuring only a semantic cache does not establish a supported library capacity.

The import and retrieval benchmark scripts use the existing product routes. `scripts/benchmark_library_scale.py` writes and independently verifies real synthetic originals. `scripts/benchmark_library_catalog.py` uses `LibraryApp.plan_import_page`, `continue_import_scan`, `run_import_batch` and `search_page`. It never seeds catalog, FTS, revision, review, checkpoint or receipt rows directly. Its worker measurement delegates the existing owned process runner and observes the actual worker exit.

The default import limits remain unchanged. Large sources use an explicit, frozen `ImportBudget`; its allowance does not grant source authority. Original identity, whole-source digest, sealed segments, publication attempts and source-backed quotations retain their current checks.

### Six completed tiers on candidate 71ca, 28 November 2026

At the 09:30 UTC evidence checkpoint, the [actual corpus run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124999025) had completed the six jobs below on exact head `71ca92be691455d0ff51ed163b1d91749e5ae62e`. The six 10,000-book, 50,000-book and exact 2 GiB jobs were still running; the four required largest-tier backup/fresh-restore verdicts were pending. This dated checkpoint qualifies only the listed jobs on that tested head. It does not qualify a changed candidate or the complete twelve-tier protocol.

The [separate read-only artifact harvest](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46129830331), head `ce8a6c326988c5b8d73bb107a0dcf043c9f2b151`, checked each fixed artifact's run, job, head, compressed byte size and SHA-256 before reading its bounded saved JSON. It did not replay a corpus or import application code. All six reports matched the original exact-head source and installed inventories: **426 Python files, 45 selected origins and three measurement scripts**, checked before and after the original executions and independently checked against the selected Git source by the harvester.

| Actual input | OS | Books / original segments | Original bytes | Measured import API calls | Complete search scenario |
| --- | --- | ---: | ---: | ---: | ---: |
| 1,000 books | Linux | 1,000 / 40,000 | 10,279,003 | 92.994 s | 41.298 s |
| 1,000 books | Windows | 1,000 / 40,000 | 10,279,003 | 198.130 s | 48.393 s |
| 65 MiB + 173 bytes TXT | Linux | 1 / 17 | 68,157,613 | 6.037 s | 260.631 s |
| 65 MiB + 173 bytes TXT | Windows | 1 / 17 | 68,157,613 | 11.284 s | 463.918 s |
| 129 MiB + 173 bytes TXT | Linux | 1 / 33 | 135,266,477 | 12.961 s | 414.437 s |
| 129 MiB + 173 bytes TXT | Windows | 1 / 33 | 135,266,477 | 18.560 s | 558.043 s |

Times are rounded to three decimal places; the saved records retain the original measurements. Import totals include the genuine first committed publication, owned worker cleanup and any remaining public resume calls. They exclude coordinator downtime between commands and are not CI step-wall measurements. Search times cover the complete authored scenario: full literal traversal, all 50 title anchors, current-source copies, Unicode and foreign-scope checks, 21 auxiliary libraries, first and reopened complete-scope ranking, and confirmed search-owner cleanup. The 1,000-book jobs traversed 400 literal windows; the 65 MiB and 129 MiB jobs traversed three and five respectively. Every reopened index reported zero updated rows. All six jobs retained the actual exit 73 after one committed publication and resumed the same durable queue.

The following are separate process observations in bytes. The coordinator column is the search process's own high-water observation; the worker column is the maximum observed individual import-worker high-water value.

| Actual input | OS | Coordinator own peak, bytes | Maximum individual worker own peak, bytes |
| --- | --- | ---: | ---: |
| 1,000 books | Linux | 52,183,040 | 46,968,832 |
| 1,000 books | Windows | 53,153,792 | 45,686,784 |
| 65 MiB + 173 bytes TXT | Linux | 667,365,376 | 134,471,680 |
| 65 MiB + 173 bytes TXT | Windows | 641,994,752 | 96,649,216 |
| 129 MiB + 173 bytes TXT | Linux | 1,219,563,520 | 131,936,256 |
| 129 MiB + 173 bytes TXT | Windows | 1,170,731,008 | 98,275,328 |

Linux uses `resource.getrusage(RUSAGE_SELF).ru_maxrss`; Windows uses `GetProcessMemoryInfo.PeakWorkingSetSize`. These values must not be added as a simultaneous family peak, which remains `NOT_MEASURED`. The 1,000-book jobs used the unchanged derived 64 MiB search working allowance; the large-single-source jobs selected 1 GiB. A working allowance is not a whole-interpreter RSS ceiling or an operating-system memory cap. The observed 129 MiB coordinator peaks above 1 GiB do not change that distinction.

The next table compares logical byte counts from the same named after-search checkpoint. SQLite columns are the main files; the full reports also retain any observed WAL, SHM or journal sidecars.

| Actual input | OS | External originals | Retained originals | library.sqlite | imports.sqlite | Whole application profile |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1,000 books | Linux | 10,279,003 | 10,279,948 | 41,193,472 | 31,391,744 | 114,811,402 |
| 1,000 books | Windows | 10,279,003 | 10,279,948 | 41,287,680 | 31,457,280 | 115,026,949 |
| 65 MiB + 173 bytes TXT | Linux | 68,157,613 | 68,158,558 | 145,633,280 | 68,280,320 | 359,798,767 |
| 65 MiB + 173 bytes TXT | Windows | 68,157,613 | 68,158,558 | 145,633,280 | 68,280,320 | 359,798,724 |
| 129 MiB + 173 bytes TXT | Linux | 135,266,477 | 135,267,422 | 289,144,832 | 135,479,296 | 711,780,347 |
| 129 MiB + 173 bytes TXT | Windows | 135,266,477 | 135,267,422 | 289,144,832 | 135,479,296 | 711,780,304 |

The whole application profile includes its retained originals, SQLite files, derived search data and the 21 auxiliary-library fixtures. Its columns overlap and must not be summed as disjoint storage. External originals are measured separately. These are disk checkpoints, not continuous peak allocation measurements. POSIX allocated-byte observations remain available in the full records; Windows physical allocation is `NOT_MEASURED`, so the table deliberately compares logical bytes only. Each runner's actual temporary-volume identities, total/free space and admission decisions remain in the same evidence.

Complete measured records, frozen artifact identities, raw-log references and the six-row field mapping are retained in evidence `e9c9059cc506f40aa1a08d86776e779d61235938`; the read-only harvest raw log is `269bf468f4286923b99d7655151fdc86a45b2100`. The common package SHA-256 is `805d2713cccb7ee797412ff3aac0977c414f846580d83850cb589bdaa2ec3bd1`. These measurements do not establish larger-tier backup capacity, simultaneous family RAM, semantic-model quality or a general cross-run speedup. Final acceptance still requires the complete bound results for the ultimately selected head.

### Latest completed index publication correction

The [actual Windows 512 MiB protocol](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46112104315)
passed at exact head `04ef743f2b68069516e19adefd39ef405097938a`.
It generated and verified a real 536,871,085-byte original with 250 segments,
admitted it through the public product API, reached the genuine committed
interruption, resumed the same saved queue, and completed catalog and full-scope
retrieval. The original 671,088,813-byte worker scratch allowance, 600-second
worker deadline and saved resource policy were retained. There was no marker,
observer, replacement process runner or hand-seeded index in this completed run.

| Actual measurement | Windows result |
| --- | ---: |
| Public import/resume measurement | 71.033 s |
| Measured source scan | 2.683 s |
| Complete catalog verification | 9.399 s |
| Complete retrieval protocol | 1791.572 s |
| Literal windows, 17 samples, P50 / P95 | 13.375 / 14.165 s |
| Scoped title anchors, 50 samples, P50 / P95 | 9.199 / 9.282 s |
| First complete-scope ranking / unchanged-cache reopen | 29.399 / 30.399 s |
| Ranking rows rebuilt on first call / reopen | 250 / 0 |
| Actual worker's own observed peak | 69,058,560 bytes |
| Coordinator's own observed peak | 1,836,339,200 bytes |
| Application profile logical bytes | 2,821,986,267 bytes |

Worker and coordinator figures are separate Windows process observations. They
are not a concurrent process-family peak, physical allocation measurement or a
claim that the 1 GiB accounting allowance enforces an OS memory ceiling.
Application profile bytes include the retained originals and SQLite files; the
categories overlap and must not be added again. The full record, exact source,
package identity, original hash, saved budgets and raw log are retained in
evidence `18a9c2c04c248166a5e7b323d0a3c33dc39ad074`, raw
`9b48bcf1e1ce377fac667a78da47f4ced39fdcad`.

The source correction is limited to the two existing index-fill paths. They
iterate the selected page IDs through the current cursor and execute one scalar
primary-key-backed insert for each ID. Existing savepoints, triggers, receipts,
foreign-row guards and complete integrity checks stay in place. The
[24-suite publication gate](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46112053954),
head `409d2d7bebdeae13dc5c7ac97aa5d5304f331a8c`, passed 306 source and
306 isolated installed cases on each OS, with three subtests per context.
It exercises actual rollback after three real inserts for ordinary exceptions,
KeyboardInterrupt and SystemExit; independent readers/writers; and the prior
canonical validation oracle. All four contexts bound the complete 406-file
Python package and 15 selected origins before and after execution. Evidence:
`e0ff493a9ebac6bdfb6817e33693023efc3044e1`.

The preceding diagnostic is retained as failure evidence. On the old SQL path,
one sequential original-stat check observed the 536,871,085-byte source, a
146,620,416-byte SQLite temporary file, the 332-byte request and the zero-byte
before-insert marker: 683,491,833 sampled logical bytes against the unchanged
671,088,813-byte quota. The after-insert marker was not observed. This is a
sequential sample, not an atomic allocation snapshot or proof of an exact CPU
instruction. The marker/observer are absent from the shipped product.
Its record is `9a39e59a88222ca9925191c2c7fc0cd166802ffd`.
Separate actual native SQLite plan evidence is retained in
`f3f978f0f49990e17f9c05fdd4a87b1368d8608f`.

This completed 512 MiB component does not establish the final 12-tier result.
The exact final composed head still requires every tier below, including actual
50,000-book and 2 GiB sources on both operating systems and the largest-tier
backup/fresh-restore protocols. Their completed Actions records, rather than
this predecessor measurement, determine the final capacity qualification.

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

The first 1,000-book run spent approximately 381.826 seconds between starting resume of the remaining 999 books and starting the catalog-verification step. This includes the step transition; it is not a final API profile. A simple linear extrapolation would exceed that predecessor's 95-minute resume phase at 50,000 books. That observation triggered an actual parent/worker profile rather than an unsupported throughput claim.

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

### Completed corrected components, 28 November 2026

The corrected component at exact head `bc195d800d9ffc5846cef37fac81e94e3e0a69b0` completed both its [source/installed contracts](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46094685065) and its [actual 1,000-book protocol](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46094685110). The contract gate passed **648 source and 648 installed cases on Linux**, and **641 source and 641 installed cases on Windows**, with no failures, errors, skips or expected failures. All four contexts checked the full 393-file package and 36 selected module origins before and after execution. This includes the current publication-receipt, directory-identity and Windows transient-directory corrections.

The separate actual corpus jobs generated and imported 1,000 books / 40,000 segments, observed the authored exit 73 after a genuine committed publication, resumed the saved queue, and completed every catalog, original, quotation and scope oracle:

| Measurement | Linux | Windows |
| --- | ---: | ---: |
| Public import calls, including owned worker cleanup | 92.936 s | 193.363 s |
| Public scan planning | 1.655 s | 4.050 s |
| Complete catalog verification | 1.527 s | 2.875 s |
| Complete retrieval scenario | 102.586 s | 178.964 s |
| Literal page calls | 400 | 400 |
| Literal page P50 / P95 | 0.217 / 0.221 s | 0.391 / 0.419 s |
| First / reopened complete-scope ranking | 6.842 / 6.593 s | 8.093 / 8.741 s |

Both reopened ranking calls reported zero updated index rows. The actual selected temporary volume had approximately 87.86 GiB free on Linux and 219.84 GiB free on Windows; the Windows Python temporary volume separately had approximately 76.91 GiB free. These observations describe those runners. Every future tier must perform its own resource admission.

The [Windows import profile](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46095787806), exact head `27b5380eee386059affa89f5c38cb354ba20a034`, completed four genuine 250-book slices and full resulting catalog/source verification. Its 336.484-second parent measurement includes cProfile overhead and is not an uninstrumented throughput result. Each child performed three full storage checks, while the remaining live session checks and path observations consumed a substantial share of the measured time. Nested cumulative profile timings overlap and must not be summed. This evidence does not authorize removing current-source, ancestor, cancellation or resource observations.

The [small backup-scale adapter gate](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46095579146), exact head `1d85bae4c35bd7fedf385b3eeafc9519d770b0c5`, passed all **17 cases in source and isolated installed modes on both operating systems**. It checked 45 selected origins, all 405 package files and all three benchmark scripts before and after execution. These are actual small-corpus backup/verification/restore and refusal contracts; they do not establish a 50,000-book or 2 GiB roundtrip.

These completed component results supersede the corresponding unresolved component observations above only for their exact source compositions. They do not establish the final twelve-tier verdict, larger-corpus throughput, simultaneous process-family RAM peak, semantic quality or physical accessibility. The current final-head workflow reports remain the acceptance authority.

### Executed source and search growth components

The [explicit search-memory contract gate](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099085535), exact head `734962a62f4efa329f04d8c2f69ae20057ca6114`, passed **62 source and 62 isolated installed cases on each operating system**, with no failures, errors, skips or expected failures. It bound all 405 package files, 45 module origins and three measurement scripts before and after execution. This qualifies the explicit allowance and small backup/import/search contracts, including their refusal controls; it does not by itself qualify a large-source performance result. Evidence is retained in Git blob `256ce11b9b4d6605649c60ae351544c9a3d1d24b`.

The [uninstrumented search component](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46101042884), exact head `25518b4c49c8fa55c3ad4f45a178403e733609dd`, completed all six jobs below. Every job generated actual originals, interrupted after one genuine committed publication, resumed the same saved queue, verified the full catalog and sources, and passed complete literal traversal, all 50 exact-title samples, current-source quotation copy, Unicode, foreign-scope, federation and complete-scope ranking checks. The reopened index reported zero updated rows in every job.

| Actual component input | OS | Measured public import calls | Complete retrieval scenario | Coordinator's own peak |
| --- | --- | ---: | ---: | ---: |
| 1,000 books / 40,000 segments | Linux | 162.520 s | 49.243 s | 52,084,736 bytes |
| 1,000 books / 40,000 segments | Windows | 436.043 s | 97.320 s | 53,100,544 bytes |
| 65 MiB / 17 segments | Linux | 8.094 s | 273.075 s | 672,157,696 bytes |
| 65 MiB / 17 segments | Windows | 17.031 s | 478.268 s | 618,704,896 bytes |
| 129 MiB / 33 segments | Linux | 22.896 s | 624.945 s | 1,232,633,856 bytes |
| 129 MiB / 33 segments | Windows | 31.906 s | 760.929 s | 1,166,299,136 bytes |

The two single-source component inputs were exactly **68,157,440** and **135,266,304** bytes. The final twelve-tier fixtures specified below add 173 bytes to those boundary inputs, so these component jobs are not substituted for the exact final tiers. The large-source component used the explicit 1 GiB search working allowance; the book component retained its derived allowance. The measured interpreter peaks are separate observations and may exceed the search working allowance. No simultaneous family peak or operating-system memory ceiling is claimed.

These jobs had no `cProfile` observer. They ran on independently hosted machines and on a different composition from the earlier instrumented diagnostics, so their table does not claim a causal speedup ratio. Full measurements, source identities, six raw-log blobs and before/after proofs are retained in Git blob `9e0725e77172d97d4cda28c18c3b58fa0ff805d0`.

The Linux job of the [separate 10,000-book growth component](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46095787780), exact head `27b5380eee386059affa89f5c38cb354ba20a034`, completed 400,000 actual source segments and all end-to-end oracles. Measured import calls took 1,031.729 seconds, full catalog verification 36.562 seconds and retrieval 1,849.135 seconds. The two complete-scope ranking calls took 419.176 and 427.914 seconds, with zero updated rows on reopen. This exposed growing query cost; it is not a 50,000-book projection or a result for the later SQL composition. Exact evidence is retained in Git blob `40eac2193fd9f9394291ca26ba27f9996c6d6937`.

The same-head Windows job completed generation, admission, the genuine first-commit interruption, resume and full catalog verification, then **failed** its existing 3,600-second retrieval allowance. The trace stopped in the old `ranked_scope._sync` current-page query: SQLite reported `interrupted`, followed by `TimeoutError: Search cancelled or timed out.` The final proof refused the failed search phase. The trace does not identify which first/reopened ranking iteration was active. Resume occupied 3,294 seconds and catalog verification 82 seconds of completed step wall time; those values are not the API timing distributions, which remain in the captured artifact. This is a pre-SQL-correction failure, not a result for the later composition. Exact phase states, the bounded traceback, raw-log identity and artifact digest are retained in Git blob `5cc22e1ee7db36e43711d4f0540f27df2613db8b`.

The [canonical query diagnostic](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46101997783), exact head `3bd0453809dbade69dbc6131079bdb5e79dc733a`, used fresh public-import corpora of 100 and 1,000 books. Read-only old/new queries preserved exact rows, ordering where required, source scope and database bytes. For a 100-ID refetch, the scoped page-first query used 5,254 native VM steps at both sizes; the previous form grew from 53,354 to 495,254. This is actual query-plan and equivalence evidence, not a full-library performance acceptance for the product patch. Git blob `269e6b8fe71173723004635dbd40b23fb914ab37` retains the actual plans, measurements and parity checks.

### Completed current SQL growth measurements

The [current 10,000-book component](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46103586773), exact head `60f01a5630bdc4127cfc60bdf26474d527ba1744`, completed on both operating systems after the first three scoped SQL corrections. Every actual catalog/source row, all 4,000 literal windows, 50 title-scoped queries, exact quotations, 21-library navigation and both complete-scope ranking calls passed after genuine interruption and resume. The complete source/package inventory remained unchanged.

| Actual 10,000-book measurement | Linux | Windows |
| --- | --- | --- |
| Public import calls, including owned worker cleanup | 1,868.085 s | 2,015.626 s |
| First to last resume call for 1,000-book saved batches | 173.843 to 198.523 s | 188.691 to 212.715 s |
| Owned child within those first/last calls | 156.506 to 174.359 s | 171.469 to 189.312 s |
| Complete catalog verification | 59.648 s | 49.889 s |
| Complete retrieval scenario | 668.823 s | 558.904 s |
| Complete 400,000-result ranking, first / reopen | 66.330 / 59.902 s | 59.224 / 57.748 s |
| Updated ranking rows on reopen | 0 | 0 |

The child times are included in their public API calls and must not be added to them. These hosted runs are not controlled causal comparisons with earlier machines. The ten chronological saved-event identities, counts, timing reconciliation, complete metrics and raw logs are retained in Git blob `650fec0a6d68cacb301dfe526b6e7c98cff0f988`.

The [512 MiB growth component](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46104136822), exact head `6d30844bc79f951fc3387a29b2d753370c8950bb`, generated a genuine **536,871,085-byte** source with 250 original segments. Linux completed the full protocol: public import 59.231 seconds, catalog 9.789 seconds and retrieval 1,321.129 seconds. Its 17 literal windows had P50/P95 10.248/10.333 seconds; 50 scoped anchors had P50/P95 6.836/6.889 seconds. Complete ranking took 24.313/22.255 seconds, with zero updated rows on reopen. The coordinator's individual-process peak was 1,840,365,568 bytes, not a simultaneous family peak.

The same-head Windows 512 MiB job **failed** during its first import because the selected worker temporary-output budget was exceeded. The saved batch had zero complete and one failed item; no authored interruption, resume, catalog or search phase completed. The selected quota was 671,088,813 bytes. Neither the cause nor a later correction is inferred from this message alone. The failed raw log and successful Linux measurements are preserved together in the same evidence blob. These component results do not replace the final exact 2 GiB tier.

The [follow-up bounded-fetch diagnostic](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46105648861), exact head `fb90e7797b90339561d45982e256a6f601b4489f`, completed all 24 read-only SQL pairs on actual 100-book and 1,000-book corpora, retaining identical rows, required order, database bytes and complete implementation identity. The derived current-original lookup used 57 native VM steps at both sizes. Nonempty result refetches use at most 100 page-key lookups and up to 500 selected edition alternatives per key. This finite bound has a measured tradeoff: the wide 500-edition / 100-page case used 406,763 steps versus the old 257,763 at 1,000 books. The change does not promise every small query becomes faster. Empty-ID execution returned zero rows in 28 steps at both sizes. Complete plans, source identities and raw logs are retained in Git blob `18735c1c5e74d961bae36bb57051c39c33557e1f`; the patched product receives its own functional and final-capacity verdicts.

### Native scratch diagnosis and index validation

The [first original-stat diagnostic](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46107906892), exact head `8cbc6caebb13e746fd270f4e2ff5611b25843ebc`, preserved the Windows failure and unchanged 671,088,813-byte quota. Its existing sequential filesystem check observed a 536,871,085-byte staged source, a 159,522,816-byte SQLite `etilqs_` temporary file and the 332-byte request: 696,394,233 sampled bytes in total. All three were distinct ordinary single-link files without reparse attributes. This is a sampled logical-size prefix, not simultaneous physical allocation or proof of the originating SQL statement. Complete original samples and raw evidence are retained in Git blob `f2bba32c5e2547a6b8b127c6a6b3d7a26ae0d2d2`.

The [index-validation component](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46108822754), exact head `e4262c5740452a38f10c0a0328b61c4770251c21`, passed 294 source and 294 isolated installed cases on each OS, with three subtests per context. Its 26 new cases compare the genuine canonical-schema checks with the prior `EXCEPT` oracle, including missing/stale/foreign IDs, SQLite storage classes, Unicode and schema/digest/FTS refusals. The three replacement equality queries use primary-key probes without a raw-text ephemeral set in the actual SQLite 3.45.1 opcode plans. Full evidence is retained in Git blob `94ee74d67055c4a5c8d8df55c0429232634a9b69`.

The subsequent [uninstrumented Windows 512 MiB run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46109412259), exact head `ba5cd017f18abf99ef007368f73e1f2d503ccfa7`, nevertheless **failed** the same output-disk quota during its first import after 50.623 seconds. Source generation, public admission, the pre-spawn policy assertion and package-identity checks passed; the batch had zero complete and one failed item. The authored exit 73, resume, catalog, retrieval and final saved-budget assertion were not reached. This establishes that the validation change alone does not close the Windows capacity defect. The complete failure evidence is Git blob `bfab1ec6b9db3f64dba9effbbc8c208d4353da57`; no later fix or larger-tier PASS is inferred from it.

The final workflow must use measured current-composition growth to set finite aggregate allowances. Each public matching result page and each of the 55 main-source copy operations retains its current-original proof. Empty Unicode windows do not attest an unreturned original; complete-scope index construction validates bounded database rows, with fresh original proofs when its result pages are returned. These distinctions matter when planning the 2 GiB run. No per-query limit, complete traversal, copy sample, source digest or required backup roundtrip may be removed to fit an aggregate deadline.

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

For the large-single-source tiers, add `--search-memory-mib 1024` to the `init` command. This selects the existing `SearchResources` working-memory allowance and persists that exact choice before import; the separate `--memory-mib` option selects the importer allowance. The search value must be an integer within the supported range and large enough for the maximum authored record. The same real RAM/disk admission runs before creating the application profile. Without the new option, the previous derived default remains unchanged. A 1 GiB search allowance permits up to 33,554,432 raw source characters per window while retaining the existing 100-result cap, complete source traversal, all 50 anchor samples and every quotation/scope check. The working-window allowance is not a whole-interpreter RSS ceiling or an operating-system memory limit. The real RAM admission retains the existing 256 MiB headroom above the larger of the importer and search allowances; measured coordinator and individual-worker peaks remain separate report values. The report binds the selected memory value to both the admitted and saved profile; this configuration is not itself a throughput result.

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
