# Completed audio: inspect long silence

Audio Studio can inspect the existing completed master for continuous quiet intervals.
The operation reads the current receipts and audio; it does not render, play, trim,
normalize, repair or replace the master. It does not edit the source script or job.

## Use

Open the current library's Audio Studio and select a completed job. Set **Silence
threshold (dBFS)** and **Minimum quiet duration (seconds)**, using a dot for decimals.
The defaults are **−60 dBFS** and **2 seconds**. Choose **Inspect silence**.
The existing Studio operation progress owns the background work and its cancellation.
When it completes, the full report appears in the existing named, read-only
**Production and save status** field and receives focus. No extra result window is opened.

The operation checks the current completed job, project, timeline, artifact receipts,
output directory and original audio hash. A changed file, stale job, changed library,
unavailable output or cancelled operation produces no completed inspection result.
Restore may deliberately withdraw a job's old output authority; inspect it only after
the existing production workflow has confirmed a current completed output.

## What the report measures

The scanner accepts the current PCM16 mono or stereo RIFF/RF64 master format. A frame
is quiet only when the absolute sample level in **every** channel is at or below the
selected amplitude threshold. A quiet interval must contain at least the ceiling of
the chosen seconds multiplied by the actual sample rate. Leading and trailing quiet
intervals count. Frame intervals are half-open: the start is included and the end is
excluded. The report gives the exact frame indices and their times.

Thresholds range from −120 to 0 dBFS; minimum duration must be greater than zero and
at most 86,400 seconds. The standard report retains at most 1,000 intervals. Reaching
that limit does not stop the scan: the total interval count and total quiet frames
still cover the entire master, and the report says when some individual intervals
are omitted. All original master bytes, including its container header and any
trailing bytes, participate in the retained SHA-256 identity check.

Quiet intervals are **review warnings only**. Deliberate script pauses and silence in
the original content may be correct. This amplitude inspection does not assess
speech correctness, recognition, music quality, intelligibility or subjective sound.
Use the existing playback controls to listen and decide whether a result needs a
separate, explicit production edit.

## Ownership and limits

The same open file descriptor supplies both the checked PCM header and sample scan.
Path and descriptor identities are compared before and after reading; aliases and
replacements cannot silently supply a different completed master. POSIX opens also
use nonblocking mode to refuse a replaced FIFO without hanging admission.

The existing ResourceBroker reserves one CPU and a 512 MiB memory estimate for this
operation. This is an accounting reservation, not a claim of an OS-enforced memory
sandbox or a measured process-family peak. The scan uses bounded blocks and at most
1,000 retained interval records. The existing audio deadline, cancellation and output
byte allowance remain in force. Leases settle when the actual worker finishes.

## Acceptance record

Source review covers the same-descriptor parser, full hash, current job and output
identity, exact mono/stereo interval calculation, bounded retention, cancellation,
and the existing Studio executor and read-only result field.

The completed component [run 46121179440](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121179440)
qualified exact commit `3c56c6c4489d2a5da7c3a9ebd3f9b5803a8b817e`:

| System | Source tests | Isolated installed tests |
| --- | ---: | ---: |
| Windows 2022 | 342 PASS | 342 PASS |
| Ubuntu 24.04 | 342 PASS | 342 PASS |

Each context ran the same 17 nonempty suites with no failed, errored, skipped or
expected-failure cases. The gate checked 15 named module origins and all 408 package
Python files, with selected test/helper identities retained before and after execution.
The [complete component evidence](https://api.github.com/repos/Oleksii-debug/AudioTacticalFPS/git/blobs/f8befd32c9b070f11349dfa41faac7440ee5b681)
retains the exact counts, identities and raw logs.

These tests include genuine PCM16 RIFF/RF64 files, a real short FFmpeg production,
completed receipt verification, file replacement and cancellation, the current
backup consumers of the shared PCM parser, and the current Studio progress/action
contracts.

The separate native Windows [run 46123004216](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46123004216)
qualified commit `feb07924db248c2f028517939daeec02ebeb3ce9` with **98 native
assertions and all nine fixed scenarios passed**. It exercised the actual installed
Studio, selected FFmpeg production and current completed receipts, preserving the
408-file package inventory, 17 named module origins and four outer probe/helper
scripts before and after. The retained record is
`f651a223e990d12de0f200b17656560b1616ddc6`.

These component records do not qualify a later combined application head, human
NVDA use, physical device listening or acoustic quality. The maintained final
installed/offline gates must run against the exact composed package.
