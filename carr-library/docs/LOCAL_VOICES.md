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
review metadata. `reference_engine` accepts exactly `holos`, `openvoice` or
`omnivoice`; the compatibility default is `holos`. The relative directory
settings `models`, `reference_model` and `references` default to `models`,
`models/Holos` and `models/voices`. Selected directories must already exist and
contain assets listed in the complete reviewed manifest. For example, for an actual installed profile called `profileA`:

```json
"profile_languages": {
  "profileA": {"supported": ["uk"], "candidates": ["sk"]}
}
```

Only languages present in the installed legacy catalog or the selected managed
profile may be declared. The catalog's
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
bounded PCM WAV output and cancellation. The owner reference backend reuses its
existing clone-prompt synthesis; Carr adds no model training pipeline or
undocumented synthesis controls. Manifest enumeration reports inference as
unverified until an actual configured runtime is exercised. Use
`tests/audio/native_configured_model_probe.py --config ... --voice ... --language uk`
for that separate installed-runtime test; it writes a temporary synthetic-text
utterance and reports measurements without publishing private voice names.

## Reference voice packages through the existing owner API

Open **Audio Studio → Local voice engines… → Reference voice packages…** after
registering the reviewed owner installation. This uses the same voice registry
as Studio, the reader and command-line production.

1. Select **holos**, **openvoice** or **omnivoice** and the existing relative
   model and reference folders. Choose **Save backend and folders**. Changing
   backend or reference folders clears prior production-language approvals.
2. Choose **Inspect data-only package…**. The installed owner's
   inspect_package/validate_adapter functions verify the package; the dialog
   displays its exact identity, version, source, license and reference roles.
   OmniVoice requires reference_audio and reference_text, OpenVoice requires
   target_embedding, and Holos requires holos_profile.
3. Confirm **I have permission to use the exact voice references shown in this
   review**. Enter only the profile's languages you have reviewed for production,
   separated by commas. A blank field preserves all declared languages as
   candidates and does not make the profile selectable for synthesis.
4. Choose **Apply reviewed profile**. Carr invokes the existing owner
   install_voice_package, verifies the resulting data and activation index,
   checks that unrelated runtime files stayed unchanged, and updates the
   reviewed local inventory and library registration. No model is downloaded
   and no reference leaves the local computer.
5. For a previously installed version, enter the profile ID, choose
   **List installed versions**, enter the version, then choose **Review selected
   installed version**. Activation calls the owner's activate_voice_version
   and requires a fresh explicit rights/language review.
6. Open a new Studio or reader window, select that profile and explicitly
   generate an audition. Inspecting or installing metadata does not run a
   neural model or certify the voice's sound.

The runtime must include its reviewed voice_policy.py, voice_packages.py
and voice_assets.py alongside its selected worker. Those private modules are
not included in Carr. A bounded isolated child captures and hashes their source
and compiles those exact bytes; it does not load their bytecode caches or import
them into the application process. The existing network and child-process
audit denials remain active. Some owner multilingual routes require additional
native helpers; they remain unavailable under this restriction until their
exact helpers are independently reviewed. A Python audit hook does not sandbox
arbitrary native code.

Managed discovery applies the owner's profile policy and active-package
inventory. An inactive, damaged or incompatible managed identity cannot be
silently replaced by a legacy voice with the same ID. Production language
claims are checked against that individual package. A changed reference,
package version or permission review changes cache provenance; a stale review
does not authorize the new data.

The explicit review is recorded in the local installation manifest as
profile_reviews, bound to the version, package metadata hash and each reference
role's asset hash. These records establish which bytes the user approved; they
do not establish legal ownership or acoustic quality. Research-library backups
retain only the existing local registration, with its restore/revalidation
barrier. They do not copy the separate private runtime, reference recordings,
embeddings or voice packages.

Cancel or close requests stop the bounded metadata child and prevent a later
unreviewed registration. If the owner installer had already written data when
it was interrupted, the old registration fails closed. Review the external
installation and its complete inventory before re-registering; the application
does not silently delete package data or invent a rollback. A failure while
rebinding a successfully updated manifest requires explicit revalidation in
**Local voice engines**.

