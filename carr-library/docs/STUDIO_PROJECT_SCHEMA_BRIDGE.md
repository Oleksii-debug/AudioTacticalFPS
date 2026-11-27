# Canonical studio project data bridge

## Source and scope

The project schema and its data-only package initializer are reused byte for byte
from canonical commit `33dcc42edd6781b9aeb02dcaaf016b386bcf37b6`:

- `studio/project.py`: Git blob `1acf68727fdaadd14055d357e28f2eb140ab2a52`.
- `studio/__init__.py`: Git blob `7e38d3875a79e43e24321e4a14f613a0bb7b5064`.

The selected host base is local commit `306255732878c6d43f208124a6413e34e320b449`
(same tree as published `d9ffe4515a625cf1157db71bf7d2a95229ae5dda`,
tree `27518cae86fffe993129584cf0bfd42502740dfe`). The draft branches from
`b00c173c63f9f414f7e1c656f2d99d1e0d3ffa78`, preserving its CI directory fix.
The shared `durable_state.py` is identical to the canonical dependency.
No additional dependency is introduced.

## Supported data

The host's existing typed sidecar adapter can validate and preserve
`state/studio/projects/<id>/project.json` and immutable revision files under
`revisions/<revision>-<fingerprint>.json`. Project identity, revision number
and content fingerprint remain authoritative. The exact canonical data model
is retained; no production plan conversion or renderer is added.

The supported project/revision subset has **no external media cues**.
The host sidecar adapter rejects any nonempty `Scene.cues`, including cues
in historical revisions. It does not open, copy or rebase external input audio.
Capturing such media requires a separate typed media inventory and restore
adapter before those projects can be reported as complete backups.

Voice identifiers and generated-music engine choices are preserved metadata.
Voice/model packs, executables and generated output are external dependencies
and are not installed or made available by restoring these JSON documents.
Current host Audio Studio cannot yet open or render the canonical project schema.

Studio jobs and engine settings retain the existing explicit rejection behavior.
The existing host policy excludes rebuildable `state/studio/cache` files from
backup capture; a manifest that declares unsupported canonical cache payloads
is rejected on restore. Provider settings, translation jobs and arbitrary
canonical state are not newly accepted. This is not whole canonical state
compatibility or PR125 convergence.

`carr_library.studio` is listed explicitly in the installed package inventory
so the typed validator is available from a wheel, not just a source checkout.

## Focused verification

`tests/storage/test_backup_studio_project_schema.py` covers host v4, host v5
and canonical state+pending v4 project/revision round trips, exact original bytes
and revision fingerprints, re-signed semantic corruption, unsupported runtime
state, unchanged cache exclusion, and external-cue rejection before publication.

The existing Linux/Windows backup workflow includes these tests and an isolated
installed-wheel check. That check imports both studio modules and the host
sidecar adapter from site-packages, verifies their exact Git blobs, and performs
an actual project/revision backup, verification and restoration. Its record is
`ci-results/studio-installed.json` in the workflow artifacts.

The executor was unavailable during preparation, so local tests were not run.
The component is published as a draft for GitHub Actions qualification. Neither
source preparation nor a queued CI run counts as a test pass.
