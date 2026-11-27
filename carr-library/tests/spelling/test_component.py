"""The installed optional component is the exact reviewed code-only derivative."""
import base64
import csv
from hashlib import sha256
import importlib.metadata
from io import StringIO
import json
from pathlib import Path
import runpy
from zipfile import ZipFile

import pytest

from carr_library.reader import spellcheck


def test_installed_backend_preserves_all_upstream_python_and_has_no_dictionary_payload():
    backend = spellcheck._backend(lambda: False)
    distribution = importlib.metadata.distribution('spylls')
    assert distribution.version == '0.1.7+carr.1'
    assert distribution.metadata['License'] == 'MPL-2.0'
    assert not distribution.requires
    files = tuple(distribution.files)
    assert not any(str(path).endswith(('.aff', '.dic')) for path in files)
    assert not list((Path(backend['root']) / 'spylls').rglob('*.aff'))
    assert not list((Path(backend['root']) / 'spylls').rglob('*.dic'))
    manifest = json.loads(Path(spellcheck.__file__).with_name('spellcheck_backend.json').read_text())
    provenance = json.loads(distribution.read_text('CARR_REPACK.json'))
    assert provenance == manifest['provenance']
    assert len(provenance['unchanged_python_sha256']) == 21 and len(provenance['removed']) == 6
    assert 'Mozilla Public License Version 2.0' in distribution.read_text('LICENSE')
    for name, expected in provenance['unchanged_python_sha256'].items():
        assert sha256(Path(distribution.locate_file(name)).read_bytes()).hexdigest() == expected
    for name, digest, size in csv.reader(StringIO(distribution.read_text('RECORD'))):
        if not digest:
            continue
        data = Path(distribution.locate_file(name)).read_bytes()
        assert len(data) == int(size)
        assert digest == 'sha256=' + base64.urlsafe_b64encode(sha256(data).digest()).rstrip(b'=').decode()


def test_repack_refuses_unreviewed_input_without_creating_an_output(tmp_path):
    helper = runpy.run_path(str(Path(__file__).parents[2] / 'scripts' / 'repack_spellcheck.py'))
    source = tmp_path / helper['UPSTREAM_NAME']
    output = tmp_path / helper['OUTPUT_NAME']
    source.write_bytes(b'not the reviewed wheel')
    with pytest.raises(ValueError, match='size'):
        helper['repack'](source, output)
    assert not output.exists()
    with source.open('wb') as stream:
        stream.truncate(helper['UPSTREAM_SIZE'])
    with pytest.raises(ValueError, match='hash'):
        helper['repack'](source, output)
    assert not output.exists()


def test_source_lock_and_runtime_manifest_pin_the_same_derived_component():
    root = Path(__file__).parents[2]
    helper = runpy.run_path(str(root / 'scripts' / 'repack_spellcheck.py'))
    manifest = json.loads(Path(spellcheck.__file__).with_name('spellcheck_backend.json').read_text())
    assert manifest['provenance']['upstream_sha256'] == helper['UPSTREAM_SHA256']
    assert manifest['provenance']['upstream_size'] == helper['UPSTREAM_SIZE']
    assert manifest['wheel'] == helper['OUTPUT_NAME']
    active = [line for line in (root / 'packaging' / 'spellcheck.lock').read_text().splitlines()
              if line.strip() and not line.startswith('#')]
    assert active == ['spylls==0.1.7+carr.1 --hash=sha256:' + manifest['sha256']]


@pytest.fixture
def prepared_code_wheel():
    import os
    selected = os.environ.get('CARR_SPELLCHECK_WHEEL')
    if not selected:
        pytest.fail('Set CARR_SPELLCHECK_WHEEL to the explicitly prepared exact code-only wheel.')
    path = Path(selected).absolute()
    assert path.name == 'spylls-0.1.7+carr.1-py2.py3-none-any.whl'
    assert path.stat().st_size == 224323
    assert sha256(path.read_bytes()).hexdigest() == '10b65cc18b2166c2fe4ae2aba1ee0839e7a0a4a0f5aa2a76d13ec653f9c71b66'
    return path


def packaging_modules():
    fixture = runpy.run_path(str(Path(__file__).parents[1] / 'packaging/test_windows_bundle.py'))
    return fixture['builder'], fixture['installer'], fixture['wheel']


