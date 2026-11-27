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
