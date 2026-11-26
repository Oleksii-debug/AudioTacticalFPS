"""Resolve an approved local Windows x64 wheelhouse with pinned upstream uv.
This is cross-target resolution, not a Windows installation or security scan.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from packaging.requirements import Requirement

UV_VERSION = 'uv 0.10.0'
TARGET = ['--python-platform', 'x86_64-pc-windows-msvc', '--python-version', '3.11']


def resolve(requirements, wheelhouse, output):
    requirements, wheelhouse, output = map(Path, (requirements, wheelhouse, output))
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ValueError('expected an approved local wheelhouse')
    wheels = list(wheelhouse.iterdir())
    if not wheels or any(p.is_symlink() or not p.is_file() or p.suffix != '.whl' for p in wheels):
        raise ValueError('wheelhouse must contain only regular wheels')
    # Root requirements are explicit, named pins; no includes, URLs or pip options.
    pins = []
    for line in requirements.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        req = Requirement(line)
        specs = list(req.specifier)
        if req.url or len(specs) != 1 or specs[0].operator != '==' or '*' in specs[0].version:
            raise ValueError('each root requirement must be an exact named version')
        pins.append(str(req))
    if not pins:
        raise ValueError('empty requirements')
    uv = shutil.which('uv')
    if uv is None:
        raise RuntimeError('trusted uv executable required')
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('UV_', 'PIP_', 'VIRTUAL_ENV', 'CONDA_PREFIX'))}
    env['PIP_CONFIG_FILE'] = os.devnull
    version = subprocess.check_output([uv, '--version'], env=env, text=True).strip()
    if version.split()[:2] != UV_VERSION.split():
        raise RuntimeError('unexpected uv version')
    with tempfile.TemporaryDirectory(prefix='Carr Windows Ž ') as d:
        tmp = Path(d)
        roots = tmp / 'roots.in'
        roots.write_text('\n'.join(pins) + '\n', encoding='utf-8')
        lock = tmp / 'target.lock'
        cmd = [uv, '--no-config', '--offline', '--no-cache', '--no-python-downloads', 'pip']
        flags = ['--no-index', '--find-links', str(wheelhouse.resolve()), '--no-build', *TARGET]
        subprocess.run(cmd + ['compile', *flags, '--generate-hashes', '--no-header', '--no-annotate', str(roots), '-o', str(lock)], cwd=tmp, env=env, check=True, timeout=180)
        # uv's own hash-mode dry run checks target resolution, never imports binaries.
        subprocess.run(cmd + ['install', *flags, '--dry-run', '--target', str(tmp / 'target'), '--require-hashes', '-r', str(lock)], cwd=tmp, env=env, check=True, timeout=180)
        data = lock.read_bytes()
        if b'--hash=sha256:' not in data:
            raise ValueError('empty or unhashed target lock')
        # Publish only on success and never replace an existing lock.
        with output.open('xb') as f:
            f.write(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requirements', type=Path)
    parser.add_argument('wheelhouse', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    resolve(args.requirements, args.wheelhouse, args.output)
