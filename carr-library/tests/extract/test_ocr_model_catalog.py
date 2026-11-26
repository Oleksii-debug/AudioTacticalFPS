"""Local-only model catalogue, immutable inputs and bounded pack preparation."""
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.extract import ocr_model_catalog as catalog


@pytest.fixture
def model_files(tmp_path, monkeypatch):
    root = tmp_path / 'Local models Українська'
    root.mkdir()
    contents = {'eng': b'synthetic English model\x00', 'script/Latin': b'synthetic script model\x01'}
    models = []
    for code, raw in contents.items():
        path = root / (code + '.traineddata')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        models.append(catalog.Model('fast', code, code, 'script' if '/' in code else 'language',
            len(raw), catalog._git_hash(raw), code + '.traineddata', 'https://example.invalid/model'))
    source = catalog.Source('fast', 'tesseract-ocr/tessdata_fast', 'a' * 40,
                            'Synthetic complete license notice.\n', tuple(models), (('alias', 'eng'),))
    monkeypatch.setattr(catalog, 'load_source', lambda profile='fast': source)
    return root, contents, source


@pytest.mark.parametrize('profile,total', [('fast', 700353173), ('best', 1774547803)])
def test_packaged_official_catalogue_has_all_regular_models(profile, total):
    source = catalog.load_source(profile)
    assert len(source.models) == 162
    assert sum(model.kind == 'language' for model in source.models) == 123
    assert sum(model.kind == 'script' for model in source.models) == 37
    assert {model.code for model in source.models if model.kind == 'special'} == {'osd', 'equ'}
    assert sum(model.size for model in source.models) == total
    assert 'TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION' in source.license_text
    assert all(model.url.startswith(f'https://raw.githubusercontent.com/{source.repository}/{source.revision}/')
               for model in source.models)
    assert ('frk', 'deu_latf') in source.aliases if profile == 'fast' else not source.aliases


@pytest.mark.parametrize('query', ['ukr', 'Ukrainian', 'українська'])
def test_offline_name_search(query):
    assert [model.code for model in catalog.list_models(query=query)] == ['ukr']


def test_script_and_special_filters_do_not_promise_reading_languages():
    assert len(catalog.list_models('best', kind='script')) == 37
    assert {model.code for model in catalog.list_models(kind='special')} == {'equ', 'osd'}
    assert catalog.list_models(query='no such model') == ()
    with pytest.raises(ValueError):
        catalog.list_models(kind='unknown')
    with pytest.raises(ValueError):
        catalog.load_source('unreviewed')


def test_verified_pack_retains_exact_bytes_license_and_installer_manifest(model_files, tmp_path):
    root, contents, source = model_files
    destination = tmp_path / 'Prepared pack'
    result = catalog.prepare_offline_pack(root, destination, ['eng', 'script/Latin'])
    raw = (destination / 'models.json').read_bytes()
    manifest = json.loads(raw)
    assert set(manifest) == {'schema', 'upstream', 'license', 'models'}
    assert manifest['schema'] == 1 and source.revision in manifest['upstream']
    assert source.license_text in manifest['license']
    assert result.manifest_sha256 == sha256(raw).hexdigest()
    assert result.models == ('eng', 'script/Latin')
    assert result.size_bytes == sum(map(len, contents.values()))
    for code, expected in contents.items():
        output = destination / (code + '.traineddata')
        assert output.read_bytes() == (root / (code + '.traineddata')).read_bytes() == expected
        assert output.stat().st_nlink == 1
        assert manifest['models'][code] == {'size': len(expected), 'sha256': sha256(expected).hexdigest()}
    assert (destination / 'LICENSE.txt').read_text() == source.license_text
    provenance = (destination / 'MODEL_PROVENANCE.json').read_text()
    assert str(root) not in provenance and str(destination) not in provenance
    assert not list(tmp_path.glob('.ocr-prepare-*'))


def test_alias_uses_regular_target_once(model_files, tmp_path):
    root, _, _ = model_files
    result = catalog.prepare_offline_pack(root, tmp_path / 'pack', ['alias', 'eng'])
    assert result.models == ('eng',)
    assert not (tmp_path / 'pack' / 'alias.traineddata').exists()


@pytest.mark.parametrize('contents', [b'x', b'Xynthetic English model\x00', b'synthetic English model\x00extra'])
def test_changed_or_wrong_profile_bytes_never_publish(model_files, tmp_path, contents):
    root, _, _ = model_files
    (root / 'eng.traineddata').write_bytes(contents)
    destination = tmp_path / 'pack'
    with pytest.raises(catalog.OcrRuntimeError):
        catalog.prepare_offline_pack(root, destination, ['eng'])
    assert not destination.exists()
    assert not list(tmp_path.glob('.ocr-prepare-*'))


@pytest.mark.parametrize('budget', [True, 0, -1, 1.5, 1])
def test_disk_budget_checked_before_output(model_files, tmp_path, budget):
    root, _, _ = model_files
    with pytest.raises(ValueError):
        catalog.prepare_offline_pack(root, tmp_path / 'pack', ['eng'], max_bytes=budget)
    assert not (tmp_path / 'pack').exists()


