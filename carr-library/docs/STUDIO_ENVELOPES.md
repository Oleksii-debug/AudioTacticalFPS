# Sample-based Studio automation envelopes

Studio supports gain, mono pan and stereo-width curves through the existing
ordered effects editor. Each curve has two to eight explicit points. Choose
**0 — linear** for a continuous change between points or **1 — hold** for an
immediate change at the next point.

These controls are ordinary typed effects in the existing schema3 project,
preset, revision-history, render-cache and backup contracts. No second project
format or automation service is introduced. Existing projects and role records
without these effects retain their original serialized bytes and fingerprints.

## Edit a curve

Open **Studio clips**, select the project, and choose the relevant clip, speaking
role, scene or master effects. Add `gain_envelope`, `pan_envelope` or
`width_envelope` from the available kinds. Edit **point count**, **interpolation**
and the active **time/value** pairs in the scrollable numeric form. Point 1 must
start at zero; active times must strictly increase.

The dialog previews numeric settings. **Apply effect** returns that reviewed
value to the ordered chain. The existing **Undo chain change**, **Redo chain
change**, bypass, move, preset and Cancel actions remain available. Applying the
chain saves a new project revision after checking that the current project and
library still match the reviewed selection. Rendering or previewing audio is a
separate explicit action.

| Curve | Values | Supported Studio scope |
| --- | --- | --- |
| Gain | -120 to +24 dB | Clip, speaking role, scene or master |
| Pan | -1 left, 0 centre, +1 right | Mono clip or speaking role with fixed pan zero |
| Stereo width | 0 mono, 1 original stereo, 2 wider side signal | Stereo scene or master bus |

A pan curve replaces the fixed mono placement for that track and uses the
existing equal-power law. It does not collapse a mixed stereo scene or master.
A width curve scales the side signal while retaining the mid signal; width zero
therefore makes the two output channels equal. Wider settings can increase
peaks, so the existing limiter and measured output checks still apply.

The ordinary Convert dialog offers gain curves. It can accept mixed-channel
batches and therefore does not offer the spatial curves. The lower-level typed
Convert API permits explicit pan only from an actual mono source to stereo
output, and width only from an actual stereo source to stereo output. Incompatible
source/output layouts are rejected before rendering.

## Timing and preserved audio

Point times are seconds relative to the selected processing scope. For a clip,
zero follows its source trim. A speaking-role curve begins again for each
rendered line. Scene and master curves begin at their corresponding bus start.
The final point's value is held. Points beyond the end of the selected audio do
not extend its duration.

Times are quantized to the selected output sample rate. Two distinct times that
collapse to the same sample are rejected. The compiler evaluates the curve per
sample and re-establishes the selected sample rate immediately before each
envelope. This also handles a preceding dynamic loudness stage whose internal
working rate differs. Linear gain interpolates in decibels; pan and width
interpolate in their displayed numeric units.

Every control is checked even when bypassed or beyond the selected point count.
Only fixed operators and bounded finite numbers enter the filter graph; saved
expressions, filter text, commands and executable paths are not accepted.
There can be at most one enabled envelope of each kind in a processing chain.
Existing duration, staging, resource, cancellation and no-overwrite publication
boundaries remain in effect.

Original media and source text remain unchanged. Accepted render nodes include
the complete effect controls in their existing cache identities. Editing a master
curve reuses unaffected scene audio; emitted project/timeline metadata records
the exact curve. A restored job retains the curve but requires fresh output
authorization under the existing restore contract.

## Qualification

The source change includes independent sample measurements of linear and held
gain, channel-isolated pan, mid/side width, loudness-followed-by-envelope timing,
Convert layout refusals, exact sample counts and unchanged inputs. Separate
project tests cover the old wire formats, scope rejection, revision conflicts,
receipt binding, passive backup and actual reuse of unchanged scene nodes.

The installed native Studio test adds real scrollable numeric controls,
Undo/Redo, persisted points and measured rendered PCM through the existing desktop
worker. Its source is checked before and after execution. **These checks are
pending execution for this candidate.** Their presence is not a passed result.

The curves do not claim arbitrary automation of every DSP parameter, real-time
mixer control, physical-device listening quality or manual NVDA acceptance.

## Primary implementation references

- [FFmpeg aeval filter](https://ffmpeg.org/ffmpeg-filters.html#aeval): sample counter,
  channel-preserving expressions and explicit channel layouts.
- [FFmpeg loudnorm filter](https://ffmpeg.org/ffmpeg-filters.html#loudnorm): dynamic
  normalization and its internal upsampling behavior.
- [FFmpeg expression evaluation](https://ffmpeg.org/ffmpeg-utils.html#Expression-Evaluation):
  the fixed mathematical operators emitted by the typed compiler.
