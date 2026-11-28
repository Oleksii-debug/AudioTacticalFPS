# Private delivery integration readiness

## Implemented client source

The public delivery boundary and the signed client are separate layers.
[`delivery_contract`](../src/carr_library/delivery_contract.py) normalizes declared
provider capabilities and decisions; that module itself does not verify a
signature or install an update. The implemented
[`security` client](TRUSTED_CLIENT.md) performs signed permission verification,
protected freshness checks, offline TUF update verification and the existing
installation transaction. Describing the whole product as only a nonverifying
provider contract is no longer accurate.

| Area | Implemented behavior | Required owner configuration or qualification |
| --- | --- | --- |
| Permission verification | Ed25519 envelope verification, product/subject/device/policy checks, exact reviewed terms, protected sequence/digest/clock state and publication recovery | Independent keyring, actual terms and policy, OS-vault anchor provisioning and a compatible issuer |
| Renewal | One cancellable request through an explicitly supplied `AuthorityTransport`, nonce and response verification, opt-in idle scheduling and backoff | Reviewed online transport, vault credential reference, endpoint and issuer behavior; fixture renewal cadence does not set business terms |
| Signed updates | `LocalRepositoryClient` uses real TUF metadata and target verification, product/channel/build checks and bounded local repository transport | Independently trusted bootstrap root, actual signed repository and target bundle; no default HTTP update host is supplied |
| Portable activation | `PortableUpdateHandoff` uses the existing backup/installation transaction, authenticated candidate and retained A identity, exclusive installation lease, owned native child cleanup, recovery and explicit launcher restart | An explicitly sealed portable profile and trusted external helper configuration; exact-bundle native and rollout qualification |
| Standard installation | Existing managed setup/Repair/Uninstall coordination, shared application leases and conservative bootstrap cleanup vetoes | The qualified setup and its ownership metadata; managed automatic updates need their own authenticated setup handoff and cannot use the generic portable folder swap |

The implementation and configuration contracts are documented in
[Signed permissions and offline updates](TRUSTED_CLIENT.md),
[Portable update activation and recovery](PORTABLE_UPDATES.md), and the
[standard Windows installer guide](../packaging/windows_installer/README.md).

## Rights, authority and installed lifetime

An absent provider has an explicit status. Provider declarations remain
`declared_unqualified`; they are not cryptographic approval or release evidence.
Normalized decisions must match the requested product, build, channel, operation
and request identity. Malformed, incompatible or unavailable adapter responses
produce a redacted unavailable result.

The reserved read, save, export, backup, restore, recovery, settings and
active-job completion actions remain independent of entitlement. Existing
private operation does not acquire new licensing restrictions. Offline and
perpetual profiles remain explicit policies; they cannot be inferred from an
expiring account profile. No account creation, payment, signing service or change
to delivered rights is enabled by importing the modules.

Configured background checks are opt-in. They do not install an update or
restart the application. Portable activation is a separate external-helper
operation for the sealed ownership protocol; an unmarked bundle is not enrolled
by copying a marker. Active application leases and unconfirmed owner receipts
refuse activation. A cleanup error or helper crash preserves the veto and
recovery state rather than treating a missing PID as proof that movement is safe.
The standard managed profile retains its own installer ownership boundary.

## Acceptance and remaining delivery inputs

The original synthetic `delivery_contract` tests establish the provider boundary
and data-access preservation. The additional security and installation suites
exercise real Ed25519/TUF verification, actual filesystem recovery, compiled
Windows launchers, native process/Job/lease lifetime and installed module origins.
These are different evidence scopes; they must not be collapsed into a claim
that a production authority or delivery channel is qualified.

Use the [current source and execution readiness ledger](V3_RELEASE_READINESS.md)
for exact commit/tree results and the
[portable qualification selection](PORTABLE_UPDATES.md#required-qualification)
for the required source and installed Windows gates. A prior passing run does
not qualify changed source, another wheel or an owner's deployed service.
Manual Windows/NVDA checks and actual release activation keep their own evidence.

Release preparation still needs owner-reviewed policy and rights, issuer and
publisher trust material, signing/key custody and rotation procedures, private
credential provisioning, any selected online transport, the actual signed
distribution and its distribution/rollback qualification. The repository does
not contain private issuer keys, deploy an authority or approve website delivery.
This document records implemented integration paths and remaining delivery
inputs; it does not issue a release approval.
