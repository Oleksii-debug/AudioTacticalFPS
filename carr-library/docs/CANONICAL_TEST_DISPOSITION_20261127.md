# Canonical test reconciliation — 27 November 2026

This is the finite source disposition for the canonical test delta. It is not an
acceptance report. The new test sources and the eventual merged tree remain
**NOT_RUN** until their actual source, installed and native jobs finish.

## Exact compared trees

| Role | Commit | Tree |
| --- | --- | --- |
| Common base | 329d31937d029352ee36c43c62320c68347cc44d | 2de344dfdd781d19a2228eaf4822aefe16f9b37c |
| Canonical baseline | e3cf74c243d4ef930b526b5648ca9003c0900057 | 5fc67a0d9a0ab18fb0662bb01e027d29d83af72f |
| Selected published baseline | 0d7fe0de7ec3eb60481c62424e250165c024abff | c9f14d759c4ffa269447f5776815e9ac0916237a |
| Reviewed source corrections inherited here | 8383676ca800bb247c7b72d6b48f44d8e5901201 | 011ab7d56746a40ebe9916c2438c5910a3c23a55 |

All three inventories were complete and nontruncated. The accompanying
[166-row JSON ledger](acceptance/canonical-e3cf-tests-0d7fe0.json) contains exact
base/canonical/current blob IDs, every canonical declared test symbol, selected
test owners, their exact blob IDs, and references to the actual current workflow
declarations. It also links the 21 product contract families from the companion
product selection ledger. Identical canonical/current files are outside this
delta inventory.

| Three-way category | Files | Treatment |
| --- | ---: | --- |
| Canonical-only test/support path | 91 | Map each assertion family to the existing current producer, or retain a separate external qualification boundary. |
| Both branches changed an existing test | 25 | Compare canonical additions with the selected implementation and retain missing assertions. |
| Both branches added the same path | 37 | Compare contracts and schemas; matching filenames alone do not establish equivalence. |
| Canonical retained the common baseline; current extended it | 13 | Preserve the current extension and record the unchanged donor identity. |
| **Total** | **166** | **Every row has an explicit disposition.** |

The inventory includes support/probe Python, test documentation and one binary
PPTX fixture. A source-declared function count is not a pytest collection count.
The 184 selected test owners are indexed once to avoid ambiguous filename-only
claims. Workflow references can identify an invocation, a required-suite list or
a required-name assertion; the JSON does not manufacture execution evidence from
those references.

## Concrete gaps found and retained

| Finding | Selected correction | Required existing suite |
| --- | --- | --- |
| The prior CI runner could leave a pytest descendant alive and acknowledge a pass before temporary-report cleanup. | 17f03638286ebfd6e5fce8b5d1d2fe2afefb4fac reuses the current Windows Job/POSIX family owner, settles it before stream/report cleanup, preserves timeout and cleanup failure verdicts, and retains the installed/loopback child blocks byte-for-byte. Three canonical cleanup cases and two actual descendant cases were added. | tests/packaging/test_ci_check.py |
| The bundle builder discarded the verified historical source tree/proof. | 8383676ca800bb247c7b72d6b48f44d8e5901201 seals application-source.json, writes the exact verified source_tree and includes the updated START_V3_UK.txt. Its real Git/RECORD test checks the historical commit rather than checkout HEAD and rejects proof mutation. | tests/packaging/test_windows_bundle.py, test_native_checks.py |
| Read-only format listing lacked the donor's exact CLI boundary assertions. | This candidate adds four incompatible-operation cases and the no-library/no-native-runtime listing case. No product behavior changes. | tests/e2e/test_extended_document_workflows.py |
| The inherited resource-lease release case had been lost. | This candidate checks PID rejection on every platform and performs a bounded real fork branch where available. It retains the actual parent native lock and adds no strict-suite skip. | tests/operations/test_resources.py |
| Current publication tests lacked the donor's full metadata/membership race matrix and legacy watermark backup assertions. | This candidate retains seven actual competing publication cases plus two valid and four invalid watermark cases. The child observer uses the current start_owned_process boundary; no fabricated database result substitutes for a real competing import. | tests/ingest/test_worker_publication_boundary.py |

The last three rows add six test functions, with **19 expected parameterized
cases by source inspection**. That number is not an executed or collected
result. The existing before-launch equal-source competing import test remains;
the new cases supplement it.

