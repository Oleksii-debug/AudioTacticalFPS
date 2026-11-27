"""Synthetic installation metadata: no ASR package or model is executed."""
import shutil

import pytest

from carr_library.transcription import runtime as module


def install(site, name, *, suffix='0.1', record=True):
    package = name.replace('-', '_') + '.py'
    payload = site / package
    payload.write_bytes(b'# fixture only\n')
    info = site / (name.replace('-', '_') + '-' + suffix + '.dist-info')
    info.mkdir()
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {suffix}\n', encoding='utf-8')
    if record:
        (info / 'RECORD').write_text(f'{package},,\n{info.name}/METADATA,,\n{info.name}/RECORD,,\n', encoding='utf-8')
    return payload, info


@pytest.fixture
def root(tmp_path):
    root = tmp_path / 'Синтетичний runtime'
    site = root / 'Lib' / 'site-packages'
    site.mkdir(parents=True)
    for name in module.RUNTIME_DISTRIBUTIONS:
        install(site, name)
    return root, site


def test_additional_distribution_bound(root):
    base, site = root
    payload, _ = install(site, 'tqdm')
    before, _ = module._runtime_inventory([base])
    assert 'tqdm' in before
    payload.write_bytes(b'# modified only\n')
    after, _ = module._runtime_inventory([base])
    assert before['tqdm'] != after['tqdm']
    assert before['faster-whisper'] == after['faster-whisper']


def test_duplicate_normalized_distribution_rejected(root):
    base, site = root
    install(site, 'Extra.Dep')
    install(site, 'extra-dep', suffix='0.2')
    with pytest.raises(ValueError, match='ambiguous installed distribution'):
        module._runtime_inventory([base])


def test_additional_missing_record_rejected(root):
    base, site = root
    install(site, 'tqdm', record=False)
    with pytest.raises(ValueError, match='bounded installed file inventory'):
        module._runtime_inventory([base])


def test_missing_required_distribution_rejected(root):
    base, site = root
    shutil.rmtree(site / 'faster_whisper-0.1.dist-info')
    with pytest.raises(ValueError, match='faster-whisper'):
        module._runtime_inventory([base])


def test_total_file_budget(root, monkeypatch):
    base, _ = root
    monkeypatch.setattr(module, 'MAX_RUNTIME_FILES', 4)
    with pytest.raises(ValueError, match='total installed-file budget'):
        module._runtime_inventory([base])


def test_cancellation(root):
    base, _ = root
    with pytest.raises(InterruptedError):
        module._runtime_inventory([base], cancel=lambda: True)
