# Required test runner — observed Linux and Windows results

The frozen runner qualification at `8feb19893acc97a94a6bdeafa4c15459117239a4` completed **48/48 Windows cases and 49/49 Linux cases**. All exact class/function multiplicities matched the required oracle. Both receipts report `CONFIRMED_QUIESCENT` for the owned process family.

| Lane | Job | Cases | Artifact | Artifact SHA256 |
| --- | --- | ---: | --- | --- |
| Windows 2022 | 140692109321 | 48 | 15225941861 | `280f41b163545374efa3a8e046aaeab964e4fb8d6e1aea002d7569ac8671836d` |
| Ubuntu 24.04 | 140692109813 | 49 | 15224419945 | `0c41b6c8ce1d53469964957a2786cbe7c6906aee16c02cef55ee30478c0dd555` |

[Run 46054046014](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46054046014) used tree `9303960cc923977484659b5261704fe7f33cb026` and oracle SHA256 `83a96ee75a3d70b77c3fac3d2f8e683ddf96c8cf323b9ba4a1f4797a472c90ed`. The source and interpreter identities, the exact executed case maps and dependency pins are in the accompanying JSON. Raw JUnit, stdout and before/after source inventories remain in the two artifacts.

## Verified boundaries

The same current CI owner launched actual Python test processes. The timeout control started a real descendant that held a private lock file; after timeout the owner settled the entire family before deleting temporary reports and continuing to the next required suite. The outer result stayed failed for that deliberately timed-out suite. Other cases exercised transient and persistent cleanup failures, preservation of the original timeout, actual independent parallel workers, strict skip/xfail/deselection behavior, bounded loopback admission and watchdog lifetime.

Linux additionally executed the real fork regression: a forked child could neither checkpoint nor release its parent's resource reservation. The parent reservation continued to occupy capacity until the parent released it. Windows did not collect that Linux-only test and receives no skip credit for it.

The gate checked all tracked Python sources, pyproject and the oracle before and after execution, confirmed the pinned pytest9.0.2/packaging26.4/filelock3.29.0/platformdirs4.10.0 runtime, and refused any incomplete or failed JUnit population. The bounded receipt is printed directly in each job log, so its exact counters were independently compared after execution.

## Retained failures and repairs

The earlier `4e570d7` qualification failed one fixture on both platforms because its deadline wrapper did not accept the current keyword-only `allow_loopback` argument. The repair forwards the same explicit false policy; it does not broaden network admission.

The following `668671c` qualification passed Linux but failed the Windows timeout fixture. Its PID marker was visible before the writer finished, and the observing wrapper raised before returning the actual process to the runner. The fixture now publishes its closed ready marker atomically and retains full process-family ownership if observation itself fails. The runner implementation, timeouts and required population stayed unchanged through these fixture repairs.

The final result above belongs to the final frozen source. Earlier failure artifacts remain recorded in the JSON. This source-only qualification does not substitute for application, installer, native UI, model-quality or manual-accessibility evidence.
