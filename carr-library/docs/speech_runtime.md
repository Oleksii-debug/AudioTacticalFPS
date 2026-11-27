# Local speech and reversible reading copies

This is one component of Scope V3. The main application installs its native
**Voice** menu with `install_speech_actions(frame)`. `Ctrl+Alt+V` opens the editable
reader; `Ctrl+Alt+R` narrates a selected book/chapter through the existing
`ReadingActions` and `ReadingDesk` authorization and original-source checks.

## Working interfaces

```python
from carr_library.speech import VoiceRegistry, SpeechSettings, ReadingDocument

voices = VoiceRegistry.discover()
for voice in voices.voices():
    print(voice.id, voice.name, voice.language, voice.capabilities)

result = voices.synthesize(
    "A local narration example.", selected_voice_id, new_wav_path,
    settings=SpeechSettings(rate=0, volume=80),
    cancelled=cancellation_event.is_set,
)
# result includes actual frames/rate/duration, SHA256, voice and source-text hash.
```

Discovery and rendering are explicit. Importing the module does not load a
model, enumerate voices, start audio or send a network request. A voice catalog
contains only entries actually enumerated by installed engines.

| Engine | Implemented adapter | Actual evidence in this development environment |
| --- | --- | --- |
| SAPI 5 | Default isolated native Python COM worker using pinned comtypes; installed tokens, native rate/volume/punctuation, real PCM file rendering. PowerShell compatibility hosts require explicit opt-in | Exact `2b951ff0` Windows V3 run rendered actual SAPI PCM and Studio scenes with two enumerated voices; manual NVDA/listening and other source/runtime identities remain separate |
| FFmpeg Flite | Actual runtime voice enumeration, CMU English voices, PCM rendering and gain; no invented native rate/pitch controls | FFmpeg 7.1.5 Debian, five discovered voices; three distinct voices rendered and measured |
| eSpeak/eSpeak NG | Installed CLI enumeration and rendering, native rate/volume/pitch/punctuation | Adapter implemented; executable unavailable in this environment |
| Piper | Explicit local executable and existing model/config, mandatory independent SHA256 and voice-license fields, no download-by-model-name | Hash/manifest boundaries tested; no neural weights downloaded or inference claimed |
| Local JSON helper | Explicit opt-in, executable SHA256, configurable catalog budget, JSON v1 enumerate/render contract | Contract boundary implemented; no claim of a particular vendor implementation |
| SAPI 4 | No available verified Windows 11 32-bit engine contract | Unavailable; no simulated voice or promised obsolete-driver compatibility |
| dordiai-voices | Existing reviewed exact-name adapter connects through the shared voice registry and saved desktop configuration | Synthetic process/protocol/configuration tests passed; real owner-runtime inference and multilingual/cloning quality remain pending. See `DORDIAI_DESKTOP.md` |

Piper support follows the inspected upstream CLI at
`OHF-Voice/piper1-gpl` revision `6aacdbc98eda4b1cb81d960bef7c7fae710b13fd`.
Its GPL engine and each voice's separate terms still apply. No engine, voice,
model, DLL or binary is copied into this source package. Installed Flite and
FFmpeg retain their own build/license requirements. The private-use profile
does not change these obligations.

### Native Windows COM dependency and offline startup

Windows uses `NativeSapiProvider` with `comtypes==1.5.0` (MIT, pure Python,
Python 3.9+). The reviewed upstream tag points to
[`7732dc3de25797e658d26f375e78310bd7febbda`](https://github.com/enthought/comtypes/tree/7732dc3de25797e658d26f375e78310bd7febbda).
The exact wheel is `comtypes-1.5.0-py3-none-any.whl`, 325563 bytes, SHA256
`c648ed3b243b1eb3b7cb893a873d1b35104ca1c8922061f4b1efafeb255bacb5`.
The preserved `COMTYPES_LICENSE.txt` and `speech/UPSTREAM.json` record its rights
and provenance. Deployment installs the pinned Windows dependency; runtime
discovery never downloads it.

The fixed native worker uses `comtypes.client.CreateObject(..., dynamic=True,
clsctx=CLSCTX_INPROC_SERVER)` for only `SAPI.SpVoice`, `SAPI.SpFileStream` and
`SAPI.SpAudioFormat`. It needs no PowerShell/.NET startup or generated typelib
wrapper. This change addresses observed PowerShell discovery timeouts during
the firewall-blocked Windows gate; actual native COM success still must pass
that gate for each candidate. The exact `2b951ff0` Windows V3 run has since passed
real SAPI/Studio execution; it does not establish manual NVDA or every voice
installation. It does not change firewall, execution policy or OS security settings.

Native discovery exposes the application's real interpreter bitness (64-bit in
the target Windows build), preserving `sapi5-64:<token-hash>` IDs. It cannot see
an unrelated 32-bit-only voice registry. The legacy fixed PowerShell adapter
remains separately available through `SapiProvider`, or explicit
`VoiceRegistry.discover(include_powershell=True)`; it is not a silent fallback.
No SAPI4 support or 135-language qualification is implied.

The worker accepts bounded JSON in private `request.json` and returns a bounded
private `reply.json`. Frozen/windowed startup dispatch is
`--carr-sapi-native-worker REQUEST_JSON REPLY_JSON`; `main()` is invoked before
GUI initialization. File framing works even when a windowed host has no console
streams. New PCM output is confined to `speech.wav` beside the request and only
then published into the parent's private chunk staging. The normal WAV merge
and final no-overwrite publication remain in force. Source execution uses the
current Python with `-I`; install dependencies in that interpreter's environment
or the packaged application, not only an unrelated user-site installation.

## Source fidelity and persistence

