"""Acceptance admission is tested with small data; the large lane writes real PCM."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import carr_library


def harness():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'legacy_playlist_acceptance.py'
    spec = importlib.util.spec_from_file_location('legacy_playlist_acceptance_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('mib', [513, 1024, 3072])
def test_explicit_physical_queue_budget_covers_all_concurrent_copies(mib):
    module = harness()
    size = mib * module.MIB
    assert module.estimate_disk_bytes(size) == 8 * (size + 44) + 2 * module.GIB


def test_actual_harness_refuses_disk_shortage_before_any_pcm_or_library_is_created(tmp_path, monkeypatch):
    module = harness()
    work = tmp_path / 'not-created'
    selected = 513 * module.MIB
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(
        free=module.estimate_disk_bytes(selected) - 1))
    monkeypatch.setattr(module, '_physical_pcm',
                        lambda *args: (_ for _ in ()).throw(AssertionError('Unadmitted producer ran.')))
    with pytest.raises(OSError, match='before producing data'):
        module.main(['--workdir', str(work), '--output', str(tmp_path / 'report.json'),
                     '--expected-package', carr_library.__file__, '--mib', '513'])
    assert not work.exists() and not (tmp_path / 'report.json').exists()


def test_physical_fixture_writer_streams_real_complete_nonempty_pcm(tmp_path):
    module = harness()
    path = tmp_path / 'physical' / 'fixture.wav'
    module._physical_pcm(path, 2 * module.MIB, lambda: False)
    import wave
    with wave.open(str(path), 'rb') as audio:
        assert audio.getnframes() == module.MIB
        assert audio.getsampwidth() == 2 and audio.getnchannels() == 1
        assert audio.readframes(128) == bytes(range(256))
    size, digest = module._hash(path, lambda: False)
    assert size == 2 * module.MIB + 44 and len(digest) == 64
    assert path.stat().st_nlink == 1


def test_physical_fixture_creation_preserves_existing_files_and_honours_cancellation(tmp_path):
    module = harness()
    path = tmp_path / 'owned' / 'fixture.wav'
    path.parent.mkdir()
    path.write_bytes(b'Existing user data.')
    with pytest.raises(FileExistsError):
        module._physical_pcm(path, module.MIB, lambda: False)
    assert path.read_bytes() == b'Existing user data.'
    def stopped():
        raise InterruptedError('Stopped actual producer.')
    target = tmp_path / 'cancelled' / 'fixture.wav'
    with pytest.raises(InterruptedError, match='Stopped actual'):
        module._physical_pcm(target, module.MIB, stopped)
    assert target.stat().st_size < module.MIB
