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


def test_profile_additional_identity_preserves_worker_checkpoint_dialect(root):
    base, site = root
    executable = base / 'python.exe'
    executable.write_bytes(b'fixture interpreter; not executed')
    worker = base / 'worker.py'
    worker.write_bytes(b'# fixture worker; not executed')
    model = base / 'model'
    model.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (model / name).write_bytes(b'fixture model identity')
    legacy = module.inspect_runtime(executable, worker, model)
    assert 'runtime_additional_distributions' not in legacy
    payload, _ = install(site, 'tqdm')
    before = module.inspect_runtime(executable, worker, model)
    assert before['runtime_distributions'] == legacy['runtime_distributions']
    assert set(before['runtime_distributions']) == set(module.RUNTIME_DISTRIBUTIONS)
    assert set(before['runtime_additional_distributions']) == {'tqdm'}
    assert before['fingerprint'] != legacy['fingerprint']
    payload.write_bytes(b'# modified only\n')
    after = module.inspect_runtime(executable, worker, model)
    assert before['runtime_distributions'] == after['runtime_distributions']
    assert before['fingerprint'] != after['fingerprint']


@pytest.mark.parametrize('omitted', ('METADATA', 'RECORD'))
def test_required_inventory_records(root, omitted):
    base, site = root
    _, info = install(site, 'tqdm')
    record = info / 'RECORD'
    record.write_text(''.join(line for line in record.read_text(encoding='utf-8').splitlines(keepends=True)
                              if not line.startswith(f'{info.name}/{omitted},')), encoding='utf-8')
    with pytest.raises(ValueError, match='omits METADATA or RECORD'):
        module._runtime_inventory([base])


def test_duplicate_absolute_alias(root):
    base, site = root
    payload, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write(f'{payload.as_posix()},,\n')
    with pytest.raises(ValueError, match='duplicate resolved installed-file records'):
        module._runtime_inventory([base])


def test_link_within_root(root):
    base, site = root
    payload, _ = install(site, 'tqdm')
    target = site / 'other.py'
    payload.rename(target)
    try:
        payload.symlink_to(target)
    except OSError:
        pytest.skip('No symlink permission')
    with pytest.raises(ValueError, match='without links'):
        module._runtime_inventory([base])


def test_valid_absolute_record(root):
    base, site = root
    payload, info = install(site, 'tqdm')
    (info / 'RECORD').write_text(
        f'{payload.as_posix()},,\n{(info / "METADATA").as_posix()},,\n{(info / "RECORD").as_posix()},,\n',
        encoding='utf-8')
    inventory, _ = module._runtime_inventory([base])
    assert inventory['tqdm']['file_count'] == 3
