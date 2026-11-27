"""Actual console import/review/resume through the canonical clip service."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

from carr_library.durable_state import atomic_json
from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.render import ProductionService


def cli(root, *arguments):
    import bootstrap
    import carr_library
    selected = dict.fromkeys(str(Path(module.__file__).resolve().parent.parent)
                             for module in (carr_library, bootstrap))
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=os.pathsep.join(selected))
    return subprocess.run([sys.executable, '-B', '-m', 'carr_library', 'studio',
        '--data-dir', str(root), *map(str, arguments)], env=environment,
        cwd=root.parent, text=True, encoding='utf-8', capture_output=True, timeout=30)


def test_actual_cli_requires_review_and_explicit_restored_destination(tmp_path):
    source = tmp_path / 'Explicit original.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b'\x00\x00' * 2400)
    original = source.read_bytes()
    project = Project('clip-cli', 'CLI clips', (),
        (Scene('scene', 'Recorded clip', (), cues=(Cue('clip', str(source), duration=.1),)),),
        schema=2)
    saved = tmp_path / 'imported-project.json'
    atomic_json(saved, project.to_dict())
    root, outputs = tmp_path / 'library', tmp_path / 'outputs'
    outputs.mkdir()
    denied = cli(root, 'render', '--project', saved, '--output', outputs, '--no-stems')
    assert denied.returncode == 1 and 'selected input' in denied.stderr
    service = ProductionService(root / 'state/studio')
    jobs = service.jobs()
    assert len(jobs) == 1 and not jobs[0]['artifacts'] and not list(outputs.iterdir())
    identity = jobs[0]['id']
    approved = cli(root, 'resume', identity, '--approve-input', source)
    assert approved.returncode == 0, approved.stderr
    completed = json.loads(approved.stdout)
    master = Path(completed['destination']) / 'master.wav'
    master_identity = sha256(master.read_bytes()).hexdigest()
    assert completed['status'] == 'complete' and source.read_bytes() == original

    # Model the producer's restored-job projection; typed archive recovery has
    # its own integration suite. The console must require a new output choice.
    state = service.get(identity)
    state.update(status='waiting-input', destination='', artifacts=[], nodes={},
                 recovery={'requires_output_directory': True})
    service._save(state)
    denied_resume = cli(root, 'resume', identity)
    assert denied_resume.returncode == 1 and 'new output directory' in denied_resume.stderr
    relocated = tmp_path / 'relocated'
    relocated.mkdir()
    resumed = cli(root, 'resume', identity, '--output', relocated)
    assert resumed.returncode == 0, resumed.stderr
    restored = json.loads(resumed.stdout)
    new_master = Path(restored['destination']) / 'master.wav'
    assert new_master.parent.parent == relocated
    assert sha256(new_master.read_bytes()).hexdigest() == master_identity
    assert source.read_bytes() == original


def test_cli_help_does_not_create_a_library_and_explains_explicit_review(tmp_path):
    root = tmp_path / 'not-created'
    result = cli(root, 'resume', '--help')
    assert result.returncode == 0
    assert '--approve-input' in result.stdout and '--output' in result.stdout
    assert not root.exists()
