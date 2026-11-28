"""Evaluate production thematic vs MiniLM hybrid retrieval on independently supplied gold.

From the checkout's carr-library directory:
  PYTHONPATH=src python scripts/evaluate_retrieval.py --fixture gold.json --model-dir MODEL --k 5
Windows: set PYTHONPATH=src in the shell or use the installed application.
The fixture has schema carr-retrieval-gold/1, documents [{id,text}], queries
[{language,text,relevant:[document IDs]}]. Empty relevant means a negative query.
Use short, lawful, single-line documents and gold labels fixed before model use.
Exact query text must be unique across all languages; language tags are grouped
case-insensitively. Use actual search concepts, not punctuation-only queries.
Only aggregate metrics and ordinal case evidence are printed, not source/query text.
No downloads, no engine substitution, no model tuning, no release verdict.
Python socket blocking is not native/OS firewall evidence. Run behind the existing
runtime firewall for offline qualification. Independent gold provenance is external.
"""
import argparse
from hashlib import sha256
import importlib.metadata
import json
import math
import os
import platform
from pathlib import Path
import re
import statistics
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('Duplicate fixture JSON key.')
        obj[key] = value
    return obj


def valid_text(value, maximum):
    return (type(value) is str and 0 < len(value) <= maximum and
            value == value.strip() and all(c.isprintable() for c in value))


