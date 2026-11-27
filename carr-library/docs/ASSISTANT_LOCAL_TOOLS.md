# Current-source assistant tools and reviewed local conversion

This implements the typed reader, saved-evidence, voice, conversion, effects and current-player surface of V3-09. These tools use the same application services, scoped plan approval, job journal, worker queue and resource broker as the existing assistant.

## Available actions

| Tool | Reviewed input and resulting behavior |
| --- | --- |
| `reader.read` | Exact edition, revision and source positions; returns authenticated source metadata and an anchor. Reading does not approve a quotation. |
| `evidence.search` | Scoped saved-evidence query with bounded fields, tags and result count. |
| `evidence.read` | Exact topic and saved-evidence identity; reuses its existing independent authentication. |
| `evidence.export` | Exact saved-evidence identities, format and output name; publishes without replacing an existing file. Researcher notes stay labelled separately. |
| `voice.list` | Actually configured local voices and capability notices, with an optional language filter and result limit. |
| `audio.effects` | Typed effect controls plus actual selected-runtime capabilities. |
| `audio.inspect` | A selected input path; returns measured audio properties and the exact source hash. |
| `audio.convert` | Explicit input paths and reviewed SHA-256 values, bounded typed settings and a selected new output folder. |
| `audio.render` | Alias for the existing reviewed production service; output receipts must match the produced files. |
| `player.status` | Status of the current source reader explicitly bound when its Assistant button was opened. |
| `player.control` | Pause or Stop for the exact reviewed reader session and source signature. |

The existing ten library, transcription and production tools remain available. Guided commands include `List voices`, `Audio effects`, `Inspect audio "path"`, `Search evidence "query"` and `Player status`, with their Ukrainian equivalents. Exact source ranges, evidence identities, conversion hashes/settings and player controls use the versioned JSON plan.

## Current reader handoff

Open **Assistant for this source...** from the current live reader. Its assistant is bound to that existing native reader owner, library, workspace and source. A general assistant without that owner cannot control a player.

The bridge creates no player, device, executor or polling timer. Source authentication runs on the existing assistant worker; native observation and control cross the existing UI queue. It rechecks owner, current scope, source and session before acknowledging a result. A paused reader is never toggled back into playback by an assistant Pause action. Stop is acknowledged only when the same reading future and retained native resources are quiet. A stale or withdrawn queued action cannot run later. A previous uncertain control attempt is never automatically replayed on resume.

Status reports source-time position and the existing reader state. It does not claim that a physical device, headphone route or human listening check passed.

## Conversion and recovery

Conversion reuses the selected AudioRuntime and its owned native-process runner. It validates the exact source bytes, source/output scope, typed effect settings, runtime identities and resource budget before execution. Files run serially under one bounded reservation; every completed item has a persisted candidate receipt. The final output is published without clobbering another file.

Explicit resume revalidates the selected plan and each saved receipt. It retains completed outputs after cancellation and recovers only a matching recorded publication; another file at an expected path is never adopted. Hashing is descriptor-bound and bounded by the captured byte count. Source, staged bytes and output authority are rechecked at publication.

If native cleanup remains uncertain, the same cleanup owner retains the exact resource admission, private temporary directory and conversion-state lock until quiescence is confirmed. An uncertain voice inventory likewise retains its resource admission until the actual registry finishes closing its providers.

## Qualification

The focused assistant-tools workflow runs the new tools alongside the existing plan approval, source reading, transcription, templates, DSP and keyboard regressions on Linux and Windows. It repeats the selected cases against the isolated installed package and compares each selected installed module with the exact checkout before and after execution. Required cases may not be skipped.

At this source checkpoint the new integrated slice is **NOT_RUN**. Runtime results must be recorded by exact commit and workflow/job identity. Controlled GUI and PCM peers are explicitly labelled in tests; they do not qualify physical playback, NVDA behavior, voice quality or a private model.
