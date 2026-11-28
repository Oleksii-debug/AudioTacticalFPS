# Isolated FULL bundle UTF-8 fixtures — 2026-11-28

## Actual execution and remaining boundary

Source `9bf41c9245ed5ca3503680b4ddd24bf44b9b4ca6`, tree
`d16b89388f6548944d04e95a36dd201551a7747b`, MAIN
[run 46095245116](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46095245116),
Windows job `140824991742`.

The completed log records 392 required source suites with 9,645 PASS,
0 FAIL and 0 SKIP. The FULL bundle then passed its corrected SBOM guards,
production installation, native extension/OCR checks and the pre-bulk sealed
inventory verification. Its installed Python was CPython 3.11.18. The actual
installed local-translation loopback suite passed all 31 cases; the installed
sync probe passed all 171 cases with no failures or skips.

The subsequent isolated installed bulk run collected 4,880 cases:
**4,875 PASS / 5 FAIL / 0 SKIP in 2,462.28 seconds (41m02s)**.
Every failure was a test fixture's locale-default text I/O call:
three managed-project UTF-8 JSON reads, one Unicode M3U write, and one UTF-8
CPAL notice read. The actual trace selected cp1252 under isolated Python.
Changing the global locale or adding a UTF-8 interpreter flag would hide
that fixture contract instead of declaring the file encoding.

The standard setup step was skipped. The post-bulk final source-wheel proof
and tested-wheel artifact were not reached. Managed CORE results do not
qualify those missing FULL/setup stages. Physical audio was explicitly
unavailable on this runner; no NVDA or listening qualification is added.

Terminal artifact `15246311383`: 440,844 bytes, SHA-256
`be15a62cba7d9eb36d6cc650227d34cfef9a38664352a99f44bd7a882c3560ba`.

## Narrow fixture correction

Exactly eight `encoding='utf-8'` arguments are added across three files:

- `tests/audio/test_managed_projects.py`: the three failing JSON reads and
  the sibling source read paired with an existing explicit UTF-8 write.
- `tests/audio/test_media_conversion.py`: the failing Unicode M3U write
  and the sibling read of the generated UTF-8 export receipt.
- `tests/citations/test_csl.py`: the failing CPAL read and the adjacent
  UTF-8 upstream-manifest read.

The ASCII-only altered-JavaScript fixture stays unchanged. All original
Unicode literals, operations, assertions, selectors and deadlines remain.
Removing only these eight arguments restores the three original files
byte-for-byte. All 430 application source entries, build inputs, dependency
pins, interpreter flags and runtime policies are unchanged.

## Required follow-up

The five observed failing nodes will run in a focused companion gate in the
same isolated embedded installation, after the existing offline test-dependency
installation and before the long bulk suite. The complete bulk selection keeps
all five cases. Installed origin, network restrictions and no-skip acceptance
remain required.

This checkpoint is source-reviewed fixture code, **NOT_RUN**. Neither the
fixed cases, a fresh complete FULL run nor standard setup install/repair/
uninstall is claimed successful until the corresponding actual execution.
