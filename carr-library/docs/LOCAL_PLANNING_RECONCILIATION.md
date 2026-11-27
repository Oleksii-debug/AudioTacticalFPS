# Reviewed local planning through the existing assistant

The native Local assistant and the headless assistant command share the same
typed plans, scope validator, durable jobs, provider profiles, request ledger,
and audio renderer. This change adapts the useful local-planning behavior from
PR147 (abb5de8d407df84caaf63cf8d8fbf97e55e7315a); it does not introduce its separate
provider store, credential system, or Studio service.

## Proposal, preview, and execution

In the native assistant, choose the current library and permitted folders first.
Guided commands and typed JSON do not contact a language model. Preview runs
validation and local voice discovery on the dialog's existing worker so a cold
engine cannot freeze those controls. Final selected-voice validation and Run's
durable plan submission also use that worker; Run still binds the exact
previously reviewed snapshot.

For a model proposal, explicitly select a saved local OpenAI-compatible chat
profile and confirm the selected endpoint and model. The existing provider
gateway permits one loopback request with zero remote spending. Its payload is
the goal and fixed planning instructions. It does not attach the chosen folders,
catalog, books, or document contents.

The response has no execution authority. Strict JSON parsing rejects duplicate
keys, nonfinite values, unknown fields, supplied permissions, unknown tools, and
operations outside the selected scope. Empty steps are a clarification to
review. A successful response becomes an editable draft. A separate Preview
must succeed before Run can authorize it.

The selected saved profile, current library directory and request-ledger
identity are checked before and after the request and after validation. A
removed or changed profile invalidates the proposal. Goal, folder, permission,
budget, library, or revision changes discard a pending result. Run compares the
current controls with the exact reviewed snapshot, including changes that did
not emit a text event. Closing or cancelling the dialog cannot approve a late
response.

The assistant's audio bridge discovers voices from the same saved local engine
registrations as Studio. It reuses existing per-call manifest admission and
cooperative cancellation, and still accepts existing injected registries.
The default renderer also receives the existing library ResourceBroker and a
stable assistant:<operation-id> lease identity, plus the same registered music
manifest resolver used by Studio. AssistantService does not hold a second
resource lease around that call. Explicitly injected legacy renderers keep their
original callback contract. This small integration depends on the composed
operations package; no second coordinator is introduced.
No new recognition, synthesis, rendering, persistence, or model-download stack
is added.

## Headless use

The installed research-library command, or python -m carr_library, accepts
the assistant subcommand:

~~~text
research-library assistant --data-dir C:\Library --list-local-profiles

research-library assistant --data-dir C:\Library --instructions goal.txt --allow-create --local-profile LOCAL_ID > proposed-plan.json

research-library assistant --data-dir C:\Library --instructions proposed-plan.json --allow-create

research-library assistant --data-dir C:\Library --instructions proposed-plan.json --allow-create --run

research-library assistant --data-dir C:\Library --jobs
research-library assistant --data-dir C:\Library --resume JOB_ID
research-library assistant --data-dir C:\Library --resume JOB_ID --run
~~~

A model request and execution cannot be combined in one invocation.
The reviewed JSON includes its exact scope: the later invocation must select
matching permissions. Use --workspace, --input-root (repeatable), --output-root,
--allow-create and --allow-full-backup to grant only the intended authority.
The optional --max-bytes, --max-files, --max-steps and --max-seconds values
become part of that immutable scope. Resume retains the original scope and
rejects replacement permissions or budgets. Listing and inspecting jobs never
resumes them.

Instruction files are read through the existing verified reader with a byte
budget. SIGINT requests cooperative cancellation. Explicit execution returns
zero only for complete jobs, 130 for paused/cancelled jobs, and a nonzero status
for failure. Saved checkpoints remain inspectable even after cancellation.
An unavailable remembered library directory is not silently replaced by a new
empty library; use --data-dir to select a current location.

## Evidence boundary and required gates

The new source tests cover real gateway/ledger behavior, a synthetic HTTP server
bound to 127.0.0.1, CLI proposal followed by separately approved execution,
profile mutation, cancellation, and event-handler scheduling with wx doubles.
The local HTTP suite requires the explicit loopback test-runner mode; all other
suites retain the default network denial.

Source suites:

- tests/assistant/test_local_llm.py
- tests/loopback/test_local_planning.py
- tests/assistant/test_planning_handlers.py
- tests/assistant/test_planning_runtime.py
- tests/assistant/test_dialog_lifecycle.py
- tests/assistant/test_local_assistant.py
- tests/assistant/test_production_bridge.py

scripts/windows_audio_smoke.py waits for the actual asynchronous preview before
performing its existing native Run and completed-output assertions. Its SAPI,
FFmpeg, and scoped production checks remain intact.

At this checkpoint the executor is unavailable: these new changes and test
cases are NOT_RUN. A synthetic proposal, a mocked voice, or a source review is
not evidence of an installed model's language quality, persistent multi-request
engine session, GPU performance, or a native accessibility audit.
