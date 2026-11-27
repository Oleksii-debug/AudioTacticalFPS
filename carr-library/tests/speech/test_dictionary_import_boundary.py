"""Actual selected bytes, read bounds and cancellation for dictionary review."""
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.ui import pronunciation_dialog as ui


def dictionary(path, count=1):
    rows = [dict(pattern='word' + str(index), replacement='reading ' + 'x' * 1000,
                 regex=False, ignore_case=False, language='', voice_id='')
            for index in range(count)]
    raw = json.dumps(dict(schema='carr.pronunciations.v1', rules=rows)).encode('utf-8')
    path.write_bytes(raw)
    return raw


def intercept_open(monkeypatch, target, transform):
    """Schedule only an owned fixture at the actual before-open boundary."""
    target = target.resolve()
    actual_path_open = Path.open
    actual_descriptor = getattr(ui, '_open_json_file', None)
    def selected_path_open(path, *args, **kwargs):
        if Path(path) == target:
            return transform(lambda: actual_path_open(path, *args, **kwargs))
        return actual_path_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', selected_path_open)
    if actual_descriptor is not None:
        def selected_descriptor(path):
            if Path(path) == target:
                return transform(lambda: actual_descriptor(path))
            return actual_descriptor(path)
        monkeypatch.setattr(ui, '_open_json_file', selected_descriptor)


def test_captured_dictionary_bytes_keep_existing_wire_and_exact_digest(tmp_path):
    selected = tmp_path / 'rules.json'
    raw = dictionary(selected, 2)
    rows, digest = ui.read_dictionary(selected)
    assert len(rows) == 2 and rows[1].pattern == 'word1'
    assert digest == sha256(raw).hexdigest() and selected.read_bytes() == raw


def test_regular_replacement_before_open_is_refused_without_rewriting_either_file(tmp_path, monkeypatch):
    selected = tmp_path / 'rules.json'
    original = dictionary(selected)
    replacement = tmp_path / 'replacement.json'
    changed = original.replace(b'word0', b'other')
    replacement.write_bytes(changed)
    observed = []
    def replace(opened):
        if not observed:
            os.replace(replacement, selected)
            observed.append(True)
        return opened()
    intercept_open(monkeypatch, selected, replace)
    with pytest.raises((ValueError, OSError), match='changed'):
        ui.read_dictionary(selected)
    assert observed == [True] and selected.read_bytes() == changed
    assert not replacement.exists()


def test_growing_actual_dictionary_consumes_at_most_captured_size_plus_one(tmp_path, monkeypatch):
    selected = tmp_path / 'rules.json'
    original = dictionary(selected)
    actual_path_open = Path.open
    reads, grown = [], []
    class GrowingReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, size=-1):
            if not grown:
                with actual_path_open(selected, 'ab') as writer:
                    writer.write(b' ' * 100000); writer.flush(); os.fsync(writer.fileno())
                grown.append(True)
            block = self.stream.read(size)
            reads.append((size, len(block)))
            return block
    intercept_open(monkeypatch, selected, lambda open_file: GrowingReader(open_file()))
    with pytest.raises(ValueError, match='changed'):
        ui.read_dictionary(selected)
    assert grown == [True] and reads
    assert sum(count for _requested, count in reads) <= len(original) + 1
    assert all(0 < requested <= len(original) + 1 for requested, _count in reads)
    assert selected.read_bytes() == original + b' ' * 100000


def test_cancel_between_actual_dictionary_chunks_leaves_all_saved_bytes_unchanged(tmp_path, monkeypatch):
    selected = tmp_path / 'rules.json'
    original = dictionary(selected, 100)
    reads = []
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__(); return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, size=-1):
            block = self.stream.read(size); reads.append(len(block)); return block
    intercept_open(monkeypatch, selected, lambda open_file: Reader(open_file()))
    with pytest.raises(InterruptedError):
        ui.read_dictionary(selected, cancelled=lambda: bool(reads))
    assert len(reads) == 1 and reads[0] <= 64 * 1024
    assert selected.read_bytes() == original
