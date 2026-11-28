# Reproducible optional Hunspell Windows qualification

## Purpose and current evidence

This recipe builds an explicitly selected official Hunspell source snapshot for ephemeral CI use, then exercises its real C API and the installed application's existing Reader. It does not add a binary to the application, install a dictionary, or download anything from within the application.

The composed helper, tests and native probes are **DRAFT_NOT_RUN**. A successful build receipt means only **BUILT** until the actual C API gate succeeds. The separate native Reader gate must then succeed on the same application candidate. Historical donor results do not qualify this composition.

## Pinned inputs

| Input | Required identity |
| --- | --- |
| Official repository | `https://github.com/hunspell/hunspell` |
| Release tag | `v1.7.2` |
| Source commit | `2969be996acad84b91ab3875b1816636fe61a40e` |
| Source inventory | `scripts/hunspell-1.7.2-source.json`, exact 38 paths and Git blob identities |
| Preserved notices | `COPYING`, `COPYING.LESSER`, `COPYING.MPL`, `license.hunspell`, `license.myspell` |
| Build helper | `scripts/build_hunspell_msvc.py` from the exact application candidate |
| Compiler family | Explicit installed Visual Studio 2022 / v143 x64 toolchain |
| SDK | Explicit installed Windows 10 SDK directory and exact version |
| Application | Exact candidate source and its independently verified installed wheel/bundle |

The source manifest covers 3,332,658 bytes. The helper verifies every listed path's byte count and Git blob hash before copying or invoking the compiler, and repeats source verification afterward. Its selected `msbuild.exe`, `cl.exe`, `link.exe` and `rc.exe` identities are also checked before and after the build. This binds the tools actually selected; it does not claim a byte-identical binary across different compiler or SDK versions.

Source and toolchain acquisition belong to the explicit CI preparation phase, before an offline firewall is enabled. Acquire the exact source commit through the existing trusted repository mechanism and retain its identity. The helper itself performs no network acquisition.

## Build into a new external directory

Use an unused directory outside the application and the selected upstream source. Pass concrete installed tool paths; do not resolve tools from an unreviewed working directory or a later PATH change.

Illustrative PowerShell invocation, with the variables set to the already selected and acquired paths:

```powershell
& $python -B scripts/build_hunspell_msvc.py `
  --source $pinnedHunspellSource `
  --output $newHunspellBuild `
  --msbuild $selectedMSBuild `
  --vc-tools-dir $selectedVCTools `
  --sdk-dir $selectedWindowsSdk `
  --sdk-version $selectedSdkVersion
if ($LASTEXITCODE -ne 0) { throw 'Pinned Hunspell build failed.' }
```

The helper uses the unchanged upstream source project with explicit `Release_dll`, x64 and v143 properties. It supplies the selected SDK, disables project build events and ambient user/directory build overrides, and bounds MSBuild parallelism. It does not rewrite the upstream source. The default compiler deadline is 300 seconds; the helper accepts only a finite 1–600 second explicit bound. A failed, cancelled or timed-out compiler is reaped, and its bounded build log remains evidence. A failed build produces no successful receipt.

Expected successful-build outputs are:

- `bin/libhunspell.dll`: an AMD64 PE32+ DLL.
- `build-report.json`: source, notice, tool, command, build-log and DLL identities.
- `build.log`: bounded compiler diagnostics.
- `source/`: the exact verified source inventory and notices.

The receipt's `status` is `BUILT`, `native_api` and the DLL's `c_api` are `NOT_RUN`, `bundled` is false and `distribution_clearance` is `NOT_GRANTED`. Require these distinctions; a header-shaped file or a compiler fixture cannot satisfy native execution.

## Strict source and installed tests

Run all four whole modules under the existing strict child policy:

```text
tests/speech/test_spelling.py
tests/speech/test_spelling_handlers.py
tests/speech/test_spelling_reader_hook.py
tests/packaging/test_hunspell_build.py
```

Their static expected counts are 54, 20, 9 and 31, respectively; 114 in total. These counts remain **NOT_COLLECTED / NOT_RUN** until actual CI. Preserve zero skips, zero failures/errors and zero XFAIL/XPASS. The shared offline policy and existing per-suite cleanup remain unchanged.

For isolated installed tests, retain these exact fixture companions as appropriate to the existing gate's layout:

- `tests/speech/spelling_fixtures.py`.
- `tests/ui/test_find_replace_hooks.py`, which the Reader-hook suite imports as an adjacent fixture.
- `scripts/build_hunspell_msvc.py` and its adjacent `hunspell-1.7.2-source.json`.

The packaging tests inspect the checked-out helper. They are not a compiler qualification. Application imports in the installed gate must come from the installed candidate, with exact source-byte verification. No new Python distribution or package namespace is required.

## Actual C API acceptance

