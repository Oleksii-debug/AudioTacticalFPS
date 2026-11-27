"""Local setup orchestration uses the real helpers with synthetic public inputs.

No test downloads or executes a native binary. The tiny PE files only exercise
the existing checksum/archive/architecture verifier and are not working tools.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace
import urllib.request
import zipfile

import pytest

from carr_library import audio_runtime

PACKAGING = Path(__file__).resolve().parents[2] / 'packaging'


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('A local-setup test attempted a real network request.')
    monkeypatch.setattr(urllib.request, 'urlopen', denied)


@pytest.fixture
def setup():
    path = PACKAGING / 'setup_local_audio.py'
    spec = importlib.util.spec_from_file_location('carr_test_local_audio_setup', path)
    module = importlib.util.module_from_spec(spec)
    # Load the script without creating bytecode in a sealed installed fixture.
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


@pytest.fixture
def synthetic(setup, tmp_path, monkeypatch):
    root = tmp_path / 'installed program Україна'
    helpers = root / 'tools' / 'audio-setup'
    templates = helpers / 'audio-runtime'
    templates.mkdir(parents=True)
    for name in ('acquire_audio_runtime.py', 'prepare_audio_runtime.py'):
        shutil.copyfile(PACKAGING / name, helpers / name)
    (root / 'bundle-manifest.json').write_bytes(b'untouched installation seal\n')
    settings = tmp_path / 'library' / 'state' / 'audio' / 'engines.json'
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"schema":1,"ffmpeg":null,"ffprobe":null}\n')
    pe = bytearray(256)
    pe[:2] = b'MZ'
    struct.pack_into('<I', pe, 60, 128)
    pe[128:132] = b'PE\0\0'
    struct.pack_into('<H', pe, 132, 0x8664)
    payload = {name: bytes(pe) for name in audio_runtime.TOOLS.values()}
    payload['bin/fixture.dll'] = bytes(pe)
    payload['LICENSE.txt'] = b'Synthetic fixture only; not a redistributable runtime.\n'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, raw in payload.items():
            archive.writestr('fixture/' + name, raw)
    sources = {'source-one.tar.gz': b'synthetic source one',
               'source-two.tar.gz': b'synthetic source two'}
    blobs = {'fixture.zip': buffer.getvalue(), **sources}
    records = [{'name': name, 'url': 'https://fixture.invalid/' + name,
                'size': len(raw), 'sha256': _digest(raw)} for name, raw in blobs.items()]
    notice = b'Synthetic local setup notice retained byte for byte.\n'
    (templates / 'README.txt').write_bytes(notice)
    extra = {'README.txt': {'size': len(notice), 'sha256': _digest(notice)}}
    extra.update({'sources/' + name: {'size': len(raw), 'sha256': _digest(raw)}
                  for name, raw in sources.items()})
    lock = {'schema': 1, 'package_id': audio_runtime.PACKAGE_ID, 'target': 'Windows x64',
            'version': 'synthetic-only', 'executables': audio_runtime.TOOLS,
            'archive': dict(records[0], root='fixture'), 'source_packages': records[1:],
            'archive_files': {name: {'size': len(raw), 'sha256': _digest(raw)}
                              for name, raw in payload.items()}, 'extra_files': extra}
    raw_lock = (json.dumps(lock, sort_keys=True) + '\n').encode()
    (templates / 'source-lock.json').write_bytes(raw_lock)
    monkeypatch.setattr(audio_runtime, 'SOURCE_LOCK_SHA256', _digest(raw_lock))
    monkeypatch.setattr(setup, 'HERE', helpers)
    # The integration fixture verifies Windows-format bytes on either host. It
    # does not claim to execute Windows tools or qualify their audio behavior.
    monkeypatch.setattr(setup, '_require_windows', lambda: None)
    calls = []

    class Response(io.BytesIO):
        def geturl(self):
            return self.url

    def receive(request, timeout):
        calls.append((request.full_url, timeout))
        name = request.full_url.rsplit('/', 1)[1]
        value = Response(blobs[name])
        value.url = request.full_url
        return value

    monkeypatch.setattr(urllib.request, 'urlopen', receive)
    return SimpleNamespace(root=root, helpers=helpers, templates=templates,
                           settings=settings, blobs=blobs, records=records,
                           calls=calls, notice=notice, output=tmp_path / 'new audio ž Україна')


def test_default_real_pinned_plan_has_no_network_or_writes(setup, tmp_path, monkeypatch, capsys):
    base = tmp_path / 'local app data'
    monkeypatch.setenv('LOCALAPPDATA', str(base))
    path_before = list(sys.path)
    acquire = setup._load_helper('acquire_audio_runtime')
    records = acquire.inputs()
    assert len(records) == 3
    assert sum(row['size'] for row in records) == 95294294
    assert all(len(row['sha256']) == 64 and row['url'].startswith('https://') for row in records)
    assert setup.main([]) == 0
    assert sys.path == path_before
    assert not base.exists()
    assert 'Лише план' in capsys.readouterr().out


@pytest.mark.parametrize('mode', ['download', 'interactive'])
def test_real_helpers_prepare_and_verify_external_runtime_without_mutating_installation(
        setup, synthetic, monkeypatch, capsys, mode):
    before, settings = _snapshot(synthetic.root), synthetic.settings.read_bytes()
    monkeypatch.setattr('builtins.input', lambda prompt: 'YES')
    def no_native(*args, **kwargs):
        pytest.fail('Setup must not execute a native tool.')
    monkeypatch.setattr(subprocess, 'Popen', no_native)
    args = ['--output', str(synthetic.output), '--' + mode]
    assert setup.main(args) == 0
    report = audio_runtime.verify_audio_runtime(synthetic.output / 'runtime')
    assert report['native_execution'] == 'NOT_RUN'
    assert [url for url, _ in synthetic.calls] == [r['url'] for r in synthetic.records]
    assert all(timeout == 45 for _, timeout in synthetic.calls)
    assert (synthetic.output / 'runtime' / 'README.txt').read_bytes() == synthetic.notice
    for name in ('source-one.tar.gz', 'source-two.tar.gz'):
        assert (synthetic.output / 'runtime' / 'sources' / name).read_bytes() == synthetic.blobs[name]
    assert _snapshot(synthetic.root) == before
    assert synthetic.settings.read_bytes() == settings
    out = capsys.readouterr().out
    for name in ('ffmpeg', 'ffprobe'):
        assert report['tools'][name] in out
    assert 'Choose FFmpeg executable' in out
    assert 'Choose FFprobe executable' in out


@pytest.mark.parametrize('answer', [None, '', 'NO', 'yes'])
def test_plan_or_declined_confirmation_is_read_only(setup, synthetic, monkeypatch, answer):
    before = _snapshot(synthetic.root)
    monkeypatch.setattr('builtins.input', lambda prompt: answer)
    args = ['--output', str(synthetic.output)]
    if answer is not None:
        args.append('--interactive')
    assert setup.main(args) == 0
    assert synthetic.calls == []
    assert not synthetic.output.exists()
    assert _snapshot(synthetic.root) == before


def test_closed_confirmation_input_does_not_start_setup(setup, synthetic, monkeypatch):
    def eof(prompt):
        raise EOFError
    monkeypatch.setattr('builtins.input', eof)
    assert setup.main(['--interactive', '--output', str(synthetic.output)]) == 0
    assert synthetic.calls == []
    assert not synthetic.output.exists()


@pytest.mark.parametrize('kind', ['inside', 'existing', 'relative', 'network', 'traversal'])
def test_invalid_or_existing_output_is_preserved(setup, synthetic, tmp_path, kind):
    sentinel = tmp_path / 'existing'
    sentinel.mkdir()
    (sentinel / 'keep.txt').write_bytes(b'keep exactly')
    output = {'inside': synthetic.root / 'new audio',
              'existing': sentinel, 'relative': Path('relative-folder'),
              'network': Path('//server/share/audio'),
              'traversal': tmp_path / '..' / 'another-folder'}[kind]
    before = _snapshot(synthetic.root)
    assert setup.main(['--download', '--output', str(output)]) == 1
    assert synthetic.calls == []
    assert (sentinel / 'keep.txt').read_bytes() == b'keep exactly'
    assert _snapshot(synthetic.root) == before


def test_reparse_parent_is_rejected_without_download(setup, synthetic, tmp_path, monkeypatch):
    parent = tmp_path / 'reparse'
    parent.mkdir()
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == parent:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400,
                                   st_nlink=info.st_nlink)
        return info
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert setup.main(['--download', '--output', str(parent / 'new')]) == 1
    assert synthetic.calls == []
    assert not (parent / 'new').exists()


def test_output_created_during_confirmation_is_never_overwritten(setup, synthetic, monkeypatch):
    def confirm(prompt):
        synthetic.output.mkdir()
        (synthetic.output / 'keep.txt').write_bytes(b'created during confirmation')
        return 'YES'
    monkeypatch.setattr('builtins.input', confirm)
    assert setup.main(['--interactive', '--output', str(synthetic.output)]) == 1
    assert synthetic.calls == []
    assert (synthetic.output / 'keep.txt').read_bytes() == b'created during confirmation'


def test_changed_source_lock_during_confirmation_fails_before_writes(setup, synthetic, monkeypatch):
    def confirm(prompt):
        path = synthetic.templates / 'source-lock.json'
        path.write_bytes(path.read_bytes() + b' ')
        return 'YES'
    monkeypatch.setattr('builtins.input', confirm)
    assert setup.main(['--interactive', '--output', str(synthetic.output)]) == 1
    assert synthetic.calls == []
    assert not synthetic.output.exists()


def test_wrong_download_hash_never_produces_ready_runtime(setup, synthetic, capsys):
    raw = synthetic.blobs['fixture.zip']
    synthetic.blobs['fixture.zip'] = bytes([raw[0] ^ 1]) + raw[1:]
    before = _snapshot(synthetic.root)
    assert setup.main(['--download', '--output', str(synthetic.output)]) == 1
    assert not (synthetic.output / 'runtime').exists()
    assert list((synthetic.output / 'inputs').iterdir()) == []
    assert _snapshot(synthetic.root) == before
    assert 'Підготовку завершено' not in capsys.readouterr().out


def test_published_files_are_reverified_before_success(setup, synthetic, monkeypatch, capsys):
    real_load = setup._load_helper
    def load(name):
        module = real_load(name)
        if name == 'prepare_audio_runtime':
            real_prepare = module.prepare
            def changed(archive, sources, output):
                report = real_prepare(archive, sources, output)
                (output / 'bin' / 'ffmpeg.exe').write_bytes(b'changed after preparation')
                return report
            module.prepare = changed
        return module
    monkeypatch.setattr(setup, '_load_helper', load)
    assert setup.main(['--download', '--output', str(synthetic.output)]) == 1
    assert 'Підготовку завершено' not in capsys.readouterr().out


def test_unsupported_host_stops_before_download_or_creation(setup, synthetic, monkeypatch):
    def unsupported():
        raise ValueError('Windows x64 required')
    monkeypatch.setattr(setup, '_require_windows', unsupported)
    assert setup.main(['--download', '--output', str(synthetic.output)]) == 1
    assert synthetic.calls == []
    assert not synthetic.output.exists()


def test_launcher_requires_completed_installation_and_isolated_no_bytecode_python():
    text = (PACKAGING / 'SetupLocalAudio.cmd').read_text(encoding='utf-8')
    assert '".venv' not in text  # Every executable path is anchored to the launcher.
    assert 'if not exist "%~dp0.venv\\Scripts\\python.exe" goto not_installed' in text
    assert 'if not exist "%~dp0.venv\\.installation-complete" goto not_installed' in text
    assert ('"%~dp0.venv\\Scripts\\python.exe" -I -B -X utf8 '
            '-m carr_library.local_audio_setup --interactive') in text
    assert '"%~dp0tools\\audio-setup\\setup_local_audio.py"' not in text
    assert 'exit /b %audio_setup_status%' in text

