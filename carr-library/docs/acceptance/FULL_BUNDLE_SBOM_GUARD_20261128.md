# FULL bundle SBOM guard — 2026-11-28

## Observed execution

Source `89c07e451540e4caac6a36cd6142fcdd274264b1`, tree
`23e8829a0f4618101e0fe299ef6acd231a772aa5`, MAIN
[run 46089865902](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089865902),
Windows job `140808017116`.

The terminal log records 392 required source suites, 9,645 passed cases,
zero failures and zero skips. Steps 1–44 completed successfully. Step 45
assembled the base and complete bundles, then failed at the pre-installation
`SBOM seal mismatch` guard at 01:44:28 UTC. The full-bundle installation,
firewall-enforced execution and installed native/workflow checks were not
reached. Standard setup step 46 was skipped. The earlier successful Managed
core run does not qualify these unexecuted FULL stages.

The actual builders reported 55 wheels (the 53-pin semantic profile, the
application, and the explicitly selected spelling component). The base
manifest SHA-256 was
`6886501784b8f624f46ba6b6439187fb70bb3ceb58e9384fd782dffd37f22820`;
the complete bundle manifest SHA-256 was
`52a8326a7117b99c5ba1458bb9e1e0495e2b35a16d75fbe5a12b9c28168fa002`.
These are build results, not successful installation evidence.

The terminal evidence artifact is `15242691117`, 288,659 bytes, SHA-256
`1caae5c063f0d03c10a03f51a61c33b9785c1815b847f6072f9edf43026860fa`.
Its name contains the PR merge-ref identifier; the workflow run head and
checked-out source remain the exact commit stated above.

## Confirmed cause and correction

The existing build command selects `--spellcheck-wheel`.
`build_windows_bundle.spellcheck_snapshot` verifies the reviewed wheel,
separate lock, source manifest, source hashes, licence and code-only inventory.
`spellcheck_sbom_bytes` then deterministically extends the unchanged source
SBOM with exactly that component and dependency. The workflow incorrectly
expected the raw 53-component source SBOM's hash and repeated the same raw-byte
comparison after installation.

The workflow now derives its expected digest from the independent source SBOM,
checks its exact 53-name/version inventory against the original lock, and
reuses those existing optional-component admission and serialization functions.
It requires the actual sealed file hash and final manifest entry to match that
expected digest. The later installed verification still runs the complete
bundle verifier and requires the file, manifest and captured expected digest
to agree. The evidence report retains the base SBOM hash and records the
explicit 53-plus-one composition.

The bounded review of the remaining hash/seal expressions found no further
instance of this raw-base-versus-extended-SBOM mismatch. Python/native archive
pins, manifest-immutability checks, sealed source-wheel proof and copy hashes
remain unchanged. No application, builder, dependency/profile, runtime pin,
firewall rule, test selector, product deadline or CI budget is changed.

## Qualification boundary

The existing optional pre-seal builder and both original-profile preservation
cases passed in the observed source run. This workflow correction is reviewed
source; its new execution is **NOT_RUN** at this checkpoint. FULL installation,
runtime DLL/firewall checks and standard setup install/repair/uninstall still
require the following actual MAIN run. No NVDA, production-signing or
redistribution qualification is added.
