"""Real CLI editing, rendering and portable recovery over the shared ClipEditor.

Audio and recording receipts here are clearly synthetic fixtures; no microphone,
voice model, Google account or other external service is used.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave

import pytest

import carr_library
from carr_library.application import LibraryApp
from carr_library.audio.recording import RecordingOptions, RecordingService
from carr_library.durable_state import atomic_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.render import ProductionService


def cli(root, *arguments):
    environment = dict(os.environ, PYTHONIOENCODING='ascii', PYTHONUTF8='1',
                       PYTHONDONTWRITEBYTECODE='1')
    package = Path(carr_library.__file__).resolve().parent
    if 'site-packages' in {part.casefold() for part in package.parts}:
        environment.pop('PYTHONPATH', None)
    else:
        # Source bootstrap is a sibling of src, not a carr_library subpackage.
        # Select both exact checkout roots; installed invocation stays isolated.
        checkout = package.parent.parent
        assert (checkout / 'bootstrap' / 'vendor' / 'cairn_extractors' / 'hasher.py').is_file()
        environment['PYTHONPATH'] = os.pathsep.join((str(package.parent), str(checkout)))
    return subprocess.run([sys.executable, '-B', '-m', 'carr_library', 'studio',
                           '--data-dir', str(root), *map(str, arguments)],
                          cwd=root.parent, env=environment, capture_output=True,
                          text=True, encoding='utf-8', timeout=90)


def success(root, *arguments):
    result = cli(root, *arguments)
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def tone(path):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 24000, 0, 'NONE', 'synthetic CLI tone'))
        output.writeframes(b''.join(struct.pack('<h', round(10000 * math.sin(
            2 * math.pi * 440 * index / 24000))) for index in range(19200)))
    return path


@pytest.fixture
def audio_tools():
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Actual clip CLI acceptance requires local FFmpeg and FFprobe.')


def create_clip(root, source):
    project = success(root, 'clip-create', '--title', 'Кліпи — exact Unicode')
    assert project['title'] == 'Кліпи — exact Unicode'
    scene = project['scenes'][0]['id']
    project = success(root, 'clip-add', project['id'], scene, '--file', source,
                      '--trim-start', '.1', '--duration', '.4', '--fade-in', '.04',
                      '--fade-out', '.04', '--clip-title', 'Вибраний звук')
    return project, scene, project['scenes'][0]['cues'][0]['id']


def test_actual_cli_retains_edits_previews_and_renders_without_the_external_original(tmp_path, audio_tools):
    root, selected = tmp_path / 'Бібліотека', tone(tmp_path / 'selected.wav')
    original = selected.read_bytes()
    project, scene, clip = create_clip(root, selected)
    cue = project['scenes'][0]['cues'][0]
    assert cue['path'] == 'inputs/' + sha256(original).hexdigest() + '.audio'
    assert selected.read_bytes() == original
    project = success(root, 'clip-edit', project['id'], scene, clip, '--revision', project['revision'],
                      '--duration', '.2', '--pan', '1', '--gain-db', '-3')
    preview = success(root, 'clip-preview', project['id'], scene, clip)
    assert preview['playback'] == 'not-started'
    with wave.open(preview['path'], 'rb') as stream:
        assert stream.getnchannels() == 2
        assert stream.getnframes() / stream.getframerate() == pytest.approx(.2, abs=2 / stream.getframerate())
        samples = struct.unpack('<' + 'h' * stream.getnframes() * 2, stream.readframes(stream.getnframes()))
    left, right = samples[::2], samples[1::2]
    assert max(map(abs, left)) < 5 and max(map(abs, right)) > 1000
    selected.unlink()
    output = tmp_path / 'outputs'
    output.mkdir()
    rendered = success(root, 'render', '--project-id', project['id'], '--output', output)
    assert rendered['status'] == 'complete'
    assert rendered['result']['clips'][0]['source_sha256'] == sha256(original).hexdigest()
    assert (Path(rendered['destination']) / 'master.wav').is_file()
    assert (Path(rendered['destination']) / 'stems' / 'clips' / 'sfx.wav').is_file()
    retained = root / 'state' / 'studio' / cue['path']
    assert retained.read_bytes() == original and not selected.exists()


def test_actual_cli_restore_requires_new_output_and_works_after_all_old_sources_are_deleted(tmp_path, audio_tools):
    root, selected = tmp_path / 'original-library', tone(tmp_path / 'original.wav')
    project, scene, clip = create_clip(root, selected)
    output = tmp_path / 'original-outputs'
    output.mkdir()
    completed = success(root, 'render', '--project-id', project['id'], '--output', output)
    original_master = (Path(completed['destination']) / 'master.wav').read_bytes()
    app = LibraryApp(root)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    selected.unlink()
    shutil.rmtree(output)
    shutil.rmtree(root)
    restored = restore_backup(backup, tmp_path / 'Відновлена бібліотека', expected_manifest_sha256=digest)
    jobs = success(restored, 'jobs')
    assert len(jobs) == 1 and jobs[0]['requires_output_directory'] is True
    refused = cli(restored, 'resume', completed['id'])
    assert refused.returncode == 1 and 'output' in refused.stderr.lower()
    assert not root.exists() and not output.exists() and not selected.exists()
    new_output = tmp_path / 'chosen-new-outputs'
    new_output.mkdir()
    resumed = success(restored, 'resume', completed['id'], '--output', new_output)
    assert resumed['status'] == 'complete'
    master = Path(resumed['destination']) / 'master.wav'
    assert master.parent.parent == new_output and master.read_bytes() == original_master
    assert not root.exists() and not output.exists() and not selected.exists()


def test_actual_cli_rejects_invalid_controls_and_stale_revision_without_changing_the_project(tmp_path, audio_tools):
    root, selected = tmp_path / 'library', tone(tmp_path / 'selected.wav')
    original = selected.read_bytes()
    project = success(root, 'clip-create')
    scene = project['scenes'][0]['id']
    refused = cli(root, 'clip-add', project['id'], scene, '--file', selected, '--duration', '-1')
    assert refused.returncode == 1
    assert success(root, 'projects') == [project]
    assert not list((root / 'state' / 'studio' / 'inputs').glob('*.audio'))
    changed = success(root, 'clip-add', project['id'], scene, '--file', selected, '--duration', '.2')
    clip = changed['scenes'][0]['cues'][0]['id']
    refused = cli(root, 'clip-edit', project['id'], scene, clip, '--revision', project['revision'], '--pan', '1')
    assert refused.returncode == 1 and 'revision changed' in refused.stderr
    invalid = cli(root, 'clip-edit', project['id'], scene, clip, '--duration', '.1', '--remaining')
    assert invalid.returncode == 2
    assert success(root, 'projects') == [changed] and selected.read_bytes() == original


def test_actual_cli_preserves_legacy_removal_and_requires_explicit_new_source_selection(tmp_path, audio_tools):
    root = tmp_path / 'library'
    app = LibraryApp(root)
    service = ProductionService(app.root / 'state' / 'studio')
    missing = tmp_path / 'never-selected-again.wav'
    project = Project('legacy-cli', 'Original performance', (),
        (Scene('scene', 'Legacy audio', (), cues=(Cue('old-cue', str(missing)),), minimum_duration=1),),
        original_script='Original script stays exact.')
    service.projects.save(project)
    selected = tone(tmp_path / 'explicit-new-source.wav')
    denied = cli(root, 'clip-add', project.id, 'scene', '--file', selected)
    assert denied.returncode == 1
    assert service.projects.load(project.id) == project and not missing.exists()
    adjusted = success(root, 'clip-duration', project.id, 'scene', '2')
    assert adjusted['schema'] == 1 and adjusted['scenes'][0]['cues'][0]['path'] == str(missing)
    removed = success(root, 'clip-remove', project.id, 'scene', 'old-cue')
    assert removed['schema'] == 1 and removed['scenes'][0]['cues'] == []
    imported = success(root, 'clip-add', project.id, 'scene', '--file', selected)
    assert imported['schema'] == 2 and imported['original_script'] == project.original_script
    assert imported['scenes'][0]['cues'][0]['path'].startswith('inputs/')
    assert not missing.exists()


def test_actual_cli_imports_only_a_verified_saved_recording(tmp_path, audio_tools):
    root = tmp_path / 'library'
    LibraryApp(root)
    recordings = RecordingService(root)
    identity = '1' * 32
    source = tone(recordings.root / (identity + '.wav'))
    options = RecordingOptions(max_seconds=1, sample_rate=24000)
    receipt = {'schema': 'carr-recording/1', 'id': identity, 'filename': source.name,
        'title': 'Synthetic recording receipt', 'created': datetime.now(timezone.utc).isoformat(),
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': '2' * 64, 'name': 'Synthetic fixture'},
        'ffmpeg_sha256': '3' * 64, 'audio': recordings._audio(source, options, lambda: False),
        'limits': asdict(options), 'completion': 'stopped'}
    atomic_json(recordings.root / (identity + '.json'), receipt)
    project = success(root, 'clip-create')
    scene = project['scenes'][0]['id']
    changed = success(root, 'clip-add', project['id'], scene, '--recording', identity)
    cue = changed['scenes'][0]['cues'][0]
    assert cue['role'] == 'microphone' and cue['title'] == receipt['title']
    original = source.read_bytes()
    source.write_bytes(original[:-2] + b'\x00\x00')
    denied = cli(root, 'clip-add', project['id'], scene, '--recording', identity)
    assert denied.returncode == 1 and success(root, 'projects') == [changed]
    assert (root / 'state' / 'studio' / cue['path']).read_bytes() == original


@pytest.mark.parametrize('arguments', [('--help',), ('clip-add', '--help'), ('resume', '--help')])
def test_help_is_stateless_and_describes_explicit_selection(tmp_path, arguments):
    root = tmp_path / 'must-not-exist'
    result = cli(root, *arguments)
    assert result.returncode == 0 and result.stdout and not root.exists()
    if arguments[0] == 'clip-add':
        assert '--file' in result.stdout and '--recording' in result.stdout and '--revision' in result.stdout
    if arguments[0] == 'resume':
        assert '--output' in result.stdout and 'restored' in result.stdout
