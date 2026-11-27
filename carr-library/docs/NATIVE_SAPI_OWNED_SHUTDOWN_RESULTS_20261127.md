# Native SAPI owned shutdown — observed result

The frozen component `c71587eefc0df0ad69dd9d719c90c646b2a7a426` completed **114 source tests, 114 installed tests and all 38 native checks** on Windows. This result follows the earlier failed cancellation run; it does not erase that failure.

## Exact source and run

- Source tree: `c79ee1c9b8c5eaa371f16ee8d0dbcffffc206b61`.
- Run: [46050975677](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46050975677), job `140681889126`.
- Final receipt: `PASS`; oracle SHA256 `57d7d8301ba3c28556da68cda90b727e00a8b9e72dc6868de614a69df0f2549d`.
- Application wheel SHA256: `653acf08ce5be17ef0ee02f6cc64898a93fb634e4d054d6ada2aeb519f6cb207`.
- Native receipt SHA256: `ba31071d8c8715951edc85ac601e9a8eb49232c380cba823e65dfeeb92ed3c36`.
- Artifact `15224762518`: 412937 bytes, `sha256:6630625b8970591d87baf53cb7654f0240891432277c62aec16b418f9808d20e`.

The completed job log was fetched and its source and installed function/multiplicity maps were compared with the frozen oracle. All 38 native label multiplicities also matched. The strict finalizer reported success after verifying the 13 installed module identities and after removal of the run-owned outbound firewall rule. Raw JSON, XML and text remain in the artifact.

## What ran

Actual pywin32 312 COM enumeration and synthesis produced mono PCM16 at 22,050 Hz with 75,614 frames and nonzero samples. The prepared utterance retained its selected text hash and voice identity. The native reader exercised its controls, explicit punctuation, stop and close, cancellation with no partial published PCM, and passive saved-state backup/restore/rebackup.

The new control started a real isolated Python leader and an owned child that held a private file while the leader exited. After the current engine owner settled the full process family, the probe immediately removed that file without an added retry or sleep. The receipt records `owned_descendant_shutdown: ACTUAL_FILE_RELEASE_OBSERVED`. It also confirmed every native worker leader and job handle was released. The existing native label count remains 38; this control strengthens its process-release check.

The source tests additionally exercised transient retained-owner cleanup failures while request streams remained open. Original cancellation and interruption are preserved after settlement. The POSIX branch was not changed by this component.

## Limits and retained history

The earlier `fcc57dd` run passed 75 source and 75 installed tests but failed native acceptance after 27 of 35 checks: cancellation was followed by a sharing violation during private PCM cleanup. That result remains a failure in the accompanying JSON. The intermediate `6e6d259` host-admission run separately passed 109/109/38. Neither historical result is substituted for the final component's result.

Physical playback, NVDA/manual accessibility, acoustic quality, actual 32-bit PowerShell voice inference and frozen PyInstaller worker launch were not exercised here. The passive compatibility-host path checks ran, but do not establish compatibility-engine inference. A later integrated source must retain its own qualification boundary.
