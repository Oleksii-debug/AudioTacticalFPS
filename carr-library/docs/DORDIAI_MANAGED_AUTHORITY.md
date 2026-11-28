# Managed dordiai speech authority

Status: source and regression cases are written; this component has not been executed yet.

This change connects explicit package review to the existing dordiai provider and JSONL PCM adapter. It introduces no synthesis engine, catalogue, downloader or management RPC. The package manager remains responsible for the existing public owner's separate package operations.

A provider captures its exact current library engine declaration and, when present, the approved manifest SHA, management revision and runtime/reference/package/lock identities. It never adopts a later declaration. Before discovery, cache fingerprinting or synthesis, it reads the current declaration again and acquires the already established owner byte-zero lock. A direct adapter has no library approval and excludes every active or inactive owner index entry; a bound provider also excludes revoked or otherwise unapproved profile identities. Legacy installations without a package namespace create no lock while speaking. Appearance of that namespace invalidates a previously captured provider.

Managed public voice IDs include the active version, canonical package-document digest, reference-role digests and exact approval binding. The same data enters capabilities and the content fingerprint checked by the existing registry and saved Reader/Studio/Book workflows. Changed or withdrawn authority therefore refuses an old queued selection before a cache can authorize synthesis. A new selection requires fresh discovery after explicit review. Restored bindings remain dormant.

Every managed operation holds the actual owner lease through the existing discovery/PCM process and any native cleanup. Its stream polls recheck the captured library/root authority, current activation index, approval manifest and selected package document. Existing full installed-asset hashing remains before and after provider rendering. The complete manifest is also verified at the operation boundaries; the lock's hash is read from the already held descriptor. There is no cross-call asset-authority cache.

A managed or package-aware worker is closed and confirmed quiescent before the lease is released, including a warm worker after a completed request. A failed close is passed to the current retained CleanupOwner and settled while the lease remains held. It does not create a second native owner or release a lease on elapsed time. Legacy unbound warm workers retain their existing lifetime unless captured root/configuration authority changes.

## Required execution

Two new whole suites contain 40 statically enumerated cases:

- tests/integrations/test_dordiai_owner_lease.py: 22 cases, real current pipe actors, native file locks, cancellation, same-byte substitutions and retained process cleanup.
- tests/integrations/test_dordiai_managed_authority.py: 18 cases, real current state CAS, complete manifest reads, explicit registration, package activation/removal, current probe and PCM subprocesses, public identity and stale-queue refusal.

The exact companions are the unchanged tests/integrations/test_dordiai_voices.py and the shared tests/audio/voice_management_fixtures.py. The latter is purpose-written metadata code. Its inert interpreter marker is explicitly replaced by the selected test Python for the actual child; it supplies no owner implementation or acoustic model. Existing dordiai voice, speech, settings, host, probe and convergence whole suites must remain required.

These regressions qualify client authority and process ownership only when actually executed. They do not qualify speaker similarity, pronunciation, real Holos inference, the public owner's implementation, NVDA, or hardware audio. The separate pinned owner API route and any actual inference acceptance retain their own evidence and status.
