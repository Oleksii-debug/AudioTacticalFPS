# Signed permissions and offline update review

This bounded integration reuses PR152 at `76337dc932aec089e51813cb30cdbd0e6f21c42f`
with the canonical delivery contract and existing assistant credential vault.
It adds reviewed cancellation and native owner-lifetime corrections. The
portable activation backend is separately reused from reviewed checkpoint
`8c981c7e947f8d144c8706b5ea022990517c7dd9`, with a narrow first-move recovery
correction. No desktop or CLI action invokes application activation.


This supplies neutral client contracts for V3-16, V3-17, V3-18 and V3-22.
It does not select a production issuer, endpoint, account system, billing rule,
signing key or update host. No private owner runtime or signing service was
copied. No model payload is downloaded.

The current private installation remains perpetual and offline. Importing the
module does not gate existing application operations, start a network task or
change accepted terms. Help → License and signed updates presents native
readable status. Permission import, renewal and offline update actions appear
only when trusted host code supplies configured clients.

## Implemented behavior and owner boundary

| Area | Client behavior | Owner prerequisite |
| --- | --- | --- |
| Permission document | Real PyCA Ed25519 verification; exact schema, product, opaque subject/device, key, policy, validity, feature and sequence checks | Independently trusted keyring, actual terms and policy |
| Durable state | Last verified signed bytes, protected monotonic sequence/digest/clock anchor, pending/current crash recovery | Explicit OS-protected anchor provisioned for one client root |
| Renewal | Explicitly enabled idle scheduler, one background request, synthetic 7/14-day cadence, separate expiry/grace, nonce binding, persistent backoff and changed-terms review | Configured signed-renewal provider and canonical OS-vault profile/reference |
| Update | Real python-tuf verification of roles, signatures, version chains, expiry, target lengths/hashes | Independent bootstrap root/digest and product/channel/build policy |
| Offline repository | Bounded local metadata/target fetcher without network or redirect fallback | Owner-produced signed repository and application bundle |
| Activation and rollback | Portable transaction backend with verified staging, backup, protected commit and rollback; a download never invokes it | A separate reviewed restart supervisor, concrete idle lease and actual Windows A-to-B recovery qualification |
| Native UI | Named status/terms controls, explicit acceptance before import, background offline check/download | Actual production authority and activation supervisor remain unconfigured |

## Permission format

`security.entitlements.TrustPolicy` is constructed by trusted host code. It
contains product, opaque subject, optional device identity, a bounded Ed25519
keyring, supported policy versions and known features. Key IDs are SHA256 of
the 32 public-key bytes. Key generation supports an independently distributed
overlap during rotation; new renewals cannot return to an older generation.
Historical verification can retain a retired key. Documents cannot introduce
keys or policy versions.

The envelope has exactly `version=1`, `kind="carr-entitlement"`, `key_id`,
`payload` and `signature`. Payload is canonical Base64 of exact UTF-8 JSON bytes.
Signature is canonical Base64 of Ed25519 over
`b'CARR-ENTITLEMENT-v1\x00' + payload_bytes`. Signed bytes are not reserialized
before verification. Duplicate JSON keys, non-finite values, unknown fields,
malformed encoding and oversized documents are rejected. Envelope/payload
budgets are 64/32 KiB. This neutral format is not presented as the owner's
existing wire protocol or as a general JOSE/COSE implementation.

Payload fields are exactly `schema`, `product`, `entitlement_id`, `subject`,
`device`, `sequence`, `profile`, `issued_at`, `not_before`, `expires_at`,
`offline_until`, `refresh_interval`, `policy_version`, `terms_sha256`,
`features`, `revoked` and `request_nonce`. Schema is integer 1; times are UTC
epoch seconds; sequence increases monotonically. Profiles are `perpetual`,
`renewable` and `evaluation`. Perpetual grants cannot require expiry or periodic
renewal. Renewable/evaluation expiry and offline grace are separate signed
values. Synthetic 7/14-day refresh does not establish production terms.