`ReadingDocument` keeps its original UTF-8 text and SHA256 separately from the
editable performance copy. Exact source offset runs survive edits; inserted
text has no invented source position, replacements are marked inexact, and
deletion cannot join separated source runs into a false exact quotation.
Undo/redo and edit history survive project saving and reopening. Reading edits
never call quotation approval, source correction or saved-evidence APIs.

Whitespace and optional hyphenation cleanup are deterministic derivative edits.
Pronunciation dictionaries support literal and bounded regex substitutions,
optional language and voice scoping, import/export, and undo as one change.
Regex work runs in a separate standard-library process with a real deadline,
output budget and process-tree cancellation. No unsupported native stress,
phoneme, accent or naturalness quality is claimed.

The native reader offers editable text, selection/from-cursor playback, voice
choice, supported rate/volume, play/pause/resume/stop, seek, explicit WAV export,
Open, Save, Save As, undo/redo, original/diff preview and whitespace cleanup.
`F6` reads, `F7` pauses/resumes, `F8` stops, `Ctrl+S` saves, and
`Ctrl+Shift+S` exports a reading project. No speech starts automatically.
Native controls have text labels, status and keyboard focus targets; manual
Windows 11/NVDA acceptance remains a separate required test.

Valid drafts and selected voice, rate/volume, text cursor and playback position
are atomically saved under the library's `state/speech/` tree. The main complete
backup includes this tree. Source-based recovery slots include library,
edition, revision and selected source range. A new source revision cannot
silently inherit a different original. Explicit exports are new files; a saved
project can be updated only if its previous digest still matches. Original
source paths, symlinks, reparse points, hard-linked outputs and accidental
overwrites are rejected.

## Worker and playback boundaries

Every native speech call has bounded text chunks, deadline, diagnostic output
and audio bytes. Text travels in UTF-8 input files/stdin/JSON; shell invocation
and automatic SSML interpretation are disabled. SAPI uses
`SVSFIsNotXML=16`. FFmpeg's filter expression contains only fixed tokens and an
actually enumerated allowlisted voice; its input text filename is constant in
a private temporary directory. Partial WAVs are private until measured and
validated. Final publication uses an atomic no-clobber link and fsync; inputs
are unchanged, and cancellation never publishes a partial recording.

The WAV merger streams chunks from disk and checks audio format consistency,
frame counts, truncation and output budgets. Large productions should schedule
chapters/scenes in the Studio: this component's single-WAV byte budget remains
below the RIFF limit. Utterance durations are measured; word-level timing and
synchronized highlighting are unavailable unless a future engine provides
verified timing. Playback uses native Windows MCI or an installed ffplay.
MCI position is native; ffplay's elapsed position is labeled approximate.
Device playback/listening quality is not inferred from PCM tests.

`VoiceRegistry.fingerprint(voice_id)` includes visible voice metadata and the
installed executable digest; Piper additionally hashes its model and config.
On Debian-style layouts, Flite/eSpeak shared voice-library bytes are also
included. Opaque third-party SAPI model/driver assets are not fully discoverable
through a token and must not be advertised as a verified model revision.
Use a reviewed runtime manifest for stricter cross-machine cache equivalence.

These process boundaries are not an OS sandbox for malicious installed
executables or voice DLLs. The generic helper must be explicitly reviewed and
enabled; a document cannot enable or install it. OS network denial is required
when validating third-party workers' offline behavior. No API keys, network
fallback, model download or cloud speech adapter is part of this component.

## Versioned helper contract

One request is read as JSON from stdin; one reply is JSON on stdout. Diagnostics
go to stderr and are never displayed with source passages. No arguments are
interpolated into a command. Cancel kills the isolated worker/process tree.

```json
{"schema":"carr.local-voice.v1","operation":"voices"}
```

Response:

```json
{"schema":"carr.local-voice.v1","voices":[{"id":"local-voice-id","name":"Local voice","language":"uk","capabilities":["rate","volume"],"license":"Reviewed installed voice terms"}]}
```

Rendering receives operation `render`, `voice_id`, literal `text`, a private
absolute local `output` WAV path and validated `settings`. It returns
`{"schema":"carr.local-voice.v1","status":"rendered"}` only after producing PCM.
The host independently validates the actual file. A helper cannot claim
success by returning JSON without valid audio.

## Verification

`python -m pytest -q tests/speech` covers actual three-voice synthesis, RMS gain
ratio, source text invariance, exact PCM frames, Unicode paths, multi-chunk
speech longer than 100 seconds, overwrite races, cancellation, real subprocess
timeouts/output bounds, undo/redo, source mapping, catastrophic-regex deadlines,
corruption and recovery through complete library backup.

On Windows run the real native gate:

```text
python -m pytest -q tests/speech/test_runtime.py::test_native_windows_sapi_voice_and_pcm_output
```

The legacy helper `.ps1`, native Python worker, COM client license and provenance
are required package content. The gate fails if no installed SAPI
voice can generate actual nonempty PCM. Linux skips that native test explicitly.
No native Windows/NVDA, Ukrainian/Slovak listening, Piper, SAPI4 or
dordiai-voices acceptance is implied by the Linux results.

Primary API references inspected for the adapters:

- [Microsoft SAPI GetVoices](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ee125639(v=vs.85))
- [Microsoft SAPI speech flags](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms720892(v=vs.85))
- [FFmpeg Flite source filter](https://ffmpeg.org/ffmpeg-filters.html#flite)
- [eSpeak CLI documentation](https://espeak.sourceforge.net/commands.html)
- [Piper official CLI source](https://github.com/OHF-Voice/piper1-gpl/blob/6aacdbc98eda4b1cb81d960bef7c7fae710b13fd/src/piper/__main__.py)
