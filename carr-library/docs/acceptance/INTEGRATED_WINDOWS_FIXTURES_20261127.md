# Integrated Windows fixture correction — 2026-11-27

## Observed execution

Executed source: `5f7ce89b5ee3ded5e5c5efa96fb08421cf36e36d`.
[MAIN run 46053614759 / Windows job 140690731540](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46053614759/job/140690731540) completed with FAILURE at 19:44:01 UTC.

The full terminal log records 392 required suite executions: 9,627 passing cases, 3 failures and no skips. The early stages contributed 153 suites / 3,524 passing cases; integrated source contributed 239 suites / 6,103 passing cases and the three failures. The combined observations include two explicit loopback suites / 45 passing cases and must not be presented as unique tests. The native spelling flow passed 43 checks. Later full-bundle and standard-setup steps did not run. The source step completed before its 30-minute limit; this was not a timeout failure.

[Failure artifact 15226969083](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46053614759/artifacts/15226969083), 260,019 bytes, SHA-256:
`2ef708a8799c6695afbe1bff58b0a6710450346c799b5a72d45861a89dd21bcb`.

## Exact causes and bounded corrections

| Failing case | Observed cause | Correction and retained assertion |
| --- | --- | --- |
| `tests/packaging/test_offline_runtime_policy.py::test_ci_child_opts_out_before_fake_runtime_import` | The temporary project copied `ci_check.py` but omitted `src/carr_library/audio`. Its actual native process owner failed with WinError 3 before the fake ONNX runtime policy could run. | Copy the exact four existing standard-library owner files, as the established `test_ci_check` scaffold already does. Inherited telemetry opt-ins, fake-runtime import identity, legacy disable API, isolated child, 30-second limit and exact JUnit result stay unchanged. |
| `tests/e2e/test_source_file.py::test_cancellation_is_rechecked_at_the_native_launch_boundary` | The observer accepted only positional arguments, while the actual `_view_matches` call now passes `cancelled=stopped`. | Accept and forward keyword arguments to the real check, then set cancellation at the original boundary. InterruptedError, no native opener and unchanged original bytes remain required. |
| `tests/reader/test_reading_desk.py::test_noncontiguous_physical_locations_use_reading_order_not_fake_page_numbers` | After a real PDF import, the fixture replaced the extraction config with raw `synthetic-extraction`; ReadingDesk rejected it during JSON decoding before page-order assertions. | Copy the exact config from the successfully imported revision into the sparse revision. Physical pages 5/9, reading order, printed-page and quotation-authority assertions stay unchanged. The production parser is not relaxed. |

The low-level storage primitive accepts nonempty text; the production PDF/TXT producer serializes canonical JSON. Its current and reviewed canonical implementation is identical. DOCX, EPUB and extended producers also serialize extraction identity. The sparse fixture is corrected to follow that real producer contract.

Reviewed candidates, each based on `8a764003da21ceca4e6dd0206165763b639ea7f4`:

- `a1834375d863b623c8c720bae19e2002ba47c78c`: cancellation observer, two changed test lines.
- `354aa860035ce6f510c6c3fc44e379753fb94f16`: actual native-owner scaffold, six added test lines.
- `124c06d189d4618fa7546b3ba949770f88122d2e`: exact imported extraction config, three added / one removed test lines.
- `7ba09b6c7f79fe5f88fcf97e4ae09bc6a026cf83`: the three affected modules in both existing Fast source and exact-installed selections.

## Required execution and evidence boundary

The Fast matrix retains both operating systems, all previous selections, network/origin/native-owner guards and the loopback policy. It now selects 22 source suites and 21 exact-installed suites; the two loopback suites are unchanged. The three added modules use dependencies already present in the existing test/sync environment, have no skip markers and require no optional OCR, voice or model installation.

At preparation of this source record the composed corrections are NOT_RUN. Require the actual new Fast source and installed results on both operating systems, followed by the complete integrated Windows run including its full bundle and standard setup. Keep executed commit, source tree and artifact identities explicit in PR99.

These corrections change three test fixtures, one existing workflow selection and this evidence note. Application source, runtime owners, parsers, storage schemas, dependency profiles and time limits are unchanged. Earlier completed product runs retain their exact executed identities; they are not relabeled as new executions.