`TrustController` requires the actual owner terms text whose UTF-8 SHA256 equals
the signed identity. A digest alone cannot be accepted in native UI. The user
reads profile/features/validity/terms and explicitly checks acceptance. Exact
reviewed bytes are retained, so replacing the chosen file while its dialog is
open cannot substitute another grant.

Automatic renewal must retain accepted terms, policy version and profile;
feature removal requires review. A response must match the outstanding random
nonce and advance sequence. Signed revocation stops new managed work without
removing data access. There is no implicit new-account/new-contract flow.

`EntitlementStore.begin(feature)` grants an operation lease at start. A host
must not recheck it midway through an authorized save/render. Reading, export,
backup, recovery, settings and prior outputs remain permitted after expiry,
revocation, clock rollback or damaged managed state. Existing application
entrypoints are not changed to require this optional client.

## Protected persistence

Writable JSON alone cannot establish rollback-resistant state. `AnchorStore`
has explicit `load` and `compare_and_set` operations. `WindowsVaultAnchor`
adapts the existing Windows Credential Manager backend, with no plaintext
fallback. Fixed diagnostics avoid leaking authority exceptions or credentials.

Trusted setup constructs `WindowsVaultAnchor(reference, binding, product=...,
profile_id=...)` and explicitly stores `anchor.encode(Anchor())` through the
existing `WindowsCredentialVault.put(profile_id, value, reference=reference)`.
The protected value binds the product, profile, credential reference and
installation binding; a plain anchor JSON value is not accepted. Missing/inaccessible protected state
requires owner recovery. Deleting the local directory never resets sequence.
One reference belongs to one private client root. Its cross-process file lock
serializes load, comparison and protected advancement, including separate
client instances. Cancellation/deadline guards run again after the final
potentially blocking vault comparison and immediately before the protected
write. After that advance, publication completes or recovers as committed.
This does not claim hardware anti-tamper or resistance to an administrator,
replaced executable, hostile Python code or compromised user vault.

Publication order is pending signed file + fsync, protected anchor advancement,
then atomic current-file publication. Before anchor advancement the old grant
remains authoritative. After it, recovery publishes pending bytes only when
their digest matches the protected anchor. Missing/replayed bytes cannot
authorize new managed work. Material backwards clock changes require attention.
UTC high-water status writes are limited to once per minute and also occur on
accepted grants.

Signed state belongs outside the program directory. Updates leave it, the OS
vault, libraries, model settings, queue paths and prior outputs in place.
The general library backup does not export the protected anchor or make a
license portable. A different device requires owner recovery; device/path
bindings are not silently changed.

## Renewal adapter

`RefreshRequest` contains only product, opaque subject/device, entitlement ID,
last sequence, nonce, canonical credential profile/reference and timeout. It contains no book
text, library path, search, note or output content.

The owner supplies an explicitly configured `AuthorityTransport` whose
`describe()` declares `delivery_contract.Capability.SIGNED_RENEWAL`, and whose
`renew(request, cancelled)` returns the signed response. The declaration is not
a qualification claim. This is the existing delivery-provider boundary, not an
AI provider or an additional built-in HTTP stack. The adapter must
resolve credentials through the OS vault, enforce its HTTPS/redirect rules,
honor the bounded timeout and cancellation, and return signed bytes. No default
URL, embedded key, login loop or automatic network startup is provided.

`RefreshWorker.tick()` admits at most one request. Its local vault/signature
checks can block, so it runs only in the existing job executor or the opt-in
`IdleRenewalHost` background scheduler. The native timer publishes the host's
idle state without opening credentials, performing crypto or touching research
data. Automatic renewal is disabled unless trusted host configuration explicitly
sets `automatic_refresh=True`; there is no default authority. Manual checks use
the existing cancellation/progress lifecycle. A late reply cannot advance the
protected grant after cancellation or deadline. Failures retain the last
verified grant and save bounded exponential backoff
with jitter. Changed terms/profile suspend automatic retries until explicit
review. Close signals cancellation without an indefinite join. An adapter
ignoring timeout remains one in-flight daemon; it cannot create a thread storm.
Production transport behavior is an owner prerequisite, not established by the
synthetic transport tests.

