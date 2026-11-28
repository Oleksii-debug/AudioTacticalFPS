"""Passive history is typed; no keys, runtime settings or authority are restored."""
from hashlib import sha256
import json
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.sync.state import NAME, MAX_BYTES, validate_portable_state


def history():
    return {'kind': 'carr-portable-state', 'version': 1, 'family_id': 'a' * 32,
            'vaults': {'b' * 64: {'head': 'c' * 64, 'label': 'Explicit retained checkpoint'}}}


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


@pytest.mark.parametrize('shape', ['native', 'flat'])
def test_passive_history_survives_actual_backup_without_creating_a_runtime(tmp_path, monkeypatch, shape):
    from carr_library.sync.service import SyncService
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Passive transfer history')
    original = raw(history())
    (app.root / NAME).write_bytes(original)
    monkeypatch.setattr(SyncService, '__init__',
        lambda *a, **k: pytest.fail('Passive recovery created a transfer service.'))
    target = tmp_path / 'backup'
    checksum = create_backup(app, target)
    manifest = json.loads((target / 'manifest.json').read_bytes())
    assert manifest['version'] == 5 and manifest['schema_version'] == 6
    assert (target / 'sidecars' / NAME).read_bytes() == original
    if shape == 'flat':
        assert manifest['state'] == []  # This fixture contains only top-level passive history.
        (target / 'state').rmdir()  # Empty roots have no declaration in the foreign flat envelope.
        entries = [{**row, 'filename': 'state/' + row['filename']} for row in manifest['state']]
        for row in manifest['sidecars']['files']:
            shutil.copyfile(target / 'sidecars' / row['path'], target / row['path'])
            entries.append({'filename': row['path'], 'size_bytes': row['size_bytes'],
                            'sha256': row['sha256']})
        shutil.rmtree(target / 'sidecars')
        manifest = {key: manifest[key] for key in
                    ('kind', 'schema_version', 'schema_fingerprint', 'database', 'assets')}
        manifest.update(version=4, sidecars=entries, generated_audio_included=False)
        document = raw(manifest)
        (target / 'manifest.json').write_bytes(document)
        checksum = sha256(document).hexdigest()
    restored = restore_backup(target, tmp_path / 'restored', checksum)
    assert (restored / NAME).read_bytes() == original
    assert not (restored / 'runtime').exists()
    assert validate_portable_state(original) == history()


@pytest.mark.parametrize('change', ['version-bool', 'extra-secret', 'foreign-head',
                                    'label-control', 'extra-path', 'family'])
def test_invalid_passive_history_cannot_complete_a_backup(tmp_path, change):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Reject forged history')
    value = history()
    if change == 'version-bool':
        value['version'] = True
    elif change == 'extra-secret':
        value['password'] = 'not a passive history field'
    elif change == 'foreign-head':
        value['vaults']['b' * 64]['head'] = '../outside'
    elif change == 'label-control':
        value['vaults']['b' * 64]['label'] = 'bad\x00label'
    elif change == 'extra-path':
        value['vaults']['b' * 64]['source'] = '/unapproved'
    else:
        value['family_id'] = 'F' * 32
    (app.root / NAME).write_bytes(raw(value))
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('value', [b'{}', b'{"version":1,"version":2}', b'NaN', b' ' * (MAX_BYTES + 1)],
                         ids=['missing-fields', 'duplicate', 'nonfinite', 'oversize'])
def test_history_byte_envelope_is_bounded_and_unambiguous(value):
    with pytest.raises(ValueError):
        validate_portable_state(value)
