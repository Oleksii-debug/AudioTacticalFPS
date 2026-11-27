"""Native format5 and supported flat4 retain pinned helpers as passive declarations."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess

import pytest

from carr_library import runtime_settings
from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech import _io as speech_io
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION


NAME = 'state/audio/engines.json'
BINDINGS = {'fixture.helper': 'Ctrl+F9'}
FLAT = runpy.run_path(str(Path(__file__).with_name('test_canonical_backup_compat.py')))


def configuration(script, *, enabled=True):
    value = {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
             'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    value['voice_helpers'] = [
        {'executable': r'C:\Synthetic uninstalled Python\python.exe', 'sha256': 'a' * 64,
         'engine_name': 'Explicit synthetic helper', 'enabled': enabled,
         'script': script, 'script_sha256': 'b' * 64},
        {'executable': '/synthetic-uninstalled-engines/bare-helper', 'sha256': 'c' * 64,
         'engine_name': 'Historical bare helper', 'enabled': False},
    ]
    return value


def library(tmp_path, value):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pinned Python helper')
    app.add_text(workspace, 'Original', 'Original source retained; helper metadata is passive.')
    KeybindingStore(app.root).save([Command('fixture.helper', 'Synthetic helper fixture command')], BINDINGS)
    atomic_json(app.root / NAME, value)
    return app, workspace


def snapshot_files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def backup(app, destination, flat):
    if flat:
        return FLAT['_flat_backup'](app, destination)
    digest = create_backup(app, destination)
    return read_json(destination / 'manifest.json'), digest


def reseal(folder, manifest, value, flat):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (folder / NAME).write_bytes(raw)
    collection = manifest['sidecars'] if flat else manifest['state']
    filename = NAME if flat else 'audio/engines.json'
    entry, = [row for row in collection if row['filename'] == filename]
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(manifest_raw)
    return sha256(manifest_raw).hexdigest()


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('script', [r'C:\Synthetic voice bridge\міст.py',
                                    '/synthetic-uninstalled-engines/міст.py'],
                         ids=['windows-declaration', 'posix-declaration'])
@pytest.mark.parametrize('enabled', [True, False], ids=['enabled', 'disabled'])
def test_pinned_python_helper_roundtrips_passively(tmp_path, monkeypatch, script, enabled, flat):
    value = configuration(script, enabled=enabled)
    app, workspace = library(tmp_path, value)
    original = (app.root / NAME).read_bytes()
    keymap = KeybindingStore(app.root).path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Passive backup or restored activation inspected or ran an external helper.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(speech_io, 'local_file', forbidden)
    folder = tmp_path / 'backup'
    manifest, checksum = backup(app, folder, flat)
    assert manifest['version'] == (4 if flat else 5)
    assert manifest['schema_version'] == CURRENT_SCHEMA_VERSION == 6
    assert (folder / NAME).read_bytes() == original
    before = snapshot_files(folder)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    expected = {**value, 'restore_requires_revalidation': True}
    assert read_json(restored / NAME) == expected
    assert runtime_settings.load_settings(restored / 'state') == expected
    assert (app.root / NAME).read_bytes() == original
    assert snapshot_files(folder) == before
    assert KeybindingStore(restored).load() == BINDINGS
    assert KeybindingStore(restored).path.read_bytes() == keymap
    assert LibraryApp(restored).search(workspace, 'retained')
    with pytest.raises(ValueError, match='explicit revalidation'):
        runtime_settings.voice_registry(restored / 'state')


@pytest.mark.parametrize('fields', [
    {'script': '/synthetic-uninstalled-engines/bridge.py'},
    {'script_sha256': '1' * 64},
    {'script': 'https://example.invalid/bridge.py', 'script_sha256': '1' * 64},
    {'script': r'\\server\share\bridge.py', 'script_sha256': '1' * 64},
    {'script': r'C:\Synthetic\bridge.py:stream', 'script_sha256': '1' * 64},
    {'script': '/synthetic-uninstalled-engines/bridge.py', 'script_sha256': 'A' * 64},
    {'script': '/synthetic-uninstalled-engines/bridge.py', 'script_sha256': True},
    {'script': '', 'script_sha256': '1' * 64},
    {'script': None, 'script_sha256': None},
    {'script': '../relative-bridge.py', 'script_sha256': '1' * 64},
], ids=['missing-hash', 'missing-script', 'url', 'unc', 'stream', 'uppercase-hash',
        'boolean-hash', 'empty-script', 'null-pair', 'relative-script'])
def test_invalid_pinned_python_helper_fails_closed(tmp_path, fields):
    value = configuration('/synthetic-uninstalled-engines/bridge.py')
    value['voice_helpers'][0].pop('script')
    value['voice_helpers'][0].pop('script_sha256')
    value['voice_helpers'][0].update(fields)
    app, _ = library(tmp_path, value)
    original = (app.root / NAME).read_bytes()
    destination = tmp_path / 'must-not-publish'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists()
    assert (app.root / NAME).read_bytes() == original


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('change', ['missing-script', 'missing-hash', 'invalid-hash', 'relative-script'])
def test_resigned_pinned_helper_restore_rejects_invalid_pair(tmp_path, flat, change):
    value = configuration('/synthetic-uninstalled-engines/bridge.py')
    app, _ = library(tmp_path, value)
    original = (app.root / NAME).read_bytes()
    folder = tmp_path / 'backup'
    manifest, _ = backup(app, folder, flat)
    value = deepcopy(value)
    if change == 'missing-script':
        value['voice_helpers'][0].pop('script')
    elif change == 'missing-hash':
        value['voice_helpers'][0].pop('script_sha256')
    elif change == 'invalid-hash':
        value['voice_helpers'][0]['script_sha256'] = 'A' * 64
    else:
        value['voice_helpers'][0]['script'] = '../moved.py'
    checksum = reseal(folder, manifest, value, flat)
    before = snapshot_files(folder)
    target = tmp_path / 'must-not-publish'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(folder, target, checksum)
    assert not target.exists()
    assert snapshot_files(folder) == before
    assert (app.root / NAME).read_bytes() == original
    assert not list(tmp_path.glob('.carr-library-restore-*'))
