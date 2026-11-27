# Reviewed standard-installation updates

## Scope and default

The existing standard Inno installer now accepts a reviewed update handoff for
its current `versions/<source-prefix>-<counter>` layout. The same package is
still the manual install/repair/uninstall product. There is no second installer,
update queue, entitlement store, signing service, runtime download or automatic
authority configuration.

Without independently supplied publisher trust and an explicitly configured
installation transaction, the application stays private/perpetual and reports
`WAITING_INPUT` for managed activation. Checking or downloading a signed package
does not itself activate it. Reading, saving, export, backup, recovery, completed
results and already running work retain their existing access.

The portable replacement transaction still refuses both managed layouts. A
same-source standard repair is not counted as an A-to-B upgrade.

## Explicit host configuration

A trusted host supplies the current `LibraryApp`, current generation directory,
separate update work directory, one reviewed `UpdatePolicy`, its protected
anchor, and the current trusted bundle/transfer validators. The update client
and standard transaction must share that exact policy object.

```python
# All arguments below are existing, independently configured host inputs.
# This function does not discover or invent keys, endpoints or authority.
from carr_library.security.controller import TrustController
from carr_library.security.standard_installation import StandardUpdateTransaction
from carr_library.security.standard_windows import WindowsStandardHost
from carr_library.security.updates import LocalRepositoryClient

def configure_standard_updates(app, generation, work, cache, policy, anchor,
                               context, target, trusted_verify_bundle,
                               trusted_validate_manifest):
    client = LocalRepositoryClient(cache, policy)
    native = WindowsStandardHost(trusted_verify_bundle, trusted_validate_manifest)
    transaction = StandardUpdateTransaction(
        app, generation, work, policy, anchor, native)
    return TrustController(updates=client, target=target, context=context,
                           standard_update=transaction)
```

The trusted manifest callback must use the current
`packaging/create_delivery.py::_manifest` and
`_require_transferable_audio_inventory`. The bundle callback is the current
trusted `install_windows.verify`, not code imported from a proposed target.
The audio distribution gate remains in force even for a correctly signed
package. A valid update signature is not audio/model redistribution approval.

The configured host passes the same controller to the existing application and
desktop/CLI surfaces. The GUI also checks that its current library root matches
the transaction's configured library before activation or recovery. A library
switch cannot silently back up a different data root.

The returned `VerifiedPackage` capability binds release metadata, exact local
package path, and the independently pinned root/product/channel/schema scope.
A package verified by a different root is rejected before staging or backup,
even if its product/channel strings are identical. Stricter later resource
budgets remain enforceable without creating a new trust store.

## Signed target and builder protocol

The TUF target is a ZIP containing exactly these files:

- `installer-build.json`, from the current compiled standard builder.
- `bundle-manifest.json`, the exact complete payload manifest.
- `dist/Resonance-Setup-<source-first-12>.exe`, the exact compiled setup.

The build report must say `status: compiled` and
`managed_update_protocol: resonance-standard-update/1`. It binds the full
source commit, manifest digest, compiler/recipe digests, setup size and setup
digest. The trusted manifest validator requires the complete current Python
and native launcher payload and its normal OCR/source inventory. The setup is
streamed and hashed; a compressed target's length is not mistaken for its
uncompressed setup size.

The signed release build must advance the configured and protected build.
Its full source commit must differ from A, and its version must match B's
installed application package metadata. Only the currently supported library
schema is accepted; this transaction performs no schema migration.

The existing builder still prepares/compiles from an explicitly reviewed local
bundle and compiler. The probe below can assemble its three-file synthetic
test target from such outputs. No runtime command in this feature signs
production releases or exports an issuer key.

## Transaction and application lifetime

The transaction proceeds through these guarded boundaries:

1. Recheck the current registered A against its exact manifest and native
   metadata. Retain a shared installation lifetime lease.
2. Verify and extract the TUF-authenticated target into a new bounded stage.
   Review the current standard report and complete manifest, and retain the
   exact verified target archive under the fixed transaction identity.
3. Create a complete library backup using the existing typed backup framework.
   Recheck A's registration, Start-menu shortcut and uninstaller bytes. With an
   explicitly configured Windows vault anchor, verify the backup and save/read
   back the compact transaction authority before any pending native handoff.
4. Persist the previous activation snapshot and pending handoff, then release
   the coordinator's Inno setup mutexes before launching the actual setup.
