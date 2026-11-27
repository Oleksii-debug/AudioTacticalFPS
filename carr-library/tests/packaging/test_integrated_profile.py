"""Integrated release inventory and exact retained dependency boundaries."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('integrated_bundle_builder', ROOT / 'packaging/build_windows_bundle.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_integrated_lock_preserves_every_previously_admitted_semantic_hash():
    prior = builder.read_lock(ROOT / 'packaging/windows-semantic-cp311.lock')
    current = builder.read_lock(ROOT / 'packaging/windows-integrated-cp311.lock')
    assert len(prior) == 54 and len(current) == 56
    assert {name: current[name] for name in prior} == prior
    # pywin32 is already a shared, hash-pinned semantic dependency.
    assert set(current) - set(prior) == {'tuf', 'securesystemslib'}
    review = json.loads((ROOT / 'packaging/trusted-client-dependencies.json').read_text(encoding='utf-8'))
    for component in review['dependencies']:
        assert current[component['name']][0] == component['version']
        assert component['sha256'] in current[component['name']][1]
    assert current['pywin32'] == ('312', {
        'd11417d84412f859b722fad0841b3614459ed0047f7542d8362e77884f6b6e8a'})


def test_integrated_sbom_preserves_component_identities_and_has_no_missing_dependency():
    prior = json.loads((ROOT / 'packaging/windows-semantic-sbom.cdx.json').read_text(encoding='utf-8'))
    current = json.loads((ROOT / 'packaging/windows-integrated-sbom.cdx.json').read_text(encoding='utf-8'))
    pins = builder.read_lock(ROOT / 'packaging/windows-integrated-cp311.lock')
    by_name = {component['name']: component for component in current['components']}
    assert len(by_name) == len(current['components']) == 56
    assert {(name, row['version']) for name, row in by_name.items()} == {
        (name, version) for name, (version, hashes) in pins.items()}
    assert all(by_name[component['name']] == component for component in prior['components'])
    root = current['metadata']['component']['bom-ref']
    refs = {component['bom-ref'] for component in current['components']}
    graph = {node['ref']: set(node.get('dependsOn', ())) for node in current['dependencies']}
    assert set(graph) == refs | {root}
    assert all(children <= refs for children in graph.values())
    visited, pending = set(), [root]
    while pending:
        current_ref = pending.pop()
        if current_ref not in visited:
            visited.add(current_ref)
            pending.extend(graph[current_ref])
    assert visited == refs | {root}
    ref = lambda name: by_name[name]['bom-ref']
    assert graph[ref('tuf')] == {ref('securesystemslib'), ref('urllib3')}
    assert graph[ref('securesystemslib')] == {ref('cryptography')}
    assert not graph[ref('pywin32')]


def test_integrated_roots_and_mixed_word_notices_remain_explicit():
    pins = builder.read_lock(ROOT / 'packaging/windows-integrated-cp311.lock')
    roots = [line for line in (ROOT / 'packaging/windows-integrated-roots.in')
             .read_text(encoding='utf-8').splitlines() if line and not line.startswith('#')]
    for line in roots:
        name, version = line.split('==')
        assert pins[name.lower()][0] == version
    bom = json.loads((ROOT / 'packaging/windows-integrated-sbom.cdx.json').read_text(encoding='utf-8'))
    word = next(component for component in bom['components'] if component['name'] == 'pywin32')
    assert word['licenses'] == [{'license': {
        'name': 'Mixed upstream licences; retain all wheel License-File metadata and licence files'}}]
    for name in ('pywin32-312-COM-LICENSE.txt', 'pywin32-312-WIN32-LICENSE.txt'):
        data = (ROOT / 'packaging/wheel-notices' / name).read_bytes()
        assert len(data) > 500 and b'copyright' in data.lower()
    notes = {item['name']: item['value'] for item in bom['metadata']['properties']}
    assert 'not re-resolved by uv' in notes['carr:integrated-profile-composition']
