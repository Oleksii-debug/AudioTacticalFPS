"""Offline MiniLM runtime and the existing scoped BM25/RRF window adapters.

The nine hashes below were sealed by packaging/model_bundle.py against the
publisher's exact Hugging Face revision. Loading never downloads anything.
Model scores rank source slices; they do not verify quotations or citations.
"""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from time import monotonic

from .search.semantic_windows import TOKEN_LIMIT, search_windowed
from .search.thematic import Passage, search_active

MODEL_NAME = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
APPROVED_MANIFEST = {
    'repo': 'Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q',
    'revision': 'faf4aa4225822f3bc6376869cb1164e8e3feedd0',
    'license': 'Apache-2.0',
    'hashes': {
        '.gitattributes': 'f7dedb2d755f02ca56e95bd4eaa8bc5588742f97d8537dc5294aa995bade538f',
        'README.md': 'c96c17d91a9e83ec840da5a517277f5164315bd765e21da4216a568008c4f74e',
        'config.json': 'c8ec081fdad2df991bf5abbf18418fec7a5cdaa421f60ffb060a30040b8c376f',
        'model_optimized.onnx': '634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99',
        'ort_config.json': '99881c45e073696289224931dd48694398bc6bcd1fe7cb7018bca1e0cc00e1fc',
        'special_tokens_map.json': '8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835',
        'tokenizer.json': 'fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00',
        'tokenizer_config.json': '0666eebf692422757e1dddf3c9fb1ded73ba3dc726c5828671fc89e45bf3609f',
        'unigram.json': 'da145b5e7700ae40f16691ec32a0b1fdc1ee3298db22a31ea55f57a966c4a65d',
    },
}


def verify_model_bundle(directory, manifest=None):
    """Authenticate the pinned bundle, including caller-provided manifest bytes.

    A supplied manifest cannot approve arbitrary hashes. It must exactly match
    this application's independently sealed publisher revision. As with the
    build-time helper, the bundle must live in a private quiescent directory.
    """
    if manifest is not None:
        if isinstance(manifest, (str, Path)):
            path = Path(manifest)
            if path.stat().st_size > 64_000:
                raise ValueError('Model manifest is too large.')
            manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest != APPROVED_MANIFEST:
            raise ValueError('The model manifest is not the approved MiniLM revision.')
    root = Path(directory).absolute()
    if '..' in root.parts:
        raise ValueError('Model directory must not contain parent traversal.')
    for path in (*reversed(root.parents), root):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Model directories must be local and unlinked.')
    if {path.name for path in root.iterdir()} != set(APPROVED_MANIFEST['hashes']):
        raise ValueError('Missing or unexpected model files.')
    for name, expected in APPROVED_MANIFEST['hashes'].items():
        path = root / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Model files must be regular files without links.')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError('Model file SHA256 mismatch: ' + name)
    return root


def _unit(vector):
    vector = tuple(float(value) for value in vector)
    if len(vector) != 384 or not all(map(math.isfinite, vector)):
        raise ValueError('Invalid MiniLM embedding.')
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError('Invalid MiniLM embedding norm.')
    return tuple(value / norm for value in vector)


