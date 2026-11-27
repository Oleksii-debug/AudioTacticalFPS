# Studio role and output controls: observed Windows qualification

**PASS at exact head `aee11f99c4be0da3aa257ac7722923d768220e7f`: 211 source cases, 211 isolated installed cases and all 39 native GUI checks.** The [recorded Windows run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46067792077) completed successfully on 2026-11-27. Its job is `140737213058`; the exact tested tree is `d5bb747a4b29472c9244af103a25d884be3cc48e`.

## What actually ran

| Population | Cases per mode |
| --- | ---: |
| Existing Studio controller and UI regressions | 60 |
| Existing native effect-editing regressions | 19 |
| Existing effect-editing model regressions | 34 |
| New structured role/output model regressions | 62 |
| New current-owner form regressions | 36 |
| **Total** | **211** |

Both complete test populations contain 88 function identities across five modules. The 113 existing cases were retained alongside 98 new cases. Source tests passed in 8.750 seconds; isolated installed tests passed in 8.969 seconds. The runner bound both modes to the same source-built wheel, SHA256 `6b533c1089d559c46b07a8f1148053e11ccb63123b4a67876ba15ce70442353c`.

The native probe used actual wx 4.2.4 MSW / wxWidgets 3.2.8 controls. It exercised named role and final-output actions, exact voice/language selection, unsupported-control refusal, Cancel and Apply, one undoable draft edit, immutable source preservation, Undo/Redo, Preview, Save All, retained unavailable IDs, output-only changes, stale-draft refusal, focus return, initial-load failure cleanup and owned executor/window closure. All 39 declared labels passed. The measured native result contains two applied forms, three cancelled forms, one capability refusal, one stale refusal, two accepted draft edits, two Save All commits and one initial-load cleanup.

## Independent evidence checks

The complete final job log was fetched and its four receipts were parsed. Both exact whole-module/function Counters match the committed oracle. All 39 native labels, required typed fields, seven metrics and nine installed module paths/hashes match the declared contract and the exact fetched source. The six copied test/helper files, three recorded runner files, oracle, adapter, dispatcher and probe hashes also agree. All final job steps passed, including actual outbound blocking, firewall restoration, evidence upload and owned-work cleanup.

The complete receipts, counters, source blob/SHA256 mapping and job metadata are retained in [the companion JSON](STUDIO_ROLE_OUTPUT_WINDOWS_20261127.json). Artifact `15231573716` is 450507 bytes, with GitHub digest `87e650978db47d21c8fc27f414f40620de595aed2f7264de41d2f64dec7c7a1a`. Its run/head binding and metadata were checked. The ZIP was not independently downloaded or unpacked for this review.

## Earlier failure remains recorded

The [first run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46067267518) at `1b884e117c57d0c083c30be76d27da79a14a1133` passed 211 source and 211 installed cases, then failed after 23 of 39 native checks. Its Preview assertion compared wx's LF display representation with preserved original CRLF script text. The separately reviewed successor normalized only the expected widget display line endings and added an explicit exact accepted-script assertion. Product files, persistent source bytes, the test populations and all 39 native labels stayed unchanged. That earlier aggregate remains **FAIL**; this proof records the newly executed successor's **PASS**.

## Scope of this result

The forms use explicit synthetic voice metadata; the result makes no private-model, language-quality, physical-audio or manual NVDA claim. Those statuses remain `NOT_RUN` or `NOT_QUALIFIED` in the actual receipts. This proof binds the exact frozen head above; final composition and packaging retain their separate gates.
