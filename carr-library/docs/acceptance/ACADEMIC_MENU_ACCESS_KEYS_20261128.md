# Native menu access keys and academic acceptance — 2026-11-28

## Observed failure

Executed source: `aa36310a44b8864c2e7704accb69f1a092823e17`, tree `fcad746cb5e221f1b2270577e656ccb92e05d62d`.

Required Windows run [46086137827](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46086137827), job `140796034292`, passed 153 strict suites / 3,538 cases with zero strict failures or skips before failing the native academic comparison step. These counts cover the completed stages; the later 239-suite integrated source block, full bundle and standard setup did not execute.

The complete terminal log (1,258,118 characters) records two successful TXT imports and a successful main-command shortcut uniqueness check. The next assertion at `native_academic_comparison_smoke.py:169` fails: `main menu access keys are distinct`. The failure is deterministic source composition: `install_api_connection_actions` installs `Se&ttings`, while `install_assistant_actions` installs `&Tools`; both claim Alt+T.

The same Windows run independently passes the previously corrected installed document probe (27 checks) and installed reading-copy probe (26 checks). The companion native job succeeds with 300 strict checks and 13 native PASS reports. The other eight execution groups also succeed on this exact aa36 source; they are not evidence for an unexecuted successor.

Failure artifact: `15240720416`, 139,237 bytes, SHA-256
`63f1d139292d38593284b17743e44e4e5a68516b3c230d2052fd538b14fbbd3d`.
Its filename contains GitHub's virtual PR-merge identifier `e6674eb1`; the actual checkout and artifact workflow source are aa36.

## Product correction

Change the Settings mnemonic from `Se&ttings` to `S&ettings`: Alt+E selects Settings and Alt+T selects Tools. The visible menu title, semantic Settings identity, actions, handlers and registered command shortcuts are preserved. The existing menu constructors remain the owners of their own labels.

The current top-level mnemonic allocation is:

| Menu | Access key |
| --- | --- |
| File | F |
| Library | L |
| Research | R |
| Bibliography | B |
| Academic | A |
| Help | H |
| Source | S |
| Reading | D |
| Audio | U |
| Settings | E |
| Tools | T |
| Options | O |

The actual native global uniqueness assertion remains unchanged. The focused portable regression exercises the real Settings and Tools installer functions through their existing composition boundary.

The added `MenuMnemonicTests.test_actual_settings_and_tools_keep_distinct_access_keys_and_dispatch` case lives in the already selected `tests/ui/test_api_connections.py`. It uses headless menu-protocol doubles with the two real installers, checks the reviewed twelve-title composition and distinct access keys, retains the Local assistant Ctrl+Alt+J action and executes both bound callbacks against their intended patched dialog handlers. All four original controller tests remain. The unchanged old Settings label would recreate the Alt+T collision in this case. The separate native test still checks the complete actual wx menu.

The Ukrainian start guide contains no obsolete Alt+T Settings instruction requiring an update. Runtime behavior beyond this menu label is unchanged by the product edit.

## Exact current academic inventory

The immediately following probe assertion expected six menu items. The host now registers three actions, including DOI acquisition, alongside the four academic workflows. Replace the old numeric count with sorted-list equality against the seven exact native captions:

- Academic library...
- DOI lookup and open-access sources...
- Add selected book to academic library
- Compare sources in active topic
- Export research outline with subtopics
- Import bibliographic metadata from RIS
- Check catalog quality

List equality preserves multiplicity and rejects missing, extra or duplicate actions. The original four workflow actions remain required. All 44 literal check labels, the native menu events, modal interactions, focus/draft/source-preservation checks, 12-second waits and 60-second watchdog remain byte-identical outside this one inventory block.

The source owner review covers the existing probe's main scope action, evidence Save/focus, comparison, catalog quality/metadata and RIS confirmation flows. It found no additional current-owner mismatch. A bounded read of the next six late native probes found no further instance of the same stale parent, keyboard owner, exact menu count or decorated-caption lookup problem. Those reads are source review, not runtime acceptance.

## Early installed replay

Add one six-line step to the existing native-command job immediately before its existing source regressions:

```powershell
& $env:CARR_PYTHON -I -B tests/ui/native_academic_comparison_smoke.py --require-windows 2>&1 | Tee-Object 'keyboard-results/native-academic-comparison.txt'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

The job already installs the exact application, core dependencies, wx and its test-only dependencies and creates the evidence directory. This invocation uses normal installed imports with Python isolation and requires native Windows; it has no source override. The existing late academic invocation remains.

The new step retains the established three-minute step budget. The native job remains 12 minutes; the probe remains 60 seconds. All prior workflow commands, selectors, assertions and deadlines are unchanged. Removing only this six-line insertion reconstructs the aa36 workflow byte for byte.

## Candidate provenance and validation boundary

All candidates descend directly from the observed aa36 head and were read back independently:

- Product and focused regression: `5c67f9e734c73e53edddf1e5bd14fc8955c9e913`, tree `236fc8287e9d9e392b632e41cea69ad228aaf731`; exactly two paths.
- Exact academic inventory: `f4d14b87edb7ed1724d967ec3b4ddfd1f916b59c`, tree `f9a4185a52e39a2175b85c4a85aa0a8f8c0a1800`; exactly one probe path.
- Early installed invocation: `37f24255b38b96fc2b0114da01cc815d350566a4`, tree `d75c7a020cdc3db81ac223e35b3221c48def1e13`; exactly one workflow path.

Root and an independent peer verified the inverse changes, unchanged behavioral assertions and current owners. Their composition contains only these four existing paths plus this evidence note. The only application source change is the single menu-mnemonic literal; no dependency, version, storage, provider or processing code changes.

The version remains `0.2.0.dev3`. The 17-core / 53-semantic profiles and pinned FULL FFmpeg 9.1 archive and executable identities are unchanged.

This note records the observed failure and the reviewed corrections. Corrected actual installed academic acceptance, the complete required current-source execution matrix, the full installed bundle and standard setup remain PENDING at this commit. Subsequent results and exact artifact identities are recorded in [PR #99](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/99) against their actual executed commit. No runtime PASS or release qualification is inferred from this source review.
