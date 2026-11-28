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

The component gate executes the actual source and isolated installed package on
Windows and Linux. It includes genuine PCM16 RIFF/RF64 files, a real short FFmpeg
production, completed receipt verification, file replacement and cancellation,
the current backup consumers of the shared PCM parser, and the current Studio
progress/action contracts. Native Windows Studio control verification is a separate
maintained probe. Until the exact run records are attached, this document makes no
runtime PASS claim. Human NVDA, physical device listening and acoustic quality are
not inferred from automated acceptance.
