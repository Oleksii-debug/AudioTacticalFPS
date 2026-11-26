"""Independent synthetic-only compatibility probe for pinned FastEmbed."""
import hashlib
import math
from pathlib import Path
import re

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
REQUIRED = {'model_optimized.onnx', 'tokenizer.json', 'config.json',
            'tokenizer_config.json', 'special_tokens_map.json'}
DOCS = ('Railway transport connected industrial cities.',
        'Залізниця з’єднувала промислові міста.',
        'Botanists studied flowers in a garden.',
        'У саду ботаніки досліджували квіти.')
QUERY = 'How did trains link manufacturing towns?'


def verified_bundle(root, approved_hashes):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('model directory must be local and non-symlink')
    if not REQUIRED <= approved_hashes.keys():
        raise ValueError('incomplete approved manifest')
    actual = set()
    for p in root.iterdir():
        if p.is_symlink() or not p.is_file():
            raise ValueError('unexpected directory or symlink')
        actual.add(p.name)
    if actual != set(approved_hashes):
        raise ValueError('unexpected or missing model files')
    for name, expected in approved_hashes.items():
        if not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('invalid SHA256')
        with (root / name).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('model hash mismatch')
    return str(root.resolve())


def load_engine(root, approved_hashes):
    path = verified_bundle(root, approved_hashes)
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL, specific_model_path=path,
                         local_files_only=True, providers=['CPUExecutionProvider'], threads=2)


def unit(vector):
    vector = tuple(float(v) for v in vector)
    if len(vector) != 384 or not all(map(math.isfinite, vector)):
        raise ValueError('invalid embedding')
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError('invalid embedding norm')
    return tuple(v / norm for v in vector)


def evaluate(engine):
    vectors = list(engine.embed([QUERY, *DOCS], batch_size=5))
    if len(vectors) != 5:
        raise ValueError('embedding count mismatch')
    q, *docs = map(unit, vectors)
    ranked = sorted(range(4), key=lambda i: (-math.fsum(a*b for a,b in zip(q,docs[i])), i))
    return {'ranked_ids': ranked, 'recall_at_2': len(set(ranked[:2]) & {0,1}) / 2,
            'mrr': 1 / next(i for i, doc in enumerate(ranked, 1) if doc in {0,1})}
