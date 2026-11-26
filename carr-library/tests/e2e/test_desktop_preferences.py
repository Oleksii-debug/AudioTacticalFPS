"""Remember the explicitly opened library without risking existing settings."""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.preferences import remember_directory, selected_directory


def test_remembered_folder_reopens_the_same_unicode_library(tmp_path):
    settings = tmp_path / 'settings' / 'settings.json'
    app = LibraryApp(tmp_path / 'Дослідження Україна')
    workspace = app.create_workspace('Persistent research')
    app.add_text(workspace, 'Source', 'A persistent original quotation.')
    assert selected_directory(settings) is None
    remember_directory(app.root, settings)
    reopened = LibraryApp(selected_directory(settings))
    assert reopened.workspaces()[0].id == workspace
    assert reopened.search(workspace, 'original quotation')[0].verified


def test_incomplete_restore_cannot_replace_previous_folder(tmp_path):
    settings = tmp_path / 'settings.json'
    original = LibraryApp(tmp_path / 'original')
    remember_directory(original.root, settings)
    previous = settings.read_bytes()
    incomplete = LibraryApp(tmp_path / 'incomplete')
    (incomplete.root / '.restore-incomplete').write_text('Restore not published')
    with pytest.raises(ValueError, match='complete library'):
        remember_directory(incomplete.root, settings)
    assert settings.read_bytes() == previous
    assert selected_directory(settings) == original.root


def test_failed_settings_publish_keeps_previous_location(tmp_path, monkeypatch):
    settings = tmp_path / 'settings.json'
    first = LibraryApp(tmp_path / 'first')
    second = LibraryApp(tmp_path / 'second')
    remember_directory(first.root, settings)

    def failed_replace(*args):
        raise OSError('Synthetic disk failure')

    monkeypatch.setattr('carr_library.preferences.os.replace', failed_replace)
    with pytest.raises(OSError, match='disk failure'):
        remember_directory(second.root, settings)
    assert selected_directory(settings) == first.root
    assert list(tmp_path.glob('settings-*.tmp')) == []


@pytest.mark.parametrize('content', ['{broken', json.dumps({'version': 1, 'active_data_dir': '../relative'})])
def test_invalid_settings_are_rejected_without_rewriting(tmp_path, content):
    settings = tmp_path / 'settings.json'
    settings.write_text(content, encoding='utf-8')
    with pytest.raises(ValueError):
        selected_directory(settings)
    assert settings.read_text(encoding='utf-8') == content
