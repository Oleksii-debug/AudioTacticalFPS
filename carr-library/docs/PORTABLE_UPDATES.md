# Portable update activation and recovery

Portable activation is **off unless the bundle was built explicitly for this profile**.
Read-only signed update checks and entitlement renewal do not download, activate,
restart, or stop application jobs. Their existing opt-in settings are independent
of the activation integration described here.

## Supported layout and authority

The owner builds a new Windows x64 / CPython 3.11 bundle with
`packaging/build_windows_bundle.py --portable-updates`, an exact source commit,
the verified application wheel, the bundled Python runtime, and the native
launcher built from that same source. The builder verifies the entry and
ownership modules against the wheel and the launcher source against its build
receipt. It then inventories the fixed `portable-update.ini` capability.

This is a new sealed distribution profile. Copying a marker into an older or
already running uncoordinated installation is not enrollment. An unmarked
portable bundle retains its previous launch behavior and is refused by the
portable activation helper. A candidate must retain the same ownership protocol.

The persistent control directory is a sibling of the program directory, named
`<program-name>.update-control`. Its `installation.lease` remains in place while
the program directory is moved. Do not put this directory inside the program,
the update workspace, or a research library.

A standard installation with `managed-install.ini` or `owned-files.json` is
refused. It needs an authenticated standard-setup handoff that updates the
application, Repair package, installation identity and Uninstall inventory
together. A generic folder swap cannot perform that operation. The trust status
dialog reports this limitation; this component does not claim managed automatic
updates.

## Application and descendant lifetime

The native launcher takes the shared installation lease before starting Python.
The existing public entrypoint decorators also acquire it. For this profile,
ordinary desktop, CLI and diagnostic callbacks run inside an owned Windows Job.

The fixed child admission bootstrap starts with `-I -S -B`. It can initially run
only standard-library admission code. A separate inherited pipe carries a
bounded owner proof and the exact CLI argument vector; ordinary stdin remains
available. Only the guardian holds the pipe's write end.

Before enabling site initialization or importing the application, the child:

1. Duplicates the guardian's exact live Job and lease handles.
2. Verifies membership in that specific Job, kill-on-close, and disabled breakaway.
3. Verifies that the duplicated lease belongs to this installation's persistent
   control directory.
4. Closes its temporary Job duplicate and retains the shared lease.

If the parent dies before Job assignment, the pipe reaches EOF and the child
exits without site initialization or application work. After assignment, the
existing Job owner contains the application and its ordinary import, engine,
transcription and playback descendants. Parent death or normal application
shutdown closes that owner. No process-name search or termination of unrelated
processes is used.

The dispatcher admits only the existing reviewed entrypoints. It rechecks the
same ownership proof after imports and preserves their arguments and exit codes.
A direct application-lease context cannot authorize portable work without this
owner. The original fixed offline bootstrap self-test remains limited to an
incomplete installation; it does not bypass a completed entrypoint's lease.

## External helper integration

Activation runs in a separately provisioned trusted helper outside the program
directory. Its interpreter, prefix, imported helper modules, trusted bundle
verifier and working directory must also be outside the program, update workspace
and selected research library. It must remain usable if the program directory
is temporarily absent after a crash.

The host configures the existing trusted update client, its product/channel/root
policy, the existing protected update anchor, and the trusted host copy of
`packaging/install_windows.py:verify`. No endpoint, publisher key, credential,
vault fallback or automatic provisioning is supplied by this component.

The host retains the current bundle-manifest SHA256 independently of the mutable
program folder, from the reviewed original bundle. It obtains the candidate
`VerifiedPackage` through the existing TUF client. A checksum supplied without
that authorization is insufficient.

The reusable entry is `carr_library.security.handoff.PortableUpdateHandoff`:

```python
helper = PortableUpdateHandoff(
    app, program_directory, update_workspace,
    update_client.policy, protected_update_anchor, trusted_bundle_verifier,
    installed_manifest_sha256=recorded_current_manifest_sha256,
)
result = helper.apply(verified_package, cancelled=cancelled)
```

All values above come from the owner's explicit configuration and review. The
host must not derive the recorded current manifest digest from an unreviewed
folder at activation time.

The helper acquires the same lease exclusively and without waiting for or
cancelling active work. It validates the current source and installed ownership
modules against the retained sealed wheel, verifies the candidate's manifest
inside the existing private hash-verified package snapshot, and delegates
staging, the complete research backup, directory publication, health and protected
commit to the existing `UpdateTransaction`.

Health runs only the existing isolated `self_test()`, with installed package
origin and signed version checks. It does not call a public CLI through an
exclusive lease. The offline installer likewise calls its fixed native
dependency check before readiness; the ordinary native diagnostic CLI retains
the application lease. Native installer and health children use the existing
Windows process owner.

Cancellation is honored during preparation. Once activation begins, the existing
bounded transaction completes or rolls back. A cancellation observed after the
protected commit cannot relabel the committed update as a failure.

An explicit `helper.restart()` after successful activation or recovery launches
only the newly verified existing `ResearchLibrary.exe`. It returns
`restart-requested` and its PID; that acknowledgement is not proof that the
desktop, audio devices, or a screen reader completed acceptance.

## Interrupted updates

Restart the external helper with the same independently retained A manifest
identity and reacquire the **same authenticated candidate package and Release**
through the configured trusted client. Then call:

```python
status = helper.recover(verified_package)
```

The journal must match the authenticated Release in full. A local journal cannot
change the source commit, version, target, notes or candidate manifest. The
retained previous installation is bound to A's original manifest, while B's
manifest remains bound to the authenticated package bytes.

A journal that says complete must still match protected state and pass B's health
checks. A rolled-back journal must match the original protected state and A's
installed ownership bytes. Before the protected commit, interrupted publication
restores A; after it, recovery finishes B. The existing backup, original research
root, saved reader drafts, quotations, pending jobs and prior outputs remain
under their existing preservation contracts.

If the authenticated package, protected state or retained installation is no
longer available or no longer agrees, automatic recovery refuses. Preserve the
program, previous/failed directories, update workspace and research backup for
the owner to resolve. There is no reset of the protected anchor or substitution
of an unverified journal as authority.

## Required qualification

The portable source selection is:

```text
tests/security/test_portable_handoff.py
tests/packaging/test_portable_entry.py
tests/packaging/test_standard_installer.py
tests/packaging/test_installer_process.py
tests/security/test_installation.py
tests/security/test_publication_integrity.py
tests/security/test_independent_review_boundaries.py
```

The separate actual Windows gate is
`tests/packaging/test_portable_entry_windows.py`. It requires Windows x64 /
Python 3.11 and `CARR_NATIVE_LAUNCHER_DIR` pointing to the fresh exact-source
`build_msvc.ps1` output. Missing native prerequisites fail that gate.

Its nine cases exercise real pre-assignment and post-assignment parent death,
assignment failure, unrelated Job/lease refusal, live descendant termination,
normal shutdown, stdin/argument preservation, and the compiled launcher's shared
lease. The pre-admission cases record real site initialization and require it
to remain absent. An unrelated process must survive owned-tree cleanup.

Run the portable source selection separately on Linux; the Windows-only module
is not Linux execution evidence. Source and installed Windows gates must report
zero skipped cases for the native module, and preserve the exact compiled C
source receipt and loaded package origin.
