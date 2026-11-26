import hashlib
import pytest
from carr_library.catalog import ris_preview as m


def test_multilingual_batch_and_provenance():
    data = ('\ufeffTY  - BOOK\r\nTI  - Історія\r\nAU  - Автор А\r\nAU  - Автор Б\r\n'
            'LA  - uk\r\nVL  - II\r\nER  -\r\n\r\nTY  - JOUR\r\n'
            'TI  - Історія\r\nAU  - Other\r\nET  - 2\r\nDO  - 10.0/test\r\nER  -\r\n').encode()
    a,b = m.preview_ris(data)
    assert a.titles == b.titles == ('Історія',)
    assert a.authors == ('Автор А','Автор Б') and b.authors == ('Other',)
    assert a.languages == ('uk',) and a.volumes == ('II',)
    assert b.editions == ('2',) and b.identifiers == ('10.0/test',)
    assert a.record_number == 1 and b.record_number == 2
    assert a.batch_sha256 == b.batch_sha256 == hashlib.sha256(data).hexdigest()
    assert not a.metadata_verified and not b.metadata_verified


def test_conflicting_titles_are_not_silently_collapsed():
    a, = m.preview_ris(b'TY  - BOOK\nTI  - First\nTI  - Second\nT1  - Alternative\nER  -\n')
    assert a.titles == ('First','Second','Alternative')


def test_no_attachments_or_page_claims():
    a, = m.preview_ris(b'TY  - BOOK\nL1  - ../../secret.pdf\nUR  - https://invalid.example/\nSP  - 55\nEP  - 60\nN1  - private notes\nER  -\n')
    assert not a.titles and not a.authors
    assert not {'start_page','end_page','urls','notes','file_attachments1'} & a.__dict__.keys()
    assert 'secret' not in repr(a)
    assert not hasattr(a,'physical') and not hasattr(a,'printed')


@pytest.mark.parametrize('data', [b'', b'\xff', b'TY  - BOOK\n', b'ER  -\n',
    b'TY  - BOOK\nTY  - BOOK\nER  -\n', b'TY  - BOOK\nER  -\nTI  - stray\n',
    b'TY  - BOOK\nTI  - name\n continuation\nER  -\n',
    b'TY  - BOOK\nTI  - bad\x00\nER  -\n', b'TY  - BOOK\nER  - x\n',
    b'TY  - BOOK\nER  -\nTY  - JOUR\n'])
def test_invalid_batch(data):
    with pytest.raises(ValueError):
        m.preview_ris(data)


def test_bounds(monkeypatch):
    monkeypatch.setattr(m, 'MAX_RECORDS', 1)
    with pytest.raises(ValueError):
        m.preview_ris(b'TY  - BOOK\nER  -\n'*2)
    monkeypatch.setattr(m, 'MAX_BYTES', 8)
    with pytest.raises(ValueError):
        m.preview_ris(b'TY  - BOOK\nER  -\n')


def test_alias_order_and_repeated_authors():
    a, = m.preview_ris(b'TY  - BOOK\nA1  - First\nAU  - Second\nA1  - First\nT1  - Earlier\nTI  - Later\nER  -\n')
    assert a.authors == ('First', 'Second', 'First')
    assert a.titles == ('Earlier', 'Later')
