# Independent Dordiai adapter acceptance

This directory owns acceptance tests only. It changes no production adapter and
contains no private Dordiai code, catalogue, models, recordings or runtime.
The synthetic actor produces tiny arbitrary PCM numbers; a passing process test
is not speech, pronunciation, Windows audio-device or NVDA acceptance.

Run from the `carr-library` project directory. For independent review of a
not-yet-integrated public CARR adapter, select its file explicitly:

    CARR_DORDIAI_ADAPTER_FILE=/absolute/path/to/integrations/dordiai_voices.py \
      python -B -m pytest -q -p no:cacheprovider tests/voice_acceptance

Without that variable, the tests import the available installed
`carr_library.integrations.dordiai_voices` module. If it
has not been integrated, the suite is explicitly skipped and must not count as
passed. An explicit missing adapter path is a failure. Adapter/helper hashes
must remain unchanged throughout a run.

## Independently verified external boundary

Read-only review of the installed-product source export was checked against the
remote Git tree at source be7f4e2468dc18bd155d2c205f89bf61c8e235a2,
version 0.13.1-dev.12. Seven local contract files matched exact Git blobs.

- Worker SHA-256: 8fb4303098489bc8ad255696895a05f3daf731e60c4dbb8b271c16fdc31d121a.
- Worker Git blob: b5c55a7c23904275a92ea5182104d68eca1765f9.
- Existing public-boundary probe Git blob: 0890cbf4b59747df01561350327a71a0db7226e6.
- Language inventory helper Git blob: ee6faebefb7fdd955ce13621656bfa4678156707.
- Voice policy helper Git blob: de305d15a955b92dc1a8bfb719b7f9df501a94d8.

Transport is one owned Windows process using the external installation's private
Python, with isolated mode, nvda_worker.py, models, --reference-model Holos,
--references voices, --reference-engine holos and --preload-language mixed.
It uses JSON Lines, not HTTP or JSON-RPC. No upstream version or capabilities
RPC exists. Utterances must be serialized because there are no request IDs.

A reference request includes explicit profile, language, text, speed,
stream=true and pitch/timbre/volume controls. Language routes use nativeProfile
for English/Russian, nativeLocale for supported local European adapters, and
wideLocale for installed wide-language adapters. Voice names alone never prove
compatible installed assets; use the installed catalogue/policy helpers.

Each chunk reply contains base64 mono s16le PCM at 24000 Hz. The client requests
each subsequent chunk with mode=next until finished=true. Loading-stage messages
are status only. Cancel has no acknowledgement: drain the outstanding response,
then send mode=drop and require its exact finished acknowledgement. Discard and
restart an owned worker after uncertain framing or failed synchronization.

Current real-source exports available during review lack the private Windows
interpreter and acoustic models. No inference was attempted by this acceptance
suite. An actual installed runtime and explicit listening/device evidence remain
separate acceptance inputs.