After acquiring the source/tools and building the external DLL, execute the acceptance offline. Use the existing source import policy for the source run. Use the installed interpreter with isolated mode for the installed run:

```powershell
& $sourcePython -B scripts/hunspell_acceptance.py `
  --library $builtHunspellDll --source-root $exactSourceRoot `
  --output $newSourceReport
if ($LASTEXITCODE -ne 0) { throw 'Source Hunspell C API acceptance failed.' }

& $installedPython -I -B scripts/hunspell_acceptance.py `
  --library $builtHunspellDll --source-root $exactSourceRoot `
  --require-installed --output $newInstalledReport
if ($LASTEXITCODE -ne 0) { throw 'Installed Hunspell C API acceptance failed.' }
```

`$exactSourceRoot` is the current checkout's `carr-library/src`, not a different revision. The source interpreter must already use the existing approved source import setup. The installed origin check uses the bundle's established `CARR_PROGRAM_DIR` when present, otherwise the interpreter's `purelib`; it rejects imports from the checked-out source tree.

Require `status=PASS`, `actual_c_api=PASS` and `child_and_resource_cleanup=PASS`. The installed report contains exactly 40 unique named checks: 16 behavior/lifetime checks and two checks for each of 12 imported application modules. Require exact before/after source hashes and installed origins. The same DLL hash must match the build receipt and both acceptance reports.

Acceptance uses original tiny fixture dictionaries, not a downloaded language pack. It exercises UTF-8, an explicit legacy encoding, real affix expansion, suggestions, Unicode/CRLF offsets, selected scope, explicit correction/Undo, mutation rejection and malformed dictionary refusal. Cancellation and deadline cases use actual owned processes; the deadline case deliberately delays the unchanged worker with the standard library. Actual child death and staging cleanup are asserted before emergency cleanup. The existing broker reservation must be empty afterward.

## Actual native Reader acceptance

Run this only on an actual interactive Windows desktop, with the same installed candidate and external DLL:

```powershell
& $installedPython -I -B scripts/windows_hunspell_smoke.py `
  --library $builtHunspellDll --source-root $exactSourceRoot `
  --require-installed --output $newNativeReport
if ($LASTEXITCODE -ne 0) { throw 'Native Hunspell Reader acceptance failed.' }
```

The script retains the existing bundled-runtime activation handle before importing wx and through cleanup. Its hard deadline is 180 seconds, including worker, frame and native-owner cleanup. The source-bound report verifies 20 installed application module identities before and after the scenario.

Require all 28 named checks, without duplicates or missing names, plus:

```text
status = PASS
actual_hunspell_c_api = PASS
actual_reader_worker = PASS
native_review_apply_undo = PASS
native_cancel_and_close = PASS
owned_child_invocations = 4
speech_audio = NOT_RUN
manual_nvda = NOT_RUN
dictionary_quality = NOT_RUN
distribution_clearance = NOT_GRANTED
```

The first native check executes the real library. The mutation check refuses changed files. Two later owned children deliberately wait before the unchanged native worker starts, making native Cancel and Close deterministic without claiming anything about dictionary speed.

The probe uses the actual DesktopFrame, retained-source reader, existing serial executor, native controls and keyboard activation. It checks one-edit Apply, Undo, derivative-only Ctrl+S, exact original asset bytes, late completion after modal destruction and final owner cleanup. A metadata-only voice registry deliberately makes spelling independent of speech synthesis and audio hardware. This does not qualify audio, pronunciation or listening quality.

Each native check must propagate failure while later independent diagnostics may still run under the existing workflow policy. Retaining or promoting an application artifact still requires the parent workflow's overall success; a successful Hunspell probe alone cannot qualify a failed candidate.

## Evidence and distribution boundaries

Retain the exact source commit, source manifest, notices, build report, compiler log, source and installed test reports, C API reports and native Reader report. Bind every report to the exact application head and the one built DLL hash. Successful source, compiler and GUI results are separate facts.

The optional DLL and original test dictionaries are ephemeral qualification inputs. This feature changes neither the existing core/semantic/trusted Python wheel inventories nor the standard application bundle's binary payload. Do not add a built native library or third-party dictionary to a delivery artifact through this recipe. Any separately authorized redistribution would require its own selected license, notice and dictionary review.

Official sources inspected on **2026-11-27**:

- [Official release reference](https://github.com/hunspell/hunspell/tree/2969be996acad84b91ab3875b1816636fe61a40e).
- [C API header](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/src/hunspell/hunspell.h).
- [Upstream Windows project](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/msvc/libhunspell.vcxproj).
- [Hunspell license notice](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/license.hunspell).

The source notice lists MPL 1.1, GPL 2 or later, or LGPL 2.1 or later alternatives. The exact five notice files are retained by the build. This is a description of the supplied notices, not a redistribution approval or dictionary-quality assessment.
