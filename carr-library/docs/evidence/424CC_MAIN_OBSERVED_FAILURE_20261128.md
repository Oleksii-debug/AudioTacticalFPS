# Frozen 424cc full-main failure — 28 November 2026

**Overall result: FAIL at the Basic native translation probe.** All **451 required source selectors passed**, recording **11,589 passing case events, zero failures and zero skips**. The protected source gate then passed its exact **252 modules / 5,947 cases**. The following native citation flow passed **108 checks**. The next translation flow failed before its dialog opened, and later main qualification was skipped.

## Exact execution identity

| Field | Value |
| --- | --- |
| Source | `424cc456992c69c1ee945bc4d5ab73bc21e4e7b8` |
| Tree | `be936242ecf2aa62ab3344de8874a92605aea39e` |
| Run / attempt | [46109011419 / 1](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46109011419) |
| Integrated job | [140868122250](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46109011419/job/140868122250) |
| Source step 21 | 05:04:39–06:13:04 UTC; 68 minutes 25 seconds |
| Protected source step 22 | PASS, completed 06:13:05 UTC |
| Native citations step 23 | PASS, completed 06:13:13 UTC |
| Basic translation step 24 | FAIL, completed 06:13:59 UTC |
| API job completion | 06:14:08 UTC |
| Final cleanup log | 06:14:06.2347318 UTC |

The complete log was refetched after the job completed. It contains **4,953,693 characters / 4,953,699 UTF-8 bytes**, SHA256 `ab872a89a838e4217461585c0cdf592e8d956100bc8309d5c4cb7d3ce0a87c33`. This hashes the decoded log text returned by the tool, not compressed transport bytes. The earlier buffered partial snapshot is not used for final totals.

## Passing source and protected populations

All 451 source selector receipts exactly match the frozen workflow inventory. They contain **11,589 passing execution events** and **5,270 normalized module/function identities**, with no failed or skipped case. Directory and individual-file selector overlap remains included; these are execution-event totals rather than a deduplicated test population.

The complete current Counter directly matches the reviewed prior population. **No joined-line correction was needed in this run.**

| Evidence | Exact result |
| --- | --- |
| Full source selector Counter | 451 of 451; all PASS |
| Full passing case events | 11,589 |
| Full function Counter SHA256 | `f7a5f4cceff7fde13ddb4276b3747e2fa40528452c024d57cef9541fd4f7a574` |
| Protected direct population | 252 modules; 5,947 passed |
| Protected function identities | 2,807 |
| Protected weighted CASE_START events | 6,603 |
| Protected Counter SHA256 | `798b61c50977fb8bc6fdd6595c2f62012b4eafb7f231a781fc39ce7d6b4ba8b4` |
| Oracle blob | `9db65750aa51b1d7011d535ec00274e1b2d828c5` |
| Oracle SHA256 | `24f1c6f2e29f944d76362a01a699d70164dbda7e7e3bd775e3379b4c4b0855cf` |

Unlike earlier failed heads, **the official protected gate actually ran and passed** here. Its typed receipt names source `424cc456992c69c1ee945bc4d5ab73bc21e4e7b8`, status PASS and all 252 exact per-module totals. The machine-readable proof retains that receipt and all 451 suite receipts.

The helper module now passed **77/77**, including both previously failing deletion-sharing fixture variants. Production integrity 62, audio CLI 30, Audio Studio 105, assistant 656, resource admission 40 and measured-duration 51 also passed without failures or skips. The preceding explicitly permitted synthetic local-planning HTTP gate passed its exact 14 cases.

## Native citation flow: actual 108 checks

Step 23 ran the installed `windows_citation_styles_smoke.py` flow. The complete log contains exactly **108 PASS labels**, followed by its typed `checks: 108`, `result: PASS` receipt. The receipt's five installed-module hashes were independently compared with the exact current source bytes for bibliography workbench, CSL, BibTeX import, academic library dialog and citation-style dialog.

The receipt preserves its actual limits: folder and custom-style selections were controlled; native clipboard, keyboard, export format and overwrite confirmation passed. Real file pickers, manual keyboard qualification, NVDA, installer work and native overwrite accessible-focus qualification remain NOT_RUN where stated. This early installed flow does not stand in for the skipped complete-bundle or standard-installation gates.

## Exact translation failure

At 06:13:14, the scheduled native callback reached:

```text
installed ui/translation_dialog.py:57
    TranslationController(frame.library.root, service=service)
installed ui/translation_dialog.py:20
    ValueError: Translation belongs to the selected library root.
```

The dialog did not open. The unchanged 45-second probe wait subsequently ended at 06:13:59:

```text
scripts/windows_translation_smoke.py:224
RuntimeError: Native translation probe failed:
TimeoutError: Native translation probe exceeded its bounded wait.
```

The existing controller requires the selected frame root to match the translation service root. The Basic probe keeps its raw temporary-library path in a synthetic frame. The service canonicalizes its root through the existing store, and the real `LibraryApp` resolves its admitted root. The already existing Advanced and local probes explicitly call `root.resolve(strict=True)` after creating that directory; Basic lacks this fixture step.

The actual differing path strings were not printed, so this record does not invent a short-name spelling or call the guard itself incorrect. The separate fixture correction is reviewed elsewhere. **No repaired-probe result or future-head qualification is transferred to this failed run.** The JSON retains the exact complete failure log group and source blobs.

## Skipped scope and artifact identity

Every main step **25 through 53** was skipped, including the later Advanced/native flows, full installed regression population, complete bundle, standard installation and original→maintenance→candidate/library qualification. Independent final-main artifact acceptance was not run. Earlier focused and auxiliary outcomes remain separately bound.

API metadata and the actual uploader agree on:

- Artifact `15250469810`: `carr-windows-424cc456992c69c1ee945bc4d5ab73bc21e4e7b8-1`.
- **310,290 bytes**; uploader reported **27 files**.
- SHA256 `db2f24dd2a0e41de043f937ee8760763e144167cdf693da29823fece65312d75`.

No artifact ZIP was downloaded, opened, extracted or executed for this proof. The [complete JSON record](424cc-main-observed-failure-20261128.json) preserves all 59 step statuses, all source receipts and the full Counter, actual protected and native receipts, five source-to-installed digest matches, precise failure boundaries and scope limitations.

This component adds evidence only. Every existing 424cc application, test, probe, workflow, timeout and oracle file remains unchanged.