@pytest.mark.parametrize('codes', [[], ['missing'], ['../eng'], ['script/../eng'], [1]])
def test_only_catalogue_models_are_selectable(model_files, tmp_path, codes):
    root, _, _ = model_files
    with pytest.raises(ValueError):
        catalog.prepare_offline_pack(root, tmp_path / 'pack', codes)
    assert not (tmp_path / 'pack').exists()


def test_existing_output_is_preserved(model_files, tmp_path):
    root, _, _ = model_files
    destination = tmp_path / 'pack'
    destination.mkdir()
    marker = destination / 'keep.txt'
    marker.write_text('User file')
    with pytest.raises(FileExistsError):
        catalog.prepare_offline_pack(root, destination, ['eng'])
    assert marker.read_text() == 'User file'
    assert list(destination.iterdir()) == [marker]


def test_cancel_before_work_has_no_output(model_files, tmp_path):
    root, _, _ = model_files
    with pytest.raises(InterruptedError):
        catalog.prepare_offline_pack(root, tmp_path / 'pack', ['eng'], cancelled=lambda: True)
    assert not (tmp_path / 'pack').exists()


def test_cancel_after_verification_before_publication(model_files, tmp_path, monkeypatch):
    root, _, _ = model_files
    cancelled = False
    original = catalog._copy_verified
    def copying(*args):
        nonlocal cancelled
        result = original(*args)
        cancelled = True
        return result
    monkeypatch.setattr(catalog, '_copy_verified', copying)
    with pytest.raises(InterruptedError):
        catalog.prepare_offline_pack(root, tmp_path / 'pack', ['eng'], cancelled=lambda: cancelled)
    assert not (tmp_path / 'pack').exists()
    assert not list(tmp_path.glob('.ocr-prepare-*'))


def test_cancel_during_publication_removes_only_new_output(model_files, tmp_path, monkeypatch):
    root, _, _ = model_files
    cancelled = False
    original = os.link
    def linking(source, target):
        nonlocal cancelled
        original(source, target)
        cancelled = True
    monkeypatch.setattr(catalog.os, 'link', linking)
    with pytest.raises(InterruptedError):
        catalog.prepare_offline_pack(root, tmp_path / 'pack', ['eng'], cancelled=lambda: cancelled)
    assert not (tmp_path / 'pack').exists()
    assert (root / 'eng.traineddata').is_file()


def test_publication_failure_has_no_ready_manifest(model_files, tmp_path, monkeypatch):
    root, _, _ = model_files
    def unavailable(*args):
        raise OSError('Synthetic publication failure')
    monkeypatch.setattr(catalog.os, 'link', unavailable)
    with pytest.raises(OSError, match='publication'):
        catalog.prepare_offline_pack(root, tmp_path / 'pack', ['eng'])
    assert not (tmp_path / 'pack' / 'models.json').exists()
    assert not list(tmp_path.glob('.ocr-prepare-*'))


def test_failed_script_publication_removes_empty_directory_and_allows_retry(model_files, tmp_path, monkeypatch):
    root, _, _ = model_files
    original = os.link
    def linking(source, target):
        if str(target).endswith('Latin.traineddata'):
            raise OSError('Synthetic script publication failure')
        return original(source, target)
    destination = tmp_path / 'pack'
    with monkeypatch.context() as patched:
        patched.setattr(catalog.os, 'link', linking)
        with pytest.raises(OSError, match='publication'):
            catalog.prepare_offline_pack(root, destination, ['script/Latin'])
    assert not destination.exists()
    assert catalog.prepare_offline_pack(root, destination, ['script/Latin']).models == ('script/Latin',)


@pytest.mark.parametrize('link_type', ['file', 'directory', 'hardlink'])
def test_linked_inputs_are_rejected(model_files, tmp_path, link_type):
    root, _, _ = model_files
    original = root / 'eng.traineddata'
    if link_type == 'hardlink':
        os.link(original, tmp_path / 'another-name')
    elif link_type == 'file':
        outside = tmp_path / 'eng.traineddata'
        original.rename(outside)
        original.symlink_to(outside)
    else:
        linked = tmp_path / 'linked-model-directory'
        linked.symlink_to(root, target_is_directory=True)
        root = linked
    with pytest.raises(catalog.OcrRuntimeError):
        catalog.prepare_offline_pack(root, tmp_path / 'pack', ['eng'])
    assert not (tmp_path / 'pack').exists()


def test_offline_cli_outputs_search_results_and_reports_errors(capsys):
    assert catalog.main(['list', '--profile', 'best', '--query', 'ukr']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result[0]['code'] == 'ukr' and result[0]['profile'] == 'best'
    assert catalog.main(['prepare', '--source', '.', '--destination', '.', '--models', 'unreviewed']) == 1
    assert 'absent' in capsys.readouterr().err
