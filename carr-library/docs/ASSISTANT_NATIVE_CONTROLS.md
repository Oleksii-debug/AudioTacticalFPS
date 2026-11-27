# Typed native reader and player commands

## Current implementation and qualification boundary

Two additive assistant tools, `reader.control` and `player.control`, operate on
one actually open source-bound live-reader window and its existing prepared
utterance. They preserve the nine original assistant tools and the existing plan
and job schema. The root assistant integration owns argument dispatch, durable
intent publication and the refusal to replay an unfinished interactive action.

This component is source-reviewed and not executed in the authoring environment.
The required oracle lists 64 source cases, the same 64 isolated installed cases,
and 26 actual Windows native checks. None is a reported PASS until the composed
candidate runs them. Native speaker quality, physical output, NVDA and actual
native pause/resume/seek remain NOT_RUN in this dedicated gate.

## Reachable native workflow

The live-reader window has a named **Assistant for this reader/player…** action.
It opens a command picker for currently available operations, then opens the
existing assistant as a child of that same modal reader. The command draft
contains the selected target identity; it does not execute automatically.
Preview and Run remain separate explicit actions.

The regular desktop assistant has no invented active reader. A typed native
command without its bound bridge is unavailable. Closed windows, changed source
locations, changed workspaces, replaced library owners and different prepared
PCM cannot silently borrow an earlier preview.

The picker lists only operations that are currently applicable. Select an exact
range and a local voice in the reader before preparing or reading. Stop the
current reading session before changing its source selection. To control a
prepared utterance, capture a new command while that exact PCM is retained.
A new or later utterance requires another captured, reviewed target.

## Exact argument contract

Both tools require `target` and `action`. The target is a JSON object:

```json
{
  "schema": "carr-native-reader-target/1",
  "session_id": "32 hexadecimal characters for this open window",
  "location": {
    "workspace": "exact workspace",
    "edition": "exact edition",
    "revision": "exact source revision",
    "kind": "logical_segment",
    "index": 1,
    "text_sha256": "64 hexadecimal characters",
    "asset_sha256": "64 hexadecimal characters"
  },
  "source_chars": 100
}
```

The session token is ephemeral. Saved JSON never reconnects it to another
window. The complete source location and current source hash must match the
actually open reader, and the workspace must equal the currently approved scope.
The target carries no executable or media path.

| Tool/action | Additional fields | Observed completion |
| --- | --- | --- |
| reader.control / select | start, end | Exact native source selection/cursor applied |
| reader.control / prepare | start, end, settings | First actual bounded utterance prepared; no playback |
| reader.control / read | start, end, settings | Actual playback start observed |
| reader.control / stop | None | Reader work settled and prepared PCM released |
| player.control / play | pcm | Actual playback start observed for that prepared utterance |
| player.control / pause | pcm | Native paused state observed |
| player.control / resume | pcm | Native playback resumed |
| player.control / seek | pcm, seconds | Native position observed within 0.25 seconds |
| player.control / stop | pcm | Owned reader/player settled and prepared PCM released |

Start/end are exact Python source-character offsets. The existing native mapping
handles UTF-16, emoji and CRLF. A selection may be collapsed for a cursor command;
speech needs nonempty readable text. Preparation retains existing first-utterance
punctuation/whitespace boundaries and never claims to synthesize the whole
selection at once.

Settings are an exact snapshot with fields `voice`, `language`, `rate`,
`pitch`, `volume`, `playback_tempo` and `playback_pitch`. The first five
retain the existing SpeechSettings contract; independent tempo/pitch retain R8's
PlaybackSettings contract. A changed selected voice or setting refuses execution
before a new native action. No settings are inferred from source text.

A PCM binding contains exactly `sha256`, `frames`, `sample_rate`, `start`
and `end`. It identifies the current prepared utterance, not an arbitrary file
or a playlist discovered by the assistant. Player controls require the current
native Windows player; an unavailable device/backend does not select a PATH
fallback.

