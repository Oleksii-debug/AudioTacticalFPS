# Standard offline Windows installer

The public packaging entry `packaging/build_standard_installer.py` wraps the
existing complete, sealed Windows bundle in **Inno Setup 6.4 or newer**. It adds
one per-user Start-menu entry and the standard Windows uninstall/Modify entry.
The native application remains `ResearchLibrary.exe`; the existing
`install_windows.py` remains the only environment and dependency installer.

## Build and qualification

Use an already reviewed complete bundle containing its native launcher, bundled
CPython, locked wheels, OCR and corresponding-source TAR. Record its exact source
commit and manifest digest independently of the command. Only listed sealed
payloads are copied; existing environments, research databases and unlisted
private files never enter the installer.

```powershell
python -B packaging/build_standard_installer.py `
  --bundle C:\private-build\complete-bundle `
  --source-commit <full-reviewed-commit> `
  --manifest-sha256 <recorded-manifest-sha256> `
  --output C:\private-build\new-installer `
  --compiler "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
  --compiler-sha256 <recorded-reviewed-compiler-sha256>
```

Without both compiler options, the same command prepares the explicit recipe and
sealed payload snapshot without running anything. The compiler is supplied
locally and pinned; this command never downloads a toolchain or executes supplied
bundle code. The compiler build report identifies its input hashes and output
EXE. `prepared` and `compiled` are build states, not native acceptance results.

On a disposable Windows CI account with no existing standard installation, first
build a separate negative fixture with
`tests/packaging/build_standard_failure_fixture.py`, using the same bundle,
source/manifest identity and compiler arguments and a new `--output`. It preserves
the original bundle, seals a test bootstrap that writes a sentinel and exits 23,
and marks its manifest as a negative fixture. Never distribute that fixture.
The native qualification command then requires both compiled installers:

```powershell
python -B packaging/probe_standard_installer_windows.py `
  --setup C:\private-build\new-installer\dist\Resonance-Setup-<commit-prefix>.exe `
  --setup-sha256 <recorded-setup-sha256> `
  --failed-setup C:\private-build\negative-installer\dist\Resonance-Setup-<commit-prefix>.exe `
  --failed-setup-sha256 <recorded-negative-setup-sha256> `
  --source-commit <full-reviewed-commit> `
  --manifest-sha256 <recorded-manifest-sha256> `
  --work C:\private-build\fresh-standard-probe `
  --report C:\private-build\standard-installer-native.json `
  --disposable-ci
```

The probe requires `CI=true`. It runs the actual setup, installed launcher
diagnostics, a deliberately failing bootstrap whose real exit must leave the old
shortcut/registry/Modify installer unchanged, repair while a previous installed interpreter remains alive, and
the standard uninstaller. It checks the registry/shortcut, exact seal, both
generated environments' removal, and preservation of pending-work and unknown
file sentinels. A report is written only after all checks complete. Run it with
the existing Windows gate's outbound firewall policy. It is not an owner-PC tool.

## Install, repair and removal

Setup runs without administrator elevation and uses the supplied offline wheels
only. Its default program location is `%LOCALAPPDATA%\Programs\Resonance`.
The existing application keeps research data outside each program directory.
Setup rejects unrelated nonempty directories and linked program paths.

Every installation or repair creates a fresh `versions\<source>-<number>`
directory. Setup verifies every payload hash, invokes the unchanged offline
installer from a fresh generation during the documented `PrepareToInstall`
prerequisite phase, and requires its final completion marker before publishing the
Start-menu target or Windows Modify entry. It does not close a previous app or
overwrite an active Python environment. Earlier generations remain available;
this deliberately trades disk space for a working previous installation.

Use **Modify** in Windows installed programs or run the same setup again to
repair. The exact setup is cached inside its completed generation so repair
does not depend on a download or the original file's location. Windows uninstall
logs append across successful repairs, allowing one uninstaller to remove all
files it installed and each exact generated `.venv` directory. No rule deletes
the program parent, an entire generation, a library or a backup recursively.
Unknown files remain. Linked/inaccessible program trees stop uninstallation
instead of following those paths. Close the application normally before removal;
the installer never forcibly terminates research jobs.

A failed bootstrap never replaces the previous launch target. An interrupted
first installation can leave a partial generation for diagnosis; the small
ownership marker allows another attempt in a fresh generation of the same root.
That marker is never treated as a ready installation. Existing
libraries/drafts/jobs do not need to be moved or deleted.

## Boundaries retained

This wrapper is a standard **unsigned development installer**. Authenticode,
human NVDA acceptance and real model/device qualification require their own
evidence. The public builder does not issue accounts, entitlements, signing keys,
trust roots or update policy. It does not automatically check a network endpoint.
Manual reinstall is an explicit local choice, not a trusted updater or rollback
protection claim.

The same distribution restriction as `create_delivery.py` applies: existing
owner-local audio packs cannot be placed into a transfer installer until their
package-specific redistribution evidence is reviewed. They remain installable
locally through the existing reviewed audio-runtime boundary. A true/missing
manifest clearance flag does not override that rule.

The private updater boundary still requires the owner's authenticated runtime,
reviewed bootstrap trust and signed policy. Neither this recipe nor the public
`delivery_contract.py` activates that authority. No private engine source,
endpoints, credentials or test production keys are introduced.

The documented nonempty `PrepareToInstall` return stops Setup before normal
file, shortcut and registry installation. Inno's `AfterInstall` callback catches
exceptions, so it is deliberately not used as a failure boundary. Temporary
extraction and one extra verified copy are required for the prerequisite phase;
normal explicit `[Files]` entries then register the same completed payload for
standard uninstall. The append log owns removal across repairs. References:

- https://jrsoftware.org/inno-setup/documentation/topic_installorder.htm
- https://jrsoftware.org/inno-setup/documentation/topic_scriptevents.htm
- https://jrsoftware.org/ishelp/topic_isxfunc_extracttemporaryfiles.htm
- https://jrsoftware.org/inno-setup/documentation/topic_appendnotes.htm
- https://jrsoftware.org/ishelp/topic_uninstalldeletesection.htm
- https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm
