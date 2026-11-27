"""Optional Word closure uses the current offline builder; no COM binary claims."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tomllib
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location('word_profile_' + name, ROOT / 'packaging' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_word_profile_extends_exact_base_graph_with_one_reviewed_platform_wheel():
    builder = module('build_windows_bundle')
    base = builder.read_lock(ROOT / 'packaging/windows-cp311.lock')
    word = builder.read_lock(ROOT / 'packaging/windows-word-cp311.lock')
    assert {key: value for key, value in word.items() if key != 'pywin32'} == base
    assert word['pywin32'][0] == '312'
    assert set(word['pywin32'][1]) == {'d11417d84412f859b722fad0841b3614459ed0047f7542d8362e77884f6b6e8a'}
    sbom = json.loads((ROOT / 'packaging/windows-word-sbom.cdx.json').read_text(encoding='utf-8'))
    assert {item['name'].lower().replace('_', '-'): item['version'] for item in sbom['components']} == {
        name: value[0] for name, value in word.items()}
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert project['project']['optional-dependencies']['word'] == ["pywin32==312; sys_platform == 'win32'"]


def test_com_notices_are_exact_hash_checked_and_selected_only_for_word():
    folder = ROOT / 'packaging/wheel-notices'
    selected = [item for item in json.loads((folder / 'SOURCES.json').read_text()) if item['component'] == 'pywin32']
    assert len(selected) == 2
    for item in selected:
        assert item['commit'] == '2a277cb5552756c2b4d42b524dc36d25e0bb6354'
        assert hashlib.sha256((folder / item['file']).read_bytes()).hexdigest() == item['sha256']
        assert 'redistribut' in (folder / item['file']).read_text().lower()


def wheel(folder, name, *, tag='py3-none-any', requirements=(), app=False):
    path = folder / f'{name}-312-{tag}.whl'
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        metadata = f'Metadata-Version: 2.3\nName: {name}\nVersion: 312\n'
        metadata += ''.join('Requires-Dist: ' + value + '\n' for value in requirements)
        archive.writestr(f'{name}-312.dist-info/METADATA', metadata)
        if app:
            archive.writestr('carr_library/__main__.py', 'print("synthetic packaging boundary")\n')
        else:
            archive.writestr('pywin32-312.dist-info/licenses/COM-LICENSE.txt', 'synthetic licence fixture')
    return path


def test_existing_builder_and_installer_keep_explicit_word_wheel_and_notices(tmp_path):
    builder, installer = module('build_windows_bundle'), module('install_windows')
    house = tmp_path / 'wheelhouse'
    house.mkdir()
    bridge = wheel(house, 'pywin32', tag='cp311-cp311-win_amd64')
    app = wheel(tmp_path, 'universal_research_library',
                requirements=("pywin32==312; sys_platform == 'win32' and extra == 'word'",), app=True)
    # Synthetic bytes have their OWN hash; never label them as the pinned wheel.
    lock = tmp_path / 'synthetic.lock'
    lock.write_text('pywin32==312 --hash=sha256:' + builder.sha256(bridge) + '\n')
    output = tmp_path / 'bundle'
    builder.build_bundle(app, house, lock, output)
    installer.verify(output)
    assert (output / 'wheelhouse' / bridge.name).read_bytes() == bridge.read_bytes()
    assert 'pywin32==312' in (output / 'install.lock').read_text()
    notices = json.loads((output / 'notices/wheels/SOURCES.json').read_text())
    assert {item['component'] for item in notices} == {'pywin32'}
    for item in notices:
        assert (output / 'notices/wheels' / item['file']).is_file()


def test_optional_native_probe_reports_not_run_and_required_qualification_fails(monkeypatch, capsys):
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('word_native_probe',
        ROOT / 'scripts/windows_word_citations_smoke.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    monkeypatch.setattr(probe, 'os', SimpleNamespace(name='posix'))
    assert probe.main([]) == 0
    optional = json.loads(capsys.readouterr().out)
    assert optional['status'] == optional['actual_office'] == 'NOT_RUN'
    assert optional['checks'] == 0 and optional['check_names'] == []
    assert probe.main(['--require-word']) == 2
    required = json.loads(capsys.readouterr().out)
    assert required['actual_office'] == 'NOT_RUN'