The standalone ASR UI/CLI was also correctly identified as insufficient for the
canonical allowlisted assistant audio.transcribe step. The reviewed
8236fa18a526181da70def2442f9bdbeecd5ce72 chain adds that current-service tool and
tests/assistant/test_transcription_tool.py. Its follow-up gate, cancellation,
installed-identity and console commits are pinned in the JSON. It is an explicit
overlay, not a claim that the old 0d7 baseline already contained the tool.

## Disposition of parallel implementations

| Canonical assertion family | Current owner and retained boundary |
| --- | --- |
| Import identity, stopped-worker recovery and V5 publication | Current ingest worker/publication_identity, real child publication races, terminal replay registration, pending backup and independent frozen-attempt tests. Restored launch hints and bounded legacy watermarks cannot become fresh publication authority. |
| OCR languages, ordered chooser and managed models | Current PR98 ordered presets plus Fast/Best manager, exact pack/runtime identity and unreadable/changed inventory refusal. Bounded parameter IDs retain the same assertions on Windows. |
| Speech, pronunciation and source anchors | Existing audio engines/VoiceRegistry and reader/narration with scoped pronunciation, bounded regex, exact legacy reading anchors and explicit inert migration. Old provider or runtime declarations do not activate themselves. |
| Studio, music, DSP, clips and production | Current audio parser/production/managed projects, exact PCM/MIDI, current shared process owner and per-project backup. Model availability and actual neural inference remain separate facts. |
| Translation and provider policy | Current ProviderGateway, LOCAL_ONLY, fixed Basic v2/explicit Advanced v3 authorities, header-only secrets, bounded approvals, accepted receipts and uncertain-charge no-retry tests. |
| Complete backup and saved state | Current typed producers plus historical common-v4/state-v3/v4 adapters; exact schemas, native identities, retained source bytes and inaccessible-root refusal. Foreign approvals/executable settings remain inert. |
| Progress, Tasks, keyboard and Save All | Existing current registry/coordinator/session owners; actual note-transaction acknowledgement, stale-root/generation checks, closed-window settlement, pending worker cleanup and observer isolation. |
| Academic/citation export | Existing shared captured destination approval and final publisher; source attribution, consent before render, newer destination preservation and committed receipt notification. |
| Resource coordination | Current pinned native filelock broker, actual spawned-process contention/crash recovery, preserved fork boundary and explicit CPU/RAM/disk declarations. No claim of measured hardware enforcement. |
| Packaging and installation | Current dev3 source proof, 17/53 dependency profiles, FULL runtime inventory, pre-seal audio admission, current native launcher lease and actual managed installation/update owners. Donor post-seal attachment does not replace these owners. |

Every file-level rationale and exact owner is in the JSON; this table is only an
index. The full product contract selection is recorded separately in
CANONICAL_CONVERGENCE_FINAL_20261127.md and its pinned companion JSON.

## Tests that remain distinct evidence

The canonical private voice probes and optional Linux mount/unshare publication
budget harness target different supplied-runtime or host conditions. Current
protocol/model-inventory tests and real installed SAPI tests do not become
evidence of private neural inference, acoustic quality or the private-mount
experiment. Their exact donor blobs and separate qualification boundary are
retained in this ledger. No private code, weights or recordings were copied.

Most mapped current tests have explicit required workflow references. Tests
under tests/audio and tests/assistant also appear in the current broad audio
regression command; tests/language_qualification is declared as a required
directory. The existing test_runtime_layout.py,
test_backup_assistant_state.py and test_backup_productivity_state.py are recorded
as supporting source tests without an invented direct workflow invocation.
Their contract families also have explicitly referenced current primary suites.

## Later canonical and execution addenda

Canonical a333f0723df107aa60b60b8ee2c5ba9f85b25475 changes only the shared export
writer and its reconciliation test after e3cf. Reviewed current overlay
4932e2bffb1b66edbc55dfe464a9e80933384c1d retains the exact pre-read stale approved
target refusal, before write and after staging. It preserves current native
metadata and final fingerprint checks. This is an explicit two-path addendum;
the reviewed 166-row e3cf baseline is not silently relabelled.

Actual 0d7 SYNC execution exposed an old installed_sync_probe caller that omitted
the required child-origin operands. Candidate
c41c7df72a0c56ba899f32da129e75ba928ca99a corrects that caller and adds real isolated
child-origin tests. It must compose with the unchanged 17f child policy and its
existing tests. The current ledger does not label that new source executed.

This candidate changes only the three named existing test files and these
disposition documents. It does not replace an application stack, move a branch
reference, weaken a runtime pin or relax privacy, redistribution, source
identity, native cleanup or acceptance requirements.
