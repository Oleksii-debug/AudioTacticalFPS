"""Actual POSIX signal delivery belongs to the platform-aware audio suite."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest


def _environment():
    import carr_library.audio_cli as module
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(module.__file__).resolve().parents[1])
    environment['PYTHONIOENCODING'] = 'utf-8'
    return environment


def _ffmpeg():
    binary = os.environ.get('CARR_AUDIO_FFMPEG') or shutil.which('ffmpeg')
    if not binary:
        pytest.fail('Actual FFmpeg is required for the audio CLI production tests.')
    return str(Path(binary).resolve())


def _pause_plan(path, *, long=False):
    from carr_library.audio.production import parse_script
    script = ('[output sample_rate=8000 stems=false]\n# First\n[pause 0.1]\n'
              '# Second\n[pause ' + ('3600' if long else '0.2') + ']\n')
    plan = parse_script(script, voice='unused:local')
    path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False), encoding='utf-8')
    return plan


@pytest.mark.skipif(os.name == 'nt', reason='POSIX SIGINT delivery; native Windows cancellation is separately probed.')
def test_actual_cli_sigint_preserves_completed_chunks_and_returns_resume_status(tmp_path):
    plan_file, output = tmp_path / 'plan.json', tmp_path / 'output'
    _pause_plan(plan_file, long=True)
    library = tmp_path / 'selected-library'
    library.mkdir()
    process = subprocess.Popen([sys.executable, '-m', 'carr_library', '--data-dir', str(library), '--audio-plan', str(plan_file),
        '--audio-output', str(output), '--ffmpeg', _ffmpeg()], env=_environment(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        seen = []
        for line in process.stderr:
            seen.append(line)
            if 'Rendering chapter 2 of 2' in line:
                process.send_signal(signal.SIGINT)
                break
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 130, (seen, stdout, stderr)
        result = json.loads(stdout)
        assert result['status'] == 'cancelled' and 'reused' in result['resume']
        journal = json.loads((output / 'render-journal.json').read_text(encoding='utf-8'))
        assert journal['state'] == 'cancelled'
        assert list((output / 'cache').glob('*.wav'))
        assert not list((output / 'renders').iterdir())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
