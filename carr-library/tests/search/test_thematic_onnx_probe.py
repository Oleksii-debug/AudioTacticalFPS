"""Opt-in CPU model acceptance probe, not a production search backend.

FastEmbed 0.9.1 source: qdrant/fastembed@4305bb4bd1d6155aaa9e21258b6255a81accf110 (Apache-2.0).
Model: Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q
revision faf4aa4225822f3bc6376869cb1164e8e3feedd0 (Apache-2.0).
Never downloads. CARR07_MODEL_DIR and CARR07_MODEL_MANIFEST must point to an
independently verified offline bundle and its PR46 sealed manifest. Synthetic data only.
"""
import hashlib
import json
import os
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import pytest

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# Match CARR-11 PR46 model_bundle.seal output; no separate download path.
REPO = "Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
LICENSE = "Apache-2.0"
PINNED = {
    "model_optimized.onnx": "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99",
    "tokenizer.json": "fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00",
    "unigram.json": "da145b5e7700ae40f16691ec32a0b1fdc1ee3298db22a31ea55f57a966c4a65d",
}
REQUIRED = set(PINNED) | {"config.json", "tokenizer_config.json", "special_tokens_map.json",
                          ".gitattributes", "README.md", "ort_config.json"}


def verify_bundle(directory, manifest, pinned=PINNED):
    if (not isinstance(manifest, dict) or set(manifest) != {"repo", "revision", "license", "hashes"}
            or manifest["repo"] != REPO or manifest["revision"] != REVISION
            or manifest["license"] != LICENSE or not isinstance(manifest["hashes"], dict)):
        raise ValueError("invalid sealed model manifest identity")
    manifest = manifest["hashes"]
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("expected local model directory")
    if set(manifest) != REQUIRED or {p.name for p in directory.iterdir()} != REQUIRED:
        raise ValueError("unexpected or missing model files")
    if any(manifest.get(k) != v for k, v in pinned.items()):
        raise ValueError("model revision mismatch")
    for name, expected in manifest.items():
        p = directory / name
        if p.is_symlink() or not p.is_file():
            raise ValueError("expected regular model file")
        with p.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("model asset hash mismatch")


def reciprocal_rank(ids, relevant):
    return next((1 / n for n, value in enumerate(ids, 1) if value == relevant), 0.0)


def rank_vectors(vectors, query, ids):
    import numpy as np
    a, q = np.asarray(vectors, dtype=float), np.asarray(query, dtype=float)
    if a.shape != (len(ids), 384) or q.shape != (384,) or len(set(ids)) != len(ids):
        raise ValueError("invalid embedding dimensions or identities")
    if not np.isfinite(a).all() or not np.isfinite(q).all():
        raise ValueError("nonfinite embedding")
    norms, qnorm = np.linalg.norm(a, axis=1), np.linalg.norm(q)
    if (norms == 0).any() or qnorm == 0:
        raise ValueError("zero embedding")
    scores = (a / norms[:, None]) @ (q / qnorm)
    return [ids[i] for i in sorted(range(len(ids)), key=lambda i: (-scores[i], ids[i]))]


@pytest.fixture
def synthetic_bundle(tmp_path):
    manifest = {}
    for name in REQUIRED:
        payload = name.encode()
        (tmp_path / name).write_bytes(payload)
        manifest[name] = hashlib.sha256(payload).hexdigest()
    return tmp_path, {"repo": REPO, "revision": REVISION, "license": LICENSE, "hashes": manifest}


