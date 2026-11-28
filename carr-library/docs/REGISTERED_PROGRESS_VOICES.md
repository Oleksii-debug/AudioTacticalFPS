# Registered local voices for progress feedback

The current Progress feedback dialog now reads the same reviewed engine
registrations as Reader and Studio, scoped to the currently selected library.
It keeps the installed system voices and adds only enabled local registrations
through runtime_settings.configured_voice_registry. It does not introduce
another adapter, engine store, executable discovery policy, or model download.

## Selection and lifetime

"Choose local progress voice" runs discovery on the existing background worker.
The user selects the exact voice and English, Ukrainian or Slovak notice
language. The saved selection still contains its voice ID, language and runtime
fingerprint. Every short notification rechecks that fingerprint; registered
adapters continue to use EngineProfileStore admission before their existing
operation. A disabled, removed, disconnected or modified registration is refused.
There is no fallback to another voice.

The private notification output remembers its library root. A root change
invalidates its cached registry and cancels the old selection. If an earlier
discovery finishes late, it cannot install its registry over a newer library or
explicit selection. The registry lock protects only these short state changes;
discovery, synthesis and player settlement run outside it. Existing players,
failed-close handles and retained temporary directories continue to determine
actual quiescence. Changing roots never discards those owners.

The native dialog checks the same library owner after each voice/language
selector. A late modal return cannot save an old selection into another library.
The previously implemented observed-digest preference writer is preserved:
opening a dialog captures a separate exact observation, OK saves against that
observation, Cancel leaves runtime/preferences unchanged, and another writer's
change is not silently overwritten.

A restored progress preference can retain its literal selected identity.
Machine engine registrations remain outside complete backups. The restored
library must have an explicitly reviewed current registration before that voice
can run; an inherited historical executable pathname is never activated.

## Qualification scope

The committed progress_registered_oracle.json declares **146 source and 146
isolated installed cases**:

| Selector | Cases |
| --- | ---: |
| tests/audio/test_engine_profiles.py | 38 retained |
| tests/ui/test_progress_view.py | 25 retained |
| tests/ui/test_progress_compatibility.py | 32 retained |
| tests/ui/test_progress_concurrency.py | 27 retained |
| tests/ui/test_progress_registered_voices.py | 24 new |

The old full-disk fixture in test_progress_view now accepts and asserts the
existing writer's new expected_sha256 keyword so it still injects its intended
OSError. Its test identity and case count remain unchanged.

New cases use real profile files and manifest hashes, real background threads,
and complete backup/restore. They cover admission after inventory, revoked
cached profiles, late discovery versus root/selection changes, cancellation
during owned synthesis, retained failed-close ownership, passive Off, modal
confirmation races, and restoration without machine activation. Synthetic model
bytes and deterministic UI/player boundaries are explicitly test fixtures.
They do not qualify a neural model or audio device.

The native probe declares **16 actual Windows checks**. It operates the real
Progress feedback dialog, actual registered-profile inventories, voice and
language selectors, save/cancel and root-change refusal. It compares the actual
current voice fingerprint, exact saved preference bytes, unchanged synthetic
assets and passive complete-backup recovery. Its seven imported implementation
origins must lie in the isolated installed environment and match the complete
source-bound wheel. Its exact metrics are two registered libraries, two selected
registered voices, zero restored active registrations, zero notification audio
threads and one changed-owner refusal.

The native fixture is a valid registered Piper metadata profile containing
public synthetic nonexecutable bytes. Audio remains Off throughout the probe.
The receipt must explicitly state model_inference=NOT_RUN,
model_quality=NOT_QUALIFIED, physical_audio=NOT_RUN and nvda=NOT_RUN. Native
selection or a synthetic adapter boundary must never be relabeled as actual
Piper/Dordiai inference, a language-quality score or physical playback.

The dedicated workflow reuses the existing strict source/installed/native
qualification runner, the reviewed 56-pin Windows runtime and verified FFmpeg
pin, exact source/wheel inventories, and actual Python/FFmpeg outbound firewall
rules. It forbids skips and expected failures and requires exact function and
native-label Counters. Its own rules must be removed and prior profiles restored
before aggregate PASS. It records both reused adapter and selected dispatcher
hashes and retains bounded textual failure evidence.

At authorship, product checkpoint 49c429774821893889f9a7a9eea41800c7c6cea0 and
test checkpoint aeb5bdc5b6a2daeebf101bd4867443fc5a105773 were durably read back.
The local executor was unavailable. New source, installed, native and syntax
execution remain NOT_RUN until a fresh exact-head workflow supplies evidence.