## TUF and repository rules

The exercised versions are `cryptography==50.0.2`, `tuf==7.0.1`,
`securesystemslib==1.6.0`, `urllib3==2.8.0`, `cffi==2.1.1` and
`pycparser==3.12`. `packaging/trusted-client.lock` contains checked PyPI release
wheel digests. Install with `--only-binary=:all: --require-hashes`. Private
status still works without optional TUF/crypto imports.

Bootstrap root bytes and their SHA256 must come from independent trusted
installation material, never both from the repository under examination.
`Updater` always receives explicit bootstrap bytes, never `bootstrap=None`.
Its internal root-history symlink or unprivileged Windows hardlink is checked
and never becomes the bootstrap authority.

A repository contains `metadata/` and `targets/`. The fetcher serves only these
local files. Other schemes/hosts, redirects, queries, fragments, traversal,
links and redirected parents are rejected. Metadata size caps are independent
of signed lengths: root 512 KB, timestamp 16 KiB, snapshot 2 MB, targets 5 MB.
Root rotation is limited to 32 steps; this adapter does not enable delegations.

Signed target custom fields are schema 1, kind `carr-application`, product,
channel, version, increasing build, exact source commit, unchanged data schema
and bounded notes. Wrong product/channel, rollback, unsupported migration and
target changes after review are refused. Source commit is a publisher-signed
provenance assertion, not proof of reproducible compilation. Only the TUF
download path produces `VerifiedPackage`; a bare path/checksum cannot
accidentally construct verified approval. The receipt is bound to its exact
immutable release fields and normalized package path; copying it onto changed
public fields is rejected again when consumed. This is a trusted Python API contract,
not a sandbox against malicious Python code.

Downloaded bytes remain in the client cache. The existing TUF verifier checks
exact target length and hashes before create-only publication. Cancellation is
checked after the final hash and immediately before publication, including
revalidation of an existing cached package. A cancellation after publication
preserves the committed verified package. Failed or cancelled preparation
before publication never produces a new destination; an existing verified
package is retained. Verified downloads are reusable; partial byte-range
download resume is not implemented.

## Portable activation backend and remaining host boundary

The existing UI and CLI implement review and download only. They provide no
activation button or managed-install lease. Trusted host code can separately
construct `security.installation.UpdateTransaction` with the existing library,
protected update anchor, verified bundle adapter and an explicit `idle_lease`.
The portable backend accepts only the existing TUF-issued package receipt,
checks a private bounded ZIP snapshot, creates a checked library backup, and
uses same-filesystem program-directory moves. Program, update workspace and
research data must remain separate. The existing installer verifies the exact
source/wheel receipt; it builds the new environment at its final path. Managed
installations with their separate installer ownership receipt are rejected.
This includes the current Inno layout: `install-layout-v1.txt` at the program
root, `versions/<source>-N` generation directories and installer-generated
`maintenance-setup.exe`. Verify, prepare, activate and health all refuse that
ownership before verification or execution; incomplete/unknown ownership
markers are never treated as permission for a portable update. The older
`app` plus parent `managed-install.ini` layout remains refused as well.

The transaction retains the previous installation and backup until health and
protected build advancement succeed. Recovery never rolls back an already
committed protected build. Missing previous-installation bytes after an
interrupted activation require owner recovery; the existence of a directory
at the program pathname is not proof that the original program was restored.

If the first native rename raises before moving the original, the still-live
transaction may publish `rolled-back` only after matching its captured directory
device/inode, exact manifest SHA256, and a fresh verification of every sealed
bundle file. The protected anchor must remain unchanged and no previous or
failed destination may exist. The original rename error is retained, and a
later explicit retry creates a new transaction. A replaced directory, changed
manifest, changed sealed file or failed terminal-journal write cannot acquire
this successful-recovery state. This live proof is not a new journal authority:
version 1 remains unchanged, and a restarted process still refuses to infer
the original installation when its retained previous directory is missing.

