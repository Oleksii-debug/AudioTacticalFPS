# Signed permissions and offline updates

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
| Installation | Bounded ZIP staging, existing installer verifier, full-library backup, program activation, health check and rollback recovery | External restart supervisor and real idle lease preventing new jobs |
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

Package length/hash are verified while making one private temporary snapshot.
Central-directory inspection and extraction use that retained descriptor, so
replacing or rewriting the downloaded pathname cannot substitute archive bytes.
Current policy limits apply before snapshot creation, and ZIP central-directory
budgets are checked before allocating entry objects.
Extraction rejects links, traversal, Windows special/alternate-stream names,
duplicate/case-aliased paths, file/directory collisions, encryption and injected
virtual environments. Target bytes, entries, unpacked bytes, free disk and
cancellation are bounded. Failed staging never becomes current. Verified
downloads are reusable; partial byte-range download resume is not claimed.

## Automatic read-only update checks

Trusted host configuration may supply the existing update client and exact
target together with `update_repository=OWNER_LOCAL_REPOSITORY` on
`TrustController`. Checks remain off unless `automatic_updates=True` is explicit.
No repository, endpoint, account or bootstrap trust is selected by default.

The existing native idle timer owns `IdleUpdatePollHost` alongside the renewal
host. One background thread calls only `TrustController.check_repository`, so
the same TUF root, product/channel/build policy, metadata limits and rollback
checks apply. The host checks metadata; it never calls package download,
installation, restart or entitlement renewal. A signed release remains display
information and does not become a `VerifiedPackage`.

`update_interval` defaults to 3,600 seconds (explicit range 1–86,400 seconds);
`update_timeout` defaults to 20 seconds (range 0.01–60 seconds). Failures use
bounded exponential retry, capped at one day. Only one check can be in flight.
Idle withdrawal, cancellation, deadline or changed client/target binding prevents
a late result from replacing the cached release. Accessible status uses fixed
diagnostics and retains the last verified release after failure. The schedule
and status belong to this application instance; reopening starts one new
configured check, without a second durable job store.

Metadata verification and its existing private TUF cache run outside the UI.
No library, rendering, save, source or output lock is acquired. Closing the owner
signals cancellation and uses a bounded join. An unusually slow local filesystem
can retain one daemon until its read returns; it cannot block application close,
start extra workers or publish a late result. This is not an HTTP update-server
adapter: supplying production transport or signed releases remains an owner
responsibility. Manual review and download remain separate explicit actions.

## Installation transaction

`UpdateTransaction` requires disjoint program, update-work and data directories,
with program/work on the same filesystem. Its host idle lease must prevent
new library/audio/jobs for the entire transaction. A running application must
use an external restart supervisor outside the program directory. A no-op
lease is permitted only in explicit synthetic fixtures.

B is verified/staged, the existing complete-library backup is created, the
transaction is journaled, A is retained, and B is placed at its final program
path. The installer runs there, followed by health and exact installed-version
checks. Only then does the protected build anchor advance. Failure before that
anchor restores A and retains backup/failed candidate. Crash recovery
distinguishes an uncommitted B from a committed build; it cannot silently roll
back a protected committed version.

`WindowsBundleAdapter` receives the existing reviewed
`packaging/install_windows.py:verify` from the installed host. Proposed code is
not imported during preview. The existing offline installer runs only after
the authenticated bundle occupies its final path: building a venv in a
temporary staging location would bake invalid absolute paths into it.
Subprocess arguments are fixed and timeouts bounded. The exact manifest
snapshot passed to the existing verifier must name the signed release's source
commit. When the canonical refresh producer supplies `application-source.json`,
its inventoried hash, source commit, project version and application wheel digest
must also agree. This uses existing provenance, not a second signature format.

A production restart/idle supervisor is not implemented by this adoption.
Independent root material, a real signed Windows bundle and its qualification
are also still required before managed activation can be exposed. UI exposes
working check/download, without an unimplemented activation button. Update
server transport is absent; explicit manual or configured automatic local
metadata checks use the same verified repository client.

This transaction delegates backup and restoration to the current canonical
complete-library backup. It does not introduce a competing journal, media
producer or reading-session schema. Application-only updates retain external
input/output paths rather than silently retargeting them. Tests verify quotation text, research notes,
bookmarks/resume, saved/recovery reading derivatives, unchanged retained
queue/voice/license/output bytes and restoration of the pre-update backup.
The tiny synthetic executable and real subprocess health check do not
represent an actual Windows application wheel installation.

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
~~~

The focused suite uses real Ed25519 signatures and real TUF metadata, tests
binding/schema/signature/policy rejection, nonce/sequence/key rotation, crashes
around protected publication, expiry/grace/clock rollback, 7/14-day refresh,
backoff/terms/cancellation, metadata expiry/rollback/mixing, malicious targets,
ZIP budgets, and A→B activation/rollback with populated-library recovery.

The existing Windows validation must select the actual Windows Credential
Manager roundtrip and `tests/ui/native_trust_status_smoke.py --native` against
the installed distribution. This native probe checks Help dispatch, named
read-only status, actual terms and checkbox acceptance; its synthetic signing
key does not establish production authority. Native `NOT_RUN` exits nonzero
and cannot count as passing. Linux skips the native vault test explicitly.
Read exact-commit CI artifacts before claiming Windows acceptance; manual NVDA
use and production bundle activation remain separate gates.

Primary upstream APIs reviewed:

- https://cryptography.io/en/50.0.2/hazmat/primitives/asymmetric/ed25519/
- https://theupdateframework.readthedocs.io/en/stable/api/tuf.ngclient.updater.html
- https://theupdateframework.readthedocs.io/en/stable/INSTALLATION.html
- https://github.com/theupdateframework/python-tuf/tree/v7.0.1
- https://pypi.org/project/tuf/7.0.1/
- https://pypi.org/project/cryptography/50.0.2/
