# Local voices and reading copies

The integrated studio discovers installed voices, renders actual PCM speech in
small cancellable chunks, and preserves the original narration text. A voice
only appears in the available list after its local engine enumerates it.

## Implemented adapters

Windows SAPI 5 uses a static installed PowerShell helper and the documented
System.Speech API. It discovers both native and WOW64 voices where those hosts
exist, preserves engine identity, checks the selected voice again before
rendering, and writes mono 24 kHz, 16-bit WAV. Source text goes through JSON and
the plain-string Speak method; it is never PowerShell or SSML. An unavailable
voice fails clearly instead of falling back to a different speaker.

An optional local FFmpeg build with Flite discovers its actual English voices.
The compatibility environment provided five voices. Flite relative speed uses
FFmpeg time stretching; it is distinct from native linguistic prosody. Optional
eSpeak NG discovery exposes installed language codes. No eSpeak language pack is
downloaded. Natural pronunciation, stress and native-accent quality require
listening acceptance for each chosen language and voice.

An explicitly configured Piper CLI/model and **dordiai-voices** installation can
join the same registry. Their adapter and manifest validator were incorporated
from this repository's PR99, commit
`7cf3ee625b0f30342ae23470f2248a0d1de32e40`. Only public adapter code was reused;
private worker sources, interpreters, dependencies, model weights and voice
assets are not copied or distributed. The dordiai protocol is the versioned
`nvda-worker-jsonl-pcm24k-v1` interface recorded by that change. Actual configured
dordiai/Piper model inference has **not** been run in the portable acceptance
environment. Real subprocess protocol fixtures are not neural speech evidence.

SAPI 4 remains unavailable until its real Windows 11 compatibility route is
tested. New roles choose only voices declaring the selected language. A missing
Ukrainian or Slovak engine fails visibly; English voices are not substituted.

Each result records engine and voice identity, exact input-text SHA256, audio
SHA256, PCM sample format, frame count and measured utterance duration. The
runtime fingerprint covers executable bytes, reported versions, the static
helper, installed voice identities and configured model manifest/asset hashes.
Model assets are rechecked before inference. Opaque proprietary SAPI assets
are not claimed to be individually hashed. Reload after installing or changing
voices. The wrappers make no service calls; a third-party installed SAPI engine
also needs its own offline/privacy verification. This is not a universal OS
network sandbox for arbitrary installed COM software.

## Explicit model configuration

Open **Audio Studio → Local voice engines...** to select local executable and
reviewed manifest files. **Verify files and save** registers those exact files
for the current library. Open a new Studio window after saving. The same
registration is used by production, the local assistant and the CLI through
`VoiceRegistry.for_library(root)`; there is one voice registry implementation.
The dialog can also import a deliberately selected UTF-8 configuration file.
CLI alternatives are `--voice-config C:/Reviewed/voices.json`, `--voices`,
`--voice-status`, `--voice-revalidate` and `--voice-reset` alongside the normal
library argument. These setup commands run separately from a production.

`VoiceRegistry.from_config_file(absolute_path)` remains available for explicit
standalone probes. None of these entry points searches imported books for
settings or automatically downloads or installs anything. The configuration
has this structure; replace
the illustrative absolute paths with the reviewed local installation:

```json
{
  "version": 1,
  "kind": "local-voice-engines",
  "config": {
    "include_sapi": true,
    "piper": {
      "executable": "C:/ReviewedPiper/piper.exe",
      "manifest": "C:/ReviewedPiper/voice/manifest.json"
    },
    "dordiai_manifest": "C:/ReviewedVoices/manifest.json"
  }
}
```

Either optional engine entry may be omitted. `ffmpeg` and `espeak_ng` optionally
select absolute executable paths; `false` disables that discovery. A configured
reviewed FFmpeg bundle takes precedence over ambient PATH and cannot be
overridden. Its integrity is checked again before a Flite process runs.

The library's `studio/voice-settings.json` stores strict configuration fields,
the SHA256 bindings of explicitly selected executables/manifests and a revision.
Changed bindings fail before use, including an already-open registry. No model
assets or executables are copied. A restored registration is inactive until
**Revalidate saved paths** or `--voice-revalidate` explicitly verifies that
installation again. Reset detaches registrations without deleting their assets.

Each model manifest uses schema version 1, engine (`piper` or `dordiai-voices`),
model ID, exact 40-hex revision or 64-hex digest, license, an explicit
`license_accepted` flag, a relative-path-to-SHA256 `files` mapping and `settings`.
dordiai-voices also requires `trusted_weights: true` and a complete installation
tree: an extra unlisted file or changed listed file prevents use. These flags
record an installation review; the software cannot infer license ownership or
speaker consent. Model manifests and hashes establish identity, not a sandbox
for hostile binaries or serialized model code.

