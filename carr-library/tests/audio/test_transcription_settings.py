import json
from pathlib import Path

import pytest

from carr_library.audio import transcription_settings as settings


def test_default_connection_is_disabled(tmp_path):
    assert settings.load_settings(tmp_path) == {'version': 1, 'enabled': False, 'manifest_path': ''}


def test_restore_disables_without_probing_external_runtime(monkeypatch):
    raw = json.dumps({'version': 1, 'enabled': True,
                      'manifest_path': 'C:\\PrivateOwnerRuntime\\manifest.json'}).encode()
    def forbidden(*args, **kwargs):
        raise AssertionError('Restoring a declaration must not inspect private runtime paths.')
    with monkeypatch.context() as patch:
        for name in ('stat', 'lstat', 'exists', 'resolve', 'open'):
            patch.setattr(Path, name, forbidden)
        value = settings.validate_settings_bytes(settings.restored_settings_bytes(raw))
    assert value['enabled'] is False
    assert value['manifest_path'] == 'C:\\PrivateOwnerRuntime\\manifest.json'


@pytest.mark.parametrize('path', ['relative.json', '//server/private.json', '\\\\server\\private.json',
                                 'https://example.invalid/manifest.json', '/bad\x00path'])
def test_connection_rejects_nonlocal_or_ambiguous_paths(path):
    with pytest.raises(ValueError):
        settings.validate_settings_bytes(json.dumps({'version': 1, 'enabled': True, 'manifest_path': path}).encode())


def test_disable_retains_review_declaration_but_never_loads_it(tmp_path):
    path = str(tmp_path / 'not-installed' / 'private-manifest.json')
    settings.save_settings(tmp_path, {'version': 1, 'enabled': True, 'manifest_path': path})
    settings.disable(tmp_path)
    assert settings.load_settings(tmp_path) == {'version': 1, 'enabled': False, 'manifest_path': path}
