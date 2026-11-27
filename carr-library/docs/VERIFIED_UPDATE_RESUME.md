# Pause and resume a reviewed update download

This extends the existing signed-update review. It does not change the publisher
keys, entitlement rules, standard installer, portable handoff, or update source.

## Native workflow

1. Open **License and signed updates**, then select the owner's update
   configuration and independently supplied configuration digest.
2. Use **Check configured signed source**. Review the release, product, channel,
   version, source commit and package digest.
3. Choose **Download reviewed package**.
4. During this download, **Pause update download** requests a durable pause.
   Wait for the status to report the retained byte count. A pause request is not
   a completed pause while the transfer is still stopping.
5. **Resume update download** rechecks the retained bytes and current signed
   metadata, then continues the same selected release.
6. **Cancel update download** stops this transfer and discards its recognized
   fragment after the owned transfer has stopped. It does not install anything
   or delete an already verified package.
7. After the complete package passes signed length and hash verification,
   activation remains a separate explicit operation. Standard installations use
   the existing Apply or Recover workflow. Portable installations retain the
   existing external maintenance command and closed-program requirement.

Closing a paused review keeps its recorded fragment. A later review of the same
configuration can find it after a fresh signed check. Opening a window, restoring
research data, or reading a checkpoint never resumes a transfer or installs a
package automatically. Closing an active review requests cancellation of its own
work; it does not cancel another Desktop operation.

A paused or interrupted download is shown as incomplete. Its byte count may be
known, but it cannot report a successfully installed or verified package.
Measured progress reaches 100 only after the complete signed package is prepared.

## What is checked on resume

The client keeps the same TUF updater and selected local or HTTPS repository.
Every resume reacquires current metadata and enforces the existing signature,
expiry, rollback, product, channel, data-schema, target-size and independently
pinned bootstrap checks. The release must still match the digest that was
explicitly reviewed.

The retained checkpoint records the selected source, release and durable prefix.
The client checks its exact fields, file and directory identities, ordinary
single-link files, bounded record length, byte count and actual prefix digest
before appending anything. A replacement file is refused even if its bytes are
equal. Unknown files inside the fragment directory prevent recursive deletion.

A locally recomputed prefix checksum cannot approve changed package data.
After continuation, the ordinary TUF `download_target` operation still verifies
the complete signed length and every declared signed hash. Only that successful
path can return the existing verified-package capability.

The fragment is held under the existing update-cache lock. If the existing
native process owner cannot confirm shutdown, its exact staging and lock stay
with that owner until cleanup is confirmed. The application does not read a
still-owned child response or start a competing transfer over that lock.

## HTTPS and local source behavior

For an HTTPS continuation, the owned stdlib HTTPS child asks the selected URL
for the exact remaining byte range. It requires a matching `206` response and
one exact `Content-Range`. It refuses changed offsets or totals, ambiguous
framing, encoded content, redirects and proxy routing. A server that ignores
Range is not silently concatenated with the old prefix: cancel that fragment
and explicitly start a new download.

The HTTPS source still uses the selected TLS trust and existing per-request and
total time bounds. The application has no new public update endpoint.

A local repository continuation uses the same guarded source descriptor and
exact offset, with checks before and after reading. It does not invoke a
network transport.

## Interruption and cleanup

The client records a durable prefix at bounded intervals of at most one MiB of
parent-captured data and when a requested pause can be confirmed. An abrupt
interruption may leave an uncheckpointed tail. Resume first rehashes the saved
prefix, then removes only that tail from the same file identity. Inspection
alone does not truncate or modify it.

The fragment remains cache data. It is not a trust key, a protected anchor, an
installation receipt, or an authority restored with the research library.

A cleanup error after verified publication does not silently reverse the
result. The client rechecks the exact saved file identity and every signed hash
before returning the package, with a separate cleanup notice. A replaced,
removed or changed destination still fails. This notice says that the verified
package is present; it does not claim ownership of arbitrary equal bytes or
claim that fragment cleanup succeeded.

## API compatibility

The existing `LocalRepositoryClient.download` and `HTTPSRepositoryClient`
ordinary download behavior remains available. Optional `paused`, `resume` and
`progress` arguments opt into retained downloads. An incomplete result is a
typed `PausedDownload`, which is rejected by the existing installation-capability
validator.

`MaintenanceSession` adds explicit resume and cancel methods and separately
tracks incomplete state. Its `package` remains unset for every paused or
interrupted result. The existing Apply and Recover methods still require a
fully verified package and fresh authentication at handoff.

There is no second executor, installer, provider registry, endpoint, or
background automatic-download service.

## Qualification boundaries

The dedicated review workflow runs source and isolated installed suites on
Linux and Windows and checks the selected module bytes before and after each
run. Required cases include actual signed TUF targets, prefix and identity
tampering, metadata expiry, every signed hash, a parent-owned interrupted child,
late fragment-cleanup failures and real loopback TLS Range behavior. Native
acceptance uses actual wx buttons and the actual Desktop queue with an authored
signed local repository. No production publisher, protected anchor, paid
service, physical keyboard or screen-reader qualification is inferred from
those fixtures.

Execution results belong to the exact reviewed commit and CI run. Source
review or a checkpoint manifest is not an execution pass.

## Protocol references

- [TUF client workflow and verified targets](https://theupdateframework.github.io/specification/latest/)
- [python-tuf 7 updater implementation](https://github.com/theupdateframework/python-tuf/blob/v7.0.0/tuf/ngclient/updater.py)
- [HTTP Semantics: Range and Content-Range](https://www.rfc-editor.org/rfc/rfc9110.html#name-range-requests)
