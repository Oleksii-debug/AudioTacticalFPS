from types import SimpleNamespace

import pytest

from carr_library.ui.translation_dialog import _matches_saved_scope


def account(identity='account-a', name='Account A'):
    return SimpleNamespace(to_dict=lambda: {'id': identity, 'name': name})


def job():
    return {'profile': account().to_dict(), 'source_text': 'Original A',
            'source_language': 'en', 'target_language': 'uk', 'authorization': {'budget': '1.00'}}


@pytest.mark.parametrize('index,changed', [(0, account('account-b')), (0, account(name='Changed')),
    (0, None), (1, 'Original B'), (2, 'auto'), (3, 'sk'), (4, '0')])
def test_changed_visible_scope_cannot_resume(index, changed):
    values = [account(), 'Original A', 'en', 'uk', '1.0']
    values[index] = changed
    assert not _matches_saved_scope(job(), *values)


def test_unchanged_scope_matches_numeric_budget():
    assert _matches_saved_scope(job(), account(), 'Original A', 'en', 'uk', '1')


def test_offline_export_does_not_need_available_account():
    assert _matches_saved_scope(job(), None, 'Original A', 'en', 'uk', '1', offline=True)
    assert not _matches_saved_scope(job(), None, 'Original B', 'en', 'uk', '1', offline=True)
    assert not _matches_saved_scope(job(), account('account-b'), 'Original A', 'en', 'uk', '1', offline=True)


def test_glossary_is_part_of_saved_scope():
    saved = job()
    saved['glossary'] = 'approved-glossary'
    assert _matches_saved_scope(saved, account(), 'Original A', 'en', 'uk', '1', 'approved-glossary')
    assert not _matches_saved_scope(saved, account(), 'Original A', 'en', 'uk', '1', None)