The Windows adapter uses the existing owned-process runner with bounded native
attempts and diagnostics. If native cleanup remains unconfirmed, it waits off
the desktop thread while retaining the caller's transaction/idle ownership;
rollback cannot retarget a live installer. Catchable interruptions during that
wait are retained: the same cleanup owner must first confirm quiescence, then the
first interruption is re-raised and the update remains unaccepted. Repeated
interruptions cannot release the update, idle or library-writer locks early.
An interrupted activation may therefore leave its version 1 journal unfinished
until a later explicit recovery, after the child has stopped. This does not
provide containment after an abrupt process kill or machine loss.
The wait can remain pending until the existing cleanup owner confirms quiescence.
A concrete supervisor outside
the program directory must prevent new work, retain ownership through shutdown,
run the transaction and restart the accepted installation. This host is not
provided by the backend or by its synthetic idle fixture. Actual installed
Windows A-to-B activation, process-loss recovery and idle/restart ownership
remain software integration and acceptance requirements, not only missing
publisher keys.

Signed state belongs outside the program directory. The current private
installation remains perpetual and the existing delivery boundary preserves
reading, export, backup, recovery, settings and prior outputs. Adding this
optional client does not begin entitlement enforcement at existing entrypoints
or imply compatibility with an owner's private issuer wire format. Trusted host
code must explicitly provide the actual product, policy, terms, key/root and
provider configuration; none is inferred from a downloaded document.

## Running and verification

Private status:

~~~console
python -m carr_library.security status
~~~

Independent-root offline check:

~~~console
research-library-trust check-update --repository OWNER_REPOSITORY \
  --bootstrap INDEPENDENT_ROOT_JSON --bootstrap-sha256 INDEPENDENT_ROOT_DIGEST \
  --product OWNER_PRODUCT --channel OWNER_CHANNEL --current-build INSTALLED_BUILD \
  --cache PRIVATE_UPDATE_CACHE --target SIGNED_TARGET_PATH
~~~

`download-update` adds the exact `--reviewed-sha256` printed by check.
`verify-permission` verifies explicit public inputs without importing or
activating a grant. There are no signing, billing or account-management
commands.

~~~console
python -m pytest tests/security -q
# On Linux only: add -k "not test_real_windows_vault_anchor_roundtrip"
~~~

The focused suite uses real Ed25519 signatures and real TUF metadata, tests
binding/schema/signature/policy rejection, nonce/sequence/key rotation, crashes
around protected publication, expiry/grace/clock rollback, 7/14-day refresh,
backoff/terms/cancellation, metadata expiry/rollback/mixing, malicious targets,
final verified-payload publication, and native callback/owner destruction boundaries.

The existing Windows validation must select the actual Windows Credential
Manager roundtrip and `tests/ui/native_trust_status_smoke.py --native` against
the installed distribution. This native probe checks Help dispatch, named
read-only status, actual terms and checkbox acceptance; its synthetic signing
key does not establish production authority. Native `NOT_RUN` exits nonzero
and cannot count as passing. Linux explicitly deselects the one OS-vault case;
the installed Windows gate executes it and rejects every skip, expected failure
or missing required suite. The native helper also exercises direct parent
destruction during permission review and during the metadata timer lifetime.
Read exact-commit CI artifacts before claiming Windows acceptance; manual NVDA
use and production bundle activation remain separate gates.

Primary upstream APIs reviewed:

- https://cryptography.io/en/50.0.2/hazmat/primitives/asymmetric/ed25519/
- https://theupdateframework.readthedocs.io/en/stable/api/tuf.ngclient.updater.html
- https://theupdateframework.readthedocs.io/en/stable/INSTALLATION.html
- https://github.com/theupdateframework/python-tuf/tree/v7.0.1
- https://pypi.org/project/tuf/7.0.1/
- https://pypi.org/project/cryptography/50.0.2/
