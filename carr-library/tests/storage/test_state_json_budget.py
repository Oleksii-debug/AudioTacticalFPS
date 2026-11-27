"""Per-caller JSON byte limits apply to the actual shared descriptor read."""
from contextlib import contextmanager
import json

import pytest

from carr_library import durable_state as state


@pytest.mark.parametrize('maximum', [True, False, 0, -1, 1.0, None, '1024', state.MAX_JSON_BYTES + 1])
def test_invalid_json_budget_is_rejected_before_any_path_access(monkeypatch, maximum):
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid byte budgets must fail before path inspection.')
    monkeypatch.setattr(state, 'local_path', forbidden)
    with pytest.raises(ValueError, match='positive JSON byte budget'):
        state.read_json('not-an-open-path', maximum=maximum)


def test_default_shared_budget_is_preserved_while_a_smaller_caller_limit_is_enforced(tmp_path):
    path = tmp_path / 'state.json'
    value = {'text': 'a' * (1024 * 1024)}
    path.write_text(json.dumps(value), encoding='utf-8')
    before = path.read_bytes()
    assert state.read_json(path) == value
    with pytest.raises(ValueError, match='configured JSON budget'):
        state.read_json(path, maximum=1024 * 1024)
    assert path.read_bytes() == before


def test_explicit_json_budget_bounds_real_growth_during_descriptor_read(tmp_path, monkeypatch):
    path = tmp_path / 'growing.json'
    path.write_bytes(b'{}')
    maximum = 1024 * 1024
    actual_open = state._open_json_file
    observed = []
    class GrowingFile:
        def __init__(self, stream):
            self.stream = stream
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            observed.append(size)
            with path.open('ab') as changed:
                changed.write(b' ' * (maximum + 16))
            return self.stream.read(size)
    @contextmanager
    def open_growing(selected):
        with actual_open(selected) as stream:
            yield GrowingFile(stream)
    monkeypatch.setattr(state, '_open_json_file', open_growing)
    with pytest.raises(ValueError, match='grew beyond'):
        state.read_json(path, maximum=maximum)
    assert observed == [maximum + 1]
    assert path.read_bytes() == b'{}' + b' ' * (maximum + 16)


def test_budget_check_rejects_existing_oversize_before_opening(tmp_path, monkeypatch):
    path = tmp_path / 'too-large.json'
    path.write_bytes(b'{"kept": "unchanged"}')
    def forbidden(*args, **kwargs):
        raise AssertionError('The bounded reader should not open this oversized file.')
    monkeypatch.setattr(state, '_open_json_file', forbidden)
    with pytest.raises(ValueError, match='configured JSON budget'):
        state.read_json(path, maximum=8)
    assert path.read_bytes() == b'{"kept": "unchanged"}'
