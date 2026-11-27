"""Measure actual SQLite work for scoped validation, without weakening full checks."""
from contextlib import closing
from hashlib import sha256
import sqlite3

import pytest

from carr_library.storage.revisions import Page, Storage


def instructions(storage, revision):
    calls = [0]
    def step():
        calls[0] += 1
        return 0
    storage.db.set_progress_handler(step, 1)
    try:
        storage._check_locators(revision)
    finally:
        storage.db.set_progress_handler(None, 0)
    return calls[0]


def test_scoped_locator_validation_does_not_scan_unrelated_books(tmp_path):
    with closing(Storage(tmp_path / 'catalog.sqlite')) as storage:
        storage.publish_revision('library', 'chosen', 'chosen-revision', '1' * 64, '{}',
                                 (Page(f'Exact page {index}.') for index in range(40)))
        before = instructions(storage, 'chosen-revision')
        storage.publish_revision('library', 'unrelated', 'unrelated-revision', '2' * 64, '{}',
                                 (Page(f'Other page {index}.') for index in range(20000)))
        after = instructions(storage, 'chosen-revision')
        assert before > 0 and after <= before * 2
        assert instructions(storage, None) > after * 20
        storage.check()
        assert storage.db.execute('SELECT count(*) FROM page').fetchone()[0] == 20040


@pytest.mark.parametrize('pages', [
    [Page('One', physical=2), Page('Two', physical=1)],
    [Page('One'), Page('Two', physical=2)],
    [Page('One', physical=1, printed=' ', verified=True)],
])
def test_scoped_publication_keeps_original_locator_refusals_and_rolls_back(tmp_path, pages):
    with closing(Storage(tmp_path / 'catalog.sqlite')) as storage:
        storage.publish_revision('library', 'good', 'good-revision', '1' * 64, '{}', [Page('Original')])
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            storage.publish_revision('library', 'bad', 'bad-revision', '2' * 64, '{}', pages)
        assert storage.db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert storage.get_page('library', 'good-revision', 1)['raw'] == 'Original'
        storage.check()
