# Local studio effects and output formats

The existing typed role-effect chain exposes 21 effect types through
**Role and master effect chains** on the studio Script page. Choose **Structured production
script**, select a role or **Master: entire programme** and edit its ordered steps. Add, edit, move, bypass and
remove operate on an isolated pending chain. **Apply chain changes** makes one
ordinary narration-draft edit; **Cancel chain changes** leaves the script alone.
The original source, spoken words, other roles, scene directions and normal
undo/recovery history are retained. Each role and the master has at most 16 effects.

Unchanged effect directives retain their spelling even when moved. New or edited
directives use the same quoted-role and bounded-numeric grammar as the parser.
Editing a legacy step without changing an omitted control keeps that control
omitted, so the existing default filter graph does not silently change. Bypass
is saved explicitly as `enabled=false`; parameters remain validated and available
when the step is enabled again. The selected voice, language, source draft and
project identity are rechecked before applying the modal result.

The same bounded model validates saved projects, native controls and script
directives. No text can become a shell command, media URL or arbitrary FFmpeg
filter graph. The Script and keyboard help dialog lists the actual ranges,
defaults and units from that model.

## Effects

- Existing highpass, lowpass, equalizer, gate, compressor, delay and denoise
  retain their previous filter graphs when new optional controls are absent.
- Gain, low/high shelves, notch, limiter, declick, declip, deesser, four-reflection
  reverb, chorus, tempo, pitch, stereo width and target loudness are added.
- Gate exposes ratio and reduction; compressor exposes knee, makeup gain and
  mix; high/low pass expose Q; denoise exposes the floor; delay exposes mix.
- Pitch uses the actual PCM input sample rate, including when output resampling
  selects another rate. Its tempo compensation preserves duration. Tempo
  changes duration without intentionally transposing pitch.
- Width operates after the role's stereo placement. When enabled, the declared
  effect chain processes that stereo signal. Existing chains without width
  keep their former mono-effects-then-pan order.
- Reverb is four bounded reflections. It is not convolution or a calibrated
  room/RT60 model. Denoise/declip/declick do not guarantee restoration of missing
  information. No speech, music or voice quality is inferred from signal tests.
- The final existing safety limiter remains active. Choose a target loudness
  and inspect the measured output; a requested normalization target is not an
  independent listening or broadcast-compliance certificate.

Examples:

    [effect Narrator notch frequency=50 q=10]
    [effect Narrator compressor threshold_db=-20 ratio=3 knee=2.8 makeup_db=2 mix=0.8]
    [effect Narrator pitch semitones=2]
    [effect Narrator tempo factor=1.1]
    [effect Narrator gain gain_db=-3 enabled=false]
    [effect Narrator loudness target_lufs=-18 peak_db=-1 range_lu=7]


## Reusable named presets

**Save named preset** writes the selected pending chain to an explicitly chosen
JSON file. Enter a visible name of up to 120 characters. Saving a preset is a
separate action and is not undone by subsequently cancelling the chain dialog.
The existing export approval binds the selected file and parent directory;
another writer's new bytes cannot acquire replacement permission from an older
confirmation. The private library, current Studio project, recovery file and
imported original source remain protected destinations.

**Load preset** reads at most 64 KiB into an inert typed preview. Review the name,
processing order, numeric settings and bypass states, select a role or master, then choose
**Apply loaded preset**. This replaces that target's pending chain only. An empty
preset explicitly removes its effects. The final **Apply chain
changes** is still required to change the draft.

The `carr-role-effect-preset/1` document has exactly `schema`, `name`
and `effects`. Every effect has exactly `kind`, `parameters` and
`enabled`, with the existing maximum of 16 steps and numeric ranges.
Duplicate JSON fields, unknown keys, invalid booleans, nonfinite values and
oversize files fail validation. Presets carry no voice, model, executable,
credential or source path. They are reusable external exports, not an automatic
preset catalogue or an active runtime profile. Complete project backup preserves
the applied chain through the existing Studio draft schema.

The preset format stays unchanged and contains only the chain, so a preset can
be applied to either target. The master's dry/wet setting is a separate project
control and is not imported from a preset. Master restrictions are checked on
every change, including preset application and enabling a bypassed effect.

## Master chain and dry/wet blend

