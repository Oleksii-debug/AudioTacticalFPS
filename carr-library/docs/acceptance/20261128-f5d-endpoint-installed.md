# f5d endpoint and installed-bundle evidence — 28 November 2026

**The complete evaluation failed.** It established real Windows playback and successful Book resume on this exact source, but the AudioPlayer assertion, unfinished desktop probe, and shortcut comparison still prevent full acceptance.

This record binds commit `f5d2481a72ca96c5ac28fa171ff88bf61c7600a3`, tree `80669c406f04efa56f7a4e9dac66f7a7ac1665ea`, [run 46120832661](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46120832661), and job `140906613810`. The job ran from **07:39:22 to 08:40:19 UTC** and concluded **FAILURE**. The evidence is stored on source parent `11a1eaace94a4b48d1f104c029a0cc2f02d0117d`; none of these results qualifies that parent or a later candidate.

The [machine record](20261128-f5d-endpoint-installed.json) contains all 67 selected text members other than the large installer log, metadata and SHA proofs for all 68 selected members, the actual run and job records, and the exact completed exit inventories. The [separate installer log](20261128-f5d-inno-install.log) preserves its original UTF-8 BOM and CRLF bytes.

## Results and their scope

| Observed scope | Actual result |
| --- | --- |
| Installed-source comparison | 341 installed Python counterparts matched the checked source |
| Strict source reports across this job | 23 reports, 988 passes, 0 failures, 0 skips; overlapping scopes |
| Union source controls | 313 passes: 288 early controls plus 25 production-admission controls |
| Endpoint refusal controls | 58 passes |
| Main source-tail phase | 4 reports, 287 passes, 0 failures, 0 skips |
| Signature JSON serialization | 10 cases in Windows PowerShell 5.1 and 10 in PowerShell 7; deliberately stubbed trust |
| Actual endpoint setup and selection | Valid package signatures, 2 WinMM outputs, successful format queries and distinct endpoint identities |
| Early sealed native inventory | All 16 entries completed with exit code 0 |
| Later sealed native inventory | 19 completed entries: 18 exit code 0 and AudioPlayer exit code 1 |
| Desktop native entry 20 | Started; no completed exit before the unchanged 35-minute bundle-step limit |
| Eight subsequent main-bundle checks | Not run |
| Independent transferable bundle without audio payload | 335 passes, 0 failures, 0 skips in 278.35 seconds |
| Standard setup lifecycle probe | Setup ran successfully; subsequent shortcut comparison failed; Repair and Uninstall were not reached |

The source figures are subsets or repeated invocations. They must not be added into a single distinct test count. In particular, `test_playlist_recovery.py` supplied **23** passes. The copied integrated job's four-suite source tail does not execute the six separate bulk-source matrix jobs.

The actual package receipt identifies driver **3.5.2.0**, two output devices and successful 16 kHz mono 16-bit format queries. `install_flags=4` is the noninteractive installation flag; the separate registration and update restart flags are both zero. No reboot, trust-store change or security-policy change was reported. The public output inventory and explicit silent-selection receipt binds two distinct endpoint ID hashes:

- `40229bf73f4606fdad92c466cfe1b258193a1e4387af79e69b1ec08825434918`
- `3d44b4b50fa211847364cefc31c01ec2dcab951fa9ca5a84e73f8e8877698116`

That selection check did not play audio. Subsequent unchanged native probes supplied the separate playback proof: ReaderPlayer completed **116 fresh / 120 sealed PASS lines**, and EPUB recorded-audio playback completed **50 fresh / 54 sealed PASS lines**. The sealed counts include additional origin checks. These are reported check calls, not independent test cases or physical listening observations.

The complete native V3/SAPI receipt contains **16 whole-probe check groups**, including two books, three parts, two explicit Run confirmations, exact hash-and-mtime reuse, preservation of original sources and completed native owner cleanup. The first retained verified Book result was observed at **10.406 seconds** in this scenario. This is a measured scenario result; it does not isolate a general performance cause.

## Failures retained

The fresh AudioPlayer chain failed at **07:47:25 UTC** in `windows_audio_player_smoke.py:193` with `live mute and balance preserve volume and other player`. The sealed run failed the same compound assertion at **07:53:18 UTC**. The saved output does not identify which operand failed.

The sealed desktop probe last emitted `PASS: native Space` at **07:53:45.9143682 UTC**. It supplied no completed exit before the bundle step timed out at **08:23:00 UTC**. This record does not infer hidden window or callback state from the silence. EPUBCheck, DAISY validation, installed pytest, the three subsequent JUnit checks, final seal verification and the final source-wheel ledger check were not reached. The final core and tail acceptance-verdict artifacts are absent; no `installed_bundle` PASS is claimed.

The pinned Inno toolchain built a **749,811,256-byte** setup executable, SHA-256 `c5ac418a77e063f417dea7a0ba716ce898cb11370df3cc6c8cb6986eff7dadae`. Its actual installation log records the destination `Resonance installed Ω` and successful installation. The old WScript shortcut reader returned all three target paths with `Resonance installed O`; the native lifecycle probe consequently failed at line 135. These records do not determine whether shortcut storage or observation caused the difference. A successful setup process does not establish Repair/Uninstall completion or release signing.

## Exact retained artifacts

| Artifact | ID | ZIP bytes | SHA-256 |
| --- | --- | ---: | --- |
| Early endpoint receipt | 15254346609 | 23,146 | `d6af49adb2b3f391296ee8e0304aca5a4dde8753aad767876fa71d94e7fd4d8a` |
| Phase diagnostics | 15256135193 | 173,963 | `1c20e0d489e1d5d3025e8825eab0b46dbe89a8e20436be1fd145b9e7ef85a16f` |
| Final job evidence | 15256415287 | 2,615,243 | `4ea56b390a8c5554c92467fb18a95769cdd4d8e6a52a0de31444af97857feaee` |

The full terminal log contains **1,071,739 UTF-8 bytes**, SHA-256 `edc1e39608ada4743d20d706d8e8868ba05eabc6938e9a73407f40209ea1a301`.

Reader commit `be5a51b33889e99dec8034aad30b53258a7dbf9d`, [run 46127839577](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46127839577), job `140926274602`, completed successfully at **08:55:30 UTC**. It verified the fixed final archive and reconstructed **all 68 selected members, 1,102,637 bytes**, with no mismatched SHA, length, duplicate name or parse error. Its complete terminal log has **1,272,496 UTF-8 bytes**, SHA-256 `3750e24f18e2ceabf337404fd6238c37ac3fbafe2e8134c465347e41181777a4`.

The separate install log has **793,836 bytes**, SHA-256 `cb628d3cabb5a3179e5d5fad942d9eab8e36a889f39a782f30bc3cf1bee0cc95`. The other 95 text members in the archive are not claimed as reconstructed selected evidence. The sole wheel was checked by hash only: **2,004,246 bytes**, SHA-256 `7fe11d031d5ab646d321e56c94cb80f906d41147d9bb5a7a29eb75d4bf7e3e0a`; the reader neither extracted nor executed it.

The saved source-wheel receipt is the actual early build proof at **07:48:26 UTC**, with 429 payload files and 356 required runtime files. Its existence does not establish execution of the later post-native source-wheel ledger entry.

Physical microphone use, physical hotplug, audible listening, NVDA and a signed application release remain outside this evidence. The virtual endpoint and signature results apply to this dated CI evaluation and do not authorize recurring driver redistribution.
