"""Real language discovery and safe offline model-pack publication."""
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from carr_library.extract.language_manager import LanguageModel, installed_languages, language_selection, install_offline_pack
from carr_library.extract.page_ocr import Config


def test_script_models_are_explicit_and_no_traversal_is_accepted():
    assert Config(languages='ukr+script/Latin').languages == 'ukr+script/Latin'
    for value in ('../eng', 'script/../eng', '/eng', 'eng+eng', 'http://remote', 'eng;touch'):
        with pytest.raises(ValueError):
            Config(languages=value)


def test_installed_selection_excludes_osd_and_missing_models():
    models = (LanguageModel('eng','English','language'), LanguageModel('script/Latin','Latin','script'),
              LanguageModel('osd','Orientation','orientation'))
    assert language_selection(('eng','script/Latin'), models) == 'eng+script/Latin'
    for values in ((), ('osd',), ('ukr',), ('eng','eng')):
        with pytest.raises(ValueError):
            language_selection(values, models)


@pytest.mark.skipif(not shutil.which('tesseract'), reason='No local Tesseract in this test environment')
def test_actual_installed_tesseract_inventory():
    models = installed_languages()
    assert any(model.code == 'eng' for model in models)
    assert all(model.kind in ('language','script','orientation') for model in models)


def test_offline_pack_checksums_no_overwrite_and_cancellation(tmp_path):
    source = tmp_path / 'pack'
    source.mkdir()
    content = b'synthetic pack bytes for installation integrity, not a working OCR model'
    (source / 'eng.traineddata').write_bytes(content)
    manifest = {'schema':1, 'upstream':'synthetic test only', 'license':'test fixture',
                'models':{'eng':{'size':len(content),'sha256':sha256(content).hexdigest()}}}
    raw = json.dumps(manifest).encode()
    (source / 'models.json').write_bytes(raw)
    checksum = sha256(raw).hexdigest()
    result = install_offline_pack(source, tmp_path / 'installed', expected_manifest_sha256=checksum)
    assert result['models'] == 1 and (tmp_path / 'installed/eng.traineddata').read_bytes() == content
    with pytest.raises(FileExistsError):
        install_offline_pack(source, tmp_path / 'installed', expected_manifest_sha256=checksum)
    with pytest.raises(InterruptedError):
        install_offline_pack(source, tmp_path / 'cancelled', expected_manifest_sha256=checksum, cancelled=lambda:True)
    assert not (tmp_path / 'cancelled').exists()
    (source / 'eng.traineddata').write_bytes(b'x' * len(content))
    with pytest.raises(ValueError, match='checksum'):
        install_offline_pack(source, tmp_path / 'corrupt', expected_manifest_sha256=checksum)
    assert not (tmp_path / 'corrupt').exists()
