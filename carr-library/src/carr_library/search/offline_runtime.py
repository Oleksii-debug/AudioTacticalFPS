"""Explicitly configured, pinned local MiniLM inference for scoped source windows.

No downloads, remote code, document uploads or model discovery. A separately
acquired bundle must match the exact reviewed upstream file hashes below.
"""
from hashlib import file_digest, sha256
from importlib.metadata import version
from itertools import islice
import json
import math
import os
from pathlib import Path
import stat

from ..citations.anchors import Locator
from .scoped import _budget
from .semantic_windows import TOKEN_LIMIT, _encoding, search_windowed
from .thematic import Passage

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
REPO = 'Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'
REVISION = 'faf4aa4225822f3bc6376869cb1164e8e3feedd0'
FASTEMBED_SOURCE = '4305bb4bd1d6155aaa9e21258b6255a81accf110'
HASHES = {
    '.gitattributes': 'f7dedb2d755f02ca56e95bd4eaa8bc5588742f97d8537dc5294aa995bade538f',
    'README.md': 'c96c17d91a9e83ec840da5a517277f5164315bd765e21da4216a568008c4f74e',
    'config.json': 'c8ec081fdad2df991bf5abbf18418fec7a5cdaa421f60ffb060a30040b8c376f',
    'model_optimized.onnx': '634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99',
    'ort_config.json': '99881c45e073696289224931dd48694398bc6bcd1fe7cb7018bca1e0cc00e1fc',
    'special_tokens_map.json': '8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835',
    'tokenizer.json': 'fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00',
    'tokenizer_config.json': '0666eebf692422757e1dddf3c9fb1ded73ba3dc726c5828671fc89e45bf3609f',
    'unigram.json': 'da145b5e7700ae40f16691ec32a0b1fdc1ee3298db22a31ea55f57a966c4a65d',
}


def _path(path, *, directory):
    path = Path(os.path.abspath(path))
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked model paths are not allowed')
    info = path.lstat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('model directory required')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('model files must be regular and have one link')
    return path


def _signature(path):
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink, info.st_mode)


def verify_model_bundle(directory, manifest_path):
    """Verify the existing sealed manifest against nine independently pinned files.

    A self-generated checksum is not accepted as authority: every value must
    equal the reviewed revision's constant. The staging directory must remain
    private and quiescent; this check is not a hostile-filesystem sandbox.
    """
    directory = _path(directory, directory=True)
    manifest_path = _path(manifest_path, directory=False)
    with manifest_path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('model manifest exceeds budget')
    manifest = json.loads(raw)
    expected = dict(repo=REPO, revision=REVISION, license='Apache-2.0', hashes=HASHES)
    if manifest != expected:
        raise ValueError('unapproved model manifest')
    if {p.name for p in directory.iterdir()} != set(HASHES):
        raise ValueError('unexpected or missing model file')
    signatures = {}
    for name, expected_hash in HASHES.items():
        path = _path(directory / name, directory=False)
        before = _signature(path)
        with path.open('rb') as stream:
            actual = file_digest(stream, 'sha256').hexdigest()
        if actual != expected_hash or _signature(path) != before:
            raise ValueError('model file changed or hash mismatch')
        signatures[name] = before
    return directory, signatures


