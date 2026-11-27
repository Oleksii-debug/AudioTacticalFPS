# Explicit Windows Hunspell C API qualification

The application already reviews an explicitly selected local native library and
dictionary pair. This separate CI workflow builds the official source with the
runner's installed compiler, then exercises that DLL through the installed
application. The helper does not download, install, or bundle anything.

## Exact source and tool inputs

The reviewed upstream is [Hunspell v1.7.2](https://github.com/hunspell/hunspell/tree/2969be996acad84b91ab3875b1816636fe61a40e),
commit `2969be996acad84b91ab3875b1816636fe61a40e`. The
[committed source manifest](../scripts/hunspell-1.7.2-source.json) binds 38 files,
3,332,658 bytes, to their Git blob identities. Its own SHA256 is
`f5ea8a9afae905cd1d2dd56415b320fd0949d85e28151eb8ddb63d19d128aefc`.
The helper also records every selected file's SHA256 in the build receipt.

Only these files enter the fresh build directory. The five upstream notices
`COPYING`, `COPYING.LESSER`, `COPYING.MPL`, `license.hunspell` and
`license.myspell` remain byte-identical. The upstream
[license notice](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/license.hunspell)
offers MPL 1.1, GPL 2 or later, or LGPL 2.1 or later. This acceptance does not
grant distribution clearance for a DLL, its runtime dependencies, or dictionaries.

The official [MSVC project](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/msvc/libhunspell.vcxproj)
already provides `Release_dll|x64`. The helper uses it unchanged and supplies
explicit VS 2022 `v143` and Windows 10 SDK properties in place of its historical
`v140_xp` and SDK 8.1 defaults. It uses the already committed `hunvisapi.h` and
disables the source-writing prebuild event. New private output/intermediate
directories, an empty user-property directory, disabled Directory.Build imports,
disabled automatic response files, and a small explicit build environment keep
unselected checkout files and inherited compiler flags out of this invocation.

MSBuild, `cl.exe`, `link.exe`, and `rc.exe` must be ordinary local files in the
explicit installed tool locations. Their bytes are recorded before compilation
and verified again afterward. The selected VC directory version and SDK version
are recorded too. Installed MSBuild/SDK headers, libraries, and target files are
still part of the runner's toolchain; this is reproducible source selection and
build configuration, not a claim of identical DLL bytes across different
toolchain installations.

## Required CI sequence

CI prepares an ephemeral checkout from the official repository at the exact
commit. Disable Git's automatic line-ending conversion (`core.autocrlf=false`)
for checkout; the helper requires the original blob bytes, including CRLF and
BOM where present. Both source and build directories belong under `RUNNER_TEMP`,
outside the application bundle. The helper performs no network access.

Call the same previously discovered `CARR_VSDEVCMD` used for the runner's native
launcher, with `-no_logo -arch=x64 -host_arch=x64`. In that `cmd` environment:

```bat
python -I -B -X utf8 scripts\build_hunspell_msvc.py ^
  --source "%RUNNER_TEMP%\hunspell-official-source" ^
  --output "%RUNNER_TEMP%\hunspell-native-build" ^
  --msbuild "%VSINSTALLDIR%MSBuild\Current\Bin\MSBuild.exe" ^
  --vc-tools-dir "%VCToolsInstallDir%." ^
  --sdk-dir "%WindowsSdkDir%." ^
  --sdk-version "%WindowsSDKVersion:\=%"
```

The final dot keeps a trailing directory backslash away from the closing native
argument quote. The SDK argument removes its trailing separator. An existing
build directory is refused. Source verification and tool admission happen before
creating it. The default compiler deadline is 300 seconds, with an explicit
1–600 second range. Cancellation/deadline cleanup targets the owned compiler
process tree, not unrelated processes; failure preserves the diagnostic log and
does not publish a successful receipt.

The successful helper output is `bin/libhunspell.dll` plus `build-report.json`.
The report requires `status: BUILT`, the exact source commit/manifest, unchanged
source and selected tool identities, successful MSBuild exit, and an AMD64
PE32+ DLL header. It intentionally reports `native_api: NOT_RUN`.

Next, use the Python environment containing the **installed** application:

```bat
python -I -B -X utf8 scripts\hunspell_acceptance.py ^
  --library "%RUNNER_TEMP%\hunspell-native-build\bin\libhunspell.dll" ^
  --output "%RUNNER_TEMP%\hunspell-native-acceptance.json"
```

The gate must require all 14 checks, `status: PASS`,
`native_windows: EXECUTED`, the same DLL SHA256 as the build report, and exact
installed origins/hashes for `speech.spelling`, `speech.spelling_worker`, and
`ui.spelling_dialog`. Those real checks cover C API creation/destruction,
dictionary encoding, spelling, suggestions/list release, selected Unicode/CRLF
positions, affixes, source-preserving correction/undo, changed and malformed
dictionary refusal, and actual child cancellation/deadline cleanup. Original
synthetic dictionaries avoid claims about a real language pack's quality.

Only the build/acceptance JSON and diagnostic log are CI evidence. Do not copy
the upstream checkout, DLL, import library, compiler artifacts, or dictionaries
into application packaging or public release assets. A reader's unavailable-
engine UI check remains a separate check and cannot replace this actual DLL run.

## Source checks and evidence limits

`tests/packaging/test_hunspell_build.py` is a **source-only** helper suite; the
helper and manifest are outside the installed wheel. It checks exact source
bytes, notices, links/junctions, bounded inputs, MSBuild property injection,
no-clobber output, unchanged source/tools, PE architecture refusal, and actual
owned Python-process cancellation/deadline/nonzero cleanup. Header-only PE and
compiler fixtures are explicitly synthetic and do not execute Hunspell.

The source suite passed 29 cases on POSIX during implementation. Actual MSVC
compilation and Windows C API execution must be established by the separate CI
reports above. Human NVDA, real dictionary/language quality, signing, runtime
redistribution, and full dependency distribution review remain unqualified by
this workflow.

Primary build references: Microsoft's
[build-event control](https://learn.microsoft.com/en-us/cpp/build/how-to-use-build-events-in-msbuild-projects?view=msvc-170),
[MSBuild command-line property overrides](https://learn.microsoft.com/en-us/visualstudio/msbuild/msbuild-command-line-reference),
and [compiler environment options](https://learn.microsoft.com/en-us/cpp/build/reference/cl-environment-variables?view=msvc-170).
