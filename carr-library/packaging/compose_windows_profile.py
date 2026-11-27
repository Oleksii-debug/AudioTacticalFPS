"""Compose existing hash locks into an exact, offline Windows wheel profile.

This reads already acquired wheels. It never resolves or downloads packages.
The resulting SBOM describes the actual selected wheel bytes and metadata.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('carr_windows_builder', HERE / 'build_windows_bundle.py')
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


def compose(locks, wheelhouse, output):
    locks, wheelhouse, output = tuple(map(Path, locks)), Path(wheelhouse), Path(output)
    if not locks or output.exists() or output.is_symlink():
        raise ValueError('Provide existing locks and a new output directory.')
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError('Choose a dedicated ordinary Windows wheel directory.')
    pins, provenance = {}, []
    for lock in locks:
        incoming = builder.read_lock(lock)
        provenance.append({'name': 'carr:input-lock-sha256', 'value': builder.sha256(lock)})
        for name, (version, hashes) in incoming.items():
            if name in pins:
                previous_version, previous_hashes = pins[name]
                if version != previous_version or not hashes.intersection(previous_hashes):
                    raise ValueError('Conflicting dependency lock: ' + name)
                hashes = hashes.intersection(previous_hashes)
            pins[name] = version, hashes
    selected = {}
    for wheel in sorted(wheelhouse.iterdir()):
        builder.regular(wheel)
        if wheel.suffix != '.whl':
            raise ValueError('The wheelhouse must contain only the locked dependency wheels.')
        name, version, metadata = builder.wheel_metadata(wheel)
        digest = builder.sha256(wheel)
        if (name in selected or name not in pins or str(version) != pins[name][0]
                or digest not in pins[name][1]):
            raise ValueError('Wheel is not uniquely admitted by every applicable lock: ' + wheel.name)
        selected[name] = version, metadata, digest
    if selected.keys() != pins.keys():
        raise ValueError('The complete locked wheel closure is required.')
    environment = default_environment()
    environment.update(os_name='nt', sys_platform='win32', platform_system='Windows',
        platform_machine='AMD64', python_version='3.11', python_full_version='3.11.0',
        implementation_name='cpython', platform_python_implementation='CPython', extra='')
    root_ref = 'carr:private-windows-runtime'
    components, edges, lines = [], [], []
    for name, (version, metadata, digest) in sorted(selected.items()):
        ref = f'pkg:pypi/{name}@{version}'
        dependencies = []
        for raw in metadata.get_all('Requires-Dist', []):
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            dependency = canonicalize_name(requirement.name)
            if (requirement.url or dependency not in selected
                    or selected[dependency][0] not in requirement.specifier):
                raise ValueError('Unsatisfied Windows dependency: ' + str(requirement))
            dependencies.append(f'pkg:pypi/{dependency}@{selected[dependency][0]}')
        component = {'type': 'library', 'bom-ref': ref, 'name': name, 'version': str(version),
                     'purl': ref, 'hashes': [{'alg': 'SHA-256', 'content': digest}]}
        expression = metadata.get('License-Expression')
        if expression:
            component['licenses'] = [{'expression': expression}]
        components.append(component)
        edges.append({'ref': ref, 'dependsOn': sorted(set(dependencies))})
        lines.append(f'{name}=={version} --hash=sha256:{digest}')
    edges.append({'ref': root_ref, 'dependsOn': [row['bom-ref'] for row in components]})
    bom = {'bomFormat': 'CycloneDX', 'specVersion': '1.5', 'version': 1,
        'metadata': {'component': {'type': 'application', 'name': 'CARR private Windows runtime',
                                  'bom-ref': root_ref}, 'properties': provenance,
                     'tools': [{'name': 'CARR locked Windows wheel composer', 'version': '1'}]},
        'components': components, 'dependencies': edges}
    output.mkdir(parents=True, exist_ok=False)
    (output / 'runtime.lock').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (output / 'runtime-sbom.cdx.json').write_text(json.dumps(bom, indent=2) + '\n', encoding='utf-8')
    return {'dependencies': len(selected), 'lock_sha256': builder.sha256(output / 'runtime.lock'),
            'sbom_sha256': builder.sha256(output / 'runtime-sbom.cdx.json')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', action='append', required=True, type=Path)
    parser.add_argument('--wheelhouse', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(compose(args.lock, args.wheelhouse, args.output)))