5. Inno owns those same mutexes during installation. Its fixed handoff checks
   the transaction token, its compiled source/manifest, its own setup digest,
   and the retained A interpreter. The fixed module in A rechecks the previous
   activation and reserves one fresh B directory before it can be created.
6. Wait for the entire existing owned Windows process job, including an Inno
   clone that outlives its initial process. Reacquire the setup mutexes and bind
   the observed B registration, shortcut and uninstaller snapshot.
7. Verify B's source-bound bundle/application receipt, native components,
   installed self-test and package version. Revalidate the complete backup,
   then commit the same protected build anchor. Only then unblock B's ordinary
   startup and mark the journal complete.

The shared application lease allows A and its current jobs to continue while
a fresh B is installed. Destructive uninstall/retirement still needs the
exclusive lease. No process is identified by a stale PID or terminated merely
because it runs the previous version.

A pending B cannot start an ordinary application before commit. The exact
isolated bootstrap self-test is the sole early entry exception. A process
already holding the application lifetime is not re-gated by new pending state.
An uncommitted rejected B remains on disk for diagnostics and is blocked from
ordinary startup after rollback.

The owned process helper bounds time and diagnostic output, including the
post-leader descendant wait. If native cleanup is not yet confirmed, the
existing cleanup owner retains the actual shared lease and update lock.
Rollback never races that still-owned child.

## Recovery and data preservation

For a fully observed but uncommitted B, rollback may restore only exact recorded
A/B registry values, shortcut bytes and uninstaller components. A third value,
newer installation or unknown component is preserved and causes explicit
recovery refusal. B's files and failed environment remain; A's program
generation and research data are not moved or deleted.

The journal is mutable recovery evidence, not publisher approval. A live apply
failure still holds its original verified package and may perform the guarded
rollback. A retry through that same live controller requires a freshly verified
`VerifiedPackage` with the same pinned publisher root, product, channel, schema
and exact release. The existing controller obtains it only from an explicitly
selected or already configured signed repository. With no fresh proof,
`recover(package=None)` refuses without changing activation, pending receipts
or research data. Different roots, different releases and rewritten journal
metadata cannot authorize rollback.

The signed B package does not authenticate A's original native registration,
shortcut or generated uninstaller bytes. An explicitly configured
`WindowsVaultAnchor` can additionally protect their exact prior digest across a
controller restart. The transaction reuses that anchor's existing vault and
opaque reference with a distinct, transaction-specific domain binding. A compact
credential binds the canonical program/data/work roots, policy/bootstrap,
before-anchor, exact release/setup candidate, complete backup identity and
bounded prior snapshot bytes. The large prior stays in the existing `PRIOR`
file; no anchor schema, separate vault, issuer or default authority is added.

After restart, fresh same-scope TUF verification, the exact protected digest,
complete backup revalidation and unchanged native A/observed B are all required
before rollback. Missing, corrupt, replayed or coherently rewritten journals and
prior bytes cannot supply that authority. A newer C remains untouched. The
credential is deleted only after terminal state and pending cleanup, and only
when its exact value still matches. Credential errors preserve research and
prevent native setup or require explicit cleanup review. A generic/in-memory
anchor has no durable recovery capability and retains the previous refusal
for restarted pre-commit controllers.

A crash before the complete new activation can be observed may leave
`recovery-required`. The application does not guess a registry/shortcut
rollback. The pending journal and retained components remain available for
owner inspection, while A remains usable.

After the protected B anchor has committed, a final journal/cleanup failure
cannot silently roll back to A. Explicit recovery first streams the retained
target archive against the protected accepted digest, extracts a fresh checked
snapshot, and derives the candidate source, manifest and setup identity again
from those exact bytes. Only then may it verify committed B and complete the
journal. Simultaneously rewriting the journal and retained archive does not
replace the protected digest. Missing or changed archives and changed protected
anchors leave pending evidence intact for explicit recovery.

The existing Help → License and signed updates dialog offers a second,
explicit “Back up and activate this verified update” review when a standard
transaction is configured. “Recover reviewed standard update” uses that same
transaction and asks for the signed repository when an uncommitted recovery
needs re-verification. The existing security CLI accepts an explicit
`recover-standard-update --repository <folder>` for that same boundary, and
exposes these operations only via
an explicitly injected `TrustController`; ordinary invocation without one
stays `WAITING_INPUT`. Repository/root flags for offline verification alone
do not configure activation.

## Source and native qualification