def load_gold(raw):
    if not 0 < len(raw) <= 1024 * 1024:
        raise ValueError('Fixture must fit in 1 MiB.')
    data = json.loads(raw, object_pairs_hook=unique_object)
    if (type(data) is not dict or set(data) != {'schema', 'documents', 'queries'}
            or data['schema'] != 'carr-retrieval-gold/1'
            or type(data['documents']) is not list or not 2 <= len(data['documents']) <= 100
            or type(data['queries']) is not list or not 1 <= len(data['queries']) <= 100):
        raise ValueError('Invalid bounded gold fixture schema.')
    docs = {}
    for item in data['documents']:
        if (type(item) is not dict or set(item) != {'id', 'text'}
                or type(item['id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', item['id'])
                or item['id'] in docs or not valid_text(item['text'], 1000)
                or item['text'] in docs.values()):
            raise ValueError('Invalid or duplicate gold document.')
        docs[item['id']] = item['text']
    queries = []
    seen_text = set()
    for item in data['queries']:
        if (type(item) is not dict or set(item) != {'language', 'text', 'relevant'}
                or type(item['language']) is not str or not re.fullmatch(r'[A-Za-z0-9-]{2,24}', item['language'])
                or not valid_text(item['text'], 256) or type(item['relevant']) is not list
                or any(type(i) is not str or i not in docs for i in item['relevant'])
                or len(set(item['relevant'])) != len(item['relevant'])):
            raise ValueError('Invalid gold query or relevance identity.')
        # Language is reporting metadata, not an input to app.search.
        # Count each exact retrieval input once, even across language labels.
        if item['text'] in seen_text:
            raise ValueError('Duplicate gold query text.')
        seen_text.add(item['text'])
        queries.append((item['language'].lower(), item['text'], set(item['relevant'])))
    if not any(q[2] for q in queries) or not any(not q[2] for q in queries):
        raise ValueError('Include positive and negative queries.')
    return docs, queries


def score(ranked, relevant, k):
    # Preserve the first occurrence of each edition; duplicate windows never
    # count as additional relevant documents.
    ranked = list(dict.fromkeys(ranked))[:k]
    if not relevant:
        return {'recall': None, 'mrr': None, 'false_positive': bool(ranked)}
    return {'recall': len(set(ranked) & relevant) / len(relevant),
            'mrr': next((1 / i for i, key in enumerate(ranked, 1) if key in relevant), 0.0),
            'false_positive': None}


def aggregate(rows):
    positives = [r for r in rows if r['recall'] is not None]
    negatives = [r for r in rows if r['false_positive'] is not None]
    times = sorted(r['milliseconds'] for r in rows)
    return {'positive_queries': len(positives), 'negative_queries': len(negatives),
            'recall': statistics.mean(r['recall'] for r in positives) if positives else None,
            'mrr': statistics.mean(r['mrr'] for r in positives) if positives else None,
            'negative_false_positive_rate': statistics.mean(r['false_positive'] for r in negatives) if negatives else None,
            'p50_ms': statistics.median(times), 'p95_ms': times[math.ceil(.95 * len(times)) - 1]}


def evaluate(raw, model_dir, k):
    from carr_library import application, semantic, semantic_cache
    from carr_library.search import semantic_windows, thematic
    docs, queries = load_gold(raw)
    # Reuse the production concept parser; a punctuation-only or otherwise
    # empty query is not an inference case and must not enter gold metrics.
    for _, query, _ in queries:
        if not thematic._groups(query):
            raise ValueError('Gold query must contain a production search concept.')
    cases = {'thematic': [], 'semantic': []}
    # Actual production model and app; no synthetic ranker or alternate BM25.
    blocked = AssertionError('Network is forbidden during evaluation.')
    with patch('socket.socket.connect', side_effect=blocked), \
            patch('socket.socket.connect_ex', side_effect=blocked), \
            patch('socket.create_connection', side_effect=blocked), \
            patch('socket.getaddrinfo', side_effect=blocked), TemporaryDirectory() as tmp:
        started = perf_counter()
        ranker = semantic.OfflineSemanticRanker(model_dir)
        load_ms = (perf_counter() - started) * 1000
        app = application.LibraryApp(Path(tmp) / 'library', semantic_ranker=ranker)
        workspace = app.create_workspace('Retrieval evaluation')
        by_edition = {}
        for identity, original in docs.items():
            imported = app.add_text(workspace, identity, original)
            edition = imported['edition']
            if edition in by_edition:
                raise ValueError('Gold documents collapsed into the same edition.')
            by_edition[edition] = identity
        for index, (language, query, relevant) in enumerate(queries):
            for mode in cases:
                statuses = []
                with patch.object(ranker, 'rank', wraps=ranker.rank) as real_rank:
                    started = perf_counter()
                    hits = app.search(workspace, query, mode=mode, limit=100, on_status=statuses.append)
                    elapsed = (perf_counter() - started) * 1000
                if statuses or real_rank.call_count != (1 if mode == 'semantic' else 0):
                    raise RuntimeError('Requested retrieval mode did not complete as declared.')
                ranked = []
                for hit in hits:
                    identity = by_edition.get(hit.edition_id)
                    if (identity is None or hit.workspace != workspace or not hit.verified
                            or not hit.quote or hit.quote not in docs[identity]
                            or app.copy(workspace, hit.ref, hit.revision, 'quote') != hit.quote):
                        raise RuntimeError('Returned result failed original-source verification.')
                    ranked.append(identity)
                row = {'ordinal': index + 1, 'language': language,
                       'milliseconds': elapsed, **score(ranked, relevant, k)}
                if mode == 'semantic':
                    row['cache'] = dict(ranker.cache_statistics)
                cases[mode].append(row)
        # Recheck the actual approved bundle after the final query.
        semantic.verify_model_bundle(model_dir)
        model_id = semantic_cache.model_identity(semantic.APPROVED_MANIFEST)
    languages = sorted({q[0] for q in queries})
    return {'schema': 'carr-retrieval-evaluation/1', 'fixture_sha256': sha256(raw).hexdigest(),
            'model': semantic.APPROVED_MANIFEST, 'model_identity': model_id,
            'harness_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
            'module_sha256': {m.__name__: sha256(Path(m.__file__).read_bytes()).hexdigest()
                              for m in (application, semantic, semantic_cache, semantic_windows, thematic)},
            'versions': {p: importlib.metadata.version(p) for p in
                         ('fastembed', 'onnxruntime', 'tokenizers', 'numpy')},
            'k': k, 'documents': len(docs), 'model_load_ms': load_ms,
            'host_profile': {'os': platform.system(), 'machine': platform.machine(),
                             'logical_cpus': os.cpu_count()},
            'runtime_profile': {'requested_provider': 'CPUExecutionProvider',
                                'gpu_qualification': 'NOT_RUN', 'threads': 2},
            'source_checks': 'PASS', 'python_socket_policy': 'BLOCKED', 'native_egress': 'NOT_MEASURED',
            'gold_independence': 'NOT_ESTABLISHED_BY_HARNESS', 'qualification': 'NOT_DECIDED',
            'timing_scope': 'search of up to 100 hits; sequential queries, cache may warm; excludes copy verification',
            'modes': {mode: {'overall': aggregate(rows), 'cases': rows,
                             'by_language': {lang: aggregate([r for r in rows if r['language'] == lang])
                                             for lang in languages}}
                      for mode, rows in cases.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--model-dir', required=True, type=Path)
    parser.add_argument('--k', type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    with args.fixture.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    print(json.dumps(evaluate(raw, args.model_dir, args.k), ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
