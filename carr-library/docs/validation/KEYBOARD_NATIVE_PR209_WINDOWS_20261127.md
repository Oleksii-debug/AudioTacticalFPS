# PR209 keyboard and draft-recovery Windows result

**PASS for the exact keyboard-native job at `f16959c76c1702fbdf6d6c9a6c113b2cb6660cd3`: 291 source regression cases and 146 actual installed native checks.** This is the completed secondary job in [run 46069523389](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46069523389), job `140742846571`. The reviewed tree is `9b1de961f054691406da899dc5377266c8e17703`.

## Actual source regressions

All 11 required modules passed with zero failed or skipped cases. The complete log contains 291 `CASE_START` events covering 142 function identities. Each event belongs to a fetched exact-head test function, and each module's event total equals its successful JUnit summary.

| Required module | Passed |
| --- | ---: |
| tests/ui/test_keyboard_registration_compatibility.py | 3 |
| tests/ui/test_command_registry.py | 107 |
| tests/storage/test_keyboard_preferences.py | 32 |
| tests/ui/test_keyboard_manager.py | 34 |
| tests/ui/test_save_all.py | 31 |
| tests/ui/test_save_all_current_drafts.py | 3 |
| tests/ui/test_desktop_evidence_jobs.py | 17 |
| tests/ui/test_reader_command_dispatch.py | 15 |
| tests/ui/test_reader_keymap_context.py | 11 |
| tests/storage/test_evidence_drafts.py | 19 |
| tests/ui/test_evidence_draft_recovery.py | 19 |
| **Total** | **291** |

Four of these modules also have exact protected whole-module Counters in the committed V3 oracle; those match independently. The companion JSON records the entire observed 291-case Counter without claiming a separate predeclared oracle for all 11 modules.

## Actual installed native controls

| Probe | Passed checks |
| --- | ---: |
| Main keyboard settings and command dispatch | 35 |
| Reader contextual key remapping | 23 |
| Evidence-note draft recovery and explicit save | 15 |
| Save All and completion-aware close | 38 |
| Measured progress, status queries and cancellation | 35 |
| **Total** | **146** |

All five probes run with `-I -B`. Their actual receipts match the logged native labels and counts. The seven Save All module-origin SHA256 values were independently compared with product files fetched at the exact tested head; the paths identify the installed package. The other three origin-bearing probes report installed paths, without a separate hash field. The progress probe reports its own exact-source checks.

Every job step completed successfully, including evidence upload and the final interpreter/checkout cleanup steps. Artifact `15232775948` contains 11431 bytes and has GitHub SHA256 `67f7d02a025366a423f909840df0f3c382cb4d52e05aca5209861b7cf198c3d3`. Its exact run/head metadata was independently fetched; the ZIP itself was not independently unpacked.

The [companion JSON](KEYBOARD_NATIVE_PR209_WINDOWS_20261127.json) retains the complete observed Counter, all native receipts and labels, source identities, job steps and artifact metadata.

This is source 291 plus native 146; it does not claim a second installed 291 regression run. Manual NVDA, physical listening and voice quality remain unqualified. The integrated-desktop and standard O→A→B gates have separate results. A later current head does not inherit this exact-head PASS automatically.
