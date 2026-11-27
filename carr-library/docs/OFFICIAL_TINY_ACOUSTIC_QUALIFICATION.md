# Official tiny acoustic qualification

The optional workflow carr-official-tiny-acoustic.yml qualifies one direct
public engine call against one newly generated English SAPI recording. It is
separate from the product's worker protocol tests and from privately supplied
source. It has no access to the supplied worker, its companion, owner recordings,
private runtime, credentials, or owner licensing implementation.

## Exact inputs and boundaries

The script pins Systran/faster-whisper-tiny revision
d90ca5fe260221311c53c58e660288d3deb8d356 and the SHA-256 and size of all five
selected files: model.bin, config.json, tokenizer.json, vocabulary.txt and the
MIT model card. It admits the existing 31 exact CPU-runtime versions and the
Windows-only colorama 0.4.6 dependency. Markers, extras, Requires-Python and all
active Requires-Dist constraints must close to that exact set before installation.

Wheels come from the exact public PyPI releases. The acquired wheel's bytes and
size must match that release's official artifact metadata. Faster-whisper and
packaging also have previously reviewed wheel hashes in the source. The report
records every acquired wheel identity, metadata hash, declared license and
bundled notice hashes. No source distribution or build hook is used. Installation
is offline with the resulting exact hash lock; the complete installed RECORD
inventory is hashed before and after inference.

This is ephemeral execution permission for the qualification, not a release
package or blanket redistribution clearance for dependencies, SAPI voices or
their audio. Model, wheel, runtime and WAV files stay on the runner. The only
retained artifact is bounded JSON with public identities, the purpose-written
sentence, actual synthetic transcript, timings and measurements.

## Actual execution

The existing product SAPI5Engine creates one PCM16 WAV using a real installed
English voice. The direct official faster-whisper engine uses CPU, requested
int8, one native CPU thread, one model worker, local-files-only, beam size 5,
zero temperature and no VAD download. The process has a 300-second allowance.
It validates segment times, compares the transcript with the exact sentence,
requires word error rate no greater than 0.35, and verifies that source, model
and installed runtime bytes remain unchanged.

Windows Defender Firewall rules block outbound traffic for the selected venv
launcher, resolved base Python and host Python. All firewall profiles must be
active. A native connection to the same official endpoint must succeed before
the rule and fail with Winsock WSAEACCES 10013 after it; an unrelated timeout
does not count as isolation evidence. The inference process repeats that denial
check before and after loading the engine. Owned rules and the original profile
settings are restored afterward.

## Reading the result

A pass demonstrates this exact official tiny pack executing successfully on
that runner for the bounded synthetic English recording. The JSON deliberately
reports private worker interoperability, its cache/cancellation behavior, human
audio quality and multilingual qualification as NOT_RUN. It must never be used
to trust a private worker, approve an unreviewed runtime, or mark all ASR release
requirements complete. Product profile registration keeps its separate strict
identity and ownership review.

Primary inputs:

- https://huggingface.co/Systran/faster-whisper-tiny/tree/d90ca5fe260221311c53c58e660288d3deb8d356
- https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1
- https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/LICENSE
- https://pypi.org/project/colorama/0.4.6/
