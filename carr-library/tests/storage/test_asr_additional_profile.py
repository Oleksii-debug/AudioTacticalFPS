"""Cross-boundary regression for recorded ASR dependencies; no actual ASR inference."""
import importlib.util
from pathlib import Path

import pytest

from carr_library import backup_transcription
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.transcription import TranscriptionService
from carr_library.transcription.runtime import MAX_RUNTIME_FILES


def _package(count=3):
    return {'version': '0.0.synthetic', 'file_count': count,
            'installed_files_sha256': 'a' * 64}


@pytest.mark.parametrize('extra', [
    {}, [], {'numpy': _package()}, {'Extra_Dep': _package()},
    {'tqdm': _package(True)}, {'tqdm': _package(65537)},
    {'tqdm': {**_package(), 'installed_files_sha256': 'x' * 64}},
    {'tqdm': {**_package(), 'unknown': True}},
    {f'package-{i}': _package(65536) for i in range(4)},
])
def test_invalid_additional_inventory_rejected(extra):
    core = {name: _package() for name in backup_transcription._PACKAGES}
    with pytest.raises(ValueError):
        backup_transcription._additional_packages(core, extra)


def test_additional_inventory_exact_budget():
    core = {name: _package() for name in backup_transcription._PACKAGES}
    remainder = MAX_RUNTIME_FILES - sum(item['file_count'] for item in core.values())
    extra = {}
    while remainder:
        count = min(65536, remainder)
        extra[f'package-{len(extra)}'] = _package(count)
        remainder -= count
    backup_transcription._additional_packages(core, extra)


def test_registered_extra_distribution_backup_restore(tmp_path):
    fixture_path = Path(__file__).parents[1] / 'transcription' / 'fixture_runtime.py'
    spec = importlib.util.spec_from_file_location('asr_additional_profile_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    engine = fixture.make_runtime(tmp_path / 'external-engine')
    payload = engine.site / 'synthetic_extra.py'
    payload.write_text('# synthetic dependency; never executed\n', encoding='utf-8')
    info = engine.site / 'synthetic_extra-0.0.synthetic.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text(
        'Metadata-Version: 2.1\nName: synthetic-extra\nVersion: 0.0.synthetic\n',
        encoding='utf-8')
    (info / 'RECORD').write_text(
        f'{payload.name},,\n{info.name}/METADATA,,\n{info.name}/RECORD,,\n',
        encoding='utf-8')
    app = LibraryApp(tmp_path / 'library')
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    service = TranscriptionService(app.root / 'state' / 'transcription', broker=broker)
    profile = fixture.register(service, engine)
    assert 'synthetic-extra' in profile['runtime_additional_distributions']
    assert set(profile['runtime_distributions']) == backup_transcription._PACKAGES
    source = tmp_path / 'input.wav'
    original = b'Synthetic retained source; never decoded by an inference engine.'
    source.write_bytes(original)
    job = service.create_job(profile['id'], [source])
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    source.unlink()
    destination = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = TranscriptionService(destination / 'state' / 'transcription')
    declaration, = restored.list_runtimes()
    assert declaration['id'] == profile['id']
    assert declaration['fingerprint'] == profile['fingerprint']
    assert declaration['runtime_additional_distributions'] == profile['runtime_additional_distributions']
    assert declaration['trusted'] is False and declaration['restored'] is True
    recovered = restored.get_job(job['id'])
    assert recovered['status'] == 'interrupted' and recovered['restored']
    retained = restored.jobs_root / job['id'] / recovered['sources'][0]['retained_path']
    assert retained.read_bytes() == original
