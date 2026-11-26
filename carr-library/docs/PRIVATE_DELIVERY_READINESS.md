# Private delivery integration readiness

## Recovery draft status: NOT RUN

This branch preserves source prepared when the execution environment disconnected
on 2026-11-26. The proposed synthetic tests were not executed. No compile, package,
native Windows, cryptographic or release PASS is claimed. This draft is separate
from the core integration branch and does not enable licensing restrictions.

## Boundary and integration requirements

The delivery_contract module is a versioned public boundary for a separately
supplied, qualified private runtime. It is preparation, not a license verifier,
activation service, signed updater or proof that a provider is trustworthy.
Entitlement and update capabilities are independent. No private implementation,
issuer keys, deployment endpoints or business policies are included.

An absent provider has an explicit status. Provider declarations remain
declared_unqualified; they never become a release PASS. Normalized decisions
must match the requested product, build, channel, operation and request identity.
Malformed, incompatible or unavailable adapter responses produce a redacted
unavailable result. The reserved read, save, export, backup, restore, recovery,
settings and active-job completion actions remain independent of entitlement.

The existing private application does not call this boundary or gain a new
restriction. There is no automatic adapter loading, account creation, network
call, renewal, payment, update installation or change to delivered rights.

Actual integration needs a compatible private runtime package and adapter,
reviewed trust roots and policy schema, independently verified signatures and
identities, durable freshness/clock/key-rotation state, and native installation
and recovery tests. An updater additionally needs signed product-bound expiry,
rollback protection, verified staging, preserved data and real A-to-B recovery.
An owner endpoint and signing facility must be configured privately before
website delivery. Offline and perpetual profiles require their own explicit
compatible policy; they cannot be inferred from an expiring account profile.

The proposed tests use synthetic in-process adapters only. They target the
application boundary and data-access preservation, not cryptographic signatures,
a deployed authority, native security or signed release.
