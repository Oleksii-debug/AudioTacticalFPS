"""The shared Unicode staging path must retain the selected external pack."""
from hashlib import sha256

import pytest

from carr_library.extract.language_manager import VerifiedModelPack
from carr_library.extract.ocr_runtime import OcrRuntime, OcrRuntimeError, PackOcrRuntime
from carr_library.extract.ocr_process import _model_expectations


@pytest.fixture
def selected_pack(tmp_path):
    root = tmp_path / 'Окремий перевірений пакет'
    root.mkdir()
    models = []
    for code, content in (('eng', b'synthetic selected English model'),
                          ('osd', b'synthetic selected orientation model')):
        (root / (code + '.traineddata')).write_bytes(content)
        models.append((code, len(content), sha256(content).hexdigest()))
    return VerifiedModelPack(root, 'a' * 64, 'synthetic compatibility fixture', tuple(models))


def base_runtime(tmp_path, portable):
    if not portable:
        return OcrRuntime('synthetic-installed-engine')
    root = tmp_path / 'portable engine'
    directory = root / 'tessdata'
    directory.mkdir(parents=True)
    return OcrRuntime('synthetic-portable-engine', tessdata=directory, root=root,
                      manifest_sha256='b' * 64,
                      files_sha256=(('tessdata/eng.traineddata', 'c' * 64),))


@pytest.mark.parametrize('portable', [False, True], ids=['system-engine', 'portable-engine'])
def test_external_verified_pack_supplies_its_own_approved_hashes(tmp_path, selected_pack, portable):
    runtime = PackOcrRuntime(base_runtime(tmp_path, portable), selected_pack)
    expected = {code: digest for code, _, digest in selected_pack.models}
    assert _model_expectations(runtime, selected_pack.root) == expected
    assert expected['eng'] != 'c' * 64


@pytest.mark.parametrize('portable', [False, True], ids=['system-engine', 'portable-engine'])
def test_external_verified_pack_refuses_a_different_staging_source(tmp_path, selected_pack, portable):
    runtime = PackOcrRuntime(base_runtime(tmp_path, portable), selected_pack)
    other = tmp_path / 'different models'
    other.mkdir()
    with pytest.raises(OcrRuntimeError, match='selected.*pack'):
        _model_expectations(runtime, other)


def test_plain_portable_runtime_keeps_its_existing_model_binding(tmp_path):
    runtime = base_runtime(tmp_path, True)
    assert _model_expectations(runtime, runtime.tessdata) == {'eng': 'c' * 64}
