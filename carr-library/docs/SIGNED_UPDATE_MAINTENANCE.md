# Signed update review and maintenance

The application checks an explicitly selected owner configuration, verifies
signed metadata, downloads the exact reviewed package, and then applies or
recovers it through an existing installation owner. Checking, downloading and
applying are separate actions. Opening the application does not contact an
update server.

This flow reuses the current TUF verifier, Windows credential vault, complete
library backup, native launcher, offline bundle installer and standard Inno
setup. It does not introduce a second installer or signature format.

## Qualification boundary

New source candidates and their portable/native tests must run on the exact
composed application. Source review does not establish native installation,
TLS, rollback or release acceptance. Native probes use a disposable CI account
and synthetic signing authority. Production keys, domain ownership, protected
anchor provisioning, Authenticode and manual NVDA acceptance remain separate
owner inputs and qualification.

The native standard probe reuses the same compiled payload at advancing
synthetic build numbers. It proves the signed transaction, actual generation
installer, backup, protected commit and repair/rollback. It does not claim
cross-version data migration.

## Independently reviewed configuration

Open **Help → License and signed updates → Review owner update configuration**.
Choose the owner-provided JSON file and enter its SHA256 obtained independently.
Review shows the selected product, channel, source, current program and
maintenance workspace. No production URL, key or fallback protected store is
selected by default.

The exact schema is **carr-update-owner/1**:

| Field | Required meaning |
| --- | --- |
| product, channel, current_build | Publisher namespace and positive current build number. |
| bootstrap.path, bootstrap.sha256 | Bounded TUF root file and independently retained SHA256. |
| repository | Explicit local repository or HTTPS base URL; optional private CA requires its own exact path and SHA256. |
| target | Exact signed target path inside that repository. |
| cache | Private update cache outside the program and research library. |
| installation | Supported layout, exact current program folder/generation, separate workspace, current manifest/setup pins and distribution version. |
| anchor | Previously provisioned Windows credential reference, profile and binding. Missing state is never provisioned or reset by this client. |
| verifier | Trusted current local install_windows.py and SHA256. Downloaded verifier code is never used. |
| helper_python | Separately reviewed external interpreter, required for portable activation; optional for standard setup. |

The two repository forms are:

~~~json
{"kind":"local","path":"C:\\OwnerUpdateRepository"}
~~~

~~~json
{"kind":"https","url":"https://updates.example.invalid/resonance/","ca_file":null,"ca_sha256":null}
~~~

The .invalid URL is documentation only, not an application endpoint. For a
private TLS CA supply both absolute ca_file and separate ca_sha256. HTTPS uses
normal certificate/hostname verification, bounded requests and a finite overall
deadline. Redirects, embedded credentials and environment proxies are refused.

Local and HTTPS sources use the same TUF role, signature, expiry, version,
target length/hash, product/channel and data-schema verification. TLS trust
alone is not update authority.

Use plain absolute local paths. Keep program, research data and workspace
separate. Portable helper, verifier, configuration and executing helper code
must also remain outside every replaced or data root. Install the reviewed
application and its locked trusted-client dependencies in that external helper
beforehand; maintenance never downloads its own verifier or helper.

## Review, download, apply

**Check configured signed source** presents one exact release. A new failed
check clears the earlier in-session release/package.

**Download reviewed package** authenticates that same release again and
captures its exact bytes. It does not install, restart or change permissions.

**Apply reviewed standard update** and **Recover interrupted standard update**
each require separate confirmation. They reauthenticate the exact selected
package at the actual handoff boundary.

For update-capable portable bundles, **Show external maintenance command**
displays the exact apply and recovery commands. Finish work, close the program,
then use the separately reviewed external interpreter. The UI never creates a
breakaway helper to escape its lifetime owner.

~~~text
python -I -B -X utf8 -m carr_library.security review-configuration --configuration OWNER.json --configuration-sha256 OWNER_SHA256
python -I -B -X utf8 -m carr_library.security check-configured-update --configuration OWNER.json --configuration-sha256 OWNER_SHA256 --data-dir RESEARCH
python -I -B -X utf8 -m carr_library.security download-configured-update --configuration OWNER.json --configuration-sha256 OWNER_SHA256 --data-dir RESEARCH --reviewed-sha256 PACKAGE_SHA256
python -I -B -X utf8 -m carr_library.security apply-update --configuration OWNER.json --configuration-sha256 OWNER_SHA256 --data-dir RESEARCH --reviewed-sha256 PACKAGE_SHA256 --accept
python -I -B -X utf8 -m carr_library.security recover-update --configuration OWNER.json --configuration-sha256 OWNER_SHA256 --data-dir RESEARCH --reviewed-sha256 PACKAGE_SHA256 --accept
~~~

All uppercase values and the interpreter are explicit reviewed inputs. A cached
package or mutable journal cannot replace signed authority.

## Existing standard installer

The supported current layout retains install-layout-v1.txt and
versions/<source-prefix>-<generation>/. The current Inno setup remains the only
writer of Windows registration, shortcuts, Modify/Repair and Uninstall records.

An update creates a complete research backup, retains the old setup, runs the
authenticated new setup, verifies the sealed new generation, executes its
actual installed self-test and confirms the real Windows selection. Only then
can the protected update anchor advance. A failed precommit acceptance uses the
independently pinned old setup through the existing repair route.

