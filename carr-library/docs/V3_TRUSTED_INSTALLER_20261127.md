# Signed client and standard installer composition — 2026-11-27

This checkpoint composes the public donor source at `934b632f98098d55253354e0c6f7d8d482b73461` with the existing PR99 library, provider vault, typed reader, resource coordinator and shared owned-process helper. It does not copy private authority code, credentials or supplied transcription artifacts.

## Source changes

- Optional public-key entitlement and local TUF repository client with retained last-valid state, explicit permission/terms review, offline rollback/freshness checks, optional idle renewal and independent user-data access.
- The protected anchor adapts to the existing `providers.WindowsCredentialVault` with a domain-separated installation/product/profile binding. There is no second credential store or automatic anchor reset.
- The native Help command uses the existing keyboard registry and existing asynchronous job executor. The default remains private-perpetual; production authority, site, terms and account policy are not configured by this source.
- The generic bundle transaction uses the shared owned-process supervisor. Uncertain native cleanup retains ownership before any rollback. Bare portable activation refuses a standard-managed app directory.
- Reused native launcher and Inno setup sources retain an installation lease before Python starts. CLI entrypoints share the same lease. Maintenance waits for the owned installer family, verifies inventories and preserves separate user data.
- Packaging preserves the existing 53-pin full runtime, source-wheel binding, SBOM, OCR/semantic/audio runtime and offline installation gates. Setup and compiler binaries stay in ephemeral CI work directories; only reports are retained.
- Fixes from exact731 execution: explicit replacement of an already verified old policy-location record; first queued document has no invented native attempt while replay invalidates stale authority; Advanced v3 cache admission; real FFmpeg provision for its required audio case.

## Actual evidence before this composition

Exact source `731cf6c964c600fdb8ed71d751d84251aa8dc57b`:

- Windows bibliography run45946416670/job140332029118: SUCCESS, 26/26 required suites, 992 passing cases and zero skipped/failed cases; 108 installed native bibliography/clipboard/export checks.
- Linux run45946416632/job140332031927: FAILURE, 38/42 required suites passed. The 34 real isolated import/publication cases and all64 desktop-handler cases passed. Four failed suites are preserved as observed failures; the source corrections above require new execution.
- The following source composition is **NOT_RUN until its own exact CI succeeds**. Donor review or prior-commit results are not reassigned.

## Required execution

The existing integrated Windows gate now builds the pinned native launcher, seals it into the full bundle, runs the existing installed application tests, then executes actual standard installation/repair/uninstallation with populated user data. Source and native acceptance have explicit nonempty/no-skip checks.

A separate Ubuntu/Windows signed-client matrix installs the hash-pinned optional trust profile, runs real cryptographic/recovery tests, and on Windows requires an actual temporary Credential Manager round trip plus installed wx permission/status controls. Keys, accounts and bundles in protocol fixtures are synthetic. No synthetic fixture qualifies a real publisher or release.

## Unqualified external requirements

No production signing identity, owner website, licensing authority, approved commercial policy, external restart supervisor or signed managed A-to-B release is provisioned here. Actual owner-model/ASR acoustic quality, native-code egress tracing, NVDA listening, 135-entry per-capability language qualification and per-profile distribution clearance remain distinct requirements. The source does not claim any of them from control tests or registry counts.
