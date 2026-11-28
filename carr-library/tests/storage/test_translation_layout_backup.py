"""Actual native5/flat4 recovery of partitioned translation/1 jobs.

The existing typed backup, provider revocation and usage receipts are used.
Restored document export is offline; it does not approve another translation.
"""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import runpy
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Gateway
from carr_library.assistant.translation import TranslationService
from carr_library.assistant.translation_layout import capture_layout, review_layout, prepare_layout, export_layout
from carr_library.durable_state import encode, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import create_backup, restore_backup, verify_backup

TESTS = Path(__file__).resolve().parents[1]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

_docx = module('backup_docx_translation_fixture', TESTS / 'assistant/test_translation_layout.py')
_epub = module('backup_epub_translation_fixture', TESTS / 'assistant/test_translation_epub_layout.py')
_base = module('backup_current_translation_gateway', TESTS / 'assistant/test_library_translation.py')
setup = _base.setup
FLAT = runpy.run_path(str(Path(__file__).with_name('test_canonical_backup_compat.py')))


def authored(case, tmp_path, kind):
    raw = _docx.docx_bytes() if kind == 'docx' else _epub.epub_bytes()
    source = tmp_path / ('Original Ω.' + kind)
    source.write_bytes(raw)
    imported = case.app._import_file(case.workspace, source, title='Passive formatted translation',
        work_id='backup-layout-work', edition_id='backup-layout-edition')
    assert imported['state'] == 'complete'
    KeybindingStore(case.app.root).save(
        [Command('fixture.translation-layout', 'Synthetic formatted-translation backup')],
        {'fixture.translation-layout': 'Ctrl+F9'})
    captured = capture_layout(case.app, case.workspace, 'backup-layout-edition', kind=kind)
    review = review_layout(case.service, case.profile, captured, 'en', 'uk', '1')
    identity = prepare_layout(case.service, case.profile, captured, 'en', 'uk', '1',
                              approved_review=review['review_sha256'])
    assert case.service.resume(identity)['status'] == 'complete'
    return source, raw, captured, identity


def snapshot(case, directory, flat):
    if flat:
        return FLAT['_flat_backup'](case.app, directory)
    checksum = create_backup(case.app, directory)
    return read_json(directory / 'manifest.json'), checksum


@pytest.mark.parametrize('flat', [False, True], ids=['native5', 'flat4'])
@pytest.mark.parametrize('kind', ['docx', 'epub'])
def test_actual_recovery_preserves_partition_receipts_and_exports_without_original_paths_or_credentials(
        setup, tmp_path, monkeypatch, flat, kind):
    case = setup
    source, raw, captured, identity = authored(case, tmp_path, kind)
    before = case.service.get(identity)
    baseline = tmp_path / ('Baseline.' + kind)
    export_layout(case.service, identity, baseline)
    expected = baseline.read_bytes()
    directory = tmp_path / 'backup'
    manifest, checksum = snapshot(case, directory, flat)
    assert manifest['version'] == (4 if flat else 5) and manifest['schema_version'] == 6
    assert verify_backup(directory, checksum)
    source.unlink()
    case.app.close_search()
    shutil.rmtree(case.app.root)
    restored_root = restore_backup(directory, tmp_path / 'restored', checksum)
    app = LibraryApp(restored_root)
    try:
        service = TranslationService(app.root / 'state/translation', library=app, workspace=case.workspace,
            gateway=Gateway(app.root / 'state/translation/provider'))
        calls = list(case.calls)
        def forbidden(*args, **kwargs):
            pytest.fail('Passive recovered export attempted provider or credential access.')
        monkeypatch.setattr(service.gateway, 'request', forbidden)
        monkeypatch.setattr(service.gateway.vault, 'get', forbidden)
        restored = service.get(identity)
        assert restored['source_partition'] == before['source_partition'] == captured.source_partition
        assert restored['source_binding'] == before['source_binding']
        assert restored['receipts'] == before['receipts'] and restored['chunks'] == before['chunks']
        assert restored['restore_requires_review'] is True and restored['profile']['enabled'] is False
        assert KeybindingStore(app.root).load() == {'fixture.translation-layout': 'Ctrl+F9'}
        assert not ProfileStore(app.root / 'state/providers').get(case.profile.id).enabled
        with pytest.raises(PermissionError, match='fresh review'):
            service.resume(identity)
        target = tmp_path / ('Recovered.' + kind)
        receipt = export_layout(service, identity, target)
        assert receipt['status'] == 'complete' and receipt['job_id'] == identity
        assert target.read_bytes() == expected
        assert app.assets.resolve(captured.mapping['source_sha256']).read_bytes() == raw
        assert service.get(identity) == restored and case.calls == calls
        assert not source.exists() and not case.app.root.exists()
    finally:
        app.close_search()


@pytest.mark.parametrize('flat', [False, True], ids=['native5', 'flat4'])
@pytest.mark.parametrize('damage', ['null', 'boundaries', 'profile', 'mapping'])
def test_rehashed_partition_damage_is_rejected_before_restore_publication(setup, tmp_path, flat, damage):
    case = setup
    _, _, _, identity = authored(case, tmp_path, 'docx')
    relative = case.service.jobs.path(identity).relative_to(case.app.root)
    original = case.service.jobs.path(identity).read_bytes()
    directory = tmp_path / 'bad-backup'
    manifest, _ = snapshot(case, directory, flat)
    path = directory / relative
    value = read_json(path)
    if damage == 'null':
        value['source_partition'] = None
    elif damage == 'boundaries':
        value['source_partition']['boundaries'][-1] -= 1
    elif damage == 'profile':
        value['source_partition']['profile'] = 'unapproved-renderer'
    else:
        value['source_partition']['mapping_sha256'] = '0' * 64
    changed = encode(value)
    path.write_bytes(changed)
    entries = manifest['sidecars'] if flat else manifest['state']
    name = relative.as_posix() if flat else relative.relative_to('state').as_posix()
    entry, = [row for row in entries if row['filename'] == name]
    entry.update(size_bytes=len(changed), sha256=sha256(changed).hexdigest())
    raw = encode(manifest)
    (directory / 'manifest.json').write_bytes(raw)
    target = tmp_path / 'must-not-exist'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(directory, target, sha256(raw).hexdigest())
    assert not target.exists() and path.read_bytes() == changed
    assert case.service.jobs.path(identity).read_bytes() == original
    assert (directory / 'manifest.json').read_bytes() == raw
