# Offline semantic retrieval

The application can load the existing quantized multilingual MiniLM model through
FastEmbed 0.9.1. Loading and searching use local files only. Model installation is
a separate build operation; a missing model leaves literal and lexical thematic
search available with an explicit fallback message.

## Source and verification

- Publisher: <https://huggingface.co/Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q/tree/faf4aa4225822f3bc6376869cb1164e8e3feedd0>
- Exact revision: `faf4aa4225822f3bc6376869cb1164e8e3feedd0`.
- Publisher model card declares Apache-2.0. The model card travels with the bundle.
- All nine files were downloaded with upstream `huggingface_hub.snapshot_download`
  at that revision, copied into a dedicated regular-file directory, and checked
  by the existing `packaging/model_bundle.py` helper on 2026-11-26.
- That helper used the upstream `HfApi.verify_repo_checksums` implementation and
  checked the three previously pinned publisher SHA256 values. All nine matched.
- `carr_library.semantic.APPROVED_MANIFEST` records the exact resulting hashes.
  A caller-supplied manifest must match that manifest; it cannot approve another
  model. Runtime checks reject missing/extra files, symlinks, reparse points,
  hardlinks and changed file hashes before loading the native runtime.
- Weights remain outside the Git repository. The build package may include them.

## Integration

`OfflineSemanticRanker(directory, manifest=None)` loads CPU inference with
`specific_model_path`, `local_files_only=True` and two worker threads. The
application passes it to `LibraryApp(data_dir, semantic_ranker=ranker)`.

Before constructing a model session, the adapter explicitly disables ONNX
Runtime telemetry and sets Hugging Face's offline and telemetry opt-out flags.
Using local weights alone does not disable a dependency's diagnostic reporting.
The complete test entrypoint also disables telemetry and rejects Python socket
connections and name resolution. This is a test policy, not an operating-system
network sandbox.

The adapter reuses the existing `source_windows`, `search_windowed`, scoped
SQLite BM25 and vendored Rifflux reciprocal-rank fusion. It does not generate
text. Every hit retains its original edition, revision, page and exact Unicode
slice; the application independently verifies source bytes before enabling
quotation copy or saved evidence.

The model accepts at most 128 tokens per input. An independent instance of the
same upstream tokenizer has padding and truncation disabled. Existing window
logic partitions the complete source, and each final window is encoded again.
Before inference, the adapter compares complete token IDs to the runtime's
actual non-padding IDs. The final portion of a long page is therefore included,
or a clear budget error triggers lexical fallback. Attention-mask length alone
is never treated as proof of full source coverage.

Each query admits at most 500 authorized source pages/windows and 10 million
expanded source characters under the existing adapters. Budgets and cancellation
are checked between bounded native inference batches. An in-progress native CPU
batch is cooperative and can finish before cancellation is observed. No partial
results are returned after cancellation. Similarity `0.25` is a filtering
heuristic, not a probability or a source-verification verdict.

## Actual verification

On Linux with Python 3.12, FastEmbed 0.9.1, ONNX Runtime 1.32.0, tokenizers 0.23.3,
huggingface-hub 1.16.1 and NumPy 2.3.5:

```bash
CARR_TEST_MODEL_DIR=/absolute/path/to/verified/minilm \
HF_HUB_OFFLINE=1 PYTHONPATH=src:. \
python -m pytest -q tests/e2e/test_semantic_runtime.py
```

The initial run passed all six tests in 4.25 seconds, with both
`socket.socket.connect` and `socket.create_connection` blocked throughout model
loading and inference. It covered English-to-Ukrainian retrieval, collection
isolation and revocation, exact copied source slices, retrieval from the end of a
long page, explicit overlong-query fallback, cancellation, modified manifests
and a linked model directory.

The existing four-document compatibility fixture ranked the two relevant railway
documents first (Ukrainian then English), giving recall@2 of 1.0 and reciprocal
rank of 1.0 on that small synthetic fixture. This is an integration check, not a
held-out relevance evaluation or a general quality claim.

Native Windows inference and NVDA interaction require separate execution on
Windows. Linux results do not establish those acceptance gates.
