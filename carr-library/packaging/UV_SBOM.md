# Build-only dependency locking and SBOM preflight (candidate)

## Integrated desktop build update — 2026-11-26

The current application pins pypdf 7.1.1 (publisher wheel SHA256 `04c5680d4b4cfa04ab505b09393fc949860c34a4dad6565284d11a2f5c3ce8b1`), replacing the historical synthetic-only 5.9.0 smoke pin. The offline PDF smoke and tampered-wheel test use that same pin. The native uv resolver/SBOM wrappers now pin uv 0.13.9, upstream commit `6b2d39dcc390293d51c9c0d2bbcd650f4bcbfdce` (MIT/Apache-2.0), and preserve hash verification, Windows target selection and negative checks. The earlier evidence below remains historical. Current packaging entrypoints and offline installation instructions are in `docs/RUNNING.md`; application wheels retain all vendored license files.


Adopt Astral uv's existing resolver, hash compiler, installer and CycloneDX 1.5 exporter instead of implementing a resolver or SBOM generator. Tested tool: uv 0.10.0, upstream commit [0ba432459aa4e84d793fd8cfc98d13cf619316bb](https://github.com/astral-sh/uv/tree/0ba432459aa4e84d793fd8cfc98d13cf619316bb), dual MIT/Apache-2.0 (LICENSE-MIT and LICENSE-APACHE read at that commit). Build host tool only; no uv executable or wheel redistributed here. The installed uv version was exercised, not authenticated against an upstream binary attestation. A release build must use a separately security-reviewed, provenance-verified tool binary. See https://docs.astral.sh/uv/concepts/projects/export/.

## Reproduce the smoke

With a trusted uv 0.10.0 already on PATH and the genuine locally supplied pip 25.1.1 fixture wheel, from carr-library run:

    python packaging/uv_sbom_smoke.py PATH_TO/pip-25.1.1-py3-none-any.whl EXPECTED_SHA256

EXPECTED_SHA256 must be from trusted fixture provenance, not calculated from an untrusted download. Linux test used the pre-existing distribution wheel /usr/share/python-wheels/pip-25.1.1-py3-none-any.whl, local SHA-256 20568e5d750393b2a331d6b261886cbe5d62aca3f54d49a39815b7474d34cd9c. This local wheel digest is not claimed to be the PyPI publisher digest. No external source code is copied. The wheel is a build-tool fixture, not a proposed product dependency.

Actual Linux Python 3.13.5 result: exit 0, six checks PASS: native CycloneDX component and edge, hash compile, fresh offline venv install, stale lock rejection, missing wheel rejection, and modified ZIP comment (valid ZIP, different hash) rejection. Uses temporary Unicode/space paths, no network index or cache, no source builds. No OS-level network sandbox claimed. The original tamper attempt with invalid ZIP was rejected structurally; the final valid ZIP test specifically requires hash failure. Tested script Git blob 35ec5b011091d8142c51ef0b5810eb5b2b59c8a5. Windows NOT_RUN. This is not the complete application SBOM or installer.

## Integration after dependency freeze

Use a canonical reviewed application pyproject and independently verified platform wheelhouse. Native commands (run in the staging project; keep the wheelhouse immutable):

    uv --no-config --offline --no-cache --no-python-downloads lock --no-index --find-links wheelhouse --no-build
    uv --no-config --offline --no-cache --no-python-downloads export --locked --no-index --find-links wheelhouse --no-build --format cyclonedx1.5 -o sbom.json
    uv --no-config --offline --no-cache --no-python-downloads export --locked --no-index --find-links wheelhouse --no-build --no-emit-project -o resolved.txt
    uv --no-config --offline --no-cache --no-python-downloads pip compile --no-index --find-links wheelhouse --no-build --generate-hashes resolved.txt -o requirements.lock

For each target OS/architecture/Python version, verify actual wheel compatibility, lock and SBOM component/version parity (including marker resolution), publisher hashes against downloaded bytes, all transitive licenses and vulnerability findings. Install with hash checking into a fresh venv, then compare installed inventory and run required suites. Local flat-index uv lock/export did not emit wheel hashes in the actual test; native pip compile --generate-hashes did. Do not mistake a locally generated hash for independent publisher authentication. Do not use --frozen to conceal a stale lock. CycloneDX output is experimental in this version; its UUID/timestamp vary. Supplement it with pinned vendored source notices (including PR17 cairnsearch), native runtime/OCR libraries and separately verified model files: these are not automatically included in Python dependency exports.

## Optional local models

FastEmbed 0.9.1 source inspected at [2099c36036115e689ea9b3b5bf1aafa79dc01190](https://github.com/qdrant/fastembed/tree/2099c36036115e689ea9b3b5bf1aafa79dc01190), Apache-2.0 LICENSE and pyproject read. Its dependencies include CPU onnxruntime, numpy, tokenizers, huggingface-hub, Pillow, mmh3 and py-rust-stemmers; Python 3.13 requires onnxruntime >1.21.0 excluding 1.24.0 and 1.24.1 in this revision. Its TextEmbedding documents local_files_only and specific_model_path; CARR-07 must test actual behavior with selected exact model files. Do not infer model-weight rights from the library license. No implicit download, no remote code, no GPU or paid API requirement. Keep embeddings optional with deterministic BM25 fallback. Model pack requires exact upstream revision, all model/tokenizer/config hashes, complete license notices, offline CPU inference and missing/tampered model tests. FastEmbed, onnxruntime and tokenizers absent locally; DNS failed for external git download. No actual model packaging or inference claimed.

Full candidate dependency resolution, security-reviewed PDF upgrade, Windows artifact, actual app SBOM and CI are still open. No root workflow or unrelated files modified.