def spelling_bundle(tmp_path, prepared):
    builder, installer, make_wheel = packaging_modules()
    wheels = tmp_path / 'base-wheels'
    wheels.mkdir()
    dependency = make_wheel(wheels, 'fixture')
    lock = tmp_path / 'base.lock'
    lock.write_text('fixture==1.0 --hash=sha256:' + builder.sha256(dependency) + '\n', encoding='utf-8')
    sbom = tmp_path / 'base-sbom.json'
    sbom.write_text(json.dumps(dict(bomFormat='CycloneDX', specVersion='1.5',
        components=[dict(name='fixture', version='1.0', **{'bom-ref': 'pypi:fixture@1.0'})],
        dependencies=[dict(ref='pypi:fixture@1.0', dependsOn=[])])), encoding='utf-8')
    app = make_wheel(tmp_path, 'universal_research_library', requires=['fixture==1.0'], entrypoint=True)
    output = tmp_path / 'with-spelling'
    before = lock.read_bytes(), sbom.read_bytes(), {path.name: path.read_bytes() for path in wheels.iterdir()}
    result = builder.build_bundle(app, wheels, lock, output, sbom=sbom, spellcheck_wheel=prepared)
    assert before == (lock.read_bytes(), sbom.read_bytes(),
                      {path.name: path.read_bytes() for path in wheels.iterdir()})
    return builder, installer, output, result, before


def test_optional_preseal_builder_seals_exact_component_and_preserves_base_inputs(tmp_path, prepared_code_wheel):
    builder, installer, output, result, before = spelling_bundle(tmp_path, prepared_code_wheel)
    assert result['wheels'] == 3  # One synthetic base dependency + optional code + application.
    installer.verify(output)
    bom = json.loads((output / 'runtime-sbom.cdx.json').read_text(encoding='utf-8'))
    assert [row['name'] for row in bom['components']] == ['fixture', 'spylls']
    assert bom['components'][0] == json.loads(before[1])['components'][0]
    assert bom['components'][1]['version'] == '0.1.7+carr.1'
    manifest = json.loads((output / 'bundle-manifest.json').read_text(encoding='utf-8'))
    assert 'bundled_spelling' not in manifest  # Existing strict outer schema is unchanged.
    from carr_library.native_checks import _manifest
    assert _manifest(output) == manifest
    declaration = output / 'spelling-component.json'
    assert declaration.read_bytes() == Path(spellcheck.__file__).with_name('spellcheck_backend.json').read_bytes()
    assert (output / 'notices/spelling/LICENSE').read_bytes()
    assert manifest['files']['wheelhouse/' + prepared_code_wheel.name] == builder.SPELLCHECK_SHA256
    assert 'spylls==0.1.7+carr.1 --hash=sha256:' + builder.SPELLCHECK_SHA256 in (
        output / 'install.lock').read_text(encoding='utf-8')


@pytest.mark.parametrize('prefix,count', [('windows', 17), ('windows-semantic', 53)])
def test_original_profile_pins_and_sbom_are_unchanged_when_optional_component_is_extended(
        prepared_code_wheel, prefix, count):
    builder, _, _ = packaging_modules()
    root = Path(__file__).parents[2]
    lock = root / 'packaging' / (prefix + '-cp311.lock')
    bom_path = root / 'packaging' / (prefix + '-sbom.cdx.json')
    original = lock.read_bytes(), bom_path.read_bytes()
    pins = builder.read_lock(lock)
    assert len(pins) == count and 'spylls' not in pins
    base = json.loads(original[1])
    assert len(base['components']) == count
    accepted = builder.spellcheck_snapshot(prepared_code_wheel)
    extended = json.loads(builder.spellcheck_sbom_bytes(base, accepted))
    assert len(extended['components']) == count + 1
    assert extended['components'][:-1] == base['components']
    assert extended['dependencies'][:-1] == base.get('dependencies', [])
    assert (lock.read_bytes(), bom_path.read_bytes()) == original


@pytest.mark.parametrize('change', ['declaration', 'install-lock', 'version', 'wheel'])
def test_installer_rejects_self_consistent_but_unannounced_or_wrong_spelling_contract(
        tmp_path, prepared_code_wheel, change):
    builder, installer, output, _, _ = spelling_bundle(tmp_path, prepared_code_wheel)
    manifest_path = output / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if change == 'declaration':
        del manifest['files']['spelling-component.json']
        (output / 'spelling-component.json').unlink()
    elif change == 'install-lock':
        path = output / 'install.lock'
        path.write_text('\n'.join(row for row in path.read_text().splitlines()
                                 if not row.startswith('spylls')) + '\n', encoding='utf-8')
        manifest['files']['install.lock'] = builder.sha256(path)
    elif change == 'version':
        path = output / 'spelling-component.json'
        value = json.loads(path.read_bytes()); value['provenance']['version'] = '0.1.7'
        path.write_text(json.dumps(value), encoding='utf-8')
        manifest['files']['spelling-component.json'] = builder.sha256(path)
    else:
        relative = 'wheelhouse/' + prepared_code_wheel.name
        path = output / relative
        path.write_bytes(b'unreviewed dictionary-bearing component')
        manifest['files'][relative] = builder.sha256(path)
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='spelling'):
        installer.verify(output)


def test_optional_builder_refuses_wrong_input_before_creating_output(tmp_path):
    builder, _, _ = packaging_modules()
    selected = tmp_path / builder.SPELLCHECK_NAME
    selected.write_bytes(b'not approved')
    with pytest.raises(ValueError, match='reviewed code-only'):
        builder.spellcheck_snapshot(selected)
