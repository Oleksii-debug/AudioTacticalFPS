"""Actual local dist-info/RECORD bytes; no optional runtime, network or licence inference."""
import base64
import csv
from hashlib import sha256
from importlib import metadata
from io import StringIO
import json
from pathlib import Path
import stat
from types import SimpleNamespace as N

import pytest

from carr_library import component_notices as notices


def distribution(root, *, name='example-component', version='2.1',
                 text='Copyright Example Authors\nPermission applies to this component only.\n',
                 filename='LICENSE.txt', record=True, modern=True):
    directory = root / (name.replace('-', '_') + '-' + version + '.dist-info')
    directory.mkdir(parents=True)
    relative = directory.name + ('/licenses/' if modern else '/') + filename
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode('utf-8'))
    (directory / 'METADATA').write_text(
        'Metadata-Version: 2.4\nName: ' + name + '\nVersion: ' + version
        + '\nLicense-Expression: MIT AND BSD-3-Clause\nLicense-File: ' + filename + '\n\n',
        encoding='utf-8')
    if record:
        write_record(directory, [(relative, target.read_bytes())])
    return directory, target


def write_record(directory, entries):
    output = StringIO(newline='')
    writer = csv.writer(output)
    for name, data in entries:
        writer.writerow((name, 'sha256=' + base64.urlsafe_b64encode(sha256(data).digest()).decode().rstrip('='),
                         str(len(data))))
    writer.writerow((directory.name + '/RECORD', '', ''))
    (directory / 'RECORD').write_text(output.getvalue(), encoding='utf-8', newline='')


def selected(root):
    value = notices.catalogue(distribution_roots=(root,), bundle_root=None)
    assert len(value.components) == 1, value.warnings
    return value.components[0]


def bundle(root, entries):
    root.mkdir(exist_ok=True)
    files = {}
    for name, data in entries.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files[name] = sha256(data).hexdigest()
    value = dict(schema=1, target='Windows x64 / CPython 3.11',
        bundled_python=True, bundled_model=True, bundled_ocr=True,
        bundled_audio_runtime=True, bundled_native_runtime=True,
        bundled_native_launcher=True, files=files)
    (root / 'bundle-manifest.json').write_text(json.dumps(value), encoding='utf-8')


@pytest.mark.parametrize('modern', [True, False], ids=['license-directory', 'legacy-metadata-directory'])
def test_actual_installed_metadata_and_record_preserve_upstream_text(tmp_path, modern):
    text = 'Mixed original licence\r\nCopyright (c) Example 2026\nAll original conditions remain.\r\n'
    directory, path = distribution(tmp_path, text=text, modern=modern)
    actual = metadata.PathDistribution(directory)
    assert actual.metadata['Name'] == 'example-component'
    assert any(item.locate() == path for item in actual.files)
    component = selected(tmp_path)
    assert component.name == 'example-component' and component.version == '2.1'
    assert component.declared_license == 'MIT AND BSD-3-Clause'
    files = notices.notices(component)
    assert len(files) == 1
    rendered = notices.read_notice(files[0])
    assert rendered.endswith(text) and 'Recorded file hash matched.' in rendered
    assert sha256(path.read_bytes()).hexdigest() in rendered


def test_catalogue_uses_actual_installed_components_without_importing_optional_packages(tmp_path):
    for name in ('universal-research-library', 'pywin32', 'tuf', 'securesystemslib'):
        distribution(tmp_path, name=name, version='7')
    value = notices.catalogue(distribution_roots=(tmp_path,), bundle_root=None)
    assert {item.name for item in value.components} == {
        'universal-research-library', 'pywin32', 'tuf', 'securesystemslib'}
    assert value.version == '7'
    assert 'Resonance\nSceneria Innovative Technologies' in value.about
    assert 'separate command' in value.about and 'does not assess redistribution clearance' in value.about


def test_catalogue_and_selected_inventory_do_not_read_notice_payloads(tmp_path, monkeypatch):
    _, target = distribution(tmp_path)
    original = notices._capture
    reads = []
    def capture(root, relative, limit, cancelled):
        reads.append(relative)
        return original(root, relative, limit, cancelled)
    monkeypatch.setattr(notices, '_capture', capture)
    component = selected(tmp_path)
    files = notices.notices(component)
    assert target.relative_to(tmp_path).as_posix() not in reads
    notices.read_notice(files[0])
    assert target.relative_to(tmp_path).as_posix() in reads


