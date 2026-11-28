"""Compatibility verification with inert PE fixtures; no native process runs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import struct

import pytest

from carr_library.audio import runtime


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def compact(tmp_path, monkeypatch):
    root = tmp_path / 'installation'
    audio = root / 'audio'
    (audio / 'bin').mkdir(parents=True)
    pe = bytearray(134)
    pe[:2] = b'MZ'
    struct.pack_into('<I', pe, 60, 128)
    pe[128:] = b'PE\x00\x00\x64\x86'
    values = {'bin/ffmpeg.exe': bytes(pe), 'bin/ffprobe.exe': bytes(pe) + b'probe',
              'LICENSE': b'Synthetic license.', 'README.txt': b'Synthetic vendor notice.'}
    records = {}
    for name, data in values.items():
        path = audio / name
        path.write_bytes(data)
        records[name] = {'size': len(data), 'sha256': digest(path)}
    lock = tmp_path / 'synthetic-reviewed-lock.json'
    lock.write_text(json.dumps({'output_files': records}), encoding='utf-8')
    (audio / 'provenance.json').write_bytes(lock.read_bytes())
    monkeypatch.setattr(runtime, 'LEGACY_LOCK', lock)
    manifest = {'schema': 1, 'target': 'Windows x64 / CPython 3.11', 'bundled_audio': True,
                'files': {path.relative_to(root).as_posix(): digest(path)
                          for path in audio.rglob('*') if path.is_file()}}
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root, audio, manifest


def test_compact_pack_is_verified_without_implying_missing_player(compact):
    root, audio, _ = compact
    assert runtime._bundle(root) == audio
    path, programs = runtime._bundle(root, with_executables=True)
    assert path == audio and set(programs) == {'ffmpeg.exe', 'ffprobe.exe'}


@pytest.mark.parametrize('change', ['changed', 'missing', 'extra', 'provenance', 'resealed', 'unannounced', 'dual'])
def test_compact_pack_cannot_bypass_source_or_installation_identity(compact, change):
    root, audio, manifest = compact
    target = audio / 'bin' / 'ffmpeg.exe'
    if change in ('changed', 'resealed'):
        target.write_bytes(target.read_bytes() + b'changed')
        if change == 'resealed':
            manifest['files']['audio/bin/ffmpeg.exe'] = digest(target)
    elif change == 'missing':
        target.unlink()
    elif change == 'extra':
        (audio / 'unexpected.dll').write_bytes(b'not approved')
    elif change == 'provenance':
        (audio / 'provenance.json').write_text('{}', encoding='utf-8')
    elif change == 'unannounced':
        manifest['bundled_audio'] = False
    else:
        (root / runtime.DIRECTORY).mkdir()
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises((ValueError, FileNotFoundError)):
        runtime._bundle(root)


def test_compact_activation_never_uses_path_fallback_for_missing_player(compact, monkeypatch):
    root, audio, _ = compact
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    monkeypatch.setattr(runtime, '_active', None)
    monkeypatch.setenv('PATH', 'previous-path')
    handle = runtime.activate_bundled_audio_runtime(root)
    try:
        assert runtime.resolve_active_audio_tool('ffmpeg') == str(audio / 'bin' / 'ffmpeg.exe')
        with pytest.raises(ValueError, match='does not include ffplay'):
            runtime.resolve_active_audio_tool('ffplay')
    finally:
        handle.close()
    assert os.environ['PATH'] == 'previous-path'


def test_linked_compact_runtime_file_is_rejected(compact):
    root, audio, _ = compact
    target = audio / 'bin' / 'ffmpeg.exe'
    os.link(target, root / 'external-copy.exe')
    with pytest.raises(ValueError):
        runtime._bundle(root)
