# Observed original-maintenance result and managed-health failure

## Exact observed outcome

The frozen standard-installer run [46061428962](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46061428962), job [140716369082](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46061428962/job/140716369082), tested source `2d1a4bd89b3c8d63d55848f129e38489e2ea4157` and finished **FAIL**.

An independent inert artifact reader subsequently completed [run 46065840863](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46065840863), job [140730809423](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46065840863/job/140730809423), on `4d28c4410a6309cd49b71d36ffa7e919d7e2e11b`. It matched the exact artifact run, head, size and digest before opening the ZIP as bounded data. Nothing in the archive was extracted, imported or executed.

| Observed phase | Result |
| --- | --- |
| Original O installation | Native Inno log reports successful installation |
| Explicit ordinary O → maintenance A | **All four exact checks passed** |
| Managed candidate B setup | Native Inno log reports successful installation |
| Managed B health verification | **FAIL:** the portable-owner guard rejected a managed generation |
| Exact rollback, restart recovery and final 15-check managed result | **Not verified by a retained completion receipt** |
| Later installed 50-case library phase | No retained result |

## The four maintenance checks actually passed

The exact `original-maintenance.json` member is 731 bytes, SHA256 `bd08c407eb7f091540c3ca1f75e0e6d615d911cebabdbd6a7d3dddb276655e79`. Its complete parsed value is retained in [the companion JSON](standard-original-maintenance-r27-observed-20261127.json). It contains these ordered checks:

1. `exact-original-source-installed`
2. `ordinary-maintenance-installer-without-update-authority`
3. `original-process-and-sealed-generation-preserved`
4. `maintenance-native-self-test-and-user-data-preserved`

All three explicit preservation flags are true: the original process, original sealed generation and user data remained preserved at that checkpoint. The receipt binds unchanged original source `91332206578b4d8dc779fec08c8d51a67962b0ff` to maintenance source `3cefda9cc3be02a96d4e40b291645f1bfc1fcc6f`.

The original setup digest `9dc575f94b496b2295ac37d139b95ab3fa7a6969cde06325dc3987d2c802ff44` agrees with the executed build receipt. The original manifest digest `afacc9f2d4c34532c3f91494e8e033ce963a25dedbf776b61328ae5071694c7d` also agrees with the independently hashed archive member. The maintenance manifest member was independently hashed as `ce9efa582e9f311b12b62f1c02a9a1bc1fe3cf915b26d36813c82fed1ce256e8`, matching its compiled-build record.

## The later failure remains a failure

The retained `native-probe.log` is 2,231 bytes, SHA256 `bf0d0ae5cdd25d720336c629699826a527fde7bf5abdd5cda2c2e52916fd67ea`. It records the actual fixed system PowerShell host with two hardlinks and then the failure path:

`StandardUpdateTransaction.apply → WindowsStandardHost.health → WindowsBundleAdapter._verify_release → _require_portable`.

The refusal explains that managed installation ownership requires its setup/maintenance procedure. The existing source has rollback-on-exception, but this exception was rethrown before the native probe reached its exact rollback assertions. The archive has no `native.json`, `proof.json`, `library.json`, restart crash/recover receipts or `verdict.log`. Neither the source path nor a successful installer exit establishes successful rollback or final managed health.

## Preserved evidence and limits

Artifact **15229972246** contains 87 members, is **1,697,192 bytes**, and has SHA256 `3e2f5f9ab7011612950c0196849edcd6ae89f8d78616c86c389fd897eeb78af9`. The JSON preserves all 13 selected member identities, the actual maintenance/input/build receipts, manifest summaries, the native failure text, native setup log summaries and the explicit absent-record inventory.

The reader hashes complete selected members before decoding their bounded output. Native setup logs are represented by their last 48,000 characters; their exact full-member hashes remain recorded. Setup executable bytes were not selected archive members, so this independent read does not claim to rehash them. No Authenticode, production update authority, source/redistribution clearance, NVDA or full application qualification follows from this partial result.
