# Native Windows launcher in the existing offline bundle

The bundle builder can include the existing native `ResearchLibrary.exe` launch
entry. The implementation and its standalone Windows acceptance workflow are
reused unchanged from PR122, commit
`687506ddca74c33fa3ee62c7925ae98b3b1afa3b`. The existing Python installer, application
entrypoint, runtime layout and CMD launchers remain the bundle's shared paths.

## Assemble an explicitly selected launcher

Build and exercise the launcher using the instructions in
[`packaging/native-launcher/README.md`](../packaging/native-launcher/README.md).
Use a fresh private output directory outside the Git worktree. The recorded build
hash, native process report hash and selected executable hash must agree.

Append these arguments to the existing `build_windows_bundle.py` invocation:

```powershell
--native-launcher C:\PrivateBuild\NativeLauncher\ResearchLibrary.exe `
--native-launcher-sha256 <the-64-character-lowercase-SHA256-from-the-passed-build-and-process-reports>
```

Both arguments are required together. The builder captures at most 2 MiB of the
selected file, checks its expected digest, and inspects the copied PE structure
without executing it. It requires the unsigned x64 GUI launcher format, expected
system DLL imports, embedded privilege manifest and mitigation flags. The final
bundle inventory must contain the same digest for `ResearchLibrary.exe` before
publication. The existing installer verifies that digest with the other sealed
files. The optional native runtime assembler preserves the entry and its seal.

Only the selected `ResearchLibrary.exe` enters the bundle. Compiler files, test
children and build output directories are not copied. Omitting the two arguments
preserves the existing CMD-only bundle behavior. These checks establish exact
byte selection and bundle integrity; a SHA256 alone does not establish Windows
execution or publisher identity.

## Required Windows integration evidence

The integrated desktop workflow uses the runner's installed MSVC tools and builds
outside the checkout. It runs the unchanged native process acceptance probe,
checks that all nine checks passed for the exact build digest, and supplies that
digest to the bundle builder. After adding the native runtime, it checks the
sealed executable and manifest entry against that same digest.

The existing full offline installation and CMD checks still run. With outbound
Windows Firewall rules covering both bundled Python executables and the native
entry, the workflow also runs `probe_installed_windows.py` against the already
installed bundle. That probe invokes the actual native executable with
`--launcher-check`, `--check-installation` and `--self-test`; its reported digest
must match the selected, built and sealed executable. The existing final manifest
and file verification checks remain in place after execution.

The workflow retains these text and JSON reports in `ci-results`:

- `native-launcher-build.json`: compiler, source and unsigned PE identity.
- `native-launcher-process.json`: actual Windows execution with a synthetic child.
- `sealed-native-launcher.json`: exact executable inclusion in the final bundle.
- `installed-native-launcher.json`: real installed application's native checks and
  self-test through that executable.

No native executable or private bundle is uploaded by this integration. The
standalone native workflow continues to test both MSVC and the already pinned Zig
build independently. Source-level packaging tests and portable C core tests do
not count as either of the Windows execution reports.

## Scope of the result

The current launcher is an unsigned development candidate. Installed application
checks do not establish a fresh normal-launch installation, interactive GUI
behavior, NVDA acceptance, shortcut installation, code signing or release
publication. Each needs its own observed acceptance. The launcher and probes keep
those distinctions in their reports and preserve an existing incomplete
environment instead of replacing it.
