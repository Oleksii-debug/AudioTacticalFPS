# Incoming canonical 7e8f9d2 versus old e5c64a5 and current 87d1

**Read-only structural census. No merge or runtime qualification is claimed.** The branch `carr-library` was last observed at old `e5c64a5` at **10:54:25 UTC** and first observed at incoming `7e8f9d2` at **10:59:17 UTC** on 2026-11-28. The incoming commit's 08:46:36 UTC committer timestamp is commit metadata; it does not identify the time of the branch move.

| Role | Commit | Complete Git tree | Files |
|---|---|---|---:|
| Old canonical | `e5c64a5245583f8f8b404a0f042f6ca438c24d7f` | `344567db5c0d99f5d6d7ad24c105645ea75b65e8` | 910 |
| Incoming canonical | `7e8f9d2ef6922bbd7a3fcf02996860ebc57e273f` | `7db1dac1a3a04872faafe6934b195dd46d45f9aa` | 1,348 |
| Current candidate | `87d1ec6a05963531c5c1907f74884faddac2093e` | `f28841240a6d5163e6d8e1bdea4e8117b30650e2` | 1,833 |

All three recursive tree reads were complete. There are no Git submodule entries or mode-only changes hidden by the blob comparison.

## Exhaustive incoming change set

Old canonical to incoming has **1,002 changed paths: 620 additions, 200 modifications and 182 deletions**. Another 528 old paths retain the same blob. [The complete JSON census](canonical-7e8-vs-e5-and-87d1-census-20261128.json) records every changed path with its old, incoming and current Git blob identities, plus the full incoming/current path union.

| Relationship to our current tree | Paths |
|---|---:|
| Incoming bytes already identical to current | 214 |
| Incoming deletion already reflected in current | 156 |
| Both sides added the same path differently | 218 |
| Both sides modified an old path differently | 118 |
| Incoming-only addition absent from current | 253 |
| Incoming modification while current retains old canonical bytes | 16 |
| Incoming deletion of a path we modified | 21 |
| Incoming deletion while current retains old canonical bytes | 5 |
| Incoming modification of a path we deleted | 1 |
| **Total** | **1,002** |

The **358** different same-path additions/modifications and opposing deletions are structural overlap candidates. This review did not run a three-way merge and does not call all 358 textual merge conflicts.

Across the entire incoming/current union, **719 files share the same blob**, **375 same-path files differ**, **254 exist only in incoming**, and **739 exist only in current**. The application-package subset is **430 incoming files versus 425 current files**: 229 identical, 143 different, 58 incoming-only and 53 current-only. These counts include package data and documentation, not just Python modules.

The complete ordered changed-path array has compact-JSON SHA256 `eeaa571c9da38f30d973a91ecd8d6ff1f8239c7f4b9573ae212b6272fe672141`. The full delta rows, using recursively sorted object keys, have SHA256 `14cf0ec8487e01e3c33f8650e7829762bc6afe6383642ff665b2b73521b90c1d`.

## Ancestry and predecessor coverage

GitHub's exact compare metadata reports old canonical to incoming as **ahead 160 / behind 0**, with merge base `e5c64a5`. Current `87d1` and incoming are **diverged**, with incoming ahead 110 and behind 343 relative to current; their reported merge base is also `e5c64a5`.

Incoming is a descendant of the earlier owner `078d254`, but it is **not** a descendant of our reviewed R6 `d9db9f6`, R7 `523aa40`, or `f77cdd4` candidates. The JSON retains all six exact compare records. Shared bytes and shared deleted paths demonstrate selected overlap; they do not establish that incoming contains our completed predecessor line or all equivalent behavior.

## Concrete integration hazards

The 21 incoming deletions of our changed paths include `durable_state.py`, the native-launcher workflow, current release-readiness documentation, and the model-identity, audio-acquisition, atomic-state and numeric-progress regression files. Five additional incoming deletions affect unchanged current old-canonical files, including `tests/conversion/fixtures/ordered_slides.pptx`. The one incoming-modified/current-deleted path is `tests/ui/test_transcription_dialog.py`. All exact paths and blob identities are in the JSON; this census authorizes no deletion.

The 16 incoming modifications where current still equals old canonical include five application-package paths: `__init__.py`, `reader/reading_desk.py`, `storage/README.md`, `storage/revisions.py` and `ui/command_registry.py`. They require actual behavior review; equality with the old base is not evidence that an incoming change is unnecessary.

Incoming has **10 workflow files** versus current's **49**, and **521** files matching `tests/**/test_*.py` versus current's **569**. These are file inventories, not executable case totals or PASS claims.

## Version, main workflow and reader contract

Incoming `__init__.py` and `pyproject.toml` both declare **0.2.0.dev3**; current declares **0.2.0.dev2**. Their exact source blobs and SHA256 values are retained in the JSON. Any source-version selection must be reconciled with qualification and artifact-reader bindings before another release claim.

**None of the 21 current main-reader pinned files match incoming.** Seventeen are absent; the four differing paths are:

| Path | Current Git blob | Incoming Git blob |
|---|---|---|
| `.github/workflows/carr-windows.yml` | `6a826f25a0a4724b5665b3cfbcf34ab97336971d` | `22eb918d025c879295ca771ae53deb541981bdd4` |
| `carr-library/scripts/ci_check.py` | `2d04b9b269ae85c24349ae8d4ad9b295737866fb` | `a384145f94331dc4571c3c7c59c625618f91e62c` |
| `carr-library/tests/loopback/test_local_planning.py` | `052f67e286ad8fd6eef5ca3274575dd6c177e9ab` | `ce655ba00de7e36bf4e73fdf0a409c1bfb620992` |
| `carr-library/packaging/source_wheel.py` | `bba3d0cda29b475d51347434d832af9187840547` | `478de248451d59a44b35784eb808fb2974f0af67` |

The incoming main workflow is 136,318 bytes and declares a 150-minute job with a 30-minute integrated-source step; current is 190,999 bytes with 360/90-minute bounds. They have different qualification graphs. A metadata-only reader retarget cannot qualify the incoming tree. No timeout, population, pin or reader behavior is changed by this record.

## Method and limits

The census uses immutable complete Git trees. All 1,002 changed rows and every incoming/current file status are retained; missing files are explicit nulls. Selected version/workflow source bodies were independently read and hashed. The review did not fetch and hash every application body, execute a merge, run tests, infer semantic equivalence, or transfer any historical PASS.

This two-document evidence commit has sole parent `87d1` and preserves all 1,833 baseline files. No application source, workflow, test, oracle, ref or CI run is modified. Incoming audio, ASR and Studio behavior review is a separate next task.

Machine-readable JSON SHA256: `f00914515a78c859910de898216d6ac0a80e676fbe390c6905afa1178d4dce00`.
