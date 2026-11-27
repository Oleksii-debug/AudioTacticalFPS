"""Managed profiles coexist with reviewed packs through one Config authority."""
import json
from pathlib import Path
import re
import subprocess

import pytest

from carr_library.extract.ocr_languages import (discover_languages, load_presets,
    store_preset, FILENAME, validate_presets_bytes)
from carr_library.extract.ocr_model_store import OcrModelStore
from carr_library.extract.ocr_runtime import discover_ocr_runtime
from carr_library.extract.page_ocr import Config


def test_config_rejects_ambiguous_model_authorities(tmp_path):
    for profile in ('fast', 'best'):
        with pytest.raises(ValueError, match='one verified external pack'):
            Config(model_profile=profile, model_pack_path=str(tmp_path), model_pack_sha256='a' * 64)


def test_managed_presets_roundtrip_with_old_plain_and_pack_presets(tmp_path):
    store_preset(tmp_path, 'Installed', Config(languages='ukr'))
    pack = Config(languages='slk', model_pack_path=str(tmp_path / 'pack'), model_pack_sha256='a' * 64)
    store_preset(tmp_path, 'Reviewed pack', pack)
    managed = Config(languages='script/Latin+ukr', model_profile='best')
    store_preset(tmp_path, 'Managed', managed)
    raw = (tmp_path / FILENAME).read_bytes()
    assert validate_presets_bytes(raw)['version'] == 3
    rows = {row['name']: row for row in load_presets(tmp_path)}
    assert Config(**{k: v for k, v in rows['Managed'].items() if k != 'name'}) == managed
    assert Config(**{k: v for k, v in rows['Reviewed pack'].items() if k != 'name'}) == pack
    assert 'model_profile' not in rows['Installed']
    invalid = json.loads(raw)
    invalid['presets'][0]['model_profile'] = 'future-profile'
    with pytest.raises(ValueError):
        validate_presets_bytes(json.dumps(invalid).encode())


def test_actual_managed_multilingual_inventory_uses_shared_discovery(tmp_path, monkeypatch):
    runtime = discover_ocr_runtime()
    if runtime is None:
        pytest.skip('Actual local OCR engine is required.')
    report = subprocess.check_output(runtime.command([runtime.executable, '--list-langs']), text=True)
    match = re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.M)
    assert match
    source = Path(match[1])
    if any(not (source / (code + '.traineddata')).is_file() for code in ('eng', 'ukr', 'slk')):
        pytest.skip('Official English, Ukrainian and Slovak test models are required.')
    monkeypatch.setenv('CARR_OCR_MODELS_DIR', str(tmp_path / 'managed'))
    store = OcrModelStore()
    store.install('fast', source, ('eng', 'ukr', 'slk'))
    selected = discover_languages(model_profile='fast')
    assert selected.codes == ('eng', 'slk', 'ukr')
    assert selected.integrity == 'verified_managed_profile'
    assert selected.validate('ukr+eng+slk') == ('ukr', 'eng', 'slk')
    model = store.select('fast', 'ukr').directory / 'ukr.traineddata'
    model.write_bytes(b'Changed installed model cannot pass selection.')
    with pytest.raises(ValueError):
        discover_languages(model_profile='fast')