@pytest.mark.parametrize('filename', ['../outside-LICENSE.txt', 'sub/../../LICENSE.txt', r'..\LICENSE.txt',
                                     'C:/LICENSE.txt', 'NUL.txt'], ids=['parent', 'nested-parent', 'backslash', 'drive', 'device'])
def test_unsafe_license_file_header_is_refused_before_selected_file_probe(tmp_path, monkeypatch, filename):
    directory, _ = distribution(tmp_path)
    metadata_path = directory / 'METADATA'
    metadata_path.write_text(metadata_path.read_text().replace('License-File: LICENSE.txt',
                                                           'License-File: ' + filename))
    component = selected(tmp_path)
    actual = notices._capture
    reads = []
    def capture(root, relative, limit, cancelled):
        reads.append(relative)
        return actual(root, relative, limit, cancelled)
    monkeypatch.setattr(notices, '_capture', capture)
    with pytest.raises(ValueError):
        notices.notices(component)
    assert reads == [component.metadata_stamp.relative]


def test_record_parent_notice_is_refused_but_unrelated_script_rows_are_never_resolved(tmp_path):
    directory, target = distribution(tmp_path)
    path = directory / 'RECORD'
    with path.open('a', encoding='utf-8') as stream:
        stream.write('../../../Scripts/unrelated-script.exe,,\n')
    assert notices.read_notice(notices.notices(selected(tmp_path))[0]).endswith(target.read_bytes().decode('utf-8'))
    with path.open('a', encoding='utf-8') as stream:
        stream.write('../outside-LICENSE.txt,,\n')
    with pytest.raises(ValueError, match='Unsafe'):
        notices.notices(selected(tmp_path))


@pytest.mark.parametrize('damage', ['payload', 'size', 'record', 'metadata'])
def test_selected_notice_rejects_changed_content_or_inventory(tmp_path, damage):
    directory, target = distribution(tmp_path)
    entry = notices.notices(selected(tmp_path))[0]
    if damage == 'payload':
        target.write_text('Foreign replacement with altered bytes')
    elif damage == 'size':
        entry = notices.Notice(entry.component, entry.title, entry.root, entry.relative,
            entry.guards, entry.algorithm, entry.digest, entry.expected_size + 1)
    elif damage == 'record':
        with (directory / 'RECORD').open('a') as stream:
            stream.write('foreign.txt,,\n')
    else:
        with (directory / 'METADATA').open('a') as stream:
            stream.write('changed\n')
    with pytest.raises(ValueError):
        notices.read_notice(entry)


@pytest.mark.parametrize('guard', ['METADATA', 'RECORD'])
def test_notice_read_rechecks_inventory_after_payload_capture(tmp_path, monkeypatch, guard):
    directory, target = distribution(tmp_path)
    entry = notices.notices(selected(tmp_path))[0]
    original = notices._capture
    def capture(root, relative, limit, cancelled):
        result = original(root, relative, limit, cancelled)
        if relative == entry.relative:
            with (directory / guard).open('a') as stream:
                stream.write('changed after selected payload\n')
        return result
    monkeypatch.setattr(notices, '_capture', capture)
    with pytest.raises(ValueError, match='changed|bounded'):
        notices.read_notice(entry)


def test_notice_byte_budget_and_cooperative_cancellation(tmp_path, monkeypatch):
    _, target = distribution(tmp_path, text='x' * 200_000)
    entry = notices.notices(selected(tmp_path))[0]
    monkeypatch.setattr(notices, 'MAX_NOTICE', 128)
    with pytest.raises(ValueError, match='bounded'):
        notices.read_notice(entry)
    monkeypatch.setattr(notices, 'MAX_NOTICE', 300_000)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 13
    with pytest.raises(InterruptedError):
        notices.read_notice(entry, cancelled=cancelled)
    assert target.stat().st_size == 200_000


def test_catalogue_cancellation_is_not_converted_to_partial_availability(tmp_path, monkeypatch):
    distribution(tmp_path)
    def stopped(*args, **kwargs):
        raise InterruptedError('Stop during a real metadata boundary')
    monkeypatch.setattr(notices, '_capture', stopped)
    with pytest.raises(InterruptedError):
        notices.catalogue(distribution_roots=(tmp_path,), bundle_root=None)