class OfflineSemanticRanker:
    """Pinned FastEmbed CPU inference, with complete token coverage checks."""
    def __init__(self, directory, manifest=None, *, minimum_similarity=0.25):
        if (type(minimum_similarity) not in (int, float)
                or not math.isfinite(minimum_similarity) or not 0 <= minimum_similarity <= 1):
            raise ValueError('Invalid similarity threshold.')
        root = verify_model_bundle(directory, manifest)
        if importlib.metadata.version('fastembed') != '0.9.1':
            raise ValueError('This model runtime requires the pinned FastEmbed 0.9.1.')
        from .offline_policy import disable_native_telemetry
        disable_native_telemetry()
        from fastembed import TextEmbedding
        from fastembed.common.preprocessor_utils import load_tokenizer
        self.tokenizer, _ = load_tokenizer(root)
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.engine = TextEmbedding(model_name=MODEL_NAME, specific_model_path=str(root),
                                    local_files_only=True, providers=['CPUExecutionProvider'],
                                    threads=2)
        verify_model_bundle(root, manifest)
        self.minimum_similarity = float(minimum_similarity)
        self.directory = root
        self.cache = None
        self.cache_statistics = {}

    def bind_cache(self, library_directory, *, budget=None):
        """Bind a cache for a dedicated direct ranker, outside LibraryApp."""
        self.cache = self.cache_for(library_directory, budget=budget)

    def cache_for(self, library_directory, *, budget=None):
        """Create a private per-library cache without mutating a shared engine.

        Binding creates no files and changes no source authorization. Each rank
        still checks the current token coverage and only returns supplied keys.
        """
        from .semantic_cache import EmbeddingCache, model_identity
        return EmbeddingCache(Path(library_directory) / 'semantic-cache',
                              model_identity(APPROVED_MANIFEST), budget=budget)

    def __call__(self, query, candidates):
        return self.rank(query, candidates)

    def rank(self, query, candidates, *, cancelled=lambda: False, timeout=60.0, cache=None):
        if not callable(cancelled) or type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError('Invalid semantic runtime budget.')
        deadline = monotonic() + timeout
        latched = False

        def check():
            nonlocal latched
            latched = latched or cancelled() or monotonic() >= deadline
            if latched:
                raise TimeoutError('Semantic search cancelled or timed out.')

        check()
        candidates = tuple(candidates)
        if len(candidates) > 500:
            raise ValueError('Semantic candidate budget exceeded.')
        keys = [key for key, _ in candidates]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate semantic locator.')
        if not candidates:
            return []
        texts = [query, *(text for _, text in candidates)]
        runtime_tokenizer = self.engine.model.tokenizer
        for text in texts:
            check()
            if not isinstance(text, str) or not text or len(text) > 200_000:
                raise ValueError('Invalid semantic source text.')
            if self.tokenizer.truncation is not None or self.tokenizer.padding is not None:
                raise ValueError('Untruncated semantic coverage tokenizer required.')
            complete = self.tokenizer.encode(text, add_special_tokens=True)
            if len(complete.ids) > TOKEN_LIMIT or complete.overflowing:
                raise ValueError('Semantic text exceeds complete token budget.')
            actual = runtime_tokenizer.encode(text, add_special_tokens=True)
            ids = [token for token, active in zip(actual.ids, actual.attention_mask) if active]
            if actual.overflowing or ids != complete.ids:
                raise ValueError('Runtime tokenizer changed or truncated source text.')
        # Cache keys include the exact model/preprocessor, current source text
        # and workspace/revision/window. Authorization and complete token checks
        # above are never replaced by cache membership.
        cache = cache if cache is not None else getattr(self, 'cache', None)
        identities = []
        cached = {}
        if cache is not None:
            from .semantic_cache import content_identity
            identities = [content_identity(cache.model, query)]
            identities.extend(content_identity(cache.model, text, key) for key, text in candidates)
            check()
            cached = cache.get_many(identities)
            check()
        embeddings = [cached.get(identity) for identity in identities] if identities else [None] * len(texts)
        missing = [i for i, vector in enumerate(embeddings) if vector is None]
        self.cache_statistics = {'hits': len(texts) - len(missing), 'misses': len(missing),
                                 'embedded_texts': 0, 'available': bool(cache and cache.available)}
        # Native embedding batches remain bounded. Cancellation is checked on
        # both sides of each native batch; an in-flight CPU call is cooperative.
        for start in range(0, len(missing), 16):
            indices = missing[start:start + 16]
            check()
            batch = tuple(self.engine.embed([texts[i] for i in indices], batch_size=16))
            check()
            if len(batch) != len(indices):
                raise ValueError('Semantic embedding count mismatch.')
            normalized = tuple(_unit(vector) for vector in batch)
            for i, vector in zip(indices, normalized):
                embeddings[i] = vector
            self.cache_statistics['embedded_texts'] += len(indices)
            if cache is not None:
                cache.put_many({identities[i]: vector for i, vector in zip(indices, normalized)})
                check()
                self.cache_statistics['available'] = cache.available
        query_vector, *document_vectors = embeddings
        scored = [(key, math.fsum(a * b for a, b in zip(query_vector, vector)))
                  for key, vector in zip(keys, document_vectors)]
        check()
        # Similarity is a retrieval heuristic, never a probability or source
        # verification. Existing BM25/RRF supplies the lexical fallback.
        return [key for key, score in sorted(scored, key=lambda pair: (-pair[1], pair[0]))
                if score >= self.minimum_similarity]


