"""Installed local audio setup uses sealed source snapshots and the app lease."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

import pytest

from carr_library import installation_lease as leases
from carr_library import local_audio_setup as owned
from carr_library import portable_entry

PACKAGING = Path(__file__).resolve().parents[2] / 'packaging'


@pytest.fixture(autouse=True)
def no_network_or_native(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Reviewing local audio setup must not download or execute a tool.')
    monkeypatch.setattr(urllib.request, 'urlopen', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)


@pytest.fixture
def program(tmp_path):
    root = tmp_path / 'installed program Україна'
    root.mkdir()
    (root / '.venv').mkdir()
    (root / '.venv/.installation-complete').touch()
    inventory = {}
    for name in owned.TOOL_FILES:
        target = root / 'tools/audio-setup' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACKAGING / name, target)
        inventory['tools/audio-setup/' + name] = sha256(target.read_bytes()).hexdigest()
    (root / 'bundle-manifest.json').write_text(
        json.dumps({'schema': 1, 'files': inventory}), encoding='utf-8')
    return root


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def test_actual_installed_plan_uses_canonical_helpers_without_writes(
        program, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'prefix', str(program / '.venv'))
    output = tmp_path / 'selected audio directory'
    before, import_path = snapshot(program), list(sys.path)
    assert owned.main(['--output', str(output)]) == 0
    assert snapshot(program) == before
    assert sys.path == import_path
    assert not output.exists()
    text = capsys.readouterr().out
    assert 'Лише план' in text
    assert '95294294' in text


@pytest.mark.parametrize('name', [
    'setup_local_audio.py', 'acquire_audio_runtime.py', 'prepare_audio_runtime.py',
    'audio-runtime/source-lock.json',
])
def test_changed_sealed_source_is_rejected_before_any_source_executes(
        program, monkeypatch, name):
    source = program / 'tools/audio-setup' / name
    source.write_bytes(source.read_bytes() + b'\n# changed after installation\n')
    monkeypatch.setattr(owned, '_module',
                        lambda *_: pytest.fail('Changed source reached the loader.'))
    with pytest.raises(ValueError, match='differs from its installed inventory'):
        owned._setup(program)


def test_helper_path_replacement_after_review_cannot_change_executed_bytes(
        program, tmp_path, monkeypatch, capsys):
    helper = program / 'tools/audio-setup/acquire_audio_runtime.py'
    marker = tmp_path / 'unreviewed-code-ran'
    original = owned._module
    replaced = []

    def replace_before_compilation(name, path, source):
        if name == 'carr_bundled_local_audio_setup':
            changed = helper.with_name('replacement.py')
            changed.write_text(
                'from pathlib import Path\nPath(' + repr(str(marker))
                + ').write_text("unreviewed")\nraise RuntimeError("unreviewed helper")\n',
                encoding='utf-8')
            changed.replace(helper)
            replaced.append(True)
        return original(name, path, source)

    monkeypatch.setattr(owned, '_module', replace_before_compilation)
    setup = owned._setup(program)
    assert replaced == [True]
    output = tmp_path / 'no output'
    assert setup.main(['--output', str(output)]) == 0
    assert 'Лише план' in capsys.readouterr().out
    assert not marker.exists() and not output.exists()
    with pytest.raises(ValueError, match='differs from its installed inventory'):
        owned._setup(program)


def test_inventory_replacement_during_snapshot_cannot_certify_sources(program, monkeypatch):
    manifest = program / 'bundle-manifest.json'
    original = portable_entry._read

    def mutate_after_read(path, maximum):
        raw = original(path, maximum)
        if path.name == 'source-lock.json':
            manifest.write_bytes(manifest.read_bytes() + b'\n')
        return raw

    monkeypatch.setattr(portable_entry, '_read', mutate_after_read)
    monkeypatch.setattr(owned, '_module',
                        lambda *_: pytest.fail('Changed inventory reached the loader.'))
    with pytest.raises(ValueError, match='inventory changed'):
        owned._setup(program)


@pytest.mark.parametrize('state', ['exclusive-maintenance', 'uncertain-bootstrap'])
def test_owned_setup_refuses_managed_maintenance_before_loading_helpers(
        program, tmp_path, monkeypatch, capsys, state):
    managed = tmp_path / 'managed'
    managed.mkdir()
    root = managed / 'app'
    program.rename(root)
    (managed / leases.MARKER).write_bytes(b'Fixture managed installation identity')
    monkeypatch.setattr(sys, 'prefix', str(root / '.venv'))
    monkeypatch.setattr(owned, '_setup',
                        lambda *_: pytest.fail('Local setup bypassed installation ownership.'))
    if state == 'uncertain-bootstrap':
        (managed / leases.BOOTSTRAP_OWNER).write_bytes(b'uncertain: do not reset')
        assert owned.main(['--output', str(tmp_path / 'external')]) == 1
        assert (managed / leases.BOOTSTRAP_OWNER).read_bytes() == b'uncertain: do not reset'
    else:
        with leases.installation_lease(managed, exclusive=True):
            assert owned.main(['--output', str(tmp_path / 'external')]) == 1
    assert capsys.readouterr().err


def test_owned_launcher_uses_fixed_admitted_callback_and_legacy_launcher_remains_supported():
    owned_command = (PACKAGING / 'SetupLocalAudioOwned.cmd').read_text(encoding='utf-8')
    legacy_command = (PACKAGING / 'SetupLocalAudio.cmd').read_text(encoding='utf-8')
    assert ('"%~dp0.venv\\Scripts\\python.exe" -I -B -X utf8 '
            '-m carr_library.local_audio_setup --interactive') in owned_command
    assert ('"%~dp0.venv\\Scripts\\python.exe" -I -B -X utf8 '
            '"%~dp0tools\\audio-setup\\setup_local_audio.py" --interactive') in legacy_command
    assert ('carr_library.local_audio_setup', 'main') in portable_entry._CALLBACKS
    assert 'local_audio_setup.py' in portable_entry.OWNED_SOURCES
