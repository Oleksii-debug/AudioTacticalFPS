"""Build a source-bound, unsigned per-user setup from an existing pristine bundle.

The Inno toolchain is installed separately from its pinned official artifact.
Output and copied payload must be outside all Git worktrees. No download, upload,
publication, signing or runtime dependency resolution occurs in this helper.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from zipfile import ZipFile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


maintenance = load('resonance_setup_maintenance_build', HERE / 'maintenance.py')


def outside_repository(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if os.path.lexists(parent / '.git'):
            raise ValueError('Installer binaries and copied bundles must remain outside Git worktrees.')
    return path


def quoted(value):
    value = str(value)
    if '\n' in value or '\r' in value or '\x00' in value:
        raise ValueError('Invalid Inno compiler path.')
    return '"' + value.replace('"', '""').replace('{', '{{') + '"'


def pascal(value):
    return "'" + str(value).replace("'", "''") + "'"


def application_version(bundle):
    wheels = sorted((bundle / 'wheelhouse').glob('universal_research_library-*.whl'))
    if len(wheels) != 1:
        raise ValueError('The bundle needs exactly one application wheel.')
    with ZipFile(wheels[0]) as archive:
        metadata = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(metadata) != 1:
            raise ValueError('Ambiguous application wheel metadata.')
        from email.parser import BytesParser
        record = BytesParser().parsebytes(archive.read(metadata[0]))
        version = record['Version']
        if not isinstance(version, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}(?:[a-z0-9.]*)?', version):
            raise ValueError('Unsupported application version for standard setup.')
        for name in ('installation_lease.py', '__main__.py'):
            if archive.read('carr_library/' + name) != (ROOT / 'src/carr_library' / name).read_bytes():
                raise ValueError('Application wheel lacks the current managed installation lease: ' + name)
    return version


def prepare(bundle: Path, output: Path, *, manifest_sha256: str, installed_bundle=False):
    bundle = Path(bundle).absolute()
    document = maintenance.bundle(bundle, manifest_sha256, allow_installed=installed_bundle)
    version = application_version(bundle)
    output = outside_repository(output)
    maintenance.plain(output.parent, directory=True)
    output.mkdir(exist_ok=False)
    payload = output / 'payload'
    payload.mkdir()
    for name in sorted({**document['files'], 'bundle-manifest.json': manifest_sha256}):
        relative = maintenance.relative(name)
        target = payload.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the source timestamps used by sealed Python bytecode.
        shutil.copy2(bundle.joinpath(*relative.parts), target)
    maintenance.bundle(payload, manifest_sha256)
    helper = output / 'helpers'
    helper.mkdir()
    originals = dict(maintenance.HELPERS)
    for name, source in originals.items():
        shutil.copyfile(source, helper / name)
    files, bootstrap = [], []
    for name in sorted({**document['files'], 'bundle-manifest.json': manifest_sha256}):
        relative = PurePosixPath(name)
        destination = '{tmp}\\payload'
        if str(relative.parent) != '.':
            destination += '\\' + str(relative.parent).replace('/', '\\')
        files.append('Source: ' + quoted(payload.joinpath(*relative.parts)) + '; DestDir: "' + destination + '"; Flags: dontcopy')
        if name.startswith('runtime/'):
            windows_name = name.replace('/', '\\')
            bootstrap.append('  CopyVerified(ExpandConstant(' + pascal('{app}\\app\\' + windows_name) + '), Bootstrap + ' + pascal('\\payload\\' + windows_name) + ', ' + pascal(document['files'][name]) + ');')
    for name in originals:
        files.append('Source: ' + quoted(helper / name) + '; DestDir: "{tmp}\\helpers"; Flags: dontcopy')
        files.append('Source: ' + quoted(helper / name) + '; DestDir: "{app}\\maintenance"; Flags: ignoreversion')
        bootstrap.append('  CopyVerified(ExpandConstant(' + pascal('{app}\\maintenance\\' + name) + '), Bootstrap + ' + pascal('\\helpers\\' + name) + ', ' + pascal(maintenance.sha(helper / name)) + ');')
    template = (HERE / 'setup.iss.in').read_text(encoding='utf-8')
    replacements = {'VERSION': version, 'OUTPUT': str(output / 'compiled'),
                    'BUNDLE_HASH': manifest_sha256, 'FILES': '\n'.join(files),
                    'UNINSTALL_BOOTSTRAP': '\n'.join(bootstrap)}
    # OutputDir is a raw directive, not Pascal; restrict rather than interpret
    # arbitrary preprocessor/constant syntax in an operator-supplied path.
    if any(c in replacements['OUTPUT'] for c in '\n\r{};"'):
        raise ValueError('Choose a build output path without Inno metacharacters.')
    for key, value in replacements.items():
        token = '@@' + key + '@@'
        if template.count(token) != 1:
            raise ValueError('Unexpected installer template contract: ' + key)
        template = template.replace(token, value)
    if '@@' in template:
        raise ValueError('An installer template field was not filled.')
    (output / 'ResonanceSetup.iss').write_text(template, encoding='utf-8-sig')
    record = dict(schema=1, status='SOURCE_PREPARED', source_commit=document['source_commit'],
                  bundle_manifest_sha256=manifest_sha256, application_version=version,
                  managed_upgrade_protocol=maintenance.upgrade_protocol.PROOF_SCHEMA,
                  template_sha256=maintenance.sha(output / 'ResonanceSetup.iss'),
                  maintenance_sha256={name: maintenance.sha(helper / name) for name in originals},
                  release_status='unsigned-private-candidate', authenticode_verified=False,
                  windows_installation_verified=False, nvda_verified=False)
    (output / 'setup-build.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


def compile_setup(output, *, iscc, toolchain_receipt):
    output, iscc = outside_repository(output), Path(iscc).absolute()
    lock = json.loads((HERE / 'toolchain.lock.json').read_text(encoding='utf-8'))
    receipt = json.loads(maintenance.read(Path(toolchain_receipt), 64 * 1024), object_pairs_hook=maintenance.unique)
    if (receipt.get('schema') != 1 or receipt.get('version') != lock['version']
            or receipt.get('archive_sha256') != lock['sha256']
            or receipt.get('iscc_sha256') != maintenance.sha(iscc)):
        raise ValueError('The compiler does not match its pinned installation receipt.')
    if os.name != 'nt':
        raise RuntimeError('Actual Inno compilation requires Windows.')
    subprocess.run([str(iscc), '/Qp', str(output / 'ResonanceSetup.iss')], check=True,
                   cwd=output, timeout=900, stdin=subprocess.DEVNULL)
    result = output / 'compiled/ResonanceSetup.exe'
    checksum = maintenance.sha(result)
    record = json.loads((output / 'setup-build.json').read_text(encoding='utf-8'))
    record.update(status='BUILT_NOT_INSTALLED', setup_sha256=checksum, setup_bytes=result.stat().st_size,
                  toolchain=receipt)
    (output / 'setup-build.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--bundle-manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--iscc', type=Path)
    parser.add_argument('--toolchain-receipt', type=Path)
    parser.add_argument('--installed-bundle', action='store_true',
                        help='Capture only sealed bundle files; do not copy its existing .venv.')
    args = parser.parse_args()
    if bool(args.iscc) != bool(args.toolchain_receipt):
        parser.error('Supply both the pinned compiler and its receipt, or prepare source only.')
    result = prepare(args.bundle, args.output, manifest_sha256=args.bundle_manifest_sha256,
                     installed_bundle=args.installed_bundle)
    if args.iscc:
        result = compile_setup(args.output, iscc=args.iscc, toolchain_receipt=args.toolchain_receipt)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