The master processes the complete stereo programme once, after chapter mixing
and assembly. This lets a delay or reflection cross an interior chapter boundary
without restarting the effect for each exported chapter. The final programme
length remains unchanged; effect tails are trimmed at that last frame.

Select **Master: entire programme** to enable the named dry/wet control. Its
finite value is between zero and one. The output is `(1 - mix) × dry + mix × wet`.
Zero uses the unprocessed programme and one uses the complete ordered chain.
Mixing does not normalize the two gains. The existing 0.95 safety limiter, with
automatic level disabled, limits excess peaks. An explicitly chosen loudness
effect can still change the processed signal's level by design.

Tempo and pitch remain role effects, applied before the programme's measured
timeline is built. Active tempo or pitch steps on the master are rejected;
bypassed steps may be retained as inert preset data. Other effects are validated
by the same bounded model and fixed FFmpeg filters as role processing. The master
keeps the declared stereo layout and project rate.

New scripts use these directives before the first timeline event:

    [master mix=0.5]
    [master-effect compressor threshold_db=-20 ratio=3]
    [master-effect gain gain_db=-3 enabled=false]
    # Chapter one
    Narrator: Exact source text.

Master directives opt into production schema 3, with explicit `master_effects`
and `master_mix` fields. Existing version-1 and version-2 serialization and cache
fingerprints remain unchanged. Literal narration continues to treat these words
as source text, never as instructions.

The renderer seals the assembled and processed master as separate cache steps.
Changing only the master chain or blend reuses verified speech, music and chapter
mixes. The cache key also binds the FFmpeg identity and master algorithm revision.
Publication still follows the existing project lock, resource reservation,
whole-child-family cleanup and no-overwrite transaction.

Every chapter WAV is an exact consecutive PCM slice of the **processed** master;
joining those PCM payloads reproduces the master byte for byte. Subtitles and
chapter offsets retain their measured frame positions. Stem WAVs remain suitable
for remixing before the master chain and are labelled **pre-master** in the
completion receipt. Isolated clip preview deliberately excludes programme
mastering. EPUB/DAISY publication validates schema-3 timelines and uses the same
verified full master. Complete backup retains the typed plan, source draft,
master cache and outputs through the existing managed-project adapter.

## Outputs

WAV, FLAC, MP3, Opus, M4A, M4B and AIFF remain supported. Ogg Vorbis, raw AAC
(ADTS) and WMA (ASF) use the same sealed local FFmpeg encoder and no-overwrite
publication. WMA and Opus encode at 48,000 Hz; the WAV master/timeline retain
the selected project rate, and the plan preview states this distinction.
Container support for metadata/chapters varies; the production
manifest and subtitle outputs remain the detailed provenance record.

## Validation boundary

The new strict suite runs real synthetic PCM through the selected FFmpeg,
measures tempo/pitch/notch/width/loudness/limiting, checks all added effect
defaults, and encodes then decodes the three added formats. Source and installed
Windows gates must pass before treating this extension as verified for the
delivered runtime. The existing native Windows/NVDA and listening gates remain
separate. No supplied private model, recording or source is included.


The chain extension adds source-preservation, stale-modal, preset-file authority,
draft undo/replay and complete-backup roundtrip tests. The selected FFmpeg signal
test measures an actual order-dependent result and compares a bypassed chain
with dry audio. The installed Windows probe drives real add/edit/reorder/bypass/
remove/apply/cancel controls and checks a typed preset file roundtrip. Its file
picker automation, NVDA speech and listening remain separately labelled.
The role-chain suites were subsequently executed on exact source `947193c`:
95 passed with no skips, including actual Linux FFmpeg signal checks. Master
tests add historical v1/v2 hashes, explicit v3 validation, source-preserving
edits, master restrictions, exact PCM blend ratios and stereo placement, very
short programmes, order/bypass, cross-chapter delay, exact chapter partitions,
cancellation/resume, full backup/restore and synchronized EPUB publication.

The extended native probe now also selects the actual master control, refuses
nonfinite mix without closing the dialog, changes mix/order/bypass and verifies
the accepted typed plan. Native Windows controls, the sealed Windows FULL FFmpeg
runtime, NVDA and listening remain separate acceptance evidence; the Linux
synthetic tests do not claim those results.
