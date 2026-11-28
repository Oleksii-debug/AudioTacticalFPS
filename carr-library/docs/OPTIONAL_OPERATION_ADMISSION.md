# Optional operation admission

The private installation remains perpetual. It does not discover a licensing
server, keyring, business policy or update authority from documents, environment
variables, a model proposal or a saved project. With no explicitly supplied
`TrustController`, the new boundary performs no entitlement or network check.

An installation owner can pass one existing controller to `LibraryApp`, the
desktop or the existing CLI `main(..., trust_controller=...)` entry point. Its
`premium_features` mapping must explicitly bind one of these implemented
fresh-operation actions to a feature in its independently supplied signed
entitlement policy:

| Action | Current domain boundary |
| --- | --- |
| `audio.render` | Shared audio production, used by CLI, Studio and approved assistant audio tools |
| `transcription.run` | Existing workspace receipt/V5 service and library-wide hardened V43 service |
| `assistant.plan` | An explicitly approved local-model proposal, before the existing provider gateway call |

There is no default mapping. Unknown actions, protected user-data actions,
unknown features and mapping a premium action to a protected feature are
rejected. The controller uses the current entitlement boundary, signed store
and protected anchor; it does not create a second verifier or credential vault.

The domain captures an immutable authorization once before starting new work.
A configured grant expiring or being revoked later does not cancel that active
operation or disable its final save. Nested approved tool calls retain the same
authorization and cannot replace their selected controller. A later new
operation must pass a fresh decision. This is an admission rule, not a general
GUI, job-runner, document-access or resource-broker restriction.

The renderer first verifies an exact completed production and can return it
without fresh permission. A completed workspace transcript likewise verifies
and reopens its retained receipt without launching its worker. The V43
`completed`, reading-copy and passive export paths stay independent of new
execution. Running V43 again is a fresh execution; reopening its completed
receipt is not.

Reading, opening, editing, saving, exporting, backups, recovery, settings and
finishing active work never require this permission. Grant import remains an
explicit review of the exact signed bytes and matching terms. Automatic
refresh only runs when the trusted owner explicitly enabled its existing
provider; default startup does not contact a service.

## Verification

`tests/security/test_domain_admission.py` uses actual signed synthetic grants,
real local library operations, current CLI dispatch, the existing provider
ledger with a synthetic transport, and the existing protocol worker fixtures.
It reuses these already-required fixture modules, which must be retained in an
installed test staging tree:

- `tests/assistant/test_local_llm.py`
- `tests/transcription/test_supplied_worker_bridge.py`
- `tests/test_transcription_service.py`

`tests/security/test_audio_admission.py` requires the gate's existing local
FFmpeg and renders actual PCM through the shared domain, audio CLI, Studio
controller and assistant. Its deterministic PCM producer is not a real voice
or acoustic-quality qualification. Tests also expire permission between the
two actual scene renders and preserve the completed master.

These are source tests, not production issuer/service activation, signing,
payment, native screen-reader or model-quality evidence. A CI result must name
the exact executed source and installed artifact before claiming those tests
passed. The owner still supplies actual public roots, endpoints, terms,
entitlement policies and any distribution authorization.