The transport still accepts bounded 24 kHz mono PCM16 chunks or one-shot PCM.
The 4 MiB JSON-line ceiling limits a one-shot OmniVoice reply to approximately
65 seconds of audio; oversized replies fail visibly. Long production remains
segmented by the existing Studio workflow. Worker/model caching across segments,
recording new reference samples, reference-quality assessment, expressive
controls beyond the existing adapter, and real multilingual similarity and
listening acceptance are separate work and are not certified by package
registration.

Portable tests run actual isolated children against explicitly synthetic owner
API fixtures. tests/ui/native_voice_packages_smoke.py exercises actual wx
backend, rights, language and installed-version controls with that same
synthetic API. Its output separately labels real model inference, the native
file picker, reference quality and NVDA speech as **NOT_RUN**.

## Reviewed export, deactivation and deletion

The existing **Reference voice packages** window also maintains an explicitly
selected installed profile. It keeps one registration, voice registry, owner
package API and shared resource broker.

Review an installed version, then separately confirm permission to export its
exact reference files. **Export reviewed installed version…** creates a new
local .dordiai-voice or ZIP data package through the installed owner's
create_package API. It contains that version's document and reference assets;
shared models, interpreter, worker code and other profiles are excluded.
The actual archive is inspected again, hashed and published without overwriting
an existing file. The returned receipt includes its size and SHA-256.
**The package is not encrypted.** The explicit private local destination and
rights review do not constitute encrypted backup or permission to redistribute
someone else's voice data.

For removal, choose the installed profile ID and either **Deactivate and keep
reference files** or **Delete all installed versions and reference files**.
**Review profile removal** displays the selected identity, versions, exact file
count and byte total. It never prefills confirmation. Type exactly the displayed
REVOKE profile_id or DELETE profile_id phrase and choose **Apply confirmed
removal**. Deletion is permanent for that selected installation's profile data.
Close voice sessions in other applications that use this same installation;
Carr's cooperative reservation covers Carr operations, not other programs.

The bridge rechecks the activation index and every selected reference file
after acquiring the actual owner's installation lock. It then calls the same
owner uninstall_package API with its durable inactive tombstone. Deactivation
keeps all versions; deletion removes only the selected profile's versions.
Full post-operation inventory verification requires all unselected runtime,
model and profile files to remain byte-identical before registration is rebound.

Revocation is also retained in the existing reviewed installation manifest.
Even after deletion removes the owner's package index entry, a legacy catalog
voice with the same ID cannot become an automatic replacement. A fresh explicit
installed-version or package review, reference-rights approval and production
language selection are required to re-enable that identity. Inspecting or
exporting a version does not itself restore synthesis permission.

Cancellation and uncertain native shutdown retain the existing admission and
private staging owners. An interrupted owner mutation can leave an inactive or
partly removed package; the old registration then fails closed. There is no
automatic deletion replay, arbitrary rollback or silent adoption of changed
files. Review the actual installation and explicitly revalidate before reuse.

The callable owner contract was checked at immutable source
[a1c73a5747bc86eb11f0c037120442e0c2593e91](https://github.com/Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension/tree/a1c73a5747bc86eb11f0c037120442e0c2593e91/dordiai-voices/source/0.12.2/kx47Runtime):
voice_packages.py blob 2abe2a50bcf58aea423764ef15acc04538ee2f9b and
voice_assets.py blob 6cabd9c44a3979e2b543f2272915fdc83523c87c.
Only the public bridge and synthetic contract fixtures are added to Carr.
The private owner implementation and reference payload are not copied.

New prepared cases use actual isolated children, native locks, data archives,
failure/cancellation paths and the existing UI worker. Native source, installed
and bundle probes extend their unchanged synthetic-owner acceptance.
At this source checkpoint the new lifecycle execution is **NOT_RUN**.
Real owner-runtime inference, authorized human reference quality, manual NVDA,
speaker similarity and multilingual quality remain distinct acceptance.


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
