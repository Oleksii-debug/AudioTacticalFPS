# Resonance: composed workflow integration, 27 November 2026

This record describes source integration and its evidence. It does not certify
the whole requested platform, every language, physical microphone capture,
human NVDA operation, an installed neural model, or distribution rights.

## Reused source

| Existing implementation | Exact reviewed revision | Selected boundary |
|---|---|---|
| [PR147](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/147) | `9f8511f895ef134b1f644c398ba965546c4a190b` | Local planner proposals; flat folder selection pattern; explicit Google Advanced profile/provider contracts. |
| [PR149](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/149) | `906aa1a95a8f40ef119682d2d5c805c017c327f4` | Bounded recording, owned process cleanup, retained clips, typed Studio input recovery, measured EPUB media overlays. |
| [PR165](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/165) | `5baee73ae94d860909152fb8c8434d5864c0e638` | Translation of an explicitly selected retained document or range, provenance and account/source revalidation. |
| [PR166](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/166) | `7abb06eef27b761123ae8d489ebbbabaf6a9cf01` | Reject unreadable state directories and stop inventory at the first unlisted restore file. |

These are parts of this repository. The existing Python/wxPython application,
SQLite catalog, provider Gateway, command dispatch, source identities and job
stores remain the integration boundaries. No private owner worker, model,
recording, credential, runtime or commercial policy was copied into public Git.

## Compatibility decisions

The selected transcription service keeps its original workspace ownership,
job schema and limits: 1–64 selected files, at most 8 GiB per file and 32 GiB per
batch. Folder selection is explicitly flat. It creates a normal retained-input
job; recognition is a separate existing action. The donor's alternative ASR
package and broker are not imported.

The assistant keeps the serial background voice discovery and deferred owned
registry cleanup added in this integration. Model proposals pass through the
same typed plan validator and scope checks as the deterministic parser. The
selected local profile receives the instruction text, with no automatic file
contents. A proposal does not create or execute a job.

Studio keeps the selected voice-registry API and owned/borrowed lifecycle.
Clip-only work does not discover voices. Project schema 1 serialization and
fingerprints stay stable; edited clip metadata explicitly uses schema 2.
The bounded cancellable hash helper extends the existing API without changing
its existing callers. Cached production nodes bind executable and module bytes.

State backups retain the selected reading-state and pending-import contracts.
The donor's incompatible `backup_extensions` format is not adopted. Existing
state entries carry verified retained Studio inputs and typed media receipts.
All sealed bytes pass validation before a restore staging projection rebinds
project paths. Historical external paths require fresh review; output folders
require explicit selection. Publication receipts remain inert, including their
`prepared` state. Their external output paths are never opened during backup
or restore. Committed orphan recordings remain preserved and unverified.

The current native Studio output-format and stem controls, Save All support,
export approvals and library-switch checks are preserved when adding the new
dialogs. Recording cleanup callbacks release the holds of their own operation;
a delayed previous callback cannot unmute a new recording.

## Portable execution and native acceptance

The composed clip/edit/recovery/CLI selection passed 74 strict cases: clip
editing 26, dialog handlers 11, clip CLI 2, retained-input recovery 21, typed
media backup 10 and EPUB CLI 4. Synthetic PCM was decoded and mixed through
actual FFmpeg. The recovery scenarios delete the source library, selected
originals and old outputs, restore to a new library, and compare rendered PCM.
These counts describe that exact selected gate and must not be added to
overlapping later workflow counts.

An initial composed run exposed a missing optional cancellation argument in
the existing hash helper and mismatched donor test fixtures. The corrected
run passed every case without skips or expected failures. A separate native
Windows diagnostic reproduced the original UTF-16 rename-buffer bug and
proved the terminating-WCHAR correction; that small diagnostic is not full
application acceptance.

Final installed Windows and transfer acceptance must run against the final
published source and its actual wheel. Required native probes cover the local
planner controls, translation controls, recording controller, clip editor and
EPUB publisher in addition to all existing library and Studio gates. Human
Windows 11/NVDA, physical microphone quality, actual neural model quality and
third-party EPUB reader behavior remain separate qualification dimensions.

## Distribution profiles

The library transfer profile omits FFmpeg/FFprobe until its exact redistribution
requirements are cleared. Audio controls use the existing explicit local engine
settings when the owner supplies an independently reviewed installation. The
full CI audio profile is a test environment, not permission to include those
binaries in a transfer ZIP. Windows SAPI reading remains independent of FFmpeg.
EPUBCheck is a pinned validation tool, not a bundled application dependency.

## Academic comparison and formatting extension

