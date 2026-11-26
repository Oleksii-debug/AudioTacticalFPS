"""Real wheel metadata/locks compose into the builder's exact offline input."""
import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('compose_windows_profile', ROOT / 'packaging/compose_windows_profile.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def wheel(folder, name, *, requires=(), license='BSD-3-Clause'):
    path = folder / f'{name}-1.0-py3-none-any.whl'
    metadata = f'Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\nLicense-Expression: {license}\n'
    metadata += ''.join(f'Requires-Dist: {row}\n' for row in requires)
    with ZipFile(path, 'w') as archive:
        archive.writestr(f'{name}-1.0.dist-info/METADATA', metadata)
    return path


def lock(path, wheels):
    path.write_text(''.join(f'{row.name.split("-")[0]}==1.0 --hash=sha256:{hashlib.sha256(row.read_bytes()).hexdigest()}\n'
                            for row in wheels), encoding='utf-8')
    return path


def test_combined_closure_drives_real_bundle_builder_and_sbom(tmp_path):
    folder = tmp_path / 'wheels'
    folder.mkdir()
    dependency = wheel(folder, 'fixture')
    optional = wheel(folder, 'extra', requires=('fixture==1.0; sys_platform == "win32"',
                                               'notinstalled; extra == "ssh"'))
    first, second = lock(tmp_path / 'base.lock', [dependency]), lock(tmp_path / 'extra.lock', [optional])
    profile = tmp_path / 'profile'
    assert module.compose([first, second], folder, profile)['dependencies'] == 2
    bom = json.loads((profile / 'runtime-sbom.cdx.json').read_text())
    extra = next(row for row in bom['components'] if row['name'] == 'extra')
    assert extra['licenses'] == [{'expression': 'BSD-3-Clause'}]
    assert next(row for row in bom['dependencies'] if row['ref'] == extra['bom-ref'])['dependsOn'] == ['pkg:pypi/fixture@1.0']
    app = tmp_path / 'universal_research_library-1.0-py3-none-any.whl'
    with ZipFile(app, 'w') as archive:
        archive.writestr('universal_research_library-1.0.dist-info/METADATA',
                        'Metadata-Version: 2.4\nName: universal-research-library\nVersion: 1.0\nRequires-Dist: fixture==1.0\n')
        archive.writestr('carr_library/__main__.py', '"Synthetic packaging fixture."\n')
    result = module.builder.build_bundle(app, folder, profile / 'runtime.lock', tmp_path / 'bundle',
                                         sbom=profile / 'runtime-sbom.cdx.json')
    assert result['wheels'] == 3
    assert (tmp_path / 'bundle/runtime-sbom.cdx.json').read_bytes() == (profile / 'runtime-sbom.cdx.json').read_bytes()


@pytest.mark.parametrize('change', ['missing', 'tamper', 'conflict', 'dependency', 'extra', 'linux-marker'])
def test_incomplete_unpinned_or_wrong_platform_closure_is_not_published(tmp_path, change):
    folder = tmp_path / 'wheels'
    folder.mkdir()
    item = wheel(folder, 'fixture', requires=('missing>=1; sys_platform == "win32"',) if change == 'dependency' else ())
    pinned = lock(tmp_path / 'base.lock', [item])
    locks = [pinned]
    if change == 'missing':
        item.unlink()
    elif change == 'tamper':
        with ZipFile(item, 'a') as archive:
            archive.writestr('changed.txt', 'changed')
    elif change == 'conflict':
        other = tmp_path / 'other.lock'
        other.write_text(pinned.read_text().replace('==1.0', '==2.0'))
        locks.append(other)
    elif change == 'extra':
        wheel(folder, 'unlisted')
    elif change == 'linux-marker':
        item.rename(folder / 'fixture-1.0-cp311-cp311-manylinux_2_17_x86_64.whl')
    with pytest.raises(ValueError):
        module.compose(locks, folder, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_repeated_consistent_input_is_deterministic_and_preserves_existing_output(tmp_path):
    folder = tmp_path / 'wheels'
    folder.mkdir()
    pinned = lock(tmp_path / 'base.lock', [wheel(folder, 'fixture')])
    first, second = tmp_path / 'one', tmp_path / 'two'
    assert module.compose([pinned, pinned], folder, first) == module.compose([pinned, pinned], folder, second)
    with pytest.raises(ValueError):
        module.compose([pinned], folder, first)
    assert (first / 'runtime.lock').read_bytes() == (second / 'runtime.lock').read_bytes()
