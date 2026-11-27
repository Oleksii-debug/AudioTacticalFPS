# Canonical audio packaging and runtime disposition

This audit compares canonical **a44fac04072389caa40e0d6e799b13797712f797** (tree
`3cc479898b1430b49774fc0226630a667d94d2d2`) with current **23a09923584eb43d62d90b54f5c25b098a49d233**
(tree `1bf04768ebc47d3293d692c5de204838f6b1c15e`). It covers exactly **22 requested
canonical paths**. The adjacent JSON records every full blob identity, current
owner, test reference and limitation.

This is a source review. It does not turn authored tests, old workflow logs or a
different runtime's results into qualification of the current package.

## What the comparison found

The canonical **end-user opt-in download workflow was missing** at the audited
current root. This was more than a developer convenience:
[RUNNING.md](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/docs/RUNNING.md) directs
users to the local setup launcher, and its
[launcher](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/SetupLocalAudio.cmd)
opens the fixed helper interactively. The helper shows a read-only plan and
requires YES or explicit --download. Current manual archive preparation and
an Invoke-WebRequest step in CI did not preserve that user action.

The narrow restoration is
[c24e883d3dba7b59113e4a427697f1962f38fb59](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/c24e883d3dba7b59113e4a427697f1962f38fb59).
It adds explicit acquisition to the **existing** preparer and local setup
wrapper, uses the current Gyan pin, keeps the default/manual paths offline, and
never activates an engine. Its 69 authored controlled-transport/interactive cases
have independent source review, but have not been executed when this report was
written. The exact root-owned main-step replacement is in
[EXPLICIT_AUDIO_ACQUISITION_20261127.json](EXPLICIT_AUDIO_ACQUISITION_20261127.json).
A real run of that same setup step must qualify actual acquisition and native
preparation; there is no second downloader or runtime pipeline.

The retained source/provenance material also differs. Current Gyan binaries
cannot inherit the old BtbN source archives, build recipe index, native checks or
licensing characterization. That difference remains visible and is not hidden
behind a common FFmpeg name.

## Three separate runtime identities

| Property | Canonical BtbN shared | Canonical Gyan essentials | Current Gyan full |
| --- | --- | --- | --- |
| Build | n9.0.2-982-g836df4e858-20261126 | 9.1 essentials | 9.1 full |
| Archive bytes | 77,787,475 | 119,467,219 | 264,268,459 |
| Archive SHA-256 | `f7ba27e7986a2d0835881e742a682dec17896f6dc2cdcf9e596f043d2763d894` | `b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd` | `f9a83be8f97b8989a223a15e4af234accd0842bacf7c126af5a3aae759785e48` |
| Retained inventory | 224 archive files +12 extras | 4 selected members | All45 original publisher files |
| Build license recorded by its lock | LGPL-3.0-or-later | GPL-3.0-or-later | GPL-3.0-or-later |
| Source revision | `836df4e85894b757debe268f84ffc08c57623a73` | `74e4393595cf2b5857f0da925c4e0dd74ea306e5` | `74e4393595cf2b5857f0da925c4e0dd74ea306e5` |
| Extra source material | Exact FFmpeg and build-recipe archives, dependency index | Source reference; incomplete transitive source | Source reference; incomplete transitive source |

The table describes each committed lock, not a new download or legal clearance.
The current full build includes ffplay and requires Flite; the four-file
essentials profile cannot satisfy that contract. The BtbN source tree and build
recipes are precise historical evidence for that shared build. They are not
corresponding-source evidence for the current Gyan static build.

Canonical itself records that its third-party complete-source bundle and public
distribution clearance are absent. Current preparation retains all Gyan publisher
license/documentation bytes, but does not package a substitute complete source
bundle or dependency-source index. **Public binary distribution remains
NOT_CLEARED.** The existing transfer guard rejects audio-containing payloads by
both manifest flags and actual audio/audio-runtime inventory, including forged
false/absent flags.

## Exact requested-path disposition

