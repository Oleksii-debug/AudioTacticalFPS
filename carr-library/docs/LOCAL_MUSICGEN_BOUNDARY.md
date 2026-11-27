# Optional local MusicGen: implementation and acceptance boundary

The existing optional adapter uses the installed Hugging Face Transformers
`MusicgenForConditionalGeneration` and `AutoProcessor` APIs. It loads an explicit
local model directory with `local_files_only=True`, `trust_remote_code=False`
and `use_safetensors=True`. There is no model downloader, installer, remote
provider fallback, arbitrary command argument or automatic engine selection.

**Real MusicGen inference has not been executed for this change.** The development
host has no installed Torch, Transformers or approved MusicGen weights. Boundary
tests use inert files and never claim that these are functioning models. Windows
MusicGen remains unavailable until a reviewed native network isolation launcher
is implemented and verified. Ordinary Windows speech, procedural music and audio
processing have separate acceptance results.

## Explicit configuration and model files

The public integration remains `MusicGenConfig(model_dir, manifest,
manifest_sha256, python_executable, device, timeout_seconds)` and
`generate_music(MusicSpec(..., engine='musicgen'), ..., musicgen_config=config)`.
The approved manifest must be outside the model directory. Its exact SHA256,
the model identity and exact 40- or 64-character revision, the installed runtime
versions, every model file's size and SHA256, and separate code/weight license
identifiers are required. Only the personal noncommercial weight profile is
accepted by this adapter. A hash identifies bytes; it does not establish that a
model source, native runtime, license decision or runtime installation is trusted.

`validate_musicgen_config` verifies the installation inventory. That result alone
does **not** establish inference readiness. Actual rendering additionally checks
the complete MusicGen/EnCodec/T5 configuration, processor metadata, tokenizer
configuration and vocabulary, and either complete safetensors weights or the
approved safetensors shard index. Missing assets produce an unavailable reason
before optional model packages are imported.

Both the parent and the fixed worker enforce the following boundaries:

- Every path is a regular local file under the chosen model root. Symlinks,
  reparse points, Windows network shares and mapped network drives are rejected.
- Every file in the model directory must appear in the approved inventory.
  Binary pickle checkpoints, Python/native code, unreviewed model adapters and
  quantization loaders are rejected. JSON data and directory traversal have
  explicit resource budgets.
- Safetensors index `weight_map` values must name approved relative safetensors
  files. Absolute paths, `..`, URLs, missing files and alternate data streams
  fail before any referenced outside file is opened.
- Loader file/path fields in model and tokenizer JSON must refer to the same
  approved inventory. Remote codec links, custom-code maps and unsupported
  processor/tokenizer classes fail closed. Vocabulary text remains plain data;
  a token containing a slash or URL is not interpreted as a path.
- The worker repeats file and configuration verification before model imports.
  The parent repeats model verification before publishing the generated result.

## Network and native-runtime boundary

The worker installs a Python audit hook before importing optional model packages.
It blocks Internet socket creation and transport through `socket`, `_socket`,
constructors saved before the guard, DNS lookups and Python child-process launch.
Its environment is constructed from required OS values. Credentials, proxy
settings and native loader injection variables are not inherited; temporary and
Hugging Face/Torch cache paths are private staging directories.

**A Python audit hook is not an operating-system sandbox.** Torch, CUDA and other
native libraries can issue system calls without using Python socket APIs. For
this reason actual MusicGen inference also requires kernel-observed Linux network
isolation: the worker must have a network namespace different from PID 1, with
only the loopback interface. No manifest field or environment flag can satisfy
this check. The adapter neither elevates privileges nor installs firewall rules.
If this state cannot be verified, it reports MusicGen unavailable before loading
the model. This deliberately rejects ordinary network-connected processes and
Windows installations until a suitable reviewed launcher is available.

The selected interpreter, its standard library, installed Python packages and
native libraries remain a trusted local runtime boundary. Version strings do not
authenticate every installed runtime byte. Network-namespace checks are not a
general filesystem or hostile-native-code sandbox. No claim is made that these
guards can safely execute an untrusted runtime or arbitrary model plug-ins.

## Chunking, output and remaining acceptance

The adapter currently generates independent text-conditioned chunks of at most
25 seconds. The director may schedule many chunks; this is not a final production
duration ceiling. The worker reports `continuation=False`. It does not yet provide
audio-conditioned continuation, melody conditioning, seamless joins, coherence
over hours, or internal token-level progress. Parent cancellation interrupts the
worker and preserves completed project checkpoints.

The worker validates the actual returned waveform's dimensions, finite samples
and required frame count, records attenuation used to avoid clipping, and writes
PCM16 to private stage files. The existing renderer verifies the converted output
and publishes without overwriting a user destination. CUDA compatibility,
throughput, musical quality, real neural continuation, full-length original neural
generation, manual listening and native Windows/NVDA acceptance remain **NOT_RUN**.
Synthetic long streaming fixtures are separate evidence and cannot establish any
of these neural acceptance items.

## Reviewed primary sources

- [V3-14 pinned AudioCraft MusicGen documentation](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/docs/MUSICGEN.md)
  documents both AudioCraft and the Transformers interface. AudioCraft's MIT code
  license is separate from the CC-BY-NC 4.0 weight license and from Transformers'
  Apache-2.0 code license.
- [Pinned AudioCraft loaders](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/audiocraft/models/loaders.py)
  support pickle checkpoints and remote codec pointers; that loader is not used
  by this safetensors-only adapter.
- [Pinned AudioCraft continuation implementation](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/audiocraft/models/genmodel.py)
  distinguishes genuine audio continuation from independent chunk generation.
- [Transformers 4.57.6 local shard resolution](https://github.com/huggingface/transformers/blob/v4.57.6/src/transformers/utils/hub.py)
  joins local index values to the model root. The application therefore validates
  those references before handing the directory to Transformers.
- [AudioCraft code license](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/LICENSE)
  and [separate weight license](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/LICENSE_weights)
  were read during the review. These notices do not replace exact local asset
  provenance and use-profile review.