Piper settings are `model` (listed `.onnx`), `config` (listed `.json`), `language`
and optional `name`. The selected OHF Piper CLI must support its documented
stdin, `--output_file`, `--length_scale` and `--volume` options. Model language
metadata does not prove native pronunciation quality. Bracketed phoneme blocks
are refused by this plain-text adapter because that CLI interprets them;
another engine can read such source text literally. Piper's external runtime
dependencies remain part of the separately reviewed installation; only the
configured executable and manifest-listed files are individually fingerprinted.

dordiai settings are `protocol: "nvda-worker-jsonl-pcm24k-v1"`, optional
relative `python`, `worker`, `catalog` paths (defaults `python/python.exe`,
`nvda_worker.py`, `models/voices/catalog.json`), and Carr's explicit per-profile
review metadata. For example, for an actual installed profile called `profileA`:

```json
"profile_languages": {
  "profileA": {"supported": ["uk"], "candidates": ["sk"]}
}
```

Only languages present in the installed catalog may be declared. The catalog's
global language inventory never becomes support for every profile. Only that
profile's `supported` list participates in production/default selection; its
`candidates` remain visible review metadata and are refused for synthesis.
A profile without its own declaration is listed as unavailable in engine status.
These are user-reviewed capability declarations, not proof of acoustic quality;
each language/profile pair still needs actual listening acceptance. Changing a
declaration changes the reviewed manifest and requires explicit re-registration.

The worker consumes JSON and returns bounded
base64 PCM chunks at 24 kHz. Cancellation kills the owned worker; no private
worker code is imported into the host. Python network/child-process audit
denials and offline environment flags supplement the trusted installation;
they do not sandbox arbitrary native dependencies.

Both adapters expose relative registry rate `0.5` to `2.0`, volume `0` to `1`,
bounded PCM WAV output and cancellation. They do not expose a cloning command
or claim undocumented controls. Manifest enumeration reports inference as
unverified until an actual configured runtime is exercised. Use
`tests/audio/native_configured_model_probe.py --config ... --voice ... --language uk`
for that separate installed-runtime test; it writes a temporary synthetic-text
utterance and reports measurements without publishing private voice names.

## Reading edits

`NarrationDerivative` keeps an immutable original string and its checksum.
Accepted replacements, insertions and deletions refer to original Unicode
offsets. The reading copy exposes a mapping back to those offsets, a diff,
reject/undo/redo operations and versioned JSON recovery. Modified phrases map
to the complete original source span; this mapping is a reading aid, never
quotation approval. The module cannot write original files or the citation
database.

The Audio Studio's **Pronunciation and reading copy** editor uses this backend.
It shows read-only original/reading/diff views, previews exact literal matches,
applies only reviewed proposals, supports per-change rejection and segment
undo/redo, and imports/exports scoped dictionaries. Saving returns one new
project revision. Production actually synthesizes the accepted derivative and
exports both original text and global original-offset mappings in its timings.
The original script, source revision and research quotations remain intact.

Pronunciation dictionaries support literal word/phrase substitutions with
language, voice and project scopes. Specific scope overrides precede generic
rules; already accepted manual edits win conflicts. Regex-looking characters
remain literal. Executable regex replacement, universal stress restoration,
phoneme/style synthesis and unsupported native accent promises are absent.

## Native preview

`LocalPlayer` opens a local PCM WAV without autoplay. Play, pause, resume, stop,
seek and status use typed WinMM messages. Seconds are converted explicitly to
native milliseconds. Filename text is never a command string. The preview
does not alter global output volume or NVDA audio settings. It supports the
installed Windows default audio device and reports missing-device errors.
RF64/compressed playback and advanced device/rate controls need a broader
verified playback backend; use individual WAV chapters for this preview.

## Verification boundaries

Portable tests include real local speech, timing and amplitude changes,
Unicode filenames, command-like text, cancellation, no-overwrite behavior,
runtime provenance, immutable source mappings and persisted undo history.
The native installed-package SAPI probe requires actual Windows speech output.
The separate native player probe reports `NOT_RUN / DEVICE_UNAVAILABLE` if no
audio device exists. Typed-message unit tests are not audible-playback evidence.
Manual Windows 11 listening and NVDA verification remain distinct acceptance.

## Primary interface references

- [Microsoft installed voices](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.getinstalledvoices)
- [Microsoft plain-string speech](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.speak)
- [Microsoft speech rate](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.rate)
- [Microsoft WAV output](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.setoutputtowavefile)
- [FFmpeg Flite filter](https://ffmpeg.org/ffmpeg-filters.html#flite)
- [Microsoft waveform audio commands](https://learn.microsoft.com/en-us/windows/win32/multimedia/waveform-audio-command-set)
- [Microsoft typed MCI structures](https://learn.microsoft.com/en-us/windows/win32/multimedia/mci-structures)
- [OHF Piper CLI](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/CLI.md)
- [OHF Piper CLI argument definitions](https://github.com/OHF-Voice/piper1-gpl/blob/main/src/piper/__main__.py)

Existing engines retain their respective license terms. No voice binaries,
third-party model weights or private audio are included by these source files.
