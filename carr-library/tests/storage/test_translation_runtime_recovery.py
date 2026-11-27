"""Pure engine metadata validation and actual complete-backup preservation."""
import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.state_recovery import _prepared


def settings(digest):
    return {'schema': 1, 'ffmpeg': r'C:\local-audio\bin\ffmpeg.exe',
            'ffprobe': r'C:\local-audio\bin\ffprobe.exe', 'piper': [],
            'voice_helpers': [], 'music_models': [], 'fluidsynth': None,
            'audio_runtime_manifest_sha256': digest}


@pytest.mark.parametrize('digest', [None, True, 12, 'A' * 64, '0' * 63, 'g' * 64],
                         ids=['null', 'boolean', 'integer', 'uppercase', 'short', 'nonhex'])
def test_restored_engine_manifest_binding_is_strict(digest):
    with pytest.raises(ValueError):
        _prepared('audio/engines.json', settings(digest))


def test_complete_backup_preserves_full_audio_manifest_binding_but_revokes_activation(tmp_path):
    app = LibraryApp(tmp_path / 'original')
    workspace = app.create_workspace('Retained engine provenance')
    app.add_text(workspace, 'Small original', 'Exact original text.')
    original = settings('a' * 64)
    atomic_json(app.root / 'state/audio/engines.json', original)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    target = restore_backup(backup, tmp_path / 'restored', digest)
    restored = read_json(target / 'state/audio/engines.json')
    assert restored == {**original, 'restore_requires_revalidation': True}
    assert read_json(app.root / 'state/audio/engines.json') == original


@pytest.mark.parametrize('change', [
    {'ffmpeg': None},
    {'ffprobe': 'bin/ffprobe.exe'},
    {'ffprobe': r'C:\other-audio\bin\ffprobe.exe'},
    {'ffmpeg': r'C:\local-audio\bin\other.exe'},
    {'ffmpeg': r'C:\local-audio\tools\ffmpeg.exe', 'ffprobe': r'C:\local-audio\tools\ffprobe.exe'},
], ids=['missing-executable', 'relative-executable', 'different-pack', 'wrong-program', 'not-bin'])
def test_restored_manifest_digest_stays_bound_to_one_declared_full_pack(change):
    value = {**settings('b' * 64), **change}
    with pytest.raises(ValueError):
        _prepared('audio/engines.json', value)
