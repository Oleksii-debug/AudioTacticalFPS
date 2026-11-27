# Chapter audio clips in the existing Studio

The controls adapt the canonical PR152 clip-editing contract to the existing typed production plan, script draft, renderer, cache and player. No alternate project database or speech engine is introduced.

## Editing

Select Structured production script explicitly, choose the project's audio folder, then choose Edit chapter clips in Production. The native dialog provides chapter and clip selection, a named track, start in the chapter, source trim, optional selected duration, gain in dB, stereo balance and fades. Add local audio retains an independent PCM16 WAV or RF64 copy under audio/assets. Files up to 8 GiB are accepted within the existing project disk budget and free-space checks. Compressed audio is not silently converted by this editor.

Each action settles through the Studio's existing cancellable worker. Controls return a captured edit request; a stale script, settings or output folder refuses the result. The existing draft applies accepted changes, including Undo, Save, recovery and Save All. Remove keeps the original retained asset; removing a last clip preserves a one-second empty chapter. Move keeps the clip's stable identity. Preview uses saved controls and omits chapter delay, then plays through the existing selected-output player. No preview begins automatically.

## Script and compatibility

A clip directive is explicit, for example:

    [clip rain.wav id=rain-bed start=0 trim_start=1 duration=20 gain_db=-18 track=ambience fade_in=1 fade_out=2]
    [duration 25]

The four tracks are sfx, ambience, music and microphone. At most 32 clips may overlap per chapter. Clip start never advances the sequential spoken-event cursor. The chapter lasts through the last spoken event, clip end or chosen minimum duration. Fades must fit without overlap. Mono position uses constant-power panning; stereo balance preserves both original channels at center and attenuates the opposite side toward either extreme. Background music/ambience duck under spoken events, sound effects and microphone. The final mix is limited before PCM16 publication. Separate clip-track stems are dry with respect to ducking.

Old production version 1 serialization and cache fingerprints remain unchanged. Clip/duration scripts select version 2; the version 2 measured timeline records the retained source hash and exact trim, start, duration and fade samples. Literal narration continues speaking bracketed text. It never opts into clip instructions.

Source copies, DSP, cache sealing, final output and backup use existing checked local paths, project locks, streaming hashes and no-overwrite publication. Preview reserves the shared resource coordinator before processing. Full render includes clip spans and track stems in its resource/disk declaration. Resume validates independent checkpoints. EPUB/DAISY export validates the version 2 clip timeline and still aligns only measured spoken lines; background-only intervals remain explicitly untranscribed.

## Acceptance boundary

New strict tests cover version 1 compatibility, version 2 validation, retained sources, stale drafts, cancellation, backup/restore/rebackup and actual FFmpeg trim/pan/gain/fade/position/ducking/cache behavior. The native probe drives real wx track/parameter/edit/move/remove/preview-request controls and verifies cancellation. These sources are prepared during an executor outage: their execution is NOT_RUN until the integrated exact-head gates complete. Native file-picker interaction, manual NVDA review and playback-device listening remain separate acceptance tasks. This is bounded chapter clip editing, not a claim that every professional multitrack workstation feature is implemented.
