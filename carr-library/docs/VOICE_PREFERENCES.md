# Local voice preferences

The voice reader can save favorites, an exact-language default and named
rate/volume profiles in the current library. Open **Voice preferences** from
the reader's existing tools. Saving a preference does not start speech,
download a model, activate an engine, or change a saved Book or Studio job.

This workflow adapts the existing PR152 implementation at
14c823bcd1c7c07dc4f768be955a972c445c4469 into the current reader and settings
contracts. It uses the existing VoiceRegistry implementation, serial reader
worker and state/audio/engines.json.

## Choosing and saving

Favorites retain exact voice IDs, including unavailable saved IDs. They are
labels for the user's choices, not proof that a voice is installed.

A language default must match the selected voice's exact declared language
tag, including any script and region. A profile additionally saves its exact
voice ID, synthesis rate (-10 to 10), and volume (0 to 100). Non-default
controls require the current voice's corresponding capability. Individual
providers keep their existing render-time range and asset checks.

**Use language default**, **Use voice profile**, and **Use selected voice**
change only this reader's selection. They first validate a fresh current
inventory on the reader worker and compare the selected runtime identity
with the reader's borrowed registry. A removed voice, changed provider,
different language or unsupported profile fails visibly; no substitute is
selected. Use **Reload voices** in the reader if its inventory has changed.

The existing language qualification catalog provides tag normalization.
Preferences do not infer mappings for its 135 selected-language IDs,
candidate languages, national accents or acoustic quality. Existing
recovered reader choices retain their priority; this workflow does not
guess a document language or automatically start speech.

## Native voice pitch and punctuation

Open **Native voice options...** in the reader. Enable **Use native voice
pitch** to choose a value from **0 to 99** on the selected engine's scale,
and use **Read punctuation aloud** when that voice supports it. Playback
pitch is adjusted separately in **Playback controls**.

**Apply native voice options** accepts the request after stopping current
playback. Use Read or Save WAV to synthesize with it; existing saved audio
stays unchanged. **Cancel native voice options** discards the dialog's
changes. **Reset native voice options** selects engine-default pitch and
turns punctuation reading off; press Apply to accept that reset.

An unsupported saved request remains visible and blocks Read and Save WAV
until you choose a compatible voice or explicitly reset it. Disabled
controls and the status field explain the selected voice's limitations.

Each reading tab and its recovery draft retain these options passively.
Restoration never starts speech or substitutes an unavailable voice. Named
voice profiles keep their existing exact voice, rate and volume contract;
opening or applying a profile does not silently reset native options.

## Saving, cancellation and restoration

Preferences are an optional strictly typed field in the existing schema-1
engine document. Opening legacy settings does not rewrite them. The existing
engine-state lock and an expected-value check prevent two preference windows
from silently overwriting each other. Engine edits preserve current
preferences. Preference edits preserve runtime paths, hashes, full-pack
manifest seals and restore-revalidation markers.

The existing 32 MiB durable JSON ceiling applies to the encoded aggregate,
not just the count of favorite IDs. Existing foreign-backup limits remain
unchanged. A preference-only change during prepared runtime activation
retains the existing exact-settings check; retry the setup if its snapshot
has become stale.

A stopped review cannot apply a new voice. Closing the preferences window
revokes queued GUI completions. If an atomic save already completed before
a later cancellation, the saved preferences remain saved and an open,
current window retains the committed snapshot.

Complete format-5 and supported legacy flat backups preserve valid
preferences. Restoration retains the voice IDs and profiles as passive
data and applies the existing engine-revalidation barrier. Editing a
favorite after restore does not remove that barrier. Actual engine
availability and exact selected identity must be checked again before use.

## Evidence and commands

Prepared focused suites:

- tests/speech/test_voice_preferences.py
- tests/ui/test_voice_preferences_dialog.py
- tests/storage/test_voice_preferences_backup.py

The native probe uses actual Windows controls, real atomic settings files,
the existing registry with explicitly synthetic voice declarations, and a
serial worker. It covers fresh-inventory removal, current-library changes
and Close while review is pending. It proves control and ownership behavior,
not neural inference, physical listening, pronunciation quality or NVDA use.

Source mode:

~~~powershell
python -B -X utf8 scripts/windows_voice_preferences_smoke.py --source . --report voice-preferences-source.json
~~~

Installed mode from the exact checked-out script:

~~~powershell
python -I -B -X utf8 scripts/windows_voice_preferences_smoke.py --expect-installed --report voice-preferences-installed.json
~~~

Installed acceptance binds every loaded dependency to the actual wheel
RECORD and exact source bytes through the shared native provenance helper.
An independent 90-second watchdog keeps an unhandled modal or cleanup hang
a failed result.

The source and installed Windows native scenarios passed in
[P3 preflight run 46063639311](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46063639311),
job `140723613596`, step 11, on source
`10b9a4e36b6415cdf9a21fbc36bcd9ef2b72420a`. This result covers the stated
synthetic-inventory control and ownership scenario. Required suite reports,
complete sealed-installation acceptance, actual voice-model inference and
human NVDA/listening results remain separate evidence. The complete Windows
preflight was still running when this checkpoint was recorded.

Native pitch and punctuation were added after the P3 result above. Their
focused execution is pending; that earlier result does not qualify the new
controls.
