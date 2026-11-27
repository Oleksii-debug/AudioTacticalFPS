# Local voice preferences

Open **Voice → Voice reader and reading copy**, wait for installed voices, and
choose **Voice preferences**. This uses the reader's existing local registry.
Opening the window never plays audio or downloads a model.

Choose the exact language and an installed voice. **Save default for language**
stores that identity for the selected language. **Use language default** applies
it to the current reader, keeping the displayed rate and volume. Language,
script and region are matched exactly: an `en-US` voice is not silently selected
for `en-GB`, and a saved voice which is unavailable is not replaced by another.
The source text is not used to guess its language. An explicit current reader
choice or recovered draft keeps its own voice; reloading preserves that choice
if it remains available and otherwise leaves the selection empty.

**Add or remove favorite** marks a voice in this window. Unavailable saved
favorites remain visible and can be removed. The voice's name, declared language
and engine come from actual local enumeration, not from the preference file.

A named **voice profile** saves an exact voice, language, synthesis rate and
volume. It is a reading preset, not a cloned voice or a new installed model.
Selecting a profile displays its stored controls; **Use voice profile** applies
them without starting speech. An unsupported non-default control is refused.
Replacing a differently configured existing profile asks for explicit approval.
Saved Studio and Book plans keep their reviewed identities and controls.

Preferences belong to the current library and live in its existing
`state/audio/engines.json`. Older files remain readable without an automatic
rewrite. Saving uses a shared lock and checks the prior preference value;
another window's change produces a visible reload request rather than an
overwrite. Engine configuration changes preserve the latest voice preferences.
Full-library backup retains their exact bytes as passive settings. Restore does
not install, inspect or activate a missing voice engine.

Queued actions recheck the owning dialog and active library before saving. Close
or a library change prevents a queued action from writing to an unrelated
library. The reader retains its ordinary quiet-mode, cancellation and worker
cleanup behavior. Preferences never authorize external executables, source
edits, quotation approval or model redistribution.

## Verification

Portable checks cover real settings load/save, competing windows, unchanged
legacy bytes, exact script/region selection, unavailable voices, unsupported
controls, real backup/restore and native-handler lifetime over a serial worker.
`scripts/windows_voice_preferences_smoke.py` exercises actual Windows controls
and keyboard activation; `--installed` additionally requires installed module
origins. Its synthetic voice declarations test selection and persistence, not
speech quality. NVDA, listening and each actual voice/model's language quality
remain separate acceptance evidence.
