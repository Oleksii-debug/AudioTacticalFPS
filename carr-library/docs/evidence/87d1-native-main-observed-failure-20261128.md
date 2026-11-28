# Native main focus at 87d1: observed failure

The native focus **failed** at the federated-search assertion in focused step 32 (copied main step 34). The earlier reading, Import Center, document-format and OCR-model inspection receipts passed at this exact source. Steps 33–47 did not run. This report preserves that boundary and does not qualify the full native lane or full platform.

## Exact execution

| Item | Observed value |
| --- | --- |
| Source | `87d1ec6a05963531c5c1907f74884faddac2093e` |
| Tree | `f28841240a6d5163e6d8e1bdea4e8117b30650e2` |
| Tracked files | 1,833 |
| Run / job / attempt | 46135188389 / 140948783918 / 1 |
| Branch | `carr/native-main-final-87d1-20261128-d3a` |
| Job interval | 2026-11-28 10:30:23–10:37:13 UTC, 410 seconds |
| Outcome | FAILURE |
| Runtime | Native Windows x64, CPython 3.11.9, wxPython 4.2.4 / wxWidgets 3.2.8 |

The complete [run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46135188389) and [job](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46135188389/job/140948783918) metadata agree with the source identity. The adjacent [JSON evidence](87d1-native-main-observed-failure-20261128.json) retains every API step, the actual receipts, check Counters, current source hashes and exact traceback.

## Failure and limits of the diagnosis

The invoked `tests/ui/probe_source_open_native.py` reached its adjacent `federated_native_smoke.py` helper. Its `search_ready` callback failed at line 125:

```python
assert {hit.workspace for hit in controller.rows} == {active[0], workspace}
```

The actual failure is an `AssertionError`, followed by exit code 1. The log does not print the current rows, page cursor or worker timing. The source-open wrapper emitted a combined FAIL receipt; it did not emit a separate source-open PASS. Its preceding source-opening checks can be related to the call path, but they are not counted as a separate successful gate.

The independently read current code requests `paged=True`. Its search service returns a current nonempty library page and offers the remaining selected libraries through `next_cursor`. That establishes a fixture expectation mismatch consistent with the failed equality. It does not reveal unlogged runtime values. The separate source-only repair is [3149f07451c54460fd04f1ac76fb38589226ec76](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/3149f07451c54460fd04f1ac76fb38589226ec76); no successor execution is claimed here.

## Successful receipts retained before the failure

| Focused step | Actual result | Qualification boundary |
| --- | --- | --- |
| 6–20 | Syntax 1,118 files; installed self-test 9; ten prerequisite suites, 323 passed / 0 failed / 0 skipped; fixed-shell classification 3 | These are the copied prerequisites, not the integrated 451-selector source or full installed suite. |
| 11–12 | Academic controls 40; advanced query 18 | Installed-mode native probes; manual keyboard and NVDA remain unqualified. |
| 21 | Citation controls 108 | Five installed module hashes match current source; controlled file paths and recorded manual/NVDA limits remain. |
| 22 | Basic translation PASS, all 13 receipt fields, 30 ordered diagnostic events | Two synthetic requests; Google API, real credential vault and NVDA are NOT_RUN. |
| 23 | Advanced translation PASS | Synthetic provider, actual credential-vault/runtime cleanup PASS; Google API and NVDA are NOT_RUN. |
| 24 | Search continuation 21 | Exact raw label list and four current installed module hashes match. |
| 25 | Signed-client controls 15 | Synthetic authority; no production authority or installer qualification. |
| 26 | Explicit Word NOT_RUN, zero checks | Desktop Word was not installed/registered. The successful recording step is not Word feature qualification. |
| 27 | Language controls and explicit voice refresh PASS | 135 catalogue entries, 7 enumerated voices, 0 qualified languages; speech synthesis NOT_RUN. |
| 28 | Import Center PASS | **Source-native** probe: checkout source is explicitly inserted. The complete import/search/reopen receipt and both viewport observations passed. |
| 29 | Reading PASS with **26 occurrences**; OCR-language inspection 17 | Reading uses the installed wheel. OCR inspection reports no installed models, unavailable runtime and English inference NOT_RUN. |
| 30 | Document formats 27 | Three current installed origins, `source_override=null`; synthetic file selection, no native picker/NVDA claim. |
| 31 | OCR model manager 81 | Exact cases array and raw Counter agree; nine installed hashes match. No model installation/download or attempted Python network operation. |

The reading probe has 24 literal check call sites, including one dynamic button check. That check runs three times: **Save twice and Close once**, giving 26 emitted occurrences rather than 29. The proof keeps the full ordered list and Counter, all eight required menu bindings, uniqueness of nonempty shortcuts, actual Save dispatch, persistence/conflict checks and complete-backup restoration.

Import Center retains all 23 original assertion sites plus 18 layout/focus sites. The actual window sizes were 920 × 760 and the existing minimum 800 × 650; both outer sizer minima fit their clients, while the scroll viewport correctly contains taller virtual content. The four-field terminal receipt records the completed import/search/reopen flow.

## Source and artifact binding

The before binder reported **1,833 tracked files and 442 installed payload files**, with receipt SHA256 `11030b2ddfc1b488a0c503ef387bdae435693d57b833c38fc022d1cdc4473fa5`. The source-reviewed binder checks exact Git bytes and installed content. Its emitted temporary-script SHA256 independently matches the current workflow body. The after binder was skipped, so post-gate unchanged-source/installed-payload verification is absent.

This review freshly read and Git-blob/SHA256-verified **54 current source files**. All **26 emitted hash comparisons across 25 unique files** match. All 56 printed production package versions equal the current integrated lock. These checks do not substitute for reading the full archived binder maps.

| Retained item | Actual identity |
| --- | --- |
| Decoded complete job log | 612,261 characters; 612,271 UTF-8 bytes |
| Log SHA256 | `f00ff0aa48a39c01d9c20f5cbf87467b1f8b3d1728b163bccfd75413470eea64` |
| Artifact | 15259607296; 253,437 bytes; uploader reported 44 files |
| Artifact SHA256 | `89c93eb124dea2c699509d26f17058cf2e56abcf817b1d7147ab95ce5059a6a0` |

Artifact name, ID, size and digest agree between the upload log and API. **The ZIP, raw JUnit, full binding maps and exported derivative bytes were not independently downloaded or decoded.** Raw log bytes remain unchanged; line-ending/ANSI handling was used only to interpret evidence.

The previous [Import Center inventory erratum](import-center-audit-inventory-erratum-20261128.json) remains explicit. Its corrected 2701 and separate 09ac inventories are historical source bindings, not complete current-87d1 inventories. This proof uses the current individual bindings and before receipt.

All 44 evidence-reconciliation predicates are true. They confirm an accurately recorded **failed run**, not software qualification. Later steps 33–47 were skipped; upload and post-job cleanup succeeded. The separate current Basic/local-translation focus passed, but that outcome is not transferred into this lane. Full platform qualification remains **NOT_QUALIFIED**.

This component adds only the JSON and Markdown evidence files, with sole parent 87d1. It changes no application, probe, test, workflow, oracle or ref.
