"""QA-010: invalid verified labels must not survive publication or backup."""
from contextlib import closing

import pytest
import sqlite3

from carr_library.search.literal import search_file
from carr_library.storage.revisions import Page, Storage


@pytest.mark.parametrize('label', [b'iv', 'iv\x00forged'])
def test_verified_label_rejected_before_publication(tmp_path, label):
    path = tmp_path / 'library.db'
    with closing(Storage(path)) as store:
        store.publish_revision('a', 'e', 'old', 'a'*64, 'synthetic', [Page('previous text', 1, 'iv', True)])
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            store.publish_revision('a', 'e', 'bad', 'a'*64, 'synthetic', [Page('citation target', 1, label, True)])
        assert store.get_page('a', 'bad', 1) is None
        assert search_file(path, 'a', 'citation target') == []
        backup = tmp_path / 'backup.db'
        store.backup(backup)
    with closing(Storage(backup)) as restored:
        restored.check()
        assert restored.get_page('a', 'old', 1)['printed'] == 'iv'
        assert restored.get_page('a', 'bad', 1) is None
