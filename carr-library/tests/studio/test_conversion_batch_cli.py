"""Actual CLI review/approval uses the same installed batch/conversion services."""
from array import array
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

import carr_library
from bootstrap.vendor.cairn_extractors import hasher as bootstrap_hasher
from carr_library.audio import conversion_batch
from carr_library.operations import ResourceBroker, ResourceLimits


def environment():
    roots = (Path(carr_library.__file__).resolve().parents[1],
             Path(bootstrap_hasher.__file__).resolve().parents[3])
    return dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, dict.fromkeys(roots))),
                PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='ascii')


@pytest.fixture
def chosen(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg/FFprobe required.'
    library, output = tmp_path / 'library', tmp_path / 'output'
    library.mkdir(); output.mkdir()
    ResourceBroker.for_app(library, limits=ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    paths = []
    for index in range(2):
        path = tmp_path / f'Оригінал Ω {index}.wav'
        data = array('h', (round(2000 * math.sin(2 * math.pi * (440 + index*110) * n/24000))
                          for n in range(6000)))
        if sys.byteorder != 'little':
            data.byteswap()
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM')); stream.writeframes(data.tobytes())
        paths.append(path)
    args = ['--data-dir', str(library), 'convert-batch', '--output-dir', str(output),
            '--channels', '1', '--timeout', '30', '--max-bytes', str(8*1024**2)]
    for path in paths:
        args.extend(['--input', str(path)])
    return library, output, paths, args


_CHILD = """from pathlib import Path
import sys
from carr_library.audio import conversion_batch
from carr_library.__main__ import main
assert Path(conversion_batch.__file__).resolve() == Path(sys.argv[1]).resolve()
raise SystemExit(main(['studio', *sys.argv[2:]]))
"""


def call(args):
    result = subprocess.run([sys.executable, '-B', '-c', _CHILD,
        str(Path(conversion_batch.__file__).resolve()), *args],
        env=environment(), capture_output=True, timeout=120)
    return result


def test_actual_cli_requires_explicit_review_then_exports_unicode_and_resumes_without_rewriting(chosen):
    library, output, sources, args = chosen
    originals = [path.read_bytes() for path in sources]
    reviewed = call(args)
    assert reviewed.returncode == 0, (reviewed.stdout, reviewed.stderr)
    value = json.loads(reviewed.stdout)
    assert value['review_only'] and not (output / conversion_batch.MANIFEST).exists()
    assert value['plan']['sources'][0]['source'] == str(sources[0])
    completed = call([*args, '--approve', value['plan']['id']])
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    result = json.loads(completed.stdout)
    assert result['status'] == 'completed' and result['done'] == 2
    files = [output / row['output'] for row in value['plan']['sources']]
    old = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
    resume_review = call([*args, '--resume'])
    assert resume_review.returncode == 0, (resume_review.stdout, resume_review.stderr)
    resume_id = json.loads(resume_review.stdout)['plan']['id']
    resumed = call([*args, '--resume', '--approve', resume_id])
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    assert json.loads(resumed.stdout)['done'] == 2
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in files] == old
    assert [path.read_bytes() for path in sources] == originals


def test_actual_cli_stale_approval_does_not_encode_or_create_manifest(chosen):
    library, output, sources, args = chosen
    reviewed = call(args)
    assert reviewed.returncode == 0, (reviewed.stdout, reviewed.stderr)
    identifier = json.loads(reviewed.stdout)['plan']['id']
    raw = bytearray(sources[0].read_bytes()); raw[-1] ^= 1; sources[0].write_bytes(raw)
    refused = call([*args, '--approve', identifier])
    assert refused.returncode == 1
    assert b'fingerprint' in refused.stderr
    assert not (output / conversion_batch.MANIFEST).exists() and not list(output.glob('*.wav'))


def test_actual_cli_unknown_typed_effect_is_refused_without_codec_or_output(chosen, tmp_path):
    library, output, sources, args = chosen
    selected = tmp_path / 'effects.json'
    selected.write_text(json.dumps([{'kind': 'raw-filtergraph', 'parameters': {'script': 'unsafe'},
                                     'enabled': True}]), encoding='utf-8')
    rejected = call([*args, '--effects-json', str(selected)])
    assert rejected.returncode == 1
    assert not (output / conversion_batch.MANIFEST).exists() and not list(output.glob('*.wav'))


def test_actual_cli_no_implicit_resume_or_overwrite(chosen):
    library, output, sources, args = chosen
    reviewed = call(args)
    identifier = json.loads(reviewed.stdout)['plan']['id']
    first = call([*args, '--approve', identifier])
    assert first.returncode == 0, (first.stdout, first.stderr)
    before = {path.name: sha256(path.read_bytes()).hexdigest() for path in output.glob('*.wav')}
    repeated = call([*args, '--approve', identifier])
    assert repeated.returncode == 1
    assert b'Explicitly choose Resume' in repeated.stderr
    assert {path.name: sha256(path.read_bytes()).hexdigest() for path in output.glob('*.wav')} == before
