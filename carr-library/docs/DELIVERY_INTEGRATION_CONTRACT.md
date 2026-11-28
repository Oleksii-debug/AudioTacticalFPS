# Delivery integration contract

This is a public, product-neutral interface for a separately supplied, qualified
private delivery/security runtime. It does not add an activation requirement to
the current application. GUI, CLI and assistant entrypoints remain on their
existing authorization paths.

The module and original adapter tests reuse the existing canonical
[delivery contract at bfcc1f5](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/bfcc1f5cae137cc27030c0cec6dc46a1641892c1/carr-library/src/carr_library/delivery_contract.py).
The current port adds strict status records, bounded display-only update sizes
and a pure refresh scheduling function. It does not introduce a second
cryptographic, credential, update or account implementation.

## Implemented boundary

- Product, build, channel, request and operation identities are bounded,
  non-secret identifiers.
- Provider declarations are immutable typed capabilities and require the
  supported contract version. The status `declared_unqualified` describes
  configuration, not signature verification or release acceptance.
- Entitlement decisions must return the exact request context, request identity
  and operation, with a consistent normalized state/reason pair. Missing,
  incompatible, malformed and failing adapters cannot authorize a new operation.
  Private exception details are not exposed.
- Reading, opening, saving, export, backup, restore, recovery, settings and
  completion of an active job use an explicit reserved action set. These actions
  return `data_access_preserved` without contacting the adapter, including when
  a provider is absent, denies a feature or is unavailable. Similar-looking
  action prefixes do not inherit this exemption.
- Update display offers carry a product/build context, offer identity, target
  build, bounded byte count and SHA-256. These values are not authority to
  install bytes. The public protocol supplies no download URL, executable
  command, account token or signing material.

The private adapter is explicitly supplied trusted application infrastructure.
There is no automatic plugin discovery from library files or user settings.

## Refresh cadence is separate from offline validity

`RefreshCadence`, `RefreshCheckpoint` and `plan_refresh` are pure scheduling
metadata. They perform no networking, sleeping, persistence or entitlement
verification and do not start a thread or timer.

An enabled interval must be supplied explicitly. `None` disables unattended
refresh, including retries; a private perpetual profile can therefore remain
independent of periodic contact. The tests use synthetic seven-day and
fourteen-day intervals. Neither is a selected production policy.

After an attempt, the supplied nondecreasing retry delays form a bounded burst
of at most sixteen steps. Further failures return to the ordinary interval
instead of causing a rapid indefinite retry loop or permanently preventing
recovery after a long outage. A successful attempt resets scheduling to the
ordinary cadence. Nonnegative caller-selected jitter can delay a check anchored
to a recorded attempt; it cannot move it earlier. The first explicitly enabled
check is due immediately, avoiding a moving initial deadline on repeated polls.

A recorded attempt later than the supplied current time returns
`clock_review`. Invalid or overflowing integer times cannot produce a runnable
plan. This is a scheduling diagnostic, not proof of clock integrity and not a
reason to restrict access to user data. These technical bounds do not select
business terms, offline validity, grace periods, prices or an account policy.

The checkpoint contains only attempted/successful refresh times and a failure
count. It is not a signed license and grants no authority. The host must bind
its scheduling state to the selected product/provider context, use its existing
cancellable job and concurrency infrastructure, and keep verified entitlement
storage separate. The function cannot overwrite or discard a last valid license
because it performs no state writes.

## Reconciliation with the current application

| Existing authority | Relationship to this contract |
| --- | --- |
| Complete-bundle builder, sealed payload inventory, pinned wheels/native/model hashes and SBOM | Remain the packaging authority. An update display offer does not replace their verification or corresponding-source and rights checks. |
| Private two-archive delivery creator | Remains responsible for the unchanged sealed manifest and complete archive union. This module neither builds nor publishes artifacts. |
| Portable snapshot encryption | Protects an explicitly selected snapshot using its existing reviewed cryptographic dependency. It is not an entitlement or activation mechanism. |
| Provider profiles, OS credential vault and approved operation ledger | Retain their existing credential, network-consent, cost and retry authority. No credential or billing path is added here. |
| Existing library, reader, audio and assistant stores | Retain data ownership, atomic writes, cancellation and recovery. No store, job executor or backup format is replaced. |

Hashes and a sealed inventory establish the checks they actually perform; they
must not be described as publisher signature validation. The neutral contract
does not upgrade an unsigned package into a signed release.

## Evidence and remaining qualification

`tests/test_delivery_contract.py` exercises synthetic adapter correlation,
capability isolation, exception redaction, preservation of user-data actions,
strict schemas, seven/fourteen-day cadence, retry exhaustion, long outage and
return, disabled periodic contact, jitter and clock rollback.

The port was prepared during an executor outage. Its new tests are **NOT_RUN**
until the receiving CI runs this exact source; the canonical suite's earlier
history is not evidence for this revision.

Actual private-runtime qualification remains separate and **NOT_RUN** here:
issuer/signature and supported key verification; product/device/policy binding;
expiry/grace and anti-replay; key rotation and signed offline renewal; atomic
last-valid state replacement and crash recovery; update staging, rollback and
publisher signing; native Windows and assistive-technology acceptance. The
private assembly must supply and independently verify these capabilities before
wiring restricted operations across application entrypoints.

No real endpoint, production cadence, private source, signing key, account,
pricing, billing, license issuance, artifact upload or external distribution is
introduced or authorized by this change.
