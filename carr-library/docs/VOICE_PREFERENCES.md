# Reader voice defaults and favorites

The Voice reader's **Voice preferences by language and favorites** button opens
a native dialog over the voices already enumerated by its existing
`VoiceRegistry`. It does not discover another provider, load a private model,
install a voice or start speech.

## Choosing and remembering voices

Select **Declared language or dialect** to see installed voices declaring that
exact identifier. Choose a voice, then use one of these explicit actions:

- **Save selected voice as language default** persists that voice's exact ID
  for this language. It does not change the current reader or synthesize text.
- **Add or remove selected favorite** persists the chosen favorite. Favorites
  appear first and are marked in the dialog's installed-voice list.
- **Use selected voice in reader** closes the dialog and selects that ID in the
  existing reader. Its supported controls are refreshed, and the usual reading
  recovery snapshot retains the choice. Press F6 when ready to read.
- **Remove saved language default** or **Remove unavailable favorites** clears
  only the indicated saved choices. Missing language defaults are preserved
  until explicitly removed or replaced.

Reopen the dialog and select a language to retrieve its saved default. The
default is selected only if its exact ID is still enumerated with the same
declared language. A missing voice or changed declaration is reported with no
substitution. **Reload saved preferences** rereads choices made in another
window without rediscovering engines.

The **All installed languages** view allows choosing a voice or favorite. A
language default requires a separately selected concrete language. Engine
declarations with unspecified (`und`) or unrecognized identifiers remain
available in the all-voices view but cannot become language defaults.

Language identifiers are normalized for case and underscore/hyphen spelling.
There is no guessed text language, base-language fallback, dialect equivalence,
or automatic provider switch: `en`, `en-US` and `en-GB` are distinct choices.
An engine's declaration is not proof of intelligibility, pronunciation,
multilingual cloning, licensing or the 135-language qualification target.

## Persistence and recovery

The versioned `state/speech/voice-preferences.json` sidecar contains only
language-to-voice IDs and favorite IDs. It includes no executable paths,
credentials, model configuration, audio or text. It uses the existing atomic
JSON writer and state lock, with comparison against the state originally read
by this dialog. A concurrent edit must be reloaded before saving.

The stored format allows at most 256 language defaults, 128 favorites and
1 MiB of serialized settings. Invalid/future schemas, duplicate identifiers,
unexpected keys and unsafe filesystem entries produce visible errors while
preserving saved bytes. Replacing the library directory or switching/closing
the active library revokes the dialog's save/use authority.

Normal full-library backup includes this speech sidecar. Restore retains the
IDs without installing or executing the referenced engines. Availability is
checked against the reader's currently loaded registry before a voice is used.
The original reading text, retained source, source locator, selected text and
existing pronunciation edits are independent of these preferences.

## Evidence boundary

`tests/speech/test_voice_preferences.py` covers actual state writes/reopening,
conflicting writers, unknown settings preservation, exact language matching,
missing voices, replaced directories and real backup/delete/restore without
engine activation. `tests/speech/test_voice_preference_handlers.py` executes
the production dialog handlers and reader closure with explicit native-widget
stand-ins, including stale library authority and separate save/use actions.

These tests use original synthetic voice identities and make no speech-quality
claim. Installed Windows native controls, human keyboard operation, NVDA,
physical output devices and actual voice/language quality require separate
artifact-bound evidence.
