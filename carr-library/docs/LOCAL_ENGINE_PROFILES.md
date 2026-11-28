# Local engine profiles

The current application reuses the canonical `state/audio/engines.json` location for a bounded schema-2 registry. Each registration pins one existing local-model manifest by its complete SHA-256 and retains a stable profile ID, display name, fixed supported engine ID and enabled state.

This registry is machine-local. Complete library backups and encrypted portable snapshots omit it. A project stores only a voice ID or a music profile reference; it cannot restore an executable path. Registry bytes copied to a different library location remain inactive. Legacy schema-1 executable declarations are shown as inactive and preserved byte-for-byte when the user explicitly registers a reviewed manifest.

## Register and use

Open **Local engine profiles** (also available from the transcription window), choose an already installed manifest, and wait for the complete file inventory check. Review the model, revision, license, checksum and file count. Save the reviewed registration under a distinct local name. This does not download anything or run recognition, synthesis or voice discovery.

A registered manifest must use the existing `LocalModelManifest` contract, declare `license_accepted: true` and `trusted_weights: true`, and cover every file in its separate installation directory. Installations must not overlap the library or its projects. Registration does not establish distribution rights or measured inference quality.

Supported adapter identities remain the existing ones: Piper, dordiai-voices, MusicGen, ACE-Step, FluidSynth and the supplied Whisper worker. This module adds preferences and admission checks; it does not introduce another inference engine.

For a registered Piper installation, `settings.executable` names the inventory-covered relative executable, alongside the adapter's existing model/config/language fields. The selected program and model therefore share the same reviewed inventory.

## Studio speech and music

After changing a speech registration, use **Reload local voices** in Studio. Installed system voices remain available independently. Registered voices use stable profile-qualified IDs, so two Piper or dordiai installations can coexist without ID collisions.

Before voice discovery, cache identity or synthesis, the profile is read again, its enabled state is checked, and the complete pinned installation is verified. Missing or changed assets make that profile unavailable. They do not select another voice.

For optional music, select a registered profile in the settings window and copy its displayed reference, such as:

```text
[music engine=fluidsynth model_manifest=profiles/0123456789abcdef0123456789abcdef.json mood=calm tempo=90]
```

The ID in this example is illustrative. Use the actual saved reference. Managed Studio resolves it through the local registry and rechecks it before generation. Old project-local executable declarations cannot become an active installation through reopening or restoring a project. Explicit low-level adapter integrations retain their existing manifest API.

## Transcription

**Pick saved runtime** selects an enabled supplied-Whisper profile. The window shows the selected manifest identity and uses its explicit model selector for a new request. Prepare and Start independently revalidate the captured local profile and its manifest before calling the existing supervisor. Prepared input/settings records and completion receipts keep their existing schemas.

A disabled or removed runtime prevents a new execution. Previously accepted transcript outputs remain available through the existing byte-verifying completion reader; reading a saved transcript does not require reactivating its runtime.

## Changes, cancellation and recovery

Disabling and removing a registration never delete installed assets or library content. A changed manifest requires explicit review and a new registration identity; older registrations for that same path become disabled. Registration and enable operations are cancellable, use a nonblocking settings lock, and publish state atomically. Profile listing does not execute or hash external engines, so disconnected installations do not hide other saved registrations.

The source tests cover schema and path boundaries, legacy preservation, offline admission, content changes with unchanged size/mtime, cancellation, concurrent writers, duplicate names, active-profile removal, voice identity mapping and portable backup exclusion. Native control execution and real optional-engine inference are separate acceptance gates; their results must be reported for the exact tested build.
