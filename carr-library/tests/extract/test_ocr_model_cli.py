"""Catalogue provenance boundaries and headless, offline CLI model workflows."""
from copy import deepcopy
from hashlib import sha1, sha256
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

import pytest

from carr_library import __main__ as launcher
from carr_library.extract import model_catalog as catalogue
from carr_library.extract import ocr_model_catalog as official


@pytest.mark.parametrize('profile', ['fast', 'best'])
def test_cli_catalogue_search_is_headless_offline_and_does_not_create_store(
        profile, tmp_path, monkeypatch, capsys):
    root = tmp_path / 'not yet installed'
    monkeypatch.setitem(sys.modules, 'wx', None)
    monkeypatch.setattr(socket.socket, 'connect',
                        lambda *args, **kwargs: pytest.fail('Catalogue listing must be offline'))
    result = launcher.main(['--list-ocr-models', '--ocr-profile', profile,
        '--ocr-model-search', 'script Cyrillic', '--ocr-model-store', str(root)])
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output['network_access'] is False
    assert output['repository'] == 'tesseract-ocr/tessdata_' + profile
    assert output['revision'] == catalogue.REVISIONS[profile]
    assert [row['identifier'] for row in output['models']] == ['script/Cyrillic']
    row = output['models'][0]
    assert row['installed'] is False and output['total_bytes'] == row['size']
    assert row['source_url'].endswith('/' + catalogue.REVISIONS[profile] + '/script/Cyrillic.traineddata')
    assert not root.exists()


def test_cli_actual_official_model_install_verify_and_list(tmp_path, monkeypatch, capsys):
    executable = shutil.which('tesseract')
    if executable is None:
        pytest.skip('Actual local Tesseract models required for CLI integration')
    report = subprocess.check_output([executable, '--list-langs'], text=True)
    match = re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.M)
    assert match is not None
    source = Path(match[1])
    raw = (source / 'eng.traineddata').read_bytes()
    expected = next(row for row in catalogue.models('fast') if row.identifier == 'eng')
    digest = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
    if len(raw) != expected.size or digest != expected.blob_sha1:
        pytest.skip('Actual English model differs from pinned official revision')
    root = tmp_path / 'CLI models Україна'
    common = ['--ocr-profile', 'fast', '--ocr-model-store', str(root)]
    monkeypatch.setitem(sys.modules, 'wx', None)
    assert launcher.main(['--install-ocr-models', str(source), '--ocr-model', 'eng', *common]) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed['installed'] == ['eng'] and installed['bytes'] == len(raw)
    assert launcher.main(['--verify-ocr-models', 'eng', *common]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified['model_sha256'] == {'eng': sha256(raw).hexdigest()}
    assert str(root) not in json.dumps(verified)
    assert launcher.main(['--list-ocr-models', '--ocr-model-search', 'eng', *common]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert next(row for row in listed['models'] if row['identifier'] == 'eng')['installed'] is True


@pytest.mark.parametrize('options', [
    ['--ocr-model', 'eng'],
    ['--ocr-model-search', 'ukr'],
    ['--list-ocr-models', '--self-test'],
    ['--list-ocr-models', '--document-formats'],
    ['--list-ocr-models', '--lookup-doi', '10.1000/example'],
    ['--list-ocr-models', '--audio-voices'],
    ['--install-ocr-models', 'offline-source', '--ocr-all-models', '--ocr-model', 'eng'],
])
def test_ambiguous_cli_operations_fail_before_opening_the_application(options, monkeypatch):
    from carr_library import application
    monkeypatch.setattr(application, 'LibraryApp',
                        lambda *args, **kwargs: pytest.fail('Invalid model command must not open a library'))
    with pytest.raises(SystemExit) as error:
        launcher.main(options)
    assert error.value.code == 2


def test_empty_verification_selection_never_falls_through_to_desktop(tmp_path, monkeypatch, capsys):
    from carr_library import application
    monkeypatch.setenv('CARR_OCR_MODELS_DIR', str(tmp_path / 'empty models'))
    monkeypatch.setattr(application, 'LibraryApp',
                        lambda *args, **kwargs: pytest.fail('An OCR verification must not open a library'))
    assert launcher.main(['--verify-ocr-models', '', '--data-dir', str(tmp_path / 'untouched')]) == 1
    assert 'language selection' in capsys.readouterr().err.casefold()
    assert not (tmp_path / 'untouched').exists()


@pytest.mark.parametrize('corruption', ['wrong-upstream', 'wrong-revision', 'source-traversal', 'case-collision'])
def test_catalogue_rejects_mixed_provenance_paths_and_case_ambiguity(
        corruption, tmp_path, monkeypatch):
    original = official.files(official.__package__).joinpath(official.CATALOGUE)
    data = deepcopy(json.loads(original.read_bytes()))
    fast = data['sources']['fast']
    if corruption == 'wrong-upstream':
        fast['repository'] = 'tesseract-ocr/tessdata_best'
    elif corruption == 'wrong-revision':
        fast['revision'] = catalogue.REVISIONS['best']
    elif corruption == 'source-traversal':
        fast['models']['eng']['path'] = '../eng.traineddata'
    else:
        fast['models']['ENG'] = {**fast['models']['eng'], 'path': 'ENG.traineddata'}
    (tmp_path / official.CATALOGUE).write_text(json.dumps(data), encoding='utf-8')
    with monkeypatch.context() as patch:
        patch.setattr(official, 'files', lambda package: tmp_path)
        catalogue._catalogue.cache_clear()
        try:
            with pytest.raises(ValueError):
                catalogue.models('fast')
        finally:
            catalogue._catalogue.cache_clear()