def test_aggregate_metadata_budget_stops_before_reading_another_distribution(tmp_path, monkeypatch):
    first, _ = distribution(tmp_path, name='aaa')
    distribution(tmp_path, name='zzz')
    size = (first / 'METADATA').stat().st_size
    monkeypatch.setattr(notices, 'MAX_METADATA_TOTAL', size)
    value = notices.catalogue(distribution_roots=(tmp_path,), bundle_root=None)
    assert [item.name for item in value.components] == ['aaa']
    assert any('aggregate budget' in warning for warning in value.warnings)


def test_declared_legacy_notice_without_record_is_explicitly_unhashed(tmp_path):
    _, target = distribution(tmp_path, record=False, modern=False)
    files = notices.notices(selected(tmp_path))
    assert len(files) == 1
    text = notices.read_notice(files[0])
    assert text.endswith(target.read_bytes().decode('utf-8')) and 'no recorded file hash' in text


def test_record_cannot_silently_admit_new_declared_notice(tmp_path):
    directory, _ = distribution(tmp_path)
    (directory / 'RECORD').write_text(directory.name + '/RECORD,,\n')
    with pytest.raises(ValueError, match='missing from its installed RECORD'):
        notices.notices(selected(tmp_path))


def test_controlled_windows_reparse_parent_is_refused_before_notice_read(tmp_path, monkeypatch):
    _, target = distribution(tmp_path)
    entry = notices.notices(selected(tmp_path))[0]
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == target.parent:
            return N(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='reparse'):
        notices.read_notice(entry)


def test_sealed_bundle_groups_read_actual_notices_without_reading_models_or_engines(tmp_path, monkeypatch):
    root = tmp_path / 'bundle'
    entries = {'notices/python/LICENSE.txt': b'Python upstream',
        'notices/wheels/pywin32-COM-LICENSE.txt': b'COM upstream',
        'ocr/tessdata/LICENSE.txt': b'OCR model upstream',
        'audio-runtime/FFmpeg-LICENSE.txt': b'FFmpeg upstream',
        'native-runtime/licenses/LICENSE.TXT': b'Native upstream',
        'models/semantic/MODEL_LICENSE.txt': b'Semantic upstream',
        'native-launcher-build.json': b'{"build":"provenance only"}',
        'models/semantic/model.onnx': b'NOT A REAL MODEL',
        'audio-runtime/ffmpeg.exe': b'NOT A REAL ENGINE'}
    bundle(root, entries)
    original = notices._capture
    reads = []
    def capture(root, relative, limit, cancelled):
        reads.append(relative)
        return original(root, relative, limit, cancelled)
    monkeypatch.setattr(notices, '_capture', capture)
    value = notices.catalogue(distribution_roots=(), bundle_root=root)
    assert len(value.components) == 7
    for component in value.components:
        files = notices.notices(component)
        assert len(files) == 1
        assert notices.read_notice(files[0]).endswith(entries[files[0].relative].decode())
    assert not any(path.endswith(('.onnx', '.exe')) for path in reads)


@pytest.mark.parametrize('damage', ['notice', 'manifest'])
def test_bundle_notice_or_inventory_tampering_is_refused(tmp_path, damage):
    root = tmp_path / 'bundle'
    bundle(root, {'audio-runtime/FFmpeg-LICENSE.txt': b'Original notice'})
    component = notices.catalogue(distribution_roots=(), bundle_root=root).components[0]
    entry = notices.notices(component)[0]
    if damage == 'notice':
        (root / entry.relative).write_text('Foreign notice')
    else:
        with (root / 'bundle-manifest.json').open('a') as stream:
            stream.write(' ')
    with pytest.raises(ValueError):
        notices.read_notice(entry)


def test_development_and_missing_notices_are_explicit_and_do_not_probe_external_roots(tmp_path):
    value = notices.catalogue(distribution_roots=(tmp_path,), bundle_root=None)
    assert value.components == () and value.warnings
    assert 'unavailable in installed metadata' in value.version
    assert 'development/standalone' in value.about



def test_long_bounded_mixed_licence_metadata_does_not_hide_an_installed_component(tmp_path):
    directory, _ = distribution(tmp_path)
    path = directory / 'METADATA'
    terms = 'Upstream copyright Україна and mixed component terms. ' * 1000
    path.write_text(path.read_text().replace('License-Expression: MIT AND BSD-3-Clause',
                                           'License: ' + terms), encoding='utf-8')
    component = selected(tmp_path)
    assert component.name == 'example-component'
    assert component.declared_license == terms
