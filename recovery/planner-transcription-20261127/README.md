# Canonical local planner transcription contract

This bounded change targets canonical `e69ba2403515f21509686d66f4bc8b3673756d81` only. That source already registers, validates and dispatches `audio.transcribe`, but the existing local-planner SYSTEM omits it. PR143 source `4fae6fa69daf86fc9566f8847d74bccc8a4dc5a5` lacks that registry/dispatch contract and must not receive the prompt alone.

The production change only describes the existing transcription arguments, configured-worker requirement and selected output directory. Registry, plan validation, selected scope, profile revalidation, approvals, adapter and execution dispatch are unchanged. The append-only regressions capture the actual planning request, compare it with the registered contracts, validate the resulting proposal without executing it, and retain rejection of input/output/scope/model-option/unconfigured-worker violations.

The finite workflow first restores the exact old SYSTEM and requires the new positive regression to fail specifically for the missing registered tool. It then restores the exact candidate and runs the complete existing supplied-transcription, local-planning and loopback-planning suites with the six new cases. Source inventories and unchanged authority modules are checked before and after execution. A failed or queued run is not acceptance.

The fixtures are deliberately synthetic and local. No real engine/model, installed application, physical audio or human NVDA qualification is implied. This is a narrow public-source candidate and does not finish the unavailable full application composition.
