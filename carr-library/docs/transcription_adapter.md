# Supplied local transcription adapter

This package connects Resonance to the separately supplied Whisper Transcribe
worker through its existing `--job-dir` protocol. It does not contain that
private worker, an ASR engine, model weights, a downloader, or a second runtime
registry. The source review remains in
[TRANSCRIPTION_INTEGRATION_REVIEW.md](TRANSCRIPTION_INTEGRATION_REVIEW.md).

## What the user can do

In an existing library, open **Audio → Local transcription**. Choose a reviewed
private runtime profile and explicit local audio/video files. **Prepare
snapshots** copies the selected bytes into a new owned job, verifies the private
installation and shows the exact request fingerprint. Preparation does not run
recognition. **Run / resume** authorizes that exact saved request.

The current adapter exposes transcription, or Whisper translation **into
English**, with separate TXT, SRT, VTT and JSON exports. JSON is retained for
verification even when the user requests another export. It uses an explicit
device/compute pair, one file at a time, and the supplied worker's bounded
decoder retry choices. It does not claim diarization, word alignment, arbitrary
target-language translation, or accuracy for any of the 135 catalog entries.

Pause is observed between native inference calls. Stop writes the worker's
existing flag and allows a bounded grace interval, then stops the owned process
if it has not returned. Completed worker checkpoints are retained. A partial
file requires retranscription on resume; this is not sample-exact continuation.
The selected run deadline includes verification and paused time. Closing the
dialog or changing the parent library/root withdraws its operation and callbacks.

Completed output is a **derivative**, with `quotation_authority: false` and
`accuracy_status: not_qualified`. Reading it rechecks the receipt, inputs and
output bytes. A reopened saved result requires the independently retained
receipt SHA256; the job list does not silently supply it as trusted evidence.
Neither status text nor a status-provided pathname authorizes opening a file.

## Private installation profile

The adapter reuses `audio.model_manifest.LocalModelManifest`, schema version1,
with `engine: whisper-transcribe`. The profile is an explicit installation and
consent decision. It must pin the selected interpreter, supplied worker, native
dependencies and model assets by exact SHA256, contain an exact model revision,
and record reviewed license/execution consent. Verification rejects unlisted
installation files. Do not put private profiles, real hashes identifying private
source, runtimes, recordings or transcripts in public Git.

The profile's `settings` object has these exact fields:

| Field | Meaning |
| --- | --- |
| `protocol` | `whisper-transcribe-job-dir-v43` |
| `python`, `worker` | Relative, explicitly listed files inside the reviewed installation |
| `models`, `model` | A local model parent and one simple directory name; no repository/cache-name fallback |
| `runtime` | Exact `sys.version`, `sys.platform`, and package versions for `faster-whisper`, `ctranslate2`, `av`, `tokenizers`, `onnxruntime`, and `numpy` |
| `languages` | Explicit candidate input language codes, not an acoustic qualification |
| `inference_profiles` | Explicit `device`/`compute_type` pairs; no automatic CPU/GPU fallback |

`model.bin`, `config.json` and `tokenizer.json` must be present in the selected
model directory and included in the hash inventory. The tokenizer requirement
is intentional: the official faster-whisper1.2.1 implementation uses a local
directory for an explicit model path, but calls `Tokenizer.from_pretrained`
when the local tokenizer is absent. See the
[official versioned implementation](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/transcribe.py).
Changing the worker, including a private durability or timestamp correction,
requires a refreshed reviewed profile; the public adapter never substitutes a
new worker or blesses its hash automatically.

The child uses the selected interpreter with `-I -B`, no shell, a minimal
environment without parent credentials/proxies/Python hooks, private cache/temp
paths, Hugging Face offline/telemetry flags, and a Python audit hook refusing
network and child-process events. These measures do **not** sandbox hostile
native libraries. The reviewed interpreter, its base standard library and OS
remain trusted software. Installation byte identity, version identity, runtime
execution, acoustic quality, and redistribution rights are separate evidence.

## Owned job and completion boundaries

Each job resides under `transcription-jobs/<id>` in the existing application data
directory. `request.json` binds the selected library, private profile fingerprint,
options, resource budget and input byte hashes. The worker sees only generated
filenames inside `inputs/`, not recursive live user folders. Duplicate physical
inputs, symbolic/hard links, reparse points, unsupported formats and changed
source copies are refused. Original files are never edited.

The host generates exact `job_config.json` and `settings_live.json` files and
rejects changes on resume or during execution. The existing parent project lock
serializes host actions. A separate child-held OS lock prevents another host
from launching a second worker against an orphaned running job. Exit, logs,
elapsed time, directory entries and retained file sizes are bounded. The disk
budget is checked by polling; it is not a filesystem quota or a native-memory
limit.

At completion the host rechecks the private installation, retained inputs,
request and all expected exports. It independently validates per-output
ownership, lengths and hashes, JSON segment/text agreement, selected versus
effective decoder settings, input-language declaration, finite ordered timing,
SRT/VTT timestamp ranges and exact subtitle text. Unexpected output paths and
unowned preexisting TXT files fail closed. The adapter does not infer recognition
accuracy from those consistency checks.

Only then is `receipt.json` published through the shared native no-replace
primitive. Cancellation or a malformed result before this point cannot produce
a completed receipt. Observer failure after this point cannot erase or hide the
committed result; a directory-sync uncertainty is returned as a warning. Saved
requests are inert and require explicit review. A request bound to another data
root is not silently migrated or executed.

## Evidence boundaries

`tests/transcription` executes real isolated child interpreters and real files
with an independently written controlled protocol backend. Its synthetic
package metadata and model bytes are explicitly not inference dependencies or
ASR weights. Tests cover source/runtime/request/output tampering, source changes,
all four output formats, pause/resume/forced stop, deadlines and disk/log budgets,
late cancellation/root changes, independent subtitle validation, private child
environment, and stale/destroyed UI callbacks with the real progress hub.

A separate private-only compatibility check ran the supplied worker itself with
a controlled backend, verified all four output types and a minute-boundary
timestamp, and left the supplied source unchanged. No private source or test was
copied into this repository. Real model inference, speech quality, installed
Windows ASR execution, native transcription keyboard acceptance and manual NVDA
remain separate uncompleted gates; these source/protocol checks do not claim
them. The common backup integration is documented by its typed adapter when
that separate integration is present.
