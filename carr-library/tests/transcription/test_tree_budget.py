"""Actual filesystem inventory bounds, including Windows link-count metadata."""
from contextlib import contextmanager
import os

import pytest

from carr_library.transcription import TranscriptionBudget, TranscriptionService
from carr_library.transcription import service as module


def test_tree_budget_counts_real_ordinary_files_and_enforces_both_quotas(tmp_path):
    root = tmp_path / 'owned-job'
    (root / 'inputs').mkdir(parents=True)
    (root / 'request.json').write_bytes(b'{}\n')
    (root / 'inputs' / 'recording.wav').write_bytes(b'controlled audio snapshot')
    total = sum(path.stat().st_size for path in root.rglob('*') if path.is_file())
    assert TranscriptionService._tree_budget(root, TranscriptionBudget(), lambda: None) == total
    with pytest.raises(ValueError, match='working-disk budget'):
        TranscriptionService._tree_budget(root, TranscriptionBudget(max_files=1), lambda: None)
    with pytest.raises(ValueError, match='working-disk budget'):
        TranscriptionService._tree_budget(root,
            TranscriptionBudget(max_input_bytes=1, max_job_bytes=total - 1), lambda: None)


def test_tree_budget_refuses_actual_external_hardlink_at_rest(tmp_path):
    root = tmp_path / 'owned-job'
    root.mkdir()
    source = tmp_path / 'external-original.wav'
    source.write_bytes(b'An unrelated original remains exact.')
    retained = root / 'recording.wav'
    os.link(source, retained)
    assert retained.stat().st_nlink == 2
    with pytest.raises(ValueError, match='external links'):
        TranscriptionService._tree_budget(root, TranscriptionBudget(), lambda: None)
    assert source.read_bytes() == b'An unrelated original remains exact.'
    retained.unlink()
    retained.write_bytes(source.read_bytes())
    assert TranscriptionService._tree_budget(root, TranscriptionBudget(), lambda: None) == source.stat().st_size


def test_tree_budget_rechecks_real_link_created_after_directory_metadata_was_cached(tmp_path, monkeypatch):
    root = tmp_path / 'owned-job'
    root.mkdir()
    retained = root / 'request.json'
    retained.write_bytes(b'{}\n')
    with os.scandir(root) as entries:
        cached = tuple(entries)
    # Populate the real DirEntry cache, then create a genuine second name.
    # No fake Windows metadata and no simulated filesystem/link acceptance.
    cached[0].stat(follow_symlinks=False)
    external = tmp_path / 'external-link.json'
    os.link(retained, external)
    assert retained.stat().st_nlink == 2

    @contextmanager
    def already_enumerated(path):
        assert os.fspath(path) == os.fspath(root)
        yield iter(cached)

    monkeypatch.setattr(module.os, 'scandir', already_enumerated)
    with pytest.raises(ValueError, match='external links'):
        TranscriptionService._tree_budget(root, TranscriptionBudget(), lambda: None)
    assert external.read_bytes() == retained.read_bytes() == b'{}\n'
