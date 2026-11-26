# Independent FastEmbed compatibility candidate

## Verified release correction and actual CPU execution — 2026-11-26

The installed official FastEmbed 0.9.1 wheel corresponds to the annotated
`v0.9.1` tag at **4305bb4bd1d6155aaa9e21258b6255a81accf110**. Independent Git
blob comparison matched all 68 installed Python files and the Apache-2.0
LICENSE exactly, with no extra or missing Python files. The older reviewed
source pin below is a later development commit: comparison against that commit
matched 41 Python files, differed in 27, and lacked two source files. It must
not be described as the source of the 0.9.1 release wheel. The executable source
pin and current acceptance probe now identify the verified release.

The exact nine-file Qdrant bundle at revision
`faf4aa4225822f3bc6376869cb1164e8e3feedd0` was downloaded through the native Hub
API and sealed with the existing model_bundle verifier. Actual Linux Python
3.12.14, FastEmbed 0.9.1, ONNX Runtime 1.32.0 and tokenizers 0.23.3 execution of
`tests/search/test_thematic_onnx_probe.py`: **13 passed, zero skipped**, 3.95 s.
CPU load plus inference was 2.305 s; six synthetic multilingual queries yielded
semantic MRR@3 and recall@3 of 1.0, versus lexical MRR@3 of 0.0. Python socket
connect APIs were blocked and recorded zero connection attempts; Hub and
Transformers offline modes were enabled. These are synthetic smoke metrics,
not a general relevance benchmark or independent Windows acceptance.

The production `carr_library.search.offline_runtime` adapter pins all nine
upstream hashes, verifies the sealed identity, loads only an explicit local
CPU model, uses the existing exact-source window splitter and BM25/RRF fusion,
and keeps catalog authorization in the caller's transaction. It does not
download at startup or approve source text. Source-byte and metadata validation
remain mandatory before any result becomes a copied quotation. Weights are not
included in the repository or wheel; redistribution approval and native
Windows/NVDA acceptance remain separate.

### Historical compatibility notes

Reuses FastEmbed (Apache-2.0) at https://github.com/qdrant/fastembed/tree/2099c36036115e689ea9b3b5bf1aafa79dc01190. Inspected LICENSE, text/pooled_embedding.py and text/onnx_embedding.py under fastembed/. The latter confirms specific_model_path, CPU providers and local_files_only. Model candidate: https://huggingface.co/Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q (Apache-2.0 per model card; full model revision and artifact hashes NOT verified). Do not install from mutable model main or auto-download on startup. CARR-07 retains production ranking; this is an isolated synthetic-only probe, not competing implementation.

Run `python -m unittest -v` from this folder. Actual local execution Python 3.13.5: 5 PASS, 0 FAIL (test doubles, synthetic hash fixtures only). FastEmbed and ONNX Runtime unavailable; direct GitHub download failed DNS. Actual inference, relevance, memory, Windows and offline network isolation NOT_RUN. No model weights downloaded or included. The requirement is a source pin, not a complete transitive lock. Before real testing, require independently approved full model revision and exact SHA256 manifest, reviewed locked dependencies, local read-only bundle and network-disabled process. Then call `evaluate(load_engine(path, approved_hashes))` from Python; never treat synthetic test-double scores as real relevance. The probe does not generate quotes or access books. CARR-11/13 review packaging, license and sandbox before adoption. No production integration or release claim.