Setup and repair each create a fresh generation. Old generations, rejected
candidates, backups and setup logs remain. The existing append-log uninstaller
owns its files and preserves unknown user files.

For the next update, review a new configuration containing the returned current
program, version, installed_manifest_sha256 and installed_setup_sha256. The
same workspace can be reused. Its terminal old journal is archived create-only
with exact bytes only after independently proving the configured current
generation, health, Windows selection and protected state. Old backups and
history are not silently deleted. An unfinished or inconsistent journal
requires recovery first. The UI never rewrites owner configuration from a
mutable journal.

## Opt-in portable lifetime ownership

Ordinary portable bundles keep their current behavior. Update-capable portable
bundles require an explicit pre-seal option, exact freshly built launcher with
build/native evidence, bundled Python, a wheel with every reviewed entrypoint,
and the inventory-bound portable-update.ini protocol 2 marker.

The launcher and direct public CLI entries use one actual Windows Job and
installation lease. A site-free child waits on a private admission pipe before
application work. Standard input remains separate, preserving password pipes.

The owner holds the shared lease until root and descendant cleanup is confirmed.
Abrupt or unconfirmed cleanup leaves a durable owner receipt that vetoes
activation. It is never cleared by PID, age or an empty process inventory.
Preserve that evidence for owner recovery, or use a separately verified new
bundle in another folder.

Portable activation reuses UpdateTransaction and the current offline installer,
complete backup, installed ownership source checks, rollback and protected
commit. Managed standard generations are refused by the folder-swap route.

Direct entry inventory includes document batches, snapshots, workflow templates,
translation batches, transcription, Studio, bibliography, diagnostics and local
audio setup. Nested dispatch does not replace protection of direct module runs.

## Build the exact update package

Standard setup uses the current builder and independently pinned local Inno
compiler, adding the explicit envelope option:

~~~text
python packaging/build_standard_installer.py --bundle SEALED_BUNDLE --source-commit SOURCE_COMMIT --manifest-sha256 MANIFEST_SHA256 --output NEW_BUILD --compiler ISCC.exe --compiler-sha256 ISCC_SHA256 --update-envelope
~~~

The prepublication ZIP contains exactly ResonanceSetup.exe,
installer-build.json and bundle-manifest.json. The executable is streamed and
checked against the compiled report. The returned ZIP length/SHA256 identify
what the owner can publish through the signed TUF repository. Neither archive
nor build report is authority on its own. Standard setup refuses a portable
marker; the two layouts have different update owners.

Portable build_windows_bundle.py adds --portable-updates,
--native-launcher-build BUILD_EVIDENCE.json and
--native-launcher-probe NATIVE_PROBE.json alongside its current launcher/SHA,
bundled-Python, source and wheel arguments. It reuses the existing exact-source
launcher verifier. Marker and declaration enter the manifest before sealing;
there is no post-seal patch.

## Completion and recovery

A proven protected commit and independently verified selected bytes can survive
late acknowledgement/cleanup failure. Such a result reports completion with a
warning and completion_confirmed=false. Preserve workspace and owner receipts.
The warning is not permission to ignore uncertain native cleanup.

Altered manifest/setup, foreign selection, conflicting anchor, missing
signature, unknown owner or failed native health cannot become success merely
because a matching pathname exists. Before-commit failures remain failures.

License/entitlement policy is unchanged. This flow never grants paid features,
fabricates keys or anchors, changes a perpetual grant or blocks ordinary
reading/export/recovery for missing update authority.

## Required gates

Common source and installed suites:

~~~text
tests/security/test_https_source_policy.py
tests/security/test_standard_update_handoff.py
tests/security/test_update_maintenance.py
tests/security/test_portable_handoff.py
tests/packaging/test_portable_entry.py
tests/packaging/test_standard_update_envelope.py
tests/ui/test_update_maintenance.py
~~~

Run tests/security/test_https_updates.py in an ordinary test process **before**
outbound denial: it uses a real local TLS server and synthetic signed authority,
so it must not run inside the socket-denial wrapper.

Windows-only tests/packaging/test_portable_entry_windows.py requires actual
Jobs, pipes, descendant cleanup, stdin and compiled launcher evidence. Set
CARR_NATIVE_LAUNCHER_DIR to the freshly built verified launcher directory.
A non-Windows skip is not a portable acceptance result.

~~~text
python -I -B -X utf8 scripts/windows_update_maintenance_smoke.py --report NEW_UI_REPORT.json
python -I -B -X utf8 packaging/probe_standard_update_windows.py --envelope EXACT_ENVELOPE.zip --envelope-sha256 ENVELOPE_SHA256 --source-commit SOURCE_COMMIT --manifest-sha256 MANIFEST_SHA256 --application-version WHEEL_VERSION --work NEW_EXTERNAL_WORK --report NEW_STANDARD_REPORT.json --disposable-ci
~~~

The standard probe requires CI=true and refuses existing standard registration
or shortcut. Run after the existing standard probe has completed its own
uninstall. It performs four actual setup/repair executions plus installed health
checks; the enclosing owned CI gate must allow the existing per-process bounds.
Existing launcher, source-wheel and final seal gates remain required.

If outbound denial uses per-program rules, include the four new generation
runtime/python.exe and .venv/Scripts/python.exe paths before the native standard
gate. Seed-interpreter denial alone does not cover newly installed interpreters.
Record actual firewall scope instead of inferring it from offline flags.
