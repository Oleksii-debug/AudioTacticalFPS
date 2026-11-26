"""Resolve an approved local Windows x64 wheelhouse with pinned upstream uv.
This is cross-target resolution, not a Windows installation or security scan.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from packaging.requirements import Requirement
from packaging.utils import parse_wheel_filename

UV_VERSION = 'uv 0.10.0'
TARGET = ['--python-platform', 'x86_64-pc-windows-msvc', '--python-version', '3.11']


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate manifest key')
        result[key] = value
    return result


def read_approved_manifest(path):
    """Read independently approved filename -> lowercase SHA256, never generate it here."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('expected a bounded regular approval manifest')
    approved = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    if not isinstance(approved, dict) or not approved:
        raise ValueError('empty or invalid approval manifest')
    names = set()
    for name, digest in approved.items():
        if (not name.isascii() or not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', name)
                or name.casefold() in names):
            raise ValueError('invalid or case-colliding wheel name')
        parse_wheel_filename(name)
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('expected lowercase SHA256')
        names.add(name.casefold())
    return approved


def verify_wheelhouse(wheelhouse, approved):
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ValueError('expected an approved local wheelhouse')
    wheels = list(wheelhouse.iterdir())
    if {p.name for p in wheels} != set(approved):
        raise ValueError('wheelhouse does not match approved inventory')
    for p in wheels:
        if p.is_symlink() or not p.is_file():
            raise ValueError('wheelhouse must contain only regular wheels')
        with p.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != approved[p.name]:
            raise ValueError('wheel SHA256 mismatch: ' + p.name)


def resolve(requirements, wheelhouse, output, approved_manifest):
    requirements, wheelhouse, output, approved_manifest = map(
        Path, (requirements, wheelhouse, output, approved_manifest))
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if (output.resolve().is_relative_to(wheelhouse.resolve())
            or approved_manifest.resolve().is_relative_to(wheelhouse.resolve())):
        raise ValueError('manifest and output must be outside wheelhouse')
    approved = read_approved_manifest(approved_manifest)
    verify_wheelhouse(wheelhouse, approved)
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
        hashes = re.findall(rb'--hash=sha256:([0-9a-f]{64})(?![0-9a-f])', data)
        if (not hashes or len(hashes) != data.count(b'--hash=')
                or not {h.decode('ascii') for h in hashes} <= set(approved.values())):
            raise ValueError('target lock contains missing or unapproved hashes')
        # Catch changed staging bytes before publication; not a substitute for isolation.
        verify_wheelhouse(wheelhouse, approved)
        # Publish only on success and never replace an existing lock.
        with output.open('xb') as f:
            f.write(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requirements', type=Path)
    parser.add_argument('wheelhouse', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--approved-manifest', required=True, type=Path)
    args = parser.parse_args()
    resolve(args.requirements, args.wheelhouse, args.output, args.approved_manifest)
