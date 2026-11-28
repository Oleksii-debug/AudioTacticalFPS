# Optional remote transcription of one selected recording

Local transcription remains the default. This optional route sends one recording
to the ElevenLabs speech-to-text API only after an explicit review, an unchecked
permission box and a second confirmation whose initial answer is No.

This feature uses the existing AI/API account settings, credential vault,
authorization and usage ledger, transcription jobs, editor, export and complete
backup. It does not install another speech engine or change local Whisper jobs.

## Configure the account

Open **AI and API connections** and create a dedicated profile:

| Setting | Value |
| --- | --- |
| Adapter | `elevenlabs` |
| Endpoint | `https://api.elevenlabs.io/v1` |
| Model | `scribe_v2` |
| Capability | `speech.transcribe` only |
| Credential | Your ElevenLabs API key, saved through the existing vault |
| Enabled | Select explicitly |

Saving or enabling this profile makes no transcription request. A profile that
mixes transcription with another capability is not admitted to this route.
The application does not import credentials from a document or a restored
backup.

## Review and send

1. Open **Transcription: local audio and video...** for the current library and
   workspace. Select **Віддалене розпізнавання з дозволом** (remote transcription
   with permission).
2. Choose the enabled account and one recording. Enter `auto` or an explicit
   two- or three-letter language code. Actual language support is determined by
   the provider; the application does not certify recognition quality.
3. Enter a positive **Local budget reservation in USD**. This is an allowance in
   the application's local usage ledger. It is not a price quote and does not
   impose a billing cap on ElevenLabs.
4. Select **Review**. Read the path, byte count, SHA-256, account endpoint, model,
   language, reservation and retention notice. Review reads local bytes and
   makes no provider request.
5. Select the permission box and **Send approved recording**. The second
   confirmation begins on **No**. Select **Yes** only if this is the intended
   recording and account.

Changing the recording, language, account or reservation invalidates the review.
A changed file, expired approval, changed profile, changed library or cancelled
operation is refused before a new transfer. Active approval is checked again
after reading the recording, before credentials and immediately before the
request.

The request uses a generic upload filename. It sends the selected file bytes and
the reviewed protocol options, not other library books or recordings. File
formats below are an admission list; valid media content and actual recognition
remain subject to the provider.

## Limits and retained results

| Boundary | Local limit or behavior |
| --- | --- |
| Selected original | One regular local file, at most 24 MiB |
| Admitted suffixes | WAV, FLAC, MP3, M4A, OGG, OPUS, AAC, WMA, WEBM, MP4, MKV |
| Request | One synchronous multipart POST to the official speech-to-text route |
| Recognition model | `scribe_v2` |
| Response | At most 8 MiB, 10,000 words and 100,000 text characters |
| Total remote execution | 120 seconds, including the owned child process |
| Timing | Provider word timestamps; no independent alignment claim |
| Extra features | No diarization, audio-event tags, webhooks or URL-based input |
| Retries and redirects | Neither is followed automatically |

The original recording is retained with the job. A completed job has immutable
recognition text, word-derived segments, TXT/SRT/VTT/JSON outputs and a receipt
binding the original, request, provider profile, response and usage ledger.
When the provider omits duration, the last word timestamp bounds the returned
timeline; it is not a measurement of the recording's full length.

The existing transcription editor opens the verified result. Edits create the
normal separate editable transcript; they do not replace the retained recording
or original recognition text. Existing explicit export uses that job's verified
source and edit state.

## Stop, close and uncertain requests

**Stop** requests cooperative cancellation. Closing while work is queued or
running also requests cancellation and keeps the dialog alive until the shared
worker settles. The remote child remains owned until its process and native
handles have been cleaned up.

A request that was already sent may still be processed or charged by the
provider after cancellation, a timeout or a broken response. The local ledger
retains that uncertainty. Reopening a job does not resend the recording.

If results were committed before a later cleanup error, the application retains
the completed receipt. Local verification can reconcile that completed state
without making another paid request. A new transfer always requires a fresh
explicit review and approval; it is never inferred from an incomplete job.

## Retention and complete backups

This adapter uses normal ElevenLabs service retention (`enable_logging=true`).
It does not enable the enterprise-only zero-retention option. Read your account
terms before sending a recording.

Complete backups keep the retained original, validated remote transcript,
editable state and passive usage references through the existing typed
transcription backup format. They do not save an active transfer authorization
or credential value. Restored remote jobs require review again; restored
consent is inactive and a running job is restored as stopped. Verification and
ordinary transcript export remain local.

## Evidence and limits of qualification

The focused acceptance workflow has separate, strict obligations:

- Three complete portable suites: 39 provider, 29 retained-job and 17 UI cases,
  run from source and from an independently installed verified wheel.
- Six real local HTTP cases in source mode and six in installed mode, through
  the existing Windows loopback boundary. They send synthetic fixture bytes.
- A real Windows wx workflow with 73 required checks and 23 exact source origins:
  native keyboard consent, default No and explicit Yes, the actual shared
  worker and owned child, retained transcript and editor, cancellation and
  host cleanup.

The native workflow substitutes only the selected synthetic recording, HTTPS
reply and vault value while exercising the real account, authorization, ledger,
job, process and UI paths. It makes no paid provider call. Test inventories and
static peer review are not execution results: actual receipts from the exact
candidate are required before reporting these gates as passed. Real-service
acoustics, language coverage and manual NVDA use are separate, unclaimed
qualifications.

## Protocol references

- [ElevenLabs: convert speech to text](https://elevenlabs.io/docs/api-reference/speech-to-text/convert)
- [Machine-readable endpoint documentation](https://elevenlabs.io/docs/api-reference/speech-to-text/convert.md)
- [ElevenLabs zero-retention mode](https://elevenlabs.io/docs/eleven-api/resources/zero-retention-mode)

The local limits above are deliberately narrower than provider admission.
Future provider or account changes require another explicit compatibility
review; this adapter does not discover endpoints or invent management APIs.
