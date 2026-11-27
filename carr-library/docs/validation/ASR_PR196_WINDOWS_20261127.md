# Windows qualification: canonical ASR cache policy and retained history

**PASS for the exact frozen component `e087fce3e51f1401532f0c444f6eb9dc9d42c652`.** The actual Windows run completed all source, installed, native, aggregate and cleanup steps. This record does not qualify later composed roots.

| Phase | Actual result | Measured elapsed |
| --- | --- | --- |
| Source regressions | 422 passed; exact function Counter | 112.875 seconds |
| Installed regressions | 422 passed; exact function Counter | 110.625 seconds |
| Installed native controls | 53 checks passed; exact label Counter | Not separately measured in this receipt |
| Aggregate and owned cleanup | Passed | See the actual job log |

Run: [46030395296](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46030395296), job `140612421441`.

## Source and artifact binding

- Frozen branch: `carr/legacy-transcription-pr196-policy-20261127`.
- Source commit: `e087fce3e51f1401532f0c444f6eb9dc9d42c652`.
- Source tree: `2df564ec84e8799f0b130a03703979ccc105f8d4`.
- Wheel SHA256: `e64672fab4a2268bf02636d84dc251c01cb3e47b57a4042cf103d8793e970d4c`.
- Oracle SHA256: `e031b60b615482adc51689e3ea63cf8d8f6718ac616fac36aa545cb6906e914f`.
- Native receipt SHA256: `7dcd29dc3dbc0d7d62a69b3381c52a338e000965a7e2cee8f837efd748d1b313`.
- Artifact: `carr-legacy-transcription-e087fce3e51f1401532f0c444f6eb9dc9d42c652-1`, ID `15217520669`, 433320 bytes.
- GitHub-reported artifact SHA256: `bcaacc5645cf57f12bf40dd737efceb173c8f3448d0e3205cad56b3f99a4f402`.

The adjacent JSON retains the actual source and installed receipts, full test Counters, all native checks, module origins, aggregate receipt, job step outcomes and artifact metadata. The retrieved source and installed Counters independently match the committed oracle; the native Counter matches all 53 declared labels and the exact 12-module origin set. Source, tree, wheel and oracle bindings agree across the phase receipts.

Evidence was retrieved from the actual job logs and GitHub artifact metadata. The artifact ZIP bytes were not downloaded or independently rehashed in this review.

## What this run establishes

The 422-case population includes the previous 268-case historical ASR/recovery qualification and 154 added cache-policy and retention cases. It preserves the canonical 128-way cache-policy matrix and tests an actual rehashed restore attempt: a generated cache cannot become an accepted retained payload merely by recomputing the manifest when generated-audio inclusion is absent or false. Non-cache historical results remain inert and retainable through restore and rebackup.

The typed historical family retains its 32-GiB per-source and 64-GiB aggregate-original bounds with a separate metadata allowance. The generic historical state byte and path limits remain separate. Large-size cases use validated declarations; this run did not construct or process a real 64-GiB archive.

The native probe exercised the current installed historical-review and preparation controls with public synthetic data. It verified exact retained source bytes, immutable current preparation, separate Start authority and Cancel behavior. It expressly recorded inference and NVDA as `NOT_RUN`.

## Remaining distinctions

This is evidence for the frozen cache/retention component. Actual private model execution, recognition quality, 64-GiB throughput, manual screen-reader behavior and the later composed application remain outside its claim. The separate assistant `audio.transcribe` bridge was not present in this frozen source and is qualified separately.