The already completed receipt correction, Audio, managed update, publication, corpus and large-source results remain recorded separately in [PR99](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/99). Manual NVDA, physical audio, owner-model qualification, production signing and distribution clearance remain separate acceptance.

## Follow-up review of the full-bundle execution order

After fixture composition, [Fast run 46060179784](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46060179784) on exact `78b2883c292002dae2d800960028b240617b098f` completed successfully on both operating systems: 22 source suites / 458 passing cases, 21 exact-installed suites / 418 passing cases, and two loopback suites / 45 passing cases, without failures or skips. Each mode includes all 60 cases from the three corrected modules and all 30 receipt cases. These are separate execution observations, not unique-test totals.

A narrow source review of the still-unexecuted final full-bundle step found two further workflow defects. These findings were proven by source inspection, not reported as executed failures:

1. The installed local-translation loopback command preceded the existing offline installation of pytest/reportlab. The production installer creates an isolated virtual environment and installs the sealed production lock; that lock contains neither test package. The required CI child imports pytest. Candidate `2cd9be24a3870daf9508601fd260ec5f9ee71f71` moves only the unchanged two-line invocation/exit-check block after the already existing successful offline test-dependency installation, inside the same firewall-controlled step.
2. `verify_application_wheel` already returns `source_tree`, while the final workflow receipt also supplied it explicitly to `dict(..., **proof)`. The same candidate retains the independent Git tree lookup, asserts that `proof['source_tree']` equals it, and emits the field once. Source provenance verification is preserved and made explicit.

The candidate changes only `.github/workflows/carr-windows.yml`; exact inverse reconstruction verifies that all other workflow bytes are unchanged. The composed follow-up also appends this evidence note. Application and test source, dependency locks, production profiles, suite selections, installed-origin checks, numeric-loopback restrictions, firewall boundaries and time limits are unchanged.

At preparation of this follow-up, its full installed/native bundle and standard-setup execution remain NOT_RUN. Require the complete MAIN run on the composed follow-up source. The already completed exact 78b Fast results retain their actual source and artifact identities.

## Trust idle-renewal observation fixture

The automatic [4b Trust Windows run 46061873348 / job 140717824702](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46061873348/job/140717824702) recorded 216 PASS, 1 FAIL and no skips in its 11 required source suites. The only failure was `tests/security/test_idle_renewal.py::test_actual_idle_host_renews_without_clicks_and_stays_off_until_explicit_opt_in`: the direct read for sequence 3 encountered the valid nonblocking `.trust.lock` held by the next periodic host tick. The native vault/UI steps did not run. The Linux source lane independently passed all 217 cases.

Failure artifact: [15229660527](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46061873348/artifacts/15229660527), 9,543 bytes, SHA-256 `239bdade6ff3b4d591523cd8235550b46426ca165546b8b4260c2b76364ab799`.

`RefreshWorker.running` is a point-in-time observation. The still-active host checks every 20 ms and may acquire the lock for `store.current()` after the preceding worker observation. The store's existing context-managed release remains intact; this failure does not establish a leaked lock.

Reviewed candidate `823216ec0463fb12cfb5c2c5d46ad6a22678e490`, based on exact 4b, changes only this test. A small snapshot observer uses the existing two-second `wait_until`, checks the worker state, and retries only `BusyError`. It returns the first successfully read grant; it does not wait for a desired sequence. The direct sequence 2/3 and exactly-two-request assertions remain, as do disabled-by-default operation, explicit idle opt-in, background-thread identity, no additional UI action, live cadence and shutdown assertions. Other errors propagate.

At preparation this fixture correction is NOT_RUN and requires actual new Trust source execution on both operating systems, plus the Windows vault/native steps. Application code, locks, the host/worker lifecycle, build inputs and all workflows remain unchanged.

The MAIN workflow has no security-test selector and its reviewed full source log contains no idle-renewal fixture. The selected complete 4b MAIN execution therefore retains its unchanged product, build, workflow and selected test inputs. Its eventual result must remain labeled 4b; the corrected Trust result must identify its own actual source. No later head is credited with an unexecuted whole-package PASS.
