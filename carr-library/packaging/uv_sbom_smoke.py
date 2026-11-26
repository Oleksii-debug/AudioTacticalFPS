"""Build-tool smoke only; no application, model or release approval."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


PIN = 'pip==25.1.1'
WHEEL = 'pip-25.1.1-py3-none-any.whl'
UV_VERSION = 'uv 0.13.9'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def smoke(wheel, expected_sha):
    require(wheel.name == WHEEL and wheel.is_file(), 'Expected local pip fixture wheel')
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    require(digest == expected_sha.lower(), 'Fixture digest mismatch')
    uv = shutil.which('uv')
    require(uv is not None, 'Trusted uv executable required')
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('UV_', 'PIP_', 'VIRTUAL_ENV', 'CONDA_PREFIX'))}
    env['PIP_CONFIG_FILE'] = os.devnull
    version = subprocess.check_output([uv, '--version'], text=True, env=env).strip()
    require(version.split()[:2] == UV_VERSION.split(), 'Unexpected uv version')
    with tempfile.TemporaryDirectory(prefix='Carr SBOM Ž ') as tmp:
        root = Path(tmp)
        wheels = root / 'wheelhouse'
        wheels.mkdir()
        shutil.copyfile(wheel, wheels / WHEEL)
        project = root / 'pyproject.toml'
        project.write_text('[project]\nname = "carr-sbom-smoke"\nversion = "0.0.0"\n'
                           'requires-python = ">=3.11"\ndependencies = ["' + PIN + '"]\n', encoding='utf-8')
        (root / 'requirements.in').write_text(PIN + '\n', encoding='utf-8')
        common = [uv, '--no-config', '--offline', '--no-cache', '--no-python-downloads']
        index = ['--no-index', '--find-links', 'wheelhouse', '--no-build']

        def run(args, fail=False, needle=None):
            p = subprocess.run(common + args, cwd=root, env=env, text=True,
                               capture_output=True, timeout=90)
            require((p.returncode != 0) == fail, f'Unexpected exit {p.returncode}: {p.stderr}')
            if needle:
                require(needle.lower() in p.stderr.lower(), p.stderr)
            return p.stdout

        run(['lock', *index])
        run(['export', '--locked', *index, '--format', 'cyclonedx1.5', '-o', 'sbom.json'])
        bom = json.loads((root / 'sbom.json').read_text(encoding='utf-8'))
        require(bom['bomFormat'] == 'CycloneDX' and bom['specVersion'] == '1.5', 'Wrong SBOM format')
        components = bom['components']
        require(len(components) == 1 and components[0]['purl'] == 'pkg:pypi/pip@25.1.1', 'SBOM inventory mismatch')
        require(any(components[0]['bom-ref'] in d['dependsOn'] for d in bom['dependencies']), 'Missing dependency edge')
        run(['pip', 'compile', *index, '--generate-hashes', 'requirements.in', '-o', 'requirements.lock'])
        lock = (root / 'requirements.lock').read_text(encoding='utf-8')
        require(PIN in lock and '--hash=sha256:' + digest in lock, 'Hash lock mismatch')
        for name in ('clean', 'tampered'):
            run(['venv', '--python', sys.executable, name])
        def interpreter(name):
            return str(root / name / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
        def install(name):
            return ['pip', 'install', *index, '--python', interpreter(name), '--require-hashes', '-r', 'requirements.lock']
        run(install('clean'))
        installed = subprocess.check_output([interpreter('clean'), '-I', '-c',
            'import importlib.metadata as m; print(m.version("pip"))'], text=True, env=env).strip()
        require(installed == '25.1.1', 'Clean install version mismatch')
        project.write_text(project.read_text(encoding='utf-8').replace(PIN, 'pip==0.0.0'), encoding='utf-8')
        run(['export', '--locked', *index, '--format', 'cyclonedx1.5'], fail=True)
        (wheels / WHEEL).unlink()
        run(['pip', 'compile', *index, '--generate-hashes', 'requirements.in'], fail=True)
        shutil.copyfile(wheel, wheels / WHEEL)
        with zipfile.ZipFile(wheels / WHEEL, 'a') as archive:
            archive.comment = b'tampered fixture comment'
        run(install('tampered'), fail=True, needle='hash')
    return {'status': 'PASS', 'checks': 6, 'uv': version, 'wheel_sha256': digest,
            'python': sys.version.split()[0], 'platform': sys.platform}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('sha256', help='Expected SHA-256 from trusted fixture provenance')
    args = parser.parse_args()
    print(json.dumps(smoke(args.wheel, args.sha256), sort_keys=True))
