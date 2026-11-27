# Managed installation lifetime

The existing application and installer coordinate destructive maintenance through
one empty `installation.lease` file in the managed program root. Its live OS
handle determines ownership; file contents and recorded process IDs do not.
This is same-user coordination, not an administrator or malicious-code boundary.

Both existing layouts are recognized:

- The legacy `managed-install.ini` root containing `app/.venv`.
- The current standard installer's `install-layout-v1.txt` root containing
  `versions/<generation>/.venv`, with exact `Resonance.StandardInstall/1` contents.

Every current CLI entrypoint takes a shared application lease before entering
its command, and the desktop retains a shared lease for each actual background
worker. Nested dispatch and concurrent generation workers share one process
hold until their last operation exits. Portable environments without an
ownership marker retain their current behavior and create no lease files.

An incomplete managed environment may run only the main entrypoint's exact
isolated `--self-test` bootstrap. The current side-by-side installer also keeps
that bootstrap under a shared lease. The legacy installer's pre-existing
parent-exclusive bootstrap exception is preserved.

## Current standard installer

Install and repair continue preparing a new immutable generation. They do not
request an exclusive application lease, stop an older interpreter or overwrite
its environment. The actual uninstall script obtains the exclusive root lease
before removing its owned files or registration. If an application or its
worker still holds the shared lease, uninstall refuses without forcing it to
exit. The uninstall script retains the exclusive handle through its lifecycle.

The current portable update transaction continues to refuse both managed
layouts before staging, backup or activation. This lease does not turn a
portable bundle replacement into a standard-installer upgrade, and it does not
activate a license, signer, update service or production endpoint.

## Verification boundaries

`tests/packaging/test_installation_lease.py` exercises both layouts, malformed
markers and linked paths, all current CLI guards, real concurrent OS leases, an
owned child process, fresh generation preparation and the actual desktop worker
method after its parent context returns. The test module also runs against the
installed package; its desktop method is read from that package's own origin.

`packaging/probe_standard_installer_windows.py` retains the existing real
install, failed bootstrap, repair, old-interpreter preservation and uninstall
checks. It additionally holds the actual installed package's lease in a real
child and requires the registered uninstaller to refuse without changing its
registration, shortcut, sealed generation or user-data sentinel. After the child
exits, the same normal uninstall must succeed.

Source preparation is not execution evidence. Native Inno execution, signing,
NVDA, real trusted A-to-B upgrade and production entitlement qualification must
be recorded separately on the exact composed candidate.
