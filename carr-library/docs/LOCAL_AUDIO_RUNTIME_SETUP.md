# Local Windows audio runtime setup

The transferable Resonance application does not contain FFmpeg. The canonical
delivery guard rejects both supported audio payload layouts, including private
archives, because this project has no reviewed runtime redistribution clearance.
The application can still explicitly install and use a verified local runtime.

## Native workflow

Open **Audio Studio → Local audio runtime setup**.

- **Download and install reviewed runtime** displays the exact HTTPS source,
  archive size, SHA256 and license. The confirmation defaults to No. Accepting
  starts only the pinned public FFmpeg archive acquisition; it does not acquire
  voices, neural music, transcription models, or other packages.
- **Verify and select local source** accepts the exact already downloaded ZIP,
  or a complete runtime prepared by the same reviewed setup implementation.
  Select the source type and use the native Browse control.
- **Verify current registration** checks the complete inventory and native
  executable versions again before activating the selected runtime for this
  library.

Large file hashes, extraction and version commands run in the existing
application worker. Shared progress remains queryable with **Ctrl+Alt+G**.
**Cancel current setup** requests cancellation; closing the setup window also
requests it and rejects late activation. An in-progress network read can take
up to its 10-second timeout to return. A registration already atomically saved
remains saved if cancellation arrives afterward; its readable result asks for
verification before use. A complete unselected installation can remain if a
later configuration check fails; an incomplete reserved publication is removed.

The managed installation is outside the application bundle and library state.
The exact FFmpeg 9.1 full archive is 264,268,459 bytes, SHA256
`f9a83be8f97b8989a223a15e4af234accd0842bacf7c126af5a3aae759785e48`.
Its original notices and all 45 publisher files stay intact. Setup retains the
reviewed source lock and does not change any transfer or distribution policy.

## Selection and recovery

The existing `state/audio/engines.json` stores the FFmpeg and FFprobe paths
plus optional `audio_runtime_manifest_sha256`. A selected complete pack must
keep that exact manifest and its original source-locked files. A missing or
changed manifest cannot be treated as an unsealed executable registration or
silently replaced with a PATH/bundled executable.

An explicit complete-pack selection takes priority over an optional existing
bundled runtime. Without a complete-pack selection, the existing reviewed
compact/full bundled discovery remains available. Preparation does not change
PATH; one library binding owns activation for its GUI or audio execution
lifetime. Library switches prepare verification in the shared worker and reject
stale or cancelled completions before replacing the current owner.

Backup restore preserves the declared paths and manifest digest while marking
registrations as requiring review. Use **Scene voice and music engines →
Review and revalidate local registrations** before activation. Structural backup
validation accepts foreign absolute Windows paths without resolving them.
Explicit individual executable choices and Reset remove the complete-pack seal;
they do not reclassify a failed sealed registration automatically. After such a
change, Verify current registration applies the new selection to this session.

Saved transcript inspection, edits and export, scene project/job inspection,
assistant previews, library backups and search do not require audio activation.
Actual scene rendering/resume and transcription execution bind the selected
runtime for their operation. Assistant production callbacks bind it only when
the typed plan actually invokes audio production.

## One preparation implementation

The installed `carr_library.audio.windows_setup` module is the sole
complete-pack producer. The packaging command retains the existing offline ZIP
and explicit-download interface and delegates to it. The earlier compact
compatibility producer remains distinct. The installed lock and notice bytes
are tested against their packaging originals; neither is a new runtime grant.

Production calls use `prepare_local_audio_runtime(root)` off the window
thread, then `binding.activate(previous=old_binding, strict=False)` after
the caller's live-scope/cancellation guard. Audio-executing headless paths use
`with open_local_audio_runtime(root, strict=True): ...`.
The frame retains `audio_runtime_binding` and closes it on shutdown.
Do not also hold the old outer audio bootstrap handle: bundle and selected
runtime preparation now share this one owner. The separate native CRT handle
is unchanged.

## Qualification

Portable tests use real small ZIPs, files, atomic settings, complete inventory
checks and controlled inert PE data. They are not publisher executable tests.
The installed Windows probe
`python -I -B scripts/windows_audio_runtime_setup_smoke.py --runtime <prepared-pack> --report <json>`
uses native menu controls and keyboard dispatch, the actual shared worker,
the reviewed installed pack, and its real fixed version commands. It declines
network consent, selects the existing pack, verifies its persisted digest, and
cancels an actual inventory check at a controlled observer boundary.

Native Windows execution, NVDA and human listening must be recorded from their
actual gates. Synthetic portable checks do not establish these outcomes.
