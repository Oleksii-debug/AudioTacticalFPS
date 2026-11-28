# Supplied ASR worker: private source preparation

This preparation reuses the existing narrow ASR-03 transformation in
[PR135, commit f20f9cb](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/f20f9cb65411ee6b7ebb538ce2eb34ea02cd5aaf).
It preserves the supplied worker's inference implementation. It does not
register an engine, replace the application's job framework, install a runtime,
download a model or qualify the worker for end-user use.

## Authorized source identity

The preparer accepts exactly 37,974 bytes with SHA-256:

`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`

There is no command-line option to override that identity. Source reads are
bounded before parsing. A changed input is rejected before any source import.

The supplied worker is private. Keep both input and generated output in an
authorized private folder outside public source. Do not commit, attach or upload
the worker, private recordings, model weights, environment, paths or generated
private candidate to the public repository or public CI artifacts. The public
tests use only a small independently written protocol fixture.

## Exact transformation

The preparer changes two existing source regions:

1. The worker's recognition-attempt generator returns one copy of the requested
   settings. It cannot silently retry with changed language, beam, VAD or context
   settings.
2. The existing recognition call remains intact while its unsupported-option
   `TypeError` downgrade is removed. Unsupported multilingual settings must fail
   instead of being silently dropped.

The original call's arguments, spelling, comments, continuation lines and
literal contents are preserved. Only its first-line indentation moves outside
the removed `try` block. The script no longer reserializes this private source
statement using `ast.unparse`, whose formatting depends on the preparer's Python
version. Explicit structural checks reject an unexpected exception handler,
model call, settings signature or cleanup branch. These checks do not depend on
Python assertions being enabled.

The completed candidate is compiled before publication. Its output hash is
printed for the private build record. Recompute and record the actual candidate
hash with the selected Python version; the earlier PR135 candidate's hash is not
the identity of this revised transformation.

## Private use

From the source project's `carr-library` directory, supply private paths
explicitly:

```text
python scripts/prepare_supplied_asr.py AUTHORIZED_PRIVATE_WORKER --verify --output NEW_PRIVATE_CANDIDATE
```

`--verify` imports only temporary copies of the already identity-checked input
and its prepared candidate. It replaces the model with synthetic doubles and
checks three narrow behaviors: one unchanged-settings attempt, no unsupported
multilingual retry, and retained automatic language detection/empty-result
semantics. It does not run faster-whisper or transcribe an audio recording.

`--output` stages a new private file, flushes and fsyncs the writable handle,
then uses an exclusive hard-link publication. Existing destinations, including
the original source, are never replaced. A write or fsync failure leaves no
published candidate; a filesystem without the required exclusive publication
operation fails explicitly. No fallback performs a permissive overwrite.

The generated candidate is **preparation only**. Do not enable it in the
application until its remaining source/runtime integration and acceptance
checks pass.

## Public regression gate and evidence

Run the public fixture suite without any private source or model:

```text
python -m pytest -q tests/test_supplied_asr_preparer.py
```

It covers exact identity before parsing, bounded reads, preserved LF/CRLF call
bytes without AST reformatting, unchanged auto/multilingual arguments, the
original-versus-prepared three-check behavior, unexpected structures, exclusive
publication, fsync failure and unsupported hard links.

This revision was prepared during an executor outage. The new fixture suite and
the revised transformation on the actual private worker are **NOT_RUN** until
the receiving CI/private runtime executes them. The existing PR135 reported its
own earlier synthetic checks; those are not execution evidence for this
revision. No actual audio, installed Windows, NVDA or recognition quality result
is claimed here.

## Remaining supplied-worker integration

The other reviewed gaps remain separate work on the same supplied worker:

- Content, model, runtime and effective-settings identity for cached results.
- Removal of unrelated TXT adoption based only on a normalized basename.
- Completion only after every required output and aggregate is durably checked.
- One coordinated model session covering lazy segment iteration and replacement.
- Segment-level pause/cancel and restart validation of already completed files.
- Exact isolated runtime/model revisions, hashes and rights; real inference,
  output timing, native Windows and accessible workflow acceptance.

This tool addresses recognition-setting fallback only. It provides no cache,
job-completion, concurrency, cancellation, diarization or quality guarantee and
must not be described as complete ASR integration.
