"""Actual procedural music and input-only recovery through the shared backup."""
from hashlib import sha256
from pathlib import Path
import struct
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.ffmpeg import find_local_tool, wav_info
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.ui.audio_studio import StudioController


def _script(name):
    return ('[output sample_rate=8000 stems=false]\n'
            '[music mood=calm tempo=90 gain=0.15 seed=7]\n'
            '[sound "' + name + '"]\n[pause 0.25]')


def _input_wave(path, amplitude):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(struct.pack('<h', amplitude if i % 20 < 10 else -amplitude)
                                   for i in range(8000)))
    assert wav_info(path).frames == 8000


@pytest.mark.parametrize('include_generated', [True, False], ids=['full-audio', 'required-inputs'])
def test_real_music_and_recovery_reopen_after_original_library_is_disconnected(tmp_path, include_generated):
    # Both source and installed Windows gates provide a verified FFmpeg runtime.
    # A missing engine must fail this acceptance case, rather than turn green.
    ffmpeg = find_local_tool('ffmpeg')
    assert ffmpeg is not None and Path(ffmpeg).is_file(), 'The acceptance gate requires actual FFmpeg.'
    app = LibraryApp(tmp_path / 'library')
    studio = StudioController(private_root=app.root, ffmpeg=ffmpeg)
    studio.new(_script('original.bin'), title='Actual music recovery', input_kind='script')
    source = studio.save_in_library()
    assets = studio.output_dir / 'assets'
    assets.mkdir()
    for index, name in enumerate(('original.bin', 'recovery.WAV')):
        _input_wave(assets / name, 2000 + index * 500)

    # No spoken Line events or fake speech engine: the actual procedural
    # composer and FFmpeg create the checkpoint/cache/render link groups.
    before = studio.render()
    before_hash = sha256(Path(before['output']).read_bytes()).hexdigest()
    studio.edit_script(_script('recovery.WAV'))
    recovery = studio.save_recovery()
    source_bytes, recovery_bytes = source.read_bytes(), recovery.read_bytes()
    input_bytes = {path.name: path.read_bytes() for path in assets.iterdir()}
    for relative in ('models/private.bin', 'profiles/private.json', 'unrelated.txt'):
        path = studio.output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'SYNTHETIC EXCLUDED MATERIAL')

    groups = {}
    for path in studio.output_dir.rglob('*'):
        if path.is_file() and path.stat().st_nlink > 1:
            info = path.stat()
            groups.setdefault((info.st_dev, info.st_ino), []).append(path.relative_to(studio.output_dir).as_posix())
    assert any(any(name.startswith('music-jobs/') for name in group)
               and any(name.startswith('cache/') for name in group) for group in groups.values())
    assert any(any(name.endswith('score.partial.mid') for name in group)
               and any(name.startswith('renders/') for name in group) for group in groups.values())

    snapshot = tmp_path / 'backup'
    digest = create_backup(app, snapshot, include_generated=include_generated)
    manifest = verify_backup(snapshot, digest)
    assert manifest['generated_audio_included'] is include_generated
    names = {row['filename'] for row in manifest['sidecars']}
    assert not any('/models/' in name or '/profiles/' in name or name.endswith('/unrelated.txt') for name in names)
    assert all((snapshot / name).stat().st_nlink == 1 for name in names)
    if include_generated:
        assert any('/audio/music-jobs/' in name for name in names)
        assert any('/audio/renders/' in name for name in names)
    else:
        assert len(names) == 4  # Saved project, recovery draft and both original inputs.
        assert all('/studio/' in name or '/audio/assets/' in name for name in names)

    relative = source.relative_to(app.root)
    original_root = app.root
    disconnected = tmp_path / 'original-library-disconnected'
    original_root.rename(disconnected)
    restored = restore_backup(snapshot, tmp_path / 'restored', digest)
    restored_source = restored / relative
    assert restored_source.read_bytes() == source_bytes
    restored_recovery = restored_source.with_name(restored_source.name + '.recovery.json')
    assert restored_recovery.read_bytes() == recovery_bytes
    assert all((restored / name).stat().st_nlink == 1 for name in names)

    reopened = StudioController(private_root=restored, ffmpeg=ffmpeg).open(restored_source)
    assert reopened.output_dir.is_relative_to(restored) and reopened.output_dir.is_dir()
    assert {name: (reopened.output_dir / 'assets' / name).read_bytes() for name in input_bytes} == input_bytes
    resumed = reopened.render()
    assert Path(resumed['output']).is_relative_to(restored)
    if include_generated:
        assert sha256(Path(resumed['output']).read_bytes()).hexdigest() == before_hash
    else:
        # Omitting generated derivatives requests recomposition; input bytes
        # and source history remain exact without promising bitwise synthesis.
        assert resumed['frames'] == wav_info(Path(resumed['output'])).frames == 10000
        with wave.open(str(resumed['output']), 'rb') as stream:
            assert any(stream.readframes(stream.getnframes()))
    reopened.recover()
    assert reopened.script == _script('recovery.WAV')
    assert reopened.output_dir.is_relative_to(restored)
    recovered = reopened.render()
    assert Path(recovered['output']).is_relative_to(restored)
    assert recovered['frames'] == wav_info(Path(recovered['output'])).frames == 10000
    assert restored_source.read_bytes() == source_bytes
    assert restored_recovery.read_bytes() == recovery_bytes
    assert not original_root.exists()
    assert (disconnected / relative).read_bytes() == source_bytes
    assert (disconnected / relative).with_name(relative.name + '.recovery.json').read_bytes() == recovery_bytes
