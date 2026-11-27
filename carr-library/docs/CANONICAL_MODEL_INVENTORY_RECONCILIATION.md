# Canonical model inventory convergence

Source behavior reused from canonical commits
4b770d12ac70a76b91c55eb5099a49f0c4334795 and
30d61484f1d072e3e826f59bbfb5cb30f345b4a8 (merged at
4b6f8169986c4a58b43e4a23bc081fbefadbbdcb).

The current branch retains LocalModelManifest and EngineProfileStore as its
single runtime admission authority. Its complete-inventory verification now
uses bounded scandir iteration so unreadable subdirectories and interrupted
directory iteration cannot silently disappear. Every directory entry counts,
including empty directories and files whose extension is not lowercase .py.
Special files are refused; cancellation retains its existing InterruptedError
contract. Declared and observed file inventories must match before asset hashes
or profile admission can succeed.

This adapts the canonical security behavior; it does not copy canonical
LocalModelConfig, speech/runtime, studio/render or another profile store.
Manifest schema, engine choices, acceptance flags and per-file hashes stay
under the existing authority.

The Windows approval-race fixture now intercepts the actual
_open_json_file descriptor boundary and atomically replaces the manifest while
the checked reader is open. It verifies that the original observed bytes cannot
be authenticated by the replacement's digest. Production identity/hash checks
are unchanged.

Required focused suites:

- tests/audio/test_model_inventory.py
- tests/audio/test_model_snapshot.py
- tests/audio/test_model_manifest.py
- tests/audio/test_engine_profiles.py

New source tests are NOT_RUN while the executor is unavailable. The preexisting
7f29 Windows audio run reported a stale Path.open interception in the snapshot
fixture; it did not exercise the required replacement on Windows. No model
inference, private engine, GPU or voice-quality qualification is claimed here.
