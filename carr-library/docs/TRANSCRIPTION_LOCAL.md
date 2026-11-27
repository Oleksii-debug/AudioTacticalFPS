# Local transcription in Resonance

## Available workflow

Open **Audio → Local transcription**. Select local audio/video files, a reviewed local model manifest, and an existing result folder. Choose automatic language detection or an explicit Whisper language code. CPU/int8 is the default; CUDA/float16 is an explicit alternative. The same session serves consecutive files. The desktop submits work through its existing operation executor and cancellation event.

TXT, SRT, VTT and JSON are generated into an identity-specific folder. JSON can include word timestamps when selected. A completion marker is published only after every requested format is verified. Source audio stays unchanged. Existing edited results are preserved and reported as conflicts; unrelated same-name TXT files are never adopted. Cache identity binds captured audio bytes, exact model inventory, actual runtime files and inference settings.

## Use the model already on the computer

Choose **Register an existing local model** and select a complete CTranslate2/faster-whisper model directory containing `model.bin`, `config.json` and `tokenizer.json`. Registration records SHA256 values in `resonance-asr-model.json`; it does not copy or download model weights. Select the source and model licence deliberately. This records a local installation decision and does not grant redistribution rights. An existing manifest is never silently replaced.

Registration can run before the optional inference runtime is installed. Inference requires compatible installed faster-whisper, CTranslate2, PyAV, ONNX Runtime, NumPy, Hugging Face Hub and tokenizers. Missing dependencies produce an explicit error without a network fallback. The runtime used is fingerprinted; this is separate from a qualified redistributable Windows engine pack. Supplied virtual environments are not copied into the application.

For a developer-managed Python installation, the optional `transcription` extra selects `faster-whisper==1.2.1`: install this extra when preparing that environment. Normal application startup and model registration never install dependencies. The extra is a dependency declaration, not a complete hash-locked Windows distribution or permission to redistribute selected weights. The runtime smoke checks installed native imports and a synthetic WAV decode; it does not perform neural inference.

## Command line

```powershell
research-library-transcribe register-model "D:\Models\Whisper" --name "My local Whisper" --license "MODEL-LICENCE" --accept-license --trust-source
research-library-transcribe transcribe "D:\Audio\Book.wav" --model-manifest "D:\Models\Whisper\resonance-asr-model.json" --output "D:\Transcripts" --language uk --word-timestamps
```

The equivalent module is `python -m carr_library.transcription_cli`. Repeat input paths for a batch; select one or more formats with repeated `--format` arguments. The batch retains completed file receipts when a later file fails. Full transcript text remains in result files instead of accumulating in batch receipts.

## Accurate boundaries

- Whisper's `translate` operation targets **English only**. The separate Google translation feature supports reviewed text translation requests.
- An explicit source language must be supported by the loaded local model. An English-only model cannot silently replace a Ukrainian request with English. The returned source language and reused cache must match the explicit selection; automatic detection remains available. In translation mode the source-language check remains separate from the English output target.
- Speaker diarization is not implemented by this adapter.
- Cancellation is checked during copying, hashing and between yielded segments. A blocked native inference call cannot be interrupted by this in-process increment. Escape requests cancellation and keeps the dialog alive until completion.
- Post-publication cleanup failure preserves successful transcript status and reports the retained temporary location. Such a location may contain a private source snapshot and needs cleanup.
- A registration cleanup failure is a separate `cleanup_required` state because an unlisted temporary file would prevent complete model verification.
- Synthetic engine tests qualify control flow, identity, file publication and recovery. They do not qualify a real model's accuracy, Ukrainian pronunciation/recognition, GPU performance or manual NVDA behavior.

## Supplied source provenance

The backend adapts the local model call, segment extraction and output workflow from the owner-supplied accessible Whisper V4.3 worker, SHA256 `1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`. Private locations, historic jobs, recordings, transcripts and the supplied virtual environment are excluded. The original wrapper's distribution rights remain a separate provenance check; no licence has been invented.

## Regression and packaging gates

`tests/audio/test_transcription.py`, `test_transcription_models.py`, `tests/ui/test_transcription_controller.py` and `tests/e2e/test_transcription_cli.py` run through the existing isolated offline test runner. The language-tools workflow checks Linux and Windows CPython 3.11, actual installed-wheel module origins and entrypoints, plus native Windows control wiring with synthetic inference and translation responses. Native dialog automation does not imply manual NVDA or acoustic acceptance.
