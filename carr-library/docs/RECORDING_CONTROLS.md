# Explicit pause, resume and dry monitoring

This extension reuses PR152 commit 196a430f4a4139727021efb7236489a75293fbf2's
segmented recording and explicit output identity semantics. It keeps this
application's RecordingService, private native process owner, resource broker,
typed complete backup and existing WinMM buffer backend.

Start requires a refreshed, explicitly selected DirectShow alternative identity.
Pause requests FFmpeg's fixed graceful quit. The UI may acknowledge Paused only
after the complete owned input process family and any monitoring output have
confirmed release. The retained sample count then stays unchanged. Resume opens
that same microphone identity; a missing input or changed runtime fails without
selecting another. Stop while paused finalizes existing frames without reopening
the microphone. Cancel discards only the unfinished take. A failed native cleanup
retains the original stage, runtime reader, resource/device reservation and media
quiet holds until the same actual owner confirms quiescence.

Paused wall time is omitted. The take permits at most 128 capture segments and
one hour of total paused time in addition to its explicit recorded-time limit.
Joining copies unchanged PCM frames under immutable native input readers. A
version-2 recording receipt includes contiguous final-frame ranges and each
range's PCM SHA-256. It does not imply sample-accurate synchronization across
hardware reopen boundaries; review the joins before editing or publishing.

Version-1 receipt bytes and passive reading remain compatible. Typed backup
validates version-2 ranges, retains exact final originals and preserves bounded
opaque segment crash stages. Restoring a recording grants no device or runtime
authority. Managed Studio copies preserve and recheck the same range receipts.

Monitoring is off by default. The user must both enable it and choose an output
from a fresh stable interface inventory. Use headphones to reduce acoustic
feedback. The recorder taps only the same private WAV being written by its owned
FFmpeg child. It never opens a second microphone. The existing WinMM backend
holds at most four 50 ms buffers; stale monitoring frames are dropped rather
than delaying recording or growing memory. This is a queue bound, not a promise
of measured end-to-end latency. Counts distinguish submitted and dropped frames;
neither count proves audible delivery, listening quality or microphone quality.

The broker reserves the selected microphone and, when requested, the exact
monitoring output identity. Declared memory is the current policy allowance,
not measured peak RAM. Segmented recording reserves both retained parts and an
independent joined original, plus bounded headers and free-disk reserve.

## Verification boundaries

The authored portable suites exercise real FFmpeg synthetic captures and
pause/stop/cancel barriers, whole-file and per-range bytes, restore without
activation, actual reader mutation attempts, owned cleanup transfer, bounded
device inventory and WinMM API fixtures. Synthetic capture is not a physical
microphone qualification. API fixtures are not physical output qualification.

At this source checkpoint, execution is NOT_RUN because the executor is
unavailable. Native Windows controls, selected physical microphone, monitoring
sound/latency and NVDA remain separately unqualified until their actual gates
execute. No private audio, external model or downloaded runtime is in this
checkpoint.


## Native recording controls

The existing recording dialog now exposes named Pause and Resume buttons, a
default-off monitoring checkbox, a fresh output-inventory action and a choice
that starts unselected. Microphone, runtime, format and monitoring selections
freeze for the entire take, including pauses. The existing quiet hold, executor,
native family owner and close/cancel path remain responsible for settlement.
The layout uses a native scrolling keyboard container.

Pause requested and Paused are separate states. Resume is unavailable until the
worker's exact cleanup acknowledgement. Output preparation, late state notices
and callbacks are bound to the same current library and generation. Loading or
backing up saved receipts never activates their old output identities.

The updated installed Windows probe uses a real owned FFmpeg synthetic input,
native button/keyboard events and an acknowledged pause interval. It checks
that the first input family is closed, that the retained segment stops changing,
and that Resume opens only one next family. It verifies the final two contiguous
range receipts. Monitoring stays off in this automated native probe; the
separate bounded WinMM API tests do not claim a physical monitor was heard.
