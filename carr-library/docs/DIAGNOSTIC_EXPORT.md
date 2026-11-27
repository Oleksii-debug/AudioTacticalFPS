# Reviewed diagnostic preview and local export

V3-20 uses the existing desktop command registry and protected export writer.
Open **Help, Diagnostic preview and export**, or find **Preview and export
diagnostics** in the existing command launcher. Its stable command ID is
`help.diagnostics`; the user can assign a shortcut in Keyboard shortcuts.

The native dialog first shows the exact immutable report in the named,
read-only **Diagnostics preview** control. **Save as** opens the native file
picker. A chosen existing file needs a separate replacement confirmation;
closing the picker, declining replacement, or closing the preview writes
nothing. Saving creates only a local UTF-8 text file. The application does
not submit the report or contact a support service.

## Privacy and truthful status

The report is a bounded projection, not a redacted copy of an environment or
log dump. It contains only fixed labels, numeric runtime and installed package
versions, an allowlisted operating-system family, process architecture, and
categorical saved local-engine declaration states. Package version strings
containing arbitrary local build labels or other text are reported as
unavailable. No hostname, account, environment variable, document, transcript,
title, filename, filesystem path, endpoint, token, provider profile, or detailed
exception becomes a report field.

Collection reads at most 64 KiB from the single existing local engine settings
snapshot through the current path and schema guards. Missing settings are
reported as **not saved**. Invalid, linked, unreadable, or oversized settings
are **unavailable**. Only the fixed configured / not configured / disabled
states are exported; declared executable and model paths are never followed.
The output itself is limited to 16 KiB.

Saved declarations do not establish runtime health, installed voices, supported
languages, model quality, or distribution rights. Collection starts no engine,
loads no model, scans no user document, and runs no application self-test.
**Help, Check this installation** remains a separate explicit operation.

## Publication and acceptance

`research.export_file.capture_export_destination` binds replacement approval
to the observed destination. The existing writer rejects private-library
destinations, links, and destination changes; an initially absent destination
uses its existing atomic no-clobber publication. A library change or application
close cancels before publication. A completed publication remains saved if only
temporary-file cleanup is pending. Native status messages expose fixed
categories and never echo raw exception text or destination paths.

`tests/ui/test_diagnostics.py` exercises real collection and filesystem writes,
including seeded sensitive settings and package metadata, unavailable sources,
cancelled choices, explicit replacement, destination races, and private-root
protection. `scripts/windows_v3_smoke.py` adds a bounded probe that dispatches
the registered native Help command, inspects actual named read-only preview
and Save/Close controls, then closes through the button's real event handler.
It does not claim native file-picker operation or manual screen-reader use.
Actual Windows execution and manual NVDA observations must be recorded against
the exact candidate; source tests alone do not qualify those interactions.
