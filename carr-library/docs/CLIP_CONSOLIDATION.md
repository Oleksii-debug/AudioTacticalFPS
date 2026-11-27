# Consolidating adjacent clips

Audio Studio keeps the existing **Join adjacent clips** command for consecutive
selections of the same source. **Consolidate into new audio** explicitly renders
two adjacent clips in the same chapter and track into a new retained stereo
PCM asset. The two clips may have different original files, sample rates,
channel counts, trim selections, gain, balance and fades.

The current clip processor applies each clip's saved controls, then the existing
bounded PCM concatenator joins their exact project sample intervals. The new
clip retains the first timeline position, identifier, track and title. Its trim,
gain, balance and fades reset because those controls are already baked into the
new samples. Unrelated script lines and line endings stay intact. Studio
accepts the result through its existing draft fingerprint and Undo mechanism.

The original retained assets are never overwritten or removed. A bounded
data-only receipt beside the production plan records both complete source
identities, original clip controls, processing identity and the result's exact
sample count and hash. The receipt contains no external file paths or runnable
runtime configuration. An exact repeated operation verifies those bytes and
reuses the existing independent output. Every retry re-acknowledges directory
durability before returning a completed result.

Project snapshots retain the source assets and receipt even when rendered
outputs and caches are excluded. Full library backup uses the same strict
producer and checks the receipt against all retained audio after restore.
Restoring does not launch FFmpeg or activate a runtime.

## Operation and limits

The reusable checked operation is
carr_library.audio.clip_consolidation.consolidate_clips. It accepts two typed
current Clip values, the selected existing project root, a reviewed local
FFmpeg, sample rate, project disk allowance and optional current ResourceBroker.
It creates no second job store. A caller must still authorize the project and
selected edit; this helper does not create an assistant approval.

Inputs must be exactly adjacent in project samples and have the same track.
Gaps, overlaps, cross-chapter UI requests and inconsistent identities fail
before native processing. Existing retained-input limits apply to the independent
result: at most eight GiB per PCM asset. Up to 1,000 bounded 32 KiB provenance
receipts are retained per project. The existing project disk budget is checked
before admission and again after waiting for resources.

The native project lock and resource lease remain held through all child-family
cleanup and private-stage removal. Source and runtime readers remain held from
native admission through final validation. No output is acknowledged if that
validation fails. Receipt publication is the completion boundary; cancellation
before it leaves the original draft intact. A later Stop does not relabel an
already published complete receipt as unfinished.

## Evidence

This source checkpoint was authored while the executor was unavailable. The
new tests/audio/test_clip_consolidation.py cases and the added native
Consolidate button probe are **NOT_RUN** for these exact bytes until executed
in the composed source/installed Windows gates. The companion
tests/audio/test_clip_input_ownership.py must run after the existing FFmpeg
preparation, with no optional dependency skip.

The tests use synthetic PCM sources and the actual installed FFmpeg. They
cover differing sample rates and clip processing, exact frame order, reuse,
input changes, final-reader refusal, cancellation, post-move flush failures,
existing draft Undo/stale acceptance, full production and both project/library
backup portability. Native controls, physical listening and NVDA are separate
acceptance evidence; no subjective audio-quality claim is made here.
