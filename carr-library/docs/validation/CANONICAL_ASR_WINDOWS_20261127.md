# Canonical earlier ASR recovery — actual Windows qualification

The exact frozen candidate **763bb37a227b9dbe4802e2884bc236fff37459fe**
(tree **74e6089db0a455907cbe24c1d22fa6a0c0505cc6**) passed its focused Windows gate.
This result applies to that source and its measured installed wheel. It does not
qualify a later combined application tree.

| Actual phase | Result | Measured population | Harness elapsed |
| --- | --- | ---: | ---: |
| Source execution | PASS | 268 cases | 82.531 s |
| Isolated installed execution | PASS | 268 cases | 79.344 s |
| Actual native controls/current preparation | PASS | 53 exact assertions | See raw native log |
| Complete receipt gate | PASS | All three required phases and exact Counters | See raw job log |

Run: [46013756502](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46013756502).
Job: **140555538914**. Frozen ref:
`carr/legacy-transcription-r9-population-fix-20261127`.

Wheel SHA256:
`8f7b8519e07a9f88ff4d71b29669b2ce8c47cce5741e6332fa565cf858566d55`.

Artifact **15204754656**, **395482 bytes**, GitHub-reported SHA256:
`8e585dfb627606b36ecc00c17d79f9dfe7c97853fb46d5b7a6ec9470f4733793`.
The digest is taken from GitHub artifact metadata; an independent local ZIP hash
calculation is not claimed. The companion JSON preserves all four actual
source/installed/native/aggregate receipts and exact artifact metadata.

## What was exercised

The focused population retains the existing workspace, supplied-worker and earlier
canonical ASR families. It covers passive capture, typed backup/restore/rebackup,
current explicit preparation from a selected earlier source subset, old completion
as historical data, exact seventeen current settings and runtime manifest binding,
current native history choices, saved reading derivatives and create-only CLI export.
The actual native probe prepared a current immutable job from independent retained
synthetic source bytes, exercised its Cancel path, and verified that Start remained
a separate operation.

No recognizer model or old runtime was launched by the native recovery probe.
Recognizer inference and NVDA are **NOT_RUN**; speech quality is **NOT_CLAIMED**.
Synthetic protocol-worker regression cases in the test populations verify their
existing worker contracts, not real recognition quality.

## Preserved failed evidence and exact population correction

The earlier frozen run
[46009009552](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46009009552),
job **140539398233**, executed **268 cases per lane: 267 passed and one failed**.
Its source was `e0dcac834abb46bda55a9d662674d3638188d682`.
The repeated `history-export` regression found that the shared writer default
could replace an existing target. The exporter now requests atomic create-only
publication when no explicitly captured destination approval is supplied. The
regression protects both an identical existing export and a different user file.

The first oracle incorrectly **declared 265** cases because
`tests/ui/test_transcription_v43_dialog.py::test_shared_profile_selector_preserves_explicit_scope_and_contract`
uses `range(3)), which had been counted as zero. Actual execution already ran
all three cases. The corrected exact oracle requires 268 in each lane and keeps
all selected tests and all 53 native labels. This is a corrected expected
population, not an omitted test, weakened assertion or transfer of an earlier PASS.

The first failed artifact is **15203941780**, **387699 bytes**,
GitHub-reported SHA256
`a6605737b3caec279fa0fb943a131348ed01a1574cde5fafd330b9d97cf36cd1`.
Original failure evidence and frozen refs remain unchanged.
