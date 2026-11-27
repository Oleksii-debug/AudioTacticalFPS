# Supplied transcription: explicit V4.3 compatibility adapter

The default product assistant now routes `audio.transcribe` through the existing
library-scoped `TranscriptionService`, as described in
[assistant_runtime.md](assistant_runtime.md). This document describes only an
explicitly injected, separately pinned V4.3 compatibility installation. Its
source transform, manifest and saved intent are not interchangeable with the
receipt/V5 worker profiles. Old outputs remain verifiable; no automatic runtime
or job-format migration is implied.

This is a backend adapter to the owner's existing V4.3 headless worker, using
the primary assistant's typed plans and JobStore. It is not a second ASR engine,
an installed-model pack, an enabled desktop transcription screen, or completed
ASR acceptance. No worker source, audio, transcripts, virtual environment,
credentials, model bytes or private source-location URL is redistributed.

## Exact reused boundary

The reviewed original is 37,974 bytes with SHA256
`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
`prepare_worker` reuses the source transform from commit
`f20f9cb65411ee6b7ebb538ce2eb34ea02cd5aaf`: it removes implicit language/settings
retries and the unsupported-option TypeError fallback on a private staging copy.
The resulting exact candidate has SHA256
`d718866a468f087d5714d7595f7c82921711164c0defebbcedf56afeafcc0992`.
Both source and prepared hashes must match before launch. The original is kept.

The process still receives `worker.py --job-dir <private directory>`. Each
invocation uses a fresh `job_config.json`, immutable `settings_live.json`, one
copied audio file, a new cache and new results directory. This makes the supplied
worker's path/mtime cache and basename-only adoption inapplicable. An absolute
verified local model path prevents selecting the downloader's model-name path.
Recognition settings use CPU/int8, one file, one worker, selected language or
auto detection, beam size, VAD and previous-text conditioning. Translation,
word alignment, diarization and CUDA are not enabled by this adapter.

Required TXT, SRT, VTT and JSON must all exist and agree with the source,
requested settings, actual detected language and finite ordered segment times.
A zero exit code or the worker's completion string alone is insufficient.
Silence may yield empty TXT/SRT with empty segments; it does not trigger a
language change. Published JSON replaces disposable staging paths with original
source and installation identities. It remains a derived transcript requiring
human accuracy review.

## Explicit installation, no acquisition

Construct `SuppliedWorkerInstallation` with six explicit values: absolute local
interpreter, original worker, isolated package directory, model directory,
manifest path, and separately retained manifest SHA256. The manifest stays
outside the two inventoried directories and contains exactly:

```json
{
  "schema": 1,
  "engine": "supplied-whisper-v43",
  "python_sha256": "<reviewed interpreter SHA256>",
  "python_version": "<major.minor.patch>",
  "runtime_files": {
    "faster_whisper/__init__.py": {"size": 0, "sha256": "<reviewed SHA256>"}
  },
  "model_files": {
    "config.json": {"size": 0, "sha256": "<reviewed SHA256>"},
    "model.bin": {"size": 0, "sha256": "<reviewed SHA256>"},
    "tokenizer.json": {"size": 0, "sha256": "<reviewed SHA256>"}
  },
  "runtime_notice": "<exact source, dependency/native inventory and rights evidence>",
  "model_notice": "<exact model revision, source and rights evidence>"
}
```

The abbreviated example is not a usable manifest. Every actual file and byte
count in both trees must be inventoried, including SDK/native/transitive files.
Links, unlisted files, changed hashes and incomplete local model files fail
before launch. The interpreter bytes and reported Python version are checked.
Version and license descriptions alone do not certify compatibility or grant
distribution rights. Operating-system libraries remain part of installation
qualification; copying a virtual environment is not a portable runtime build.

Execution uses the existing bounded audio process runner with isolated Python
startup (`-I -S -B`), only the selected package path, offline cache flags, and
network/child-command and output-write audit guards. The selected process's
logs, staging bytes, file count, timeout and free disk reserve are bounded.
These checks are defense in depth, not an OS sandbox for native extensions.
Cold offline Windows execution with the real pinned SDK/model remains required.

## Explicit compatibility integration and recovery

Pass `SuppliedTranscriptionAdapter(installation)` as the optional `transcription`
argument to `AssistantService` only for this compatibility protocol. The allowlisted `audio.transcribe` step has
required `path` and `name` arguments and optional `language`, `vad_filter`,
`beam_size`, `condition_on_previous_text` arguments. `path` must be inside the
approved input scope; `name` selects a new directory inside the approved output
scope. It creates `transcript.txt`, `transcript.srt`, `transcript.vtt` and
`transcript.json`. A caller may submit several separately authorized steps for
a batch. No arbitrary worker path, model name, command or remote endpoint can
come from a plan. Existing GUI constructors do not configure this optional
adapter: their default ASR path uses the separately configured library service.
A saved intent must resume under its original authority; passing this adapter
cannot reinterpret a new library-scoped transcription job.

The existing assistant intent stores input bytes SHA256, settings, adapter and
installation identities before launch. Before publishing it stores the exact
four artifact paths, sizes and SHA256 values. Publication never replaces an
existing file. A saved completed publication is checksum-verified and reused
after a crash/cancellation. An interrupted publication may finish only if the
retry reproduces the saved artifacts exactly; externally edited or different
outputs require review. Cancellation terminates the owned worker with the
existing runner, discards incomplete staging and preserves completed files.
Resumption restarts an incomplete file; it does not promise mid-file acoustic
checkpoints. Within one application store, a per-installation OS file lock prevents
simultaneous calls using that manifest; different application roots do not share
that lock and require separate resource admission; the worker itself has `parallel_files=1` and immutable settings.
An abrupt process kill between hard-link publication and staging unlink can
leave a linked output and private staging residue. Verification then fails
closed and requires review; generic crash/power-loss recovery is not qualified.

Common-v4 backup retains these assistant plans and inert checkpoints. The result
explicitly lists source audio, exported transcripts, worker, runtime and model
as external dependencies. This adapter does not add complete ASR migration:
their bytes are not automatically copied into a library backup, and restoring
to another data directory still requires explicit input/installation review.

## Observed evidence and remaining acceptance

The exact private worker was run through this adapter on Linux using an isolated
synthetic SDK and synthetic audio. Selected Ukrainian recognition produced all
four verified exports; a synthetic auto-detection error remained failed and
produced no exports. A required SRT write failure in the real worker also remained
failed at the adapter boundary despite the worker's success status. Source and
prepared hashes above matched. Reproduce locally, with the private source kept
outside the repository, using
`PYTHONPATH=src python scripts/verify_supplied_transcription_protocol.py --worker /absolute/private/worker.py`.
This tests the
actual supplied worker protocol and correction, without claiming model inference
or acoustic quality. Public regressions use an explicitly synthetic process and
cover omitted exports, false success, forced-language/settings mismatch, source
changes preserving size/mtime, no-overwrite, partial-publication resume,
cancellation after commit, active-process cancellation, runtime/model tampering,
path authorization, and common-v4 checkpoint preservation.

Real runtime/model installation, measured long-file CPU/memory behavior,
multilingual audio quality, native Windows offline execution, NVDA/UI selection
and editing, complete pending-ASR migration, speaker diarization and word
alignment remain **NOT_RUN / not implemented by this adapter**. The supplied
project root contained no model directory or immutable dependency/model lock.
This adapter advances ASR-01 through ASR-06 protocol safeguards; it does not mark
ASR-07 or ASR-08, or the complete platform, passed.