| Canonical path | Canonical blob prefix | Current disposition |
| --- | --- | --- |
| [.github/workflows/carr-v3.yml](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/.github/workflows/carr-v3.yml) | `700a88782aad` | Current main/native and standard-generation gates; old verdicts do not transfer. |
| [packaging/acquire_audio_runtime.py](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/acquire_audio_runtime.py) | `9de23d3f79bf` | Real end-user gap found; c24e restores opt-in on current Gyan pin, pending execution. |
| [packaging/add_audio_runtime.py](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/add_audio_runtime.py) | `90f6acb4101d` | Existing current builder, verifier and transfer guard own sealing. |
| [packaging/audio-runtime/README.txt](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/README.txt) | `3b2d20479de5` | Different build; current notices/docs retain that distinction. |
| [packaging/audio-runtime/licenses/BUILD_RECIPES_LICENSE](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/licenses/BUILD_RECIPES_LICENSE) | `b9444ddc2718` | Old build/source notice; current Gyan publisher notices are retained separately. |
| [packaging/audio-runtime/licenses/COPYING.GPLv2](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/licenses/COPYING.GPLv2) | `d159169d1050` | Old build/source notice; current Gyan publisher notices are retained separately. |
| [packaging/audio-runtime/licenses/COPYING.GPLv3](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/licenses/COPYING.GPLv3) | `94a9ed024d38` | Old build/source notice; current Gyan publisher notices are retained separately. |
| [packaging/audio-runtime/licenses/COPYING.LGPLv2.1](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/licenses/COPYING.LGPLv2.1) | `40924c2a6da7` | Old build/source notice; current Gyan publisher notices are retained separately. |
| [packaging/audio-runtime/licenses/COPYING.LGPLv3](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/licenses/COPYING.LGPLv3) | `65c5ca88a67c` | Old build/source notice; current Gyan publisher notices are retained separately. |
| [packaging/audio-runtime/licenses/LICENSE.md](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/licenses/LICENSE.md) | `371b0913ce7c` | Old build/source notice; current Gyan publisher notices are retained separately. |
| [packaging/audio-runtime/provenance/build-configuration.txt](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/provenance/build-configuration.txt) | `51177e3d9c02` | BtbN build configuration cannot attest Gyan. |
| [packaging/audio-runtime/provenance/checksums.sha256](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/provenance/checksums.sha256) | `835111613379` | Current archive and45 original member hashes are independent. |
| [packaging/audio-runtime/provenance/dependency-source-index.json](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/provenance/dependency-source-index.json) | `69dab303955a` | No equivalent complete Gyan index/source bundle; explicitly unqualified. |
| [packaging/audio-runtime/source-lock.json](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/audio-runtime/source-lock.json) | `be9955a9485b` | Same path, different exact runtime and output inventory. |
| [packaging/prepare_audio_runtime.py](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/prepare_audio_runtime.py) | `2ddde57cc6e7` | Current45-file offline preparer and optional actual Windows checks. |
| [packaging/prepare_windows_audio.py](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/prepare_windows_audio.py) | `da79feb98998` | Essentials is not the full profile; only narrow acquisition concepts reused. |
| [packaging/wheel-notices/SOURCES.json](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/wheel-notices/SOURCES.json) | `2479850e2fe1` | Current selected wheels and reviewed pywin32 notices. |
| [packaging/wheel-notices/comtypes-LICENSE.txt](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/wheel-notices/comtypes-LICENSE.txt) | `d7c5350e98f8` | comtypes is unused; pywin32/protocol provenance is explicit. |
| [packaging/windows-audio.lock.json](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/packaging/windows-audio.lock.json) | `2bc706ffc620` | Different archive/member identities despite overlapping source revision. |
| [scripts/windows_audio_runtime_smoke.py](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/scripts/windows_audio_runtime_smoke.py) | `79f24d2c4110` | Current runtime/effects probes have their own source and binary scope. |
| [scripts/windows_transferable_bundle_smoke.py](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/scripts/windows_transferable_bundle_smoke.py) | `1a2dc5302662` | Focused standard payload has6 selectors/50cases; old15-suite equivalence not claimed. |
| [scripts/windows_v3_smoke.py](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/a44fac04072389caa40e0d6e799b13797712f797/carr-library/scripts/windows_v3_smoke.py) | `7fbcfaf0e4d8` | Current production/Studio and distinct COM SAPI gates; actual new SAPI pending. |

Full current-owner and test identities are in the JSON. A “current owner” maps a
specific contract; it is not a statement that every internal API or old binary
layout is interchangeable.

## Installation, discovery and notices

The current bundle builder accepts its verified audio directory, copies the
complete current runtime, checks it again, and includes its inner manifest in
the outer sealed inventory. The installer checks announced audio payloads,
required notices and unexpected files. The current local setup wrapper keeps a
fresh runtime outside the program and selected library. Its downloaded input
is private temporary material; the existing manual preparer and final verifier
still own runtime publication.

Discovery admits an explicit absolute path, CARR_FFMPEG, the verified private
bundle, or absolute PATH entries in that order. Invalid explicit or bundled
selections fail without a silent alternative, and the current directory is not
searched. Discovery itself does not execute or download. A manually selected
external executable remains separate from a current sealed-bundle claim; an old
BtbN or essentials manifest is not silently reinterpreted.

The native SAPI adaptation uses already selected **pywin32==312**, not comtypes.
The current 56-pin lock and SBOM retain the selected dependency, and reviewed COM
and WIN32 notices retain their exact upstream sources. The protocol provenance
file explains the adaptation without attributing unused comtypes implementation
to the new worker. A wrapper notice is not a native execution receipt.

## Qualification scope stays explicit

The canonical transferable-bundle script selects 15 library modules under an
audio-free installed payload. All 15 module paths remain in the broader current
main workflow. The **current focused audio-free standard-generation qualifier
selects 6 workflows and 50 exact cases**, including installed/model/OCR/native
identity and actual CSL processors. A broad private-bundle run is a different
configuration; it does not prove that all 15 canonical suites ran in the
audio-free transferable generation.

The canonical BtbN audio probe's 9 checks and old v3 Studio/SAPI workflow have
current runtime, production, effects and native-control owners. Their old PASS
results are not transferred. The current native COM SAPI successor
`fcc57dd0ef016e7743b7005a4273c5d6612951ac` declares 75 source, 75 installed and 35
native checks; run 46039343585/job 140642779359 was **queued** when this report
was written. Its earlier actual failure remains recorded separately.

No model/vendor speech quality, physical playback or NVDA claim follows from
this source map, synthetic helper tests or runtime archive verification.

## Version preservation

The audited current project and package both say **0.2.0.dev1**, while canonical
both say **0.2.0.dev2**. Restoring dev2 in those two current files is supported by
the bounded packaging review. The source-wheel verifier derives filename,
METADATA and dist-info expectations from committed pyproject data; the builder
derives install.lock from the actual wheel. The Inno template uses the source
short hash for AppVersion and an independent 0.0.0.0 VersionInfoVersion.

No dev1 hardcode was found in the reviewed setup, source-wheel, bundle,
installer, delivery or main-workflow owners. This is a bounded source finding,
not an exhaustive repository search or an installer execution.
