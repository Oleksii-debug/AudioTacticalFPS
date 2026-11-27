# Native SAPI through the current audio engine

Status: implemented and source reviewed; source/installed/native execution is
**NOT_RUN** until the exact focused workflow completes. This file is not an
acoustic, accessibility or whole-product acceptance receipt.

## Reconciled canonical behavior

The canonical e3cf74c native worker addressed observed offline PowerShell
startup delays. NativeSAPI5Engine now reuses the current VoiceRegistry,
speech/Studio callers and the existing bounded _run/WindowsJobProcess owner.
Only fixed in-process SAPI COM objects and private request/reply/PCM files are
used. Source text remains literal: SVSFIsNotXML is always selected, and
punctuation adds only the documented SVSFNLPSpeakPunc flag.

The current integrated Windows lock already admits pywin32 312. The worker
uses that client rather than adding the canonical comtypes dependency or
copying an unused licence. Exact source and rights references are retained in
src/carr_library/audio/SAPI_NATIVE_PROVENANCE.md. The integrated inventory stays
at 56 pins. Historical wheelhouse profiles without pywin32 report native SAPI
unavailable; their old package inventories are not silently rewritten.

The supported delivered runtime is the existing packaged CPython/wheel/native
launcher. A private main dispatch is exercised by the focused installed probe.
There is no committed PyInstaller build; generic frozen executable imports,
metadata collection and hidden-import qualification are not claimed.

## Compatibility boundaries

Current SAPI IDs hash the exact System.Speech registry-token leaf. Microsoft's
VoiceInfo/ObjectToken/RegistryDataKey sources establish that mapping. The
native worker preserves it and also binds the full COM token and name for each
fresh rendering operation. Duplicate leaves are refused. New cache receipts
include the changed host, worker bytes, architecture and pywin32 version; old
completed audio is never relabelled as a native render.

Native output is measured 22050 Hz mono PCM16. Earlier PowerShell 24000 Hz
receipts belong to that unchanged explicit compatibility adapter. Existing
windows_live_reading_smoke.py still explicitly selects SAPI5Engine, so its
historical evidence is neither replaced nor transferred.

Ordinary discovery never launches PowerShell. A passive sapi5-32 entry resolves
an explicitly selected saved 32-bit ID on demand through the existing adapter;
Studio/headless rendering can use the exact retained role without eager
catalogue probing. Explicit include_powershell=True performs 32-bit catalogue
discovery. SAPI4 remains unavailable. eSpeak NG is preferred; when only the
installed classic espeak executable exists, its distinct espeak namespace is
enumerated instead of claiming an NG engine.

## Punctuation and retained state

The existing live-reader voice settings expose explicit punctuation only for
voices that advertise it. Native SAPI and eSpeak accept it; other adapters
refuse the option. Literal source text is unchanged. No unsupported phoneme,
pronunciation-quality or word-alignment result is inferred.

Audio Role/project schemas and their fingerprints are unchanged. The default
false punctuation option is omitted from engine arguments and stored state.
Old reading-speech.json schema-1 rows retain exactly their five settings fields.
Only a row carrying the optional setting needs carr-live-reading/2; its typed
decoder still accepts old rows in the same document. Restored settings and
cursors remain passive, requiring explicit current voice selection and Play.
The new tests include independently constructed old state bytes, full
backup/restore/rebackup and exact legacy Role JSON roundtrip.

## Focused gate

.github/workflows/carr-native-sapi.yml uses the existing shared batch acceptance
runner, source-bound application wheel, complete tracked-source/Python
inventory, isolated source/installed children and exact function Counters.

- Source: 71 cases across four new focused modules.
- Installed: the same 71 cases and exact Counter.
- Native: 35 exact checks, actual Windows COM, nonzero measured PCM, fixed
  installed entrypoint, active owned-process cancellation, native reader
  punctuation controls and passive full backup/restore/rebackup.
- Native execution has an explicit outbound-denial firewall rule for the exact
  installed interpreter, after all dependency installation is complete.
- Job bound: 75 minutes. Each pytest child: 1200 seconds, step: 22 minutes.
  Native internal watchdog: 240 seconds, step: five minutes.
- Raw text, exact JUnit/identity receipts and failures are always retained.
  Timeout, missing execution, skips or partial Counters cannot produce PASS.

No physical playback, NVDA, third-party voice quality, 32-bit native COM or
model/private-asset acceptance is claimed. Existing broader audio, reader and
installed-package regression gates remain required on the integrated head.
