# Supplied-worker actual inference qualification — 2026-11-27

This is a bounded execution record, not general language or Windows acceptance.
Private source, recordings, runtime files and model weights are not published.
The complete receipts and original/repaired source comparison are retained in
the owner's private repair package.

## Exact executed identity

- Repaired supplied V4.3 worker SHA256:
  `b2e056e7a077cb52d6d0d105c5d33221a4ff8e35ebc3070fef924f280655c17c`.
- Executed application checkout: `bc39f41f36ac7119cb4015d159f7365e1d97378b`;
  transcription service SHA256:
  `cdc6546836edcfb359e1b7010a2ca1b6bd9901f83f58be9746e8973891fddd5e`.
- Linux, Python 3.12.14, faster-whisper 1.2.1, CTranslate2 4.8.0, PyAV 17.1.0;
  CPU, `int8_float32`, one worker, a physically separate dependency environment.
- Official tiny model revision: `d90ca5fe260221311c53c58e660288d3deb8d356`;
  five retained model files, 78,205,610 bytes. Model binary SHA256:
  `dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1`.

The source checkout had pending unrelated source-reader edits; every relevant
application/ASR/audio/state module was individually hashed before and after
execution. Those module hashes were unchanged. Subsequent composed versions
must retain this exact-source distinction rather than inherit a new acoustic PASS.

## Real execution

The normal `LibraryApp.transcription` service configured the reviewed worker,
retained the input, ran the child, verified results, read the text and exported
six files. The production `-I -S -B -X utf8` bootstrap was unchanged: no fake
recognizer, injected package path, altered worker bootstrap or instrumentation
was used in this application-boundary run. Dependency `pip check` passed.

The input was a self-authored English sentence rendered with local Flite:
4.9 seconds, 16 kHz mono PCM16, 78,400 frames. The actual service run took
3.457532 seconds; observed child peak RSS was 334,252 KiB. It completed one
segment with 17 ordered word timestamps and six verified outputs: TXT, JSON,
SRT, VTT, word SRT and word VTT. Original input, worker and model bytes remained
unchanged. Service verify, text access and new-directory export passed.

A separate actual standalone first/repeat execution measured 4.126277 and
2.771888 seconds. The repeat reused the validated recognition cache without a
second transcribe/segment collection; all six output contents matched, while
the successful-run receipt was fresh. Three cache files retained identical
content hashes and modification times.

## Quality and platform limits

Recognition changed “reading room” to “writing room” and normalized “nine” to
“9”. This demonstrates real inference and output plumbing, not perfect accuracy.
It qualifies one short synthetic English sample on Linux only. Ukrainian,
mixed-language accuracy, long recordings, diarization, Windows acoustic runtime,
human listening and NVDA were not exercised by this record.

The later supplied V5 profile has separate controlled-backend protocol tests.
Those tests do not transfer this V4.3 acoustic result to V5. No automatic model
acquisition, alternative recognizer, cloned voice or production network service
is enabled by this qualification. Python-level network observations are not
proof of an operating-system native-code sandbox.