def test_bundle_checks(synthetic_bundle):
    directory, manifest = synthetic_bundle
    verify_bundle(directory, manifest, pinned={})
    with pytest.raises(ValueError, match="revision"):
        verify_bundle(directory, manifest)
    (directory / "tokenizer.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        verify_bundle(directory, manifest, pinned={})


def test_extra_asset_rejected(synthetic_bundle):
    directory, manifest = synthetic_bundle
    (directory / "unexpected.bin").touch()
    with pytest.raises(ValueError, match="unexpected"):
        verify_bundle(directory, manifest, pinned={})


def test_missing_asset_rejected(synthetic_bundle):
    directory, manifest = synthetic_bundle
    (directory / "config.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_bundle(directory, manifest, pinned={})


@pytest.mark.parametrize("field", ["repo", "revision", "license"])
def test_wrong_manifest_identity(synthetic_bundle, field):
    directory, manifest = synthetic_bundle
    manifest[field] = "wrong"
    with pytest.raises(ValueError, match="identity"):
        verify_bundle(directory, manifest, pinned={})


def test_legacy_flat_manifest_rejected(synthetic_bundle):
    directory, manifest = synthetic_bundle
    with pytest.raises(ValueError, match="identity"):
        verify_bundle(directory, manifest["hashes"], pinned={})


def test_unigram_pin_required(synthetic_bundle):
    directory, manifest = synthetic_bundle
    pins = {"unigram.json": manifest["hashes"]["unigram.json"]}
    verify_bundle(directory, manifest, pinned=pins)
    manifest["hashes"]["unigram.json"] = "0" * 64
    with pytest.raises(ValueError, match="revision"):
        verify_bundle(directory, manifest, pinned=pins)


def test_rank_math_and_tie_break():
    np = pytest.importorskip("numpy")
    a, b = np.eye(384)[:2]
    assert rank_vectors([a, b, a], a, ["z", "b", "a"]) == ["a", "z", "b"]
    assert reciprocal_rank(["a", "z", "b"], "z") == 0.5
    assert reciprocal_rank([], "z") == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0])
def test_bad_vectors_fail_closed(bad):
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError):
        rank_vectors(np.full((1, 384), bad), np.ones(384), ["a"])


def test_real_cpu_model(monkeypatch):
    directory = os.environ.get("CARR07_MODEL_DIR")
    manifest_path = os.environ.get("CARR07_MODEL_MANIFEST")
    if not directory or not manifest_path:
        pytest.skip("NOT_RUN: approved offline model bundle not supplied")
    verify_bundle(directory, json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    assert version("fastembed") == "0.9.1"
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    import onnxruntime
    onnxruntime.disable_telemetry_events()
    from fastembed import TextEmbedding
    from carr_library.search.thematic import Passage, search

    texts = {
        "a": "The council set a ceiling on the price of bread.",
        "b": "The ship crossed the ocean during a violent storm.",
        "c": "Музиканти виконали симфонію в концертній залі.",
        "d": "Vedci skúmali šírenie nákazlivej choroby.",
        "e": "The price of a concert ticket increased in spring.",
        "f": "The council approved repairs to the town hall.",
    }
    queries = [("food price controls", "a"), ("обмеження ціни хліба", "a"),
               ("plavba cez oceán", "b"), ("orchestral performance", "c"),
               ("поширення інфекції", "d"), ("ticket price increase", "e")]
    ids = sorted(texts)
    passages = []
    for i in ids:
        raw = "Preface.\n" + texts[i] + "\nEnd."
        passages.append(Passage("A", i, "edition-" + i, "r1", i, 1, 9,
                                9 + len(texts[i]), raw, hashlib.sha256(raw.encode()).hexdigest()))
    started = perf_counter()
    model = TextEmbedding(model_name=MODEL, specific_model_path=str(Path(directory).resolve()),
                          local_files_only=True, providers=["CPUExecutionProvider"], threads=2)
    vectors = list(model.passage_embed([p.raw_page[p.start:p.end] for p in passages], batch_size=4))
    semantic, lexical = [], []
    for query, target in queries:
        q, = model.query_embed(query)
        ranked = rank_vectors(vectors, q, ids)
        semantic.append(reciprocal_rank(ranked[:3], target))
        hits = search("A", passages, query, limit=3)
        lexical.append(reciprocal_rank([h["passage_id"] for h in hits], target))
        for hit in hits:
            assert hit["raw_quote"] == texts[hit["passage_id"]]
        for i in ranked[:3]:
            p = passages[ids.index(i)]
            assert p.raw_page[p.start:p.end] == texts[i]
            assert hashlib.sha256(p.raw_page.encode()).hexdigest() == p.page_sha256
    print({"semantic_mrr_at_3": sum(semantic) / len(queries),
           "lexical_mrr_at_3": sum(lexical) / len(queries),
           "semantic_recall_at_3": sum(x > 0 for x in semantic) / len(queries),
           "seconds_including_load": perf_counter() - started})
    assert sum(semantic) > sum(lexical), "no measurable benefit on synthetic smoke queries"
    assert sum(x > 0 for x in semantic) >= 5
