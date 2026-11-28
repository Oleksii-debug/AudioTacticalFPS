"""Actual full backup/restore/rebackup of historical and optional reading state."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.live_speech import SpeechPositionStore, SpeechSettings, decode_positions


def original_state(text):
    digest = sha256(text.encode()).hexdigest()
    key = sha256(json.dumps({'text_sha256': digest, 'location': None},
                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    row = {'key': key, 'text_sha256': digest, 'characters': len(text), 'cursor': 3,
           'location': None, 'settings': {'voice': 'sapi5:historical-leaf-hash', 'language': 'en',
             'rate': 0, 'pitch': 0, 'volume': .8}, 'updated_at': '2026-11-27T00:00:00.000000Z'}
    return json.dumps({'schema': 'carr-live-reading/1', 'positions': [row]},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


@pytest.mark.parametrize('punctuation', [False, True])
def test_complete_backup_preserves_original_state_bytes_and_explicit_new_option(tmp_path, punctuation, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic retained settings')
    store = SpeechPositionStore(app.root)
    text = 'Exact historical source.'
    original = original_state(text)
    store.path.write_bytes(original)
    assert decode_positions(original)
    # These old bytes were written independently of the changed dataclass.
    if punctuation:
        store.remember('New explicit punctuation.', 4,
                       SpeechSettings('sapi5:reviewed-leaf-hash', 'en', punctuation=True))
    before = store.path.read_bytes()
    monkeypatch.setattr('carr_library.audio.engines.default_registry',
                        lambda **kwargs: pytest.fail('Passive backup/restoration invoked an engine.'))
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored_root = restore_backup(backup, tmp_path / 'restored', digest)
    restored = LibraryApp(restored_root)
    recovered = SpeechPositionStore(restored.root)
    assert recovered.path.read_bytes() == before
    assert recovered.load(text)['settings']['voice'] == 'sapi5:historical-leaf-hash'
    assert 'punctuation' not in recovered.load(text)['settings']
    if punctuation:
        assert recovered.load('New explicit punctuation.')['settings']['punctuation'] is True
    else:
        assert recovered.path.read_bytes() == original
    second = tmp_path / 'second'
    second_digest = create_backup(restored, second)
    verify_backup(second, second_digest)
    reopened = LibraryApp(restore_backup(second, tmp_path / 'reopened', second_digest))
    assert SpeechPositionStore(reopened.root).path.read_bytes() == before