class OfflineRuntime:
    """One approved CPU session; embeddings only rank source identities."""
    def __init__(self, directory, manifest_path, *, min_similarity=0.35):
        if (type(min_similarity) not in (float, int) or not math.isfinite(min_similarity)
                or not 0 <= min_similarity <= 1):
            raise ValueError('invalid semantic similarity threshold')
        self.directory, self._signatures = verify_model_bundle(directory, manifest_path)
        if version('fastembed') != '0.9.1':
            raise ValueError('approved FastEmbed 0.9.1 runtime required')
        # local_files_only prevents model downloads, but does not switch off
        # native ONNX Runtime telemetry. The process-level flag must precede
        # ONNX import to prevent its startup uploader/event/device ID as well.
        # Keep API suppression for other backends; failure aborts initialization.
        os.environ['ORT_DISABLE_TELEMETRY'] = '1'
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        from fastembed import TextEmbedding
        from tokenizers import Tokenizer
        self.engine = TextEmbedding(model_name=MODEL, specific_model_path=str(self.directory),
                                    local_files_only=True, providers=['CPUExecutionProvider'],
                                    threads=2)
        # Clone the configured upstream tokenizer, including added special
        # tokens, without altering the engine's batching/padding configuration.
        native = self.engine.model.tokenizer
        if native is None or native.truncation is None or native.truncation['max_length'] != TOKEN_LIMIT:
            raise ValueError('model tokenizer contract differs from approved 128-token budget')
        self.tokenizer = Tokenizer.from_str(native.to_str())
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.min_similarity = float(min_similarity)
        self._unchanged()

    def _unchanged(self):
        _path(self.directory, directory=True)
        if {p.name for p in self.directory.iterdir()} != set(HASHES):
            raise ValueError('model bundle changed after loading')
        for name, original in self._signatures.items():
            path = _path(self.directory / name, directory=False)
            if _signature(path) != original:
                raise ValueError('model bundle changed after loading')

    def rank(self, query, items, *, check=lambda: None):
        """Return relevance-filtered locator keys, never model-generated text.

        Cosine similarity is a ranking heuristic, not a factuality probability.
        This threshold is configurable and has only synthetic relevance smoke
        coverage; citation exactness is enforced independently downstream.
        """
        import numpy as np
        check()
        self._unchanged()
        items = tuple(islice(items, 501))
        if len(items) > 500 or not isinstance(query, str) or not query.strip():
            raise ValueError('invalid semantic request budget')
        if _encoding(self.tokenizer, query)[0] > TOKEN_LIMIT:
            raise ValueError('query exceeds model token budget; lexical fallback required')
        if not items:
            return []
        keys, texts = zip(*items)
        if (len(set(keys)) != len(keys) or any(type(k) is not tuple or len(k) != 4
                or any(not isinstance(v, str) or not v for v in k) for k in keys)
                or len({key[0] for key in keys}) != 1):
            raise ValueError('invalid or mixed-scope semantic identities')
        for text in texts:
            check()
            if (not isinstance(text, str) or not text or len(text) > 200000
                    or _encoding(self.tokenizer, text)[0] > TOKEN_LIMIT):
                raise ValueError('source exceeds model token budget')

        def unit(value):
            vector = np.asarray(value, dtype=float)
            if vector.shape != (384,) or not np.isfinite(vector).all():
                raise ValueError('invalid model embedding')
            norm = np.linalg.norm(vector)
            if not np.isfinite(norm) or norm == 0:
                raise ValueError('invalid model embedding norm')
            return vector / norm

        vectors = []
        for vector in self.engine.passage_embed(texts, batch_size=8):
            check()
            vectors.append(unit(vector))
        check()
        if len(vectors) != len(keys):
            raise ValueError('model embedding count mismatch')
        query_vectors = list(self.engine.query_embed(query))
        check()
        if len(query_vectors) != 1:
            raise ValueError('model query count mismatch')
        query_vector = unit(query_vectors[0])
        ranked = sorted(((key, float(vector @ query_vector)) for key, vector in zip(keys, vectors)),
                        key=lambda pair: (-pair[1], pair[0]))
        self._unchanged()
        check()
        return [key for key, similarity in ranked if similarity >= self.min_similarity]

    def search_catalog(self, db, workspace, query, *, collection=None, limit=20,
                       timeout=5.0, cancelled=lambda: False):
        return search_catalog(db, workspace, query, self, collection=collection,
                              limit=limit, timeout=timeout, cancelled=cancelled)


OfflineSemantic = OfflineRuntime


def search_catalog(db, workspace, query, runtime, *, collection=None, limit=20,
                   timeout=5.0, cancelled=lambda: False):
    """Compose catalog, exact source windows and existing BM25/RRF in one snapshot."""
    from .eligibility import eligible_sources
    stopped, check = _budget(timeout, cancelled)
    check()
    if not db.in_transaction or db.execute('PRAGMA read_uncommitted').fetchone()[0]:
        raise ValueError('consistent authorized read transaction required')
    eligible = eligible_sources(db, workspace, collection=collection)
    allowed = {edition: (work, asset, revision) for edition, work, asset, revision in eligible.editions}
    if not allowed:
        check()
        return []
    placeholders = ','.join('?' for _ in allowed)
    sql = ('SELECT id,workspace,edition,revision,asset,ordinal,physical,printed,verified,raw,digest '
           'FROM active_page WHERE workspace=? AND edition IN (' + placeholders + ') ORDER BY edition,ordinal,id')
    source, metadata = [], {}
    total_characters = 0
    for row in db.execute(sql, (workspace, *allowed)):
        check()
        page_id, ws, edition, revision, asset, ordinal, physical, printed, verified, raw, digest = row
        if (ws != workspace or edition not in allowed or allowed[edition][1:] != (asset, revision)
                or type(page_id) is not int or page_id < 1 or page_id in metadata
                or type(verified) is not int or verified not in (0, 1)):
            raise ValueError('source differs from authorized catalog')
        if not isinstance(raw, str):
            raise ValueError('invalid source text')
        total_characters += len(raw)
        if len(raw) > 200000 or total_characters > 10_000_000:
            raise ValueError('source scope exceeds semantic text budget')
        if sha256(raw.encode('utf-8')).hexdigest() != digest:
            raise ValueError('source digest mismatch')
        Locator('logical_segment', ordinal, printed, bool(verified)) if physical is None else Locator(
            'physical_page', physical, printed, bool(verified))
        # Blank source pages contribute no candidate and never become quotes.
        if not raw:
            continue
        if len(source) >= 500:
            raise ValueError('source scope exceeds semantic budget')
        source.append(Passage(ws, allowed[edition][0], edition, revision, str(page_id),
                              physical, 0, len(raw), raw, digest,
                              ordinal if physical is None else None))
        metadata[page_id] = (asset, printed, verified)
    hits = search_windowed(workspace, source, query, tokenizer=runtime.tokenizer,
                           semantic_ranker=lambda q, values: runtime.rank(q, values, check=check),
                           limit=limit, timeout=timeout, cancelled=stopped)
    for hit in hits:
        check()
        page_id = int(hit['parent_passage_id'])
        asset, printed, verified = metadata[page_id]
        hit.update(workspace_id=workspace, page_id=page_id, asset_sha256=asset,
                   locator_kind='logical_segment' if hit['physical_index'] is None else 'physical_page',
                   printed_label=printed if verified else None,
                   printed_label_verified=bool(verified), raw_text_sha256=hit['page_sha256'])
    check()
    return hits
