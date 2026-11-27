# Explicit local voice helper: current profile integration

This component restores the finite canonical `carr.local-voice.v1` JSON helper
contract through the existing local engine profiles, `VoiceRegistry`, process
owner and atomic PCM publisher. It does not install, discover or download an
optional helper. The canonical reference was
`e3cf74c243d4ef930b526b5648ca9003c0900057`,
`speech/runtime.py::LocalHelperProvider`; no alternate speech framework or old
store is imported.

## Select and review a helper

Use **Tools → Local engine profiles → Choose and review manifest**, inspect the
identity and license, and explicitly save the registration. Then reload voices
in the existing reader or Studio and select the registered voice. Profile
registration, settings reopening and ordinary registry construction do not
execute the helper. The same registry is available to current headless callers.

The installation must be separate from the library, complete, local, regular
and checksum-covered. Review its license and native/script code before setting
both acceptance flags. An arbitrary native executable remains trusted reviewed
code; the JSON protocol and offline environment are not an OS sandbox.

The existing local-model manifest retains schema version 1. Its engine is
`local-voice-helper`, its revision is an exact 40- or 64-character lowercase
hex identity, and its file map lists every installation file with SHA-256.
The closed settings object has these fields:

| Field | Contract |
| --- | --- |
| protocol | Exactly `carr.local-voice.v1` |
| launch | `native` or `python` |
| executable | An inventory-covered relative native executable; Windows requires `.exe` |
| engine_name | Stable nonblank display identity, at most 128 characters |
| script | Required only for `python`; inventory-covered relative `.py` |
| max_voices | Optional integer, 1–10000; default 1024 |
| timeout_seconds | Optional positive finite timeout up to one day; default 180 |

A native launch contains exactly the selected executable. The Python launch is
exactly `python -I -B -X utf8 selected-script.py`. Neither the manifest nor the
reply may supply extra command arguments, environment variables, module names,
working directories or an alternate output path. Replies travel on stdout;
requests travel on UTF-8 JSON stdin. Diagnostic output is bounded to the current
1 MiB engine limit. Catalogues use at most 30 seconds; render uses the reviewed
per-segment timeout. Requests are at most 128000 bytes and inherit the existing
8000-character speech limit.

## JSON and speech settings

Catalogue request:

```json
{"schema":"carr.local-voice.v1","operation":"voices"}
```

The response contains only `schema` and `voices`. Each voice contains
`id`, `name`, `language`, `license`, and optional unique `capabilities`.
Current active controls are `rate`, `volume`, `pitch`, and `punctuation`.
The historical `phonemes` capability is accepted as metadata; this adapter does
not advertise an unsupported phoneme editor. Duplicate JSON keys, nonfinite
numbers, unknown fields, repeated tokens and malformed language codes fail.

Render contains exactly `schema`, `operation: "render"`, `voice_id`,
`text`, the caller-owned private `output`, and `settings`. The settings are:

- Integer rate from −10 to 10. Fractional values are rejected.
- Integer volume from 0 to 100, derived from the current 0–1 setting.
- Pitch is null at the unchanged default; explicit current semitone settings
  map to `round(50 + 3 * pitch)`, the existing local adapter convention.
  This mapping is a protocol control, not a claim of acoustic pitch accuracy.
- Boolean punctuation, current timeout and the 256 MiB PCM byte bound.

Nondefault controls require the selected voice's capability. Language must
match the declared code or its explicitly requested base language. Literal text
stays data; no shell or SSML is introduced. Success is exactly:

```json
{"schema":"carr.local-voice.v1","status":"rendered"}
```

The existing engine validates actual nonempty PCM16 WAV frames before atomic
no-replace publication. Catalogue identity and the complete installation are
checked again. Manifest, executable and optional script stay open through child
settlement. Accepted PCM stays open while the last catalogue/manifest recheck
runs. Cancellation, timeout, invalid PCM, changed identity and occupied output
leave no successful visible result.

## Compatibility and retention

Current profile IDs remain machine-local and qualify the canonical helper
token hashes. Saved Piper/Dordiai voice IDs and cache keys keep their old wrapper
path. Each helper call re-admits the selected current profile; disabling,
removing or changing it cannot silently choose another voice.

Schema-1 raw `voice_helpers` settings stay inactive. The existing settings
history archives their exact original bytes on explicit reviewed registration.
No automatic migration grants executable permission. Full library backup keeps
its existing rule: machine registrations, installation paths and optional
runtimes are not restored as active executable authority. The library can be
restored and backed up again without the external helper.

## Reproducible qualification

`carr-local-voice-helper.yml` reuses the current source-wheel builder, full
tracked-source/installed-module inventory, isolated test harness and 56 pinned
Windows dependencies. It declares **111 source + 111 installed tests** and
**31 exact native checks**, with complete function/parameter and label Counters
in `local_voice_helper_oracles.json`. Timeouts, skipped/extra/missing cases,
changed identities and incomplete cleanup cannot produce aggregate PASS.
Raw textual receipts and JUnit are retained on failure.

The public fixture copies a no-pip Python venv, hashes every selected-pack file
and runs a reviewed fixed script. Its pyvenv.cfg refers to the selected test
host's trusted standard library; this is not a standalone vendor distribution.
The fixture itself installs a socket/descendant audit denial, emits deterministic
16000 Hz mono PCM16, and never claims synthesized human speech. Its base and
selected Python, generated script, manifest and public fixture source are
recorded. The workflow firewall binds the installed host; the fixture's explicit
audit denial supplies its own Python-child restriction.

Actual source/installed tests cover invalid contracts and replies, literal
Unicode/settings over real stdin, changed installation bytes, held output,
real child timeout/cancel, no-clobber, raw legacy preservation, current profile
re-admission and backup/restore/rebackup. Native controls exercise real wx
registration, disable/enable and Close, then actual helper processes. The native
single-executable argv shape is tested at the boundary; **no private native
vendor executable, vendor inference, voice quality, physical playback or NVDA
qualification is claimed**. Gate authoring and source review are not runtime
PASS; attach an actual frozen-run receipt before claiming execution.
