# Optional dordiai-voices connection

The public local bridge is an optional backend of the existing speech renderer
and Audio Studio. It starts **disabled**. No private engine source, model,
reference recording, embedding, voice pack or private Python installation is
included or downloaded. Missing or incompatible resources produce an explicit
error; another voice is never silently selected.

## Connect and disable

Open **Voice → Connect or disable dordiai-voices**. Select the separately
installed `kx47Runtime` directory, its private Python executable (or leave the
field empty to use `runtime/python/python.exe`), and the reviewed worker SHA256.
The prefilled SHA identifies the public contract's reviewed worker revision.
**Connect selected runtime** checks local paths and worker bytes, then saves the
declaration for the current library. It does not start the private interpreter.

Open **Voice → Installed local voices** to inspect resources using that
interpreter. Choose the exact voice/language entry in Studio or the reading
handoff. A resource being present is not evidence that its pronunciation,
language coverage or acoustic quality has passed acceptance.

**Disable** preserves the selected paths and fingerprint while disabling future
use for this library. It signals active work owned by the same library to stop.
That signal is sent even if saving is blocked; a failed save remains visible and
must be retried. Completed audio stays available. Changing libraries does not
borrow another library's connection or an ambient enable flag.

The bounded `dordiai-voices.json` file contains declarations only. Full-library
backup validates it without touching the declared runtime. Restore preserves its
declarations with `enabled=false`, without probing or executing those paths.
Runtime/model/interpreter files are not copied into the backup. A declaration
from another operating system remains disabled until a valid local path is
selected; it cannot resolve against the current working directory.

## Shared rendering and resource boundaries

`speech/local.py` retains responsibility for input validation, checked PCM16 WAV,
live output quotas, cancellation, deadlines and atomic no-clobber publication.
The adapter writes only its private staging file. The public protocol produces
24 kHz, mono, signed 16-bit little-endian PCM. Studio retains its ordinary mixing,
chapters, captions, checkpoint and recovery behavior.

Each public voice ID identifies a specific installed profile and language.
Shared rate remains 0.5–2, volume 0–1 and engine-relative pitch −1–1; the private
worker receives its documented numeric controls. Pitch is not measured
semitones. Timbre stays at the neutral value 50, and the adapter offers neutral
style only. There is no claimed SSML, phoneme input, emotion control or word
alignment. Pronunciation substitutions remain explicit reading-copy edits.

Requests larger than the worker's 16,384-character bound are split using the
existing exact-text splitter. Transport has bounded request/reply queues, request
and reply byte limits, deadlines and an owned-process shutdown path. A render's
overall deadline includes discovery and identity checks. Offline environment
flags disable supported model-hub downloading and telemetry; they are not an
operating-system network sandbox for arbitrary private code.

The speech fingerprint hashes the complete public bridge/probe, shared local
speech, saved-settings, text-splitter and path-validation source files; the exact
private interpreter executable; runtime helper files; and every file in the
installation's model and bundled Python directories. Hashing is cancellable and
bounded to 100,000 inventory entries and 64 GiB. It reads complete asset bytes
without trusting a size/timestamp-only cache. Changed model bytes with a
preserved timestamp cannot reuse old speech. Identity is checked again before
publication. An external interpreter's entire global package environment is not
claimed to be a sealed private runtime; supply the reviewed private installation.

A completed production directory now depends on authored revision, audio/voice
runtime identity and master hash. Updating a runtime produces a separate
immutable result while preserving earlier work. Studio backups retain these
multiple productions and still accept legacy revision-only production folders.
The existing SAPI adapter has a different identity boundary: PowerShell bytes
and voice token attributes, without a full SAPI engine DLL/model inventory.

For CLI use, `studio voices --configuration-root <library>` and
`studio render --project-dir <project> --configuration-root <library>` use the
same saved declaration. With no configuration root, the CLI permits explicit
`CARR_DORDIAI_ENABLED=1`, `CARR_DORDIAI_RUNTIME`, optional
`CARR_DORDIAI_PYTHON` and `CARR_DORDIAI_WORKER_SHA256`; no environment opt-in can
override a library's disabled saved state.

## Reuse and acceptance

The protocol bridge and private-process inventory helper reuse the public
[PR117](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/117) source at
`18be07858bb5eedcb4295c4e191720619f650d4a`, extended with cancellable discovery.
Its alternative provider registry and renderer were not adopted. Shared backend,
library scoping, native connection controls, exact asset identity and backup
composition use the active application's existing APIs.

`tests/integrations/test_dordiai_voices.py` runs real subprocess pipe operations
against a clearly labelled synthetic actor. `test_dordiai_shared.py` exercises
that actor through the real shared renderer, including exact Unicode/CRLF text,
controls, failed output, live byte limits, cancellation/disable, timeout,
changed-model identity, real FFmpeg, completed-output preservation and
backup/restore/cache-only resume. These tests contain no neural model and their
short synthetic PCM waveforms are not speech-quality evidence.

Actual private neural inference, warm latency, long private sessions, hardware
load, Windows private-process behavior, human NVDA operation, listening quality,
consented cloning and qualification of 135 languages remain **NOT RUN**. They
require separately provided authorized runtime/assets and independent review.
The native connection dialog also awaits its final installed Windows
keyboard/NVDA acceptance.