The bounded topic comparison and catalog-quality components from canonical
`966e5ccf892243ead6942c635af5fbab47e72551` were adapted at
`5ce6446a19929cc8a14834a348f404409e1bc214` and selected as `b51cb5e`.
They use existing evidence, revision, note, tag and metadata-editor contracts.
Only two new menu actions are installed; the existing registry makes both
available to command search and shortcut configuration without assigning a
new conflicting default key.

The existing Node/citeproc-js formatting controller now accepts an explicitly
chosen local CSL file through its existing bounded style loader. DOCX export
and regeneration retain style-byte provenance. No alternate citation backend,
new dependency, schema or second outline exporter is introduced.

The component passed 396 strict source cases and 163 installed Linux cases;
these overlapping sets are not cumulative platform acceptance. Six portable
suites, explicit installed-module origins and two native probes are required
in the final Windows workflow. See [academic workflows](ACADEMIC_WORKFLOWS.md)
and the [requirement evidence](RESONANCE_REQUIREMENT_STATUS_20261127.md).

## Retained document formats

Three bounded extractors from canonical `966e5cc` are unchanged at component
`7141297a20c00daa407f5a80d1fa7c9944a8657a`, selected as `2dbcffc`. Existing
intake, retained originals, import requests/attempts, source review, search,
Reading Desk and pending backup now recognize ten additional families. The
publication verifier keeps its exact receipt, configuration-size, page/index
and source identity checks. Metadata-superseded replay does not reactivate an
older source. No schema or dependency lock is changed.

The component passed 707 distinct source cases and 326 installed Linux cases,
with no skips or expected failures; these sets overlap. The Windows composition
uses one lowercase [format guide](document_formats.md), preserving the separate
conversion inventory, because two tracked names differing only by case cannot
be retained on the target filesystem. The existing native Import Center probe
keeps its source invocation and has an explicit installed mode that refuses
checkout modules; full Windows acceptance includes this mode and all four new
strict suites. The synthetic XLSX interoperability fixture is original test
data, not a user document or executable dependency.

## Resolved exact Windows acceptance failures

Published candidate `e2567344c17c6671ff5081b39629bf6e58bff0f4` passed its
complete [V3 workflow](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45958710380).
Mandatory installed recovery passed 316 cases without skips; native SAPI, Studio,
CSL and atomic conversion passed. Its broad exploratory sweep had 1,458 passes
and 22 platform skips, which are not counted as mandatory coverage.

The separate [Desktop workflow](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45958710545)
failed. The complete required source gate ran all 216 selected suites in 1,157
seconds: 214 passed and two failed. One final-page search fixture still requested
schema 4 after the application had moved to schema 5. Two PDF timeout cases
expired before their controlled commit markers. These failures are retained as
failures, rather than attributed to retrieval or publication without evidence.

The schema fixture now uses the existing `CURRENT_SCHEMA_VERSION` API while
retaining all 10,001 rows and final-page/copy assertions. Its five-suite source
gate passed 241 cases and the installed federated suite passed 37. Publication
tests use the existing owned-process `on_started` hook to wait for the actual
commit marker before arming a real 0.25-second timeout, with a bounded 30-second
startup interval. Both PDF cases deliberately exceed the old three-second
startup assumption. The corrected related gate passed 116 cases; restoring the
old deadline reproduced precisely the two original failures. Production code
and the original receipt/rollback assertions are unchanged.

The Desktop full-bundle step stopped because Windows rejected the attempted
complement-address filter. The repair keeps every selected runtime's outbound
`Any` block rule active throughout the protocol phase, with exact readback before
and after. Both Python callers use the same existing numeric-loopback guard.
A focused [Windows 2022 diagnostic](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45963008240)
at `9877d694d333bbb0447b64efcde85c3d4a03e959` completed a real owned parent/child
127.0.0.1 exchange while both Python program rules remained unchanged. It made
no external connection attempt and is not application acceptance. Original
diagnostic artifact SHA256:
`c926733111388644989ee1d26fdd2c2dfb8bb95f6bbd65c00589ca9da8c74fe0`.
The [NetSecurity address-filter contract](https://learn.microsoft.com/en-us/powershell/module/netsecurity/set-netfirewallrule)
provides the retained `Any` setting; no new remote-address exception is added.

The expanded 226-suite source gate has a 30-minute step budget based on the
measured previous 19m17s run. Each suite retains its existing deadline and strict
zero-failure/zero-skip policy. Both complete workflows, all installed native
features, original wheel/proof captures and the separate no-audio transfer
installation still have to pass on the final source. Earlier V3 or diagnostic
success is never substituted for that final result.
