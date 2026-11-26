# Remaining integration after the second executor interruption

This supplements CONSOLIDATED_CHECKPOINT_20261126.md. Application source in
this recovery branch remains the exact 119ae601 checkpoint; the menu patch
artifact is retained separately and does not apply itself.

## Current public integration

The latest observed PR103 head is
`1b939d6f21c31940eecf45192193b9843b2f331f`. It follows
`64300dfe4858968e20345a5f2f052a283de0a521`, which added translation,
windowed DOI exchange, further progress/player handling and delivery contracts.

On 64300df, native SAPI/FFmpeg/CSL/conversion smoke passed again. Its service
selection had **747 passed, 20 skipped, 1 failed**. The failure came from a
test guard intercepting CPython's fixed Windows host-version command during
packaging-marker initialization, not execution of an untrusted bundled payload.
1b939d6 initializes that cached marker snapshot before installing the existing
payload-execution guard. New exact-head verification is still required.
Preserve this latest public work when integrating the consolidated checkpoint.

## Academic port

The isolated branch `carr/fd647-academic-integration`, based on
`1e1f31cafb5eb41cb3975fa90b16a6000e60693f`, holds an uncommitted academic-only
port from PR97 `cf8bffa3d89be7cce6ee412957e03c031296cc0c`, reviewed against PR84.
It includes source comparison, outline export, catalog-quality review and RIS
preview/application. It excludes the alternative audio/provider/assistant and
backup implementations.

The worker recorded **173 focused cases passed in 8.57 seconds** and
**383 existing regression cases passed in 14.88 seconds**, plus clean diff/AST
checks and independent menu registration review. These are local source checks;
native Windows/NVDA were not run.

Keep its cancellation callback through the metadata transaction, partial-field
selection, aggregate metadata byte preflight, shared read deadline, idempotent
menu installation and four semantic commands. The comparison shortcut is
Ctrl+Alt+M. A final optional late-cancel dialog patch failed before applying.

There is no exact final academic diff in retained tool output. Its worktree is
the source of truth; recover and export its exact state before changing it.
Do not reconstruct an invented snapshot and call it tested.

## Dordiai adoption and audit

The branch `carr/fd647-dordiai`, based on
`d89db7d3f43623e36bb5cccb4a2b2d215044f9f9`, contains an unfinished no-commit
cherry-pick of `b570e69b4a5a72524878d26cc343a949c677e67e`.
The source-wheel fixture and both workflows were restored to the parent.
Conflicts remain in pyproject.toml, speech/runtime.py and ui/speech_dialog.py.

Preserve native COM SAPI, include_powershell=False, guarded source/voice candidate
application, current package entries and nonblocking reader cleanup. Add the
integrations package and optional dordiai hook without restoring the older
blocking UI teardown. Preserve the newer primary progress/player-muting changes.

Read-only audit found these concrete gaps, which have not yet been regression
tested or fixed:

- The worker process has no owned process group/Windows job; cleanup kills only
  the direct worker. A legitimate helper descendant can survive cancellation.
- Installation hashing occurs before the utterance deadline and lacks
  cancellation, cumulative byte bounds and a bounded final identity pass.
- Voice/language expansion occurs before the registry's 10,000-item limit.
  Bound inventory strings, memberships and expanded pairs before allocation.
- Direct adapter path validation needs raw and resolved UNC/device-path rejection.
- Fingerprints do not cover dependencies of an overridden external interpreter
  or arbitrary native assets outside the documented inventory.
- ProductionService needs disposal of registries it owns, while preserving
  injected registries. Studio should dispose after its pending worker exits.
  Language inventory needs finally-close. Rejected, cancelled and superseded
  reader discovery results also require owned-provider disposal.

No private runtime, voice samples, model weights or private source were accessed
for this audit. Synthetic protocol regressions are not acoustic validation.

## Native speech probe followup

The branch `carr/fd647-native-sapi-probe`, based on 4889473, has a completed
source edit to scripts/windows_sapi_probe.py but no completed followup test run
or commit. The intended behavior requires actual NativeSapiProvider discovery
and non-silent PCM, and records PowerShell 32/64 checks separately as optional
compatibility diagnostics. A failed optional host is not retried repeatedly.
Native timings must not invent PowerShell phase evidence.

The test-file edit was pending when the executor failed; inspect whether
tests/speech/test_windows_sapi_probe.py was written. The planned regressions
cover mandatory-native failure/success, optional failure, absent optional hosts,
constructor/location failure, silent/truncated PCM and discovery-only labeling.
No followup acceptance result is claimed.

## Required next integration step

Restore stable execution access, inspect all worktree/index states, fetch current
public heads, reconcile without dropping parallel changes, and run the required
source/native/installed-bundle gates on the exact resulting source. The repository
checkpoint and patch artifact preserve substantial work, but the final integrated
application and updated full V3 acceptance are not complete.