## Existing owners and timing

The bridge marshals every native control, source selection and player call to
the existing UI thread. The assistant waits on its worker. Synthesis continues
through the live reader's existing worker, SpeechOperation, resource broker,
audio admission, exact source guard and immutable PCM owner. The bridge does not
add a second synthesizer, DSP path, player, media thread or permission grant.

Prepare waits for the real owner to produce validated PCM. Play/read waits for
an actual native start observation. The receipt effect is
`playback_started`; it does not mean the selected range or whole book has
finished. Subsequent reading remains owned by the existing reader. Completed
playback is not stopped merely because the assistant dialog closes.

Cancellation before UI dispatch abandons the queued callback. Cancellation
during an owned asynchronous command asks the same reader to stop and waits for
its existing cleanup. Native errors cannot become success receipts. A late
callback cannot act on a closed bridge, replaced session or changed source.
The original reader close/cursor/cleanup behavior remains in force.

## Durable one-time dispatch and receipts

Before execution, the service validates the command and current bridge.
It then persists the deterministic dispatch intent
`{"interactive_dispatch": operation_id}`, using UUID5 of the existing
`carr-assistant:<job_id>:<step_index>` identity. Only then may the bridge
dispatch the native action. The in-process bridge also refuses a duplicate
operation ID.

A crash after that durable intent can leave uncertainty about a native action
that already happened. Resume therefore refuses automatic replay and requires
a new reviewed plan. A completed result is an inert observation. Loading it from
a backup does not reopen a window, restore playback, activate a voice or grant
another action.

The exact result shape is:

```text
{
  artifacts: [],
  interactive_receipt: {
    schema: "carr-interactive-receipt/1",
    operation_id, tool, action, target, effect,
    observation: { selection, pcm, playback, settings }
  }
}
```

Selection is a two-integer source range. PCM and settings are either the exact
typed objects described above or null when that action does not retain them.
Playback is null or a bounded observation containing `state`,
`position_frames`, `total_frames`, `sample_rate` and
`position_accuracy`. The saved-result validator checks exact command/intent
identity, scope, PCM metrics, applied selection, settings and action-specific
outcome. It repeats the same 0.25-second seek bound as the live observation.

The pure validators import no runtime, model or UI and open no saved paths.
They preserve completed receipts in unified backup. Restored jobs retain their
old library binding and cannot reuse it as current native authority.

## Verification inventory

`scripts/assistant_interactive_acceptance_oracle.json` contains exact expanded
function counts, native labels, required status fields and module-origin paths.

| Selector | Cases |
| --- | ---: |
| tests/assistant/test_interactive_tools.py | 42 |
| tests/ui/test_assistant_interactive.py | 22 |
| Source total | 64 |
| Isolated installed total | 64 |
| Actual Windows native checks | 26 |

Installed tests need the explicitly synthetic helper
`tests/assistant/interactive_fixture.py`. Its player/control state is a test
double and does not count as hardware execution. It exercises real cross-thread
dispatch, abandoned callbacks, current-owner checks, settings/PCM drift,
cancellation settlement, duplicate IDs and pure receipt integrity.

`scripts/windows_assistant_interactive_smoke.py <fresh-work> --native` opens
real native reader and child assistant dialogs, uses an actual installed SAPI
voice, prepares and independently checks PCM bytes/frames, executes a real
owned stop/release and exact emoji selection, and verifies the resulting three
assistant jobs through unified backup/restore. It reports seven actual product
module origins and SHA256 values. The wrapper must require installed-prefix
origins matching the current candidate source and the exact 26-label Counter.

The earlier live-reader native probe keeps its original ten-action verification
as an explicit required subset now that this additional action exists. Its
26/32 existing labels remain unchanged; the new assistant action has its own
separate qualification here. Earlier frozen acceptance artifacts do not qualify
the added command bridge.
