"""Build-time MiniLM provenance seal; never run this on application startup."""
import argparse
import hashlib
import json
from pathlib import Path

REPO = 'Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'
REVISION = 'faf4aa4225822f3bc6376869cb1164e8e3feedd0'
FILES = frozenset({'.gitattributes', 'README.md', 'config.json',
                   'model_optimized.onnx', 'ort_config.json', 'special_tokens_map.json',
                   'tokenizer.json', 'tokenizer_config.json', 'unigram.json'})
PUBLISHED = {
    'model_optimized.onnx': '634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99',
    'tokenizer.json': 'fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00',
    'unigram.json': 'da145b5e7700ae40f16691ec32a0b1fdc1ee3298db22a31ea55f57a966c4a65d',
}


def hashes(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('expected a dedicated local directory')
    children = list(root.iterdir())
    if {p.name for p in children} != FILES or any(p.is_symlink() or not p.is_file() for p in children):
        raise ValueError('unexpected, missing or linked model files')
    result = {}
    for p in sorted(children):
        with p.open('rb') as f:
            result[p.name] = hashlib.file_digest(f, 'sha256').hexdigest()
    return result


def seal(root):
    # Use a private, quiescent staging directory; not safe against concurrent writers.
    before = hashes(root)
    if any(before[name] != expected for name, expected in PUBLISHED.items()):
        raise ValueError('published SHA256 mismatch')
    from huggingface_hub import HfApi
    report = HfApi(endpoint='https://huggingface.co', token=False).verify_repo_checksums(
        REPO, revision=REVISION, local_dir=root, token=False)
    if (report.revision != REVISION or report.checked_count != len(FILES)
            or report.mismatches or report.missing_paths or report.extra_paths):
        raise ValueError('upstream repository verification failed')
    if hashes(root) != before:
        raise ValueError('staged files changed during verification')
    return {'repo': REPO, 'revision': REVISION, 'license': 'Apache-2.0', 'hashes': before}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    if args.manifest.resolve().is_relative_to(args.directory.resolve()):
        parser.error('manifest must be outside model directory')
    result = seal(args.directory)
    with args.manifest.open('x', encoding='utf-8') as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write('\n')


if __name__ == '__main__':
    main()