The new source suite is `tests/security/test_standard_update.py`. It uses real
TUF metadata/signatures, files, typed backup/restore and owned Python processes.
Its registry/shortcut/installer adapter is explicitly synthetic. It tests
foreign-root refusal, exact metadata rollback, interruption before/after anchor
commit, newer-state refusal, complete-backup tampering, pending/rejected startup,
old-process lifetime, GUI review choice, CLI/controller parity and bounded
cleanup. New-controller recovery with the same explicit protected vault is
exercised at prepared/installing/activated/health/committed crash boundaries;
generic anchors and coherent prior-component rewrites still refuse changes.
Native vault API stubs distinguish only actual ERROR_NOT_FOUND from failures. These tests do not claim that an inert
setup image is a native EXE.

Run it with the current reviewed optional trusted-client dependencies:

```text
python -m pytest tests/security/test_standard_update.py tests/security/test_updates.py tests/security/test_installation.py tests/security/test_independent_review_boundaries.py tests/security/test_publication_integrity.py tests/security/test_controller.py tests/packaging/test_standard_installer.py tests/packaging/test_installation_lease.py
```

Native acceptance is a separate disposable-account command:

```text
python packaging/probe_standard_update_windows.py --disposable-ci ^
  --previous-build <real-current-builder-output-A> --previous-source <full-A-sha> ^
  --previous-setup-sha256 <exact-A-setup-sha256> --previous-manifest-sha256 <exact-A-manifest-sha256> ^
  --candidate-build <real-current-builder-output-B> --candidate-source <different-full-B-sha> ^
  --candidate-setup-sha256 <exact-B-setup-sha256> --candidate-manifest-sha256 <exact-B-manifest-sha256> ^
  --candidate-version <actual-B-package-version> --work <new-disposable-directory>
```

The interpreter must be an actual installed application, with the test-only
TUF fixture available from this exact checkout. Both real compiled setups must
contain this handoff implementation. An older A without it needs the existing
explicit manual fresh-generation repair first; the bridge never pretends that
such an installation already has the new protocol.

The probe refuses an existing registered installation or Start-menu entry. Its
15 required checks cover actual A install, a running A, verified B target,
controlled rejection after real B health checks, exact native rollback,
retained failed B, complete library restore, successful A→B, active-uninstall
refusal and quiescent uninstall that preserves data. It uses ephemeral
synthetic test authority and a synthetic protected anchor. It does not qualify
production keys, a production service, Authenticode, NVDA or physical devices.

The source suite and related security/packaging suites run independently of the
two-build native probe. Only an actual Windows probe result establishes native
A→B execution. Passing previous repair/uninstall or portable update tests does
not transfer such a result. Production trust, signing and accessibility
qualification remain separate from synthetic test authority.

## Exact two-generation CI integration

`scripts/ci_standard_update.ps1` runs after the existing successful standard
installation/repair/uninstall gate in `.github/workflows/carr-windows.yml`.
That gate writes a private, hash-bound `CARR_STANDARD_B_HANDOFF` only after its
native checks pass. The update driver rechecks that proof, the compiled B,
complete sealed bundle, installed interpreter and compiler; it does not rebuild
or relabel B as A.

The machine-readable insertion steps are
`packaging/standard_update_workflow.json`. The additional checkout must run
before source builds and firewall-protected installed checks. Its exact A
commit is `f26c7eeef2ad3090ab531d1b8bca72ba68eb5991`; the executing B must be a
distinct real descendant, checked from the complete public Git graph. The
existing runtime/model/OCR/native inputs are reused only after A's dependency
lock, SBOM and native-source validator accept them. No package, compiler,
runtime, model or publisher configuration is downloaded by this driver.

The driver builds A's actual wheel from its own checkout with offline installed
build tools, verifies every wheel member against A's committed source, builds
A's own sealed payload and current standard setup, and checks both source
identities and different wheel digests. The public A wheel artifact includes
its source commit, tree recorded in the companion inputs proof, wheel digest,
size and source inventory receipt. Runtime/model payloads and setups remain
private on the disposable runner.

The current probe accepts `--previous-bundle` and `--candidate-bundle` for exact
separate sealed inputs after verified builder copies have been removed. It
still checks their complete inventories and exact report/manifest/setup hashes.
`--report` writes a new native JSON proof only after all 15 checks pass. The
driver requires the exact check multiset, typed qualification flags and both
source/setup/manifest identities, then rechecks A, B and the untouched original
full-bundle seal. The actual native execution is still a separate Windows gate;
source syntax and fixture tests alone do not qualify either compiled setup.
