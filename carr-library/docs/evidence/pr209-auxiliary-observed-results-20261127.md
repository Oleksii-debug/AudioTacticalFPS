# PR 209 auxiliary Windows outcomes

Exact tested source: `f16959c76c1702fbdf6d6c9a6c113b2cb6660cd3`, tree `9b1de961f054691406da899dc5377266c8e17703`.

| Workflow | Actual result | Evidence scope |
| --- | --- | --- |
| Encrypted snapshots | PASS; 322 source, 154 isolated installed, 44 native checks | Real synthetic encrypted publication, verification, restoration and cancellation; actual checkout is the stated PR head. |
| Native launcher | PASS; 19 + 27 source cases, 9 native checks | Actual MSVC build and synthetic-child execution; all seven compiled source hashes independently match Git. No binary artifact was published. |
| Bibliography | PASS; 28 suites / 1,066 source cases and 108 installed native checks | All 1,066 observed case starts reconcile with suite totals; five installed module hashes match exact source and the wheel receipt binds the tested commit/tree. |
| Auxiliary audio | FAIL after 16 native audio checks and 151 effect subchecks | The strict native gate rejected a PR merge/head identity mismatch. Later audio steps did not run. The separately reviewed workflow-only successor requires fresh execution. |

[The JSON record](pr209-auxiliary-observed-results-20261127.json) includes complete observed bibliography function counters, native label counters, module/source identities, decoded-log hashes and artifact metadata. Artifact ZIP files were not independently read here. Source test populations and installed/native selections remain separately identified.

The audio correction supplies the same PR-head expression already selected by checkout. It retains the strict source-mismatch rejection and all test/product/runtime contracts. Its focused push prefix prevents an auxiliary rerun from launching the entire acceptance cohort. The frozen failed run remains historical evidence.

These outcomes do not qualify the final main workflow, standard installer/update, private models, manual NVDA, physical audio or redistribution. No result is transferred to a later source tree.