def search_snapshot(db, workspace, allowed, query, ranker, *, limit=100, offset=0,
                    timeout=60.0, cancelled=lambda: False, on_status=None, cache=None):
    """Read active pages and rank windows inside the caller's authorized snapshot."""
    if (not db.in_transaction or db.execute('PRAGMA read_uncommitted').fetchone()[0]
            or not isinstance(workspace, str) or not workspace
            or not isinstance(allowed, dict) or len(allowed) > 500
            or any(not isinstance(value, str) or not value
                   for pair in allowed.items() for value in pair)
            or not isinstance(ranker, OfflineSemanticRanker)
            or type(limit) is not int or not 1 <= limit <= 100
            or type(offset) is not int or offset < 0
            or type(timeout) not in (int, float) or not 0 < timeout <= 60
            or not callable(cancelled)
            or (on_status is not None and not callable(on_status))):
        raise ValueError('An authorized snapshot and bounded semantic scope are required.')
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or cancelled() or monotonic() >= deadline
        return latched

    def check():
        if stopped():
            raise TimeoutError('Semantic search cancelled or timed out.')

    def remaining():
        check()
        value = deadline - monotonic()
        if value <= 0:
            raise TimeoutError('Semantic search cancelled or timed out.')
        # Repeated coarse clock ticks may round subtraction above the limit.
        return min(timeout, value)

    def fallback(reason):
        check()
        if on_status is not None:
            on_status('The local model\'s text limit was reached. Keyword search used. '
                      'Shorten the query or narrow the source filters to use the local model.')
        hits = search_active(db, workspace, allowed, query, limit=limit, offset=offset,
                             timeout=remaining(), cancelled=stopped)
        for hit in hits:
            hit['reason'] = 'Semantic window limit reached; using lexical thematic search. ' + reason
        return hits

    check()
    if not allowed:
        return []
    placeholders = ','.join('?' for _ in allowed)
    cursor = db.execute('SELECT id,workspace,edition,revision,asset,ordinal,physical,printed,'
                        'verified,raw,digest FROM active_page WHERE workspace=? AND edition IN ('
                        + placeholders + ') ORDER BY edition,ordinal,id', (workspace, *allowed))
    passages, metadata = [], {}
    try:
        for row in cursor:
            check()
            page_id, ws, edition, revision, asset, ordinal, physical, printed, verified, raw, raw_hash = row
            if (ws != workspace or edition not in allowed or type(page_id) is not int or page_id < 1
                    or type(ordinal) is not int or ordinal < 1 or type(verified) is not int
                    or verified not in (0, 1) or (physical is None and (printed is not None or verified))
                    or not isinstance(asset, str) or not re.fullmatch('[0-9a-f]{64}', asset)
                    or not isinstance(raw, str) or '\x00' in raw
                    or hashlib.sha256(raw.encode('utf-8')).hexdigest() != raw_hash):
                raise ValueError('Invalid active semantic source identity or digest.')
            if not raw:
                continue
            if len(passages) >= 500:
                return fallback('More than 500 active source pages.')
            key = str(page_id)
            metadata[key] = page_id, asset, printed if verified else None, bool(verified)
            passages.append(Passage(ws, allowed[edition], edition, revision, key, physical,
                                    0, len(raw), raw, raw_hash, ordinal if physical is None else None))
    finally:
        cursor.close()
    check()

    def rank(query, candidates):
        check()
        options = {'cache': cache} if cache is not None else {}
        return ranker.rank(query, candidates, cancelled=stopped, timeout=remaining(), **options)

    try:
        hits = search_windowed(workspace, passages, query, tokenizer=ranker.tokenizer,
                               semantic_ranker=rank, limit=limit, offset=offset, timeout=remaining(),
                               cancelled=stopped)
    except ValueError as exc:
        if 'budget' in str(exc).lower() or 'semantic window' in str(exc).lower():
            return fallback(str(exc))
        raise
    except sqlite3.OperationalError as exc:
        if stopped():
            raise TimeoutError('Semantic search cancelled or timed out.') from exc
        raise
    for hit in hits:
        page_id, asset, printed, verified = metadata[hit['parent_passage_id']]
        hit.update(workspace_id=workspace, page_id=page_id, asset_sha256=asset,
                   locator_kind='logical_segment' if hit['physical_index'] is None else 'physical_page',
                   segment_index=hit.get('segment_index'), printed_label=printed,
                   printed_label_verified=verified, raw_text_sha256=hit['page_sha256'])
        if hit['reason'].startswith('hybrid'):
            hit['reason'] = 'Local multilingual MiniLM and BM25 (RRF); quotation verification is separate.'
    check()
    return hits
