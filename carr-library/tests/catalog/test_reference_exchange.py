"""Real local parsers; no network, catalog mutation or fabricated source IDs."""
import hashlib
import json

import pytest

from carr_library.catalog import reference_exchange as m


def csl(*records):
    return json.dumps(list(records), ensure_ascii=False).encode('utf-8')


def reference(**values):
    return {'id': 'external/123', 'type': 'book', **values}


def test_unicode_literal_names_numbers_and_untrusted_origin():
    data = csl(reference(title='Історія Žiliny', author=[
        {'literal': 'Національна академія наук України'}, {'literal': 'Ľudovít Štúr'}],
        language='uk', edition=2, volume='IV', DOI='10.123/external',
        publisher='Not imported', issued={'date-parts': [[2026]]},
        URL='https://invalid.example/never-open', custom={'file': '../../secret'}))
    hint, = m.preview_references(data, format='csl-json')
    assert hint.titles == ('Історія Žiliny',)
    assert hint.authors == ('Національна академія наук України', 'Ľudovít Štúr')
    assert hint.languages == ('uk',) and hint.editions == ('2',) and hint.volumes == ('IV',)
    assert hint.batch_sha256 == hashlib.sha256(data).hexdigest()
    assert hint.record_number == 1 and hint.origin == 'CSL-JSON record 1'
    assert hint.source_format == 'csl-json' and hint.reference_type == 'book'
    assert not hint.metadata_verified
    assert hint.unsupported_fields == ('DOI', 'URL', 'custom', 'id', 'issued', 'publisher', 'type')
    assert not any(value in repr(hint) for value in ('external/123', '10.123/external',
                                                   'invalid.example', '../../secret', 'Not imported'))
    assert not any(key in hint.__dict__ for key in ('edition_id', 'source_id', 'revision', 'physical', 'printed'))


def test_structured_names_preserve_particles_suffix_order_and_warn():
    hint, = m.preview_references(csl(reference(author=[
        {'given': 'Jean', 'dropping-particle': 'de', 'non-dropping-particle': 'la',
         'family': 'Fontaine', 'suffix': 'Jr.', 'comma-suffix': True},
        {'family': 'Мазепа'}, {'literal': 'The A and B Foundation'},
        {'given': 'Марія', 'family': 'Кюрі', 'ORCID': 'untrusted-external-orcid'}])), format='csl-json')
    assert hint.authors == ('Jean de la Fontaine, Jr.', 'Мазепа', 'The A and B Foundation', 'Марія Кюрі')
    assert any('original name structure' in warning for warning in hint.warnings)
    assert 'author.comma-suffix' in hint.unsupported_fields
    assert 'author.ORCID' in hint.unsupported_fields
    assert 'untrusted-external-orcid' not in repr(hint)


def test_multiple_records_are_not_merged_by_title_or_external_id():
    a, b = m.preview_references(csl(reference(title='Same'), reference(title='Same')), format='csl-json')
    assert a.titles == b.titles == ('Same',)
    assert a.record_number == 1 and b.record_number == 2
    assert a.batch_sha256 == b.batch_sha256


def test_unknown_fields_are_inventory_only_and_empty_known_fields_stay_unknown():
    hint, = m.preview_references(csl(reference(title='', author=[], language=' ',
        abstract='One paragraph.\nAnother paragraph.', arbitrary={'untrusted': ['value']})), format='csl-json')
    assert hint.titles == hint.authors == hint.languages == hint.editions == hint.volumes == ()
    assert hint.unsupported_fields == ('abstract', 'arbitrary', 'id', 'type')
    assert 'paragraph' not in repr(hint)


def test_utf8_bom_and_escaped_braces_are_valid():
    hint, = m.preview_references(b'\xef\xbb\xbf' + csl(reference(title='[{a}] "quoted" \\ slash')), format='csl-json')
    assert hint.titles == ('[{a}] "quoted" \\ slash',)


@pytest.mark.parametrize('data', [b'', b'\xff', b'{}', b'[]', b'[1]', b'[null]', b'[{"id":1}]',
    b'[{"type":"book"}]', b'[{"id":true,"type":"book"}]', b'[{"id":null,"type":"book"}]',
    b'[{"id":"","type":"book"}]', b'[{"id":1,"type":"made-up"}]',
    b'[{"id":1,"type":[]}]', b'[{"id":1,"type":"book","title":null}]',
    b'[{"id":1,"type":"book","title":42}]', b'[{"id":1,"type":"book","edition":true}]',
    b'[{"id":1,"type":"book","title":"a","title":"b"}]',
    b'[{"id":1,"type":"book","custom":{"a":1,"a":2}}]',
    b'[{"id":NaN,"type":"book"}]', b'[{"id":Infinity,"type":"book"}]',
    b'[{"id":1e999,"type":"book"}]', b'[{"id":1,"type":"book","title":"bad\\u0000"}]',
    b'[{"id":1,"type":"book","title":"bad\\ud800"}]',
    b'[{"id":1,"type":"book","title":"first\\nsecond"}]',
    b'[{"id":1,"type":"book","author":[{"literal":"first\\u2028second"}]}]',
    b'[{"id":1,"type":"book"},]', b'[{"id":1,"type":"book"}] trailing'])
def test_invalid_csl_batches(data):
    with pytest.raises((ValueError, UnicodeError)):
        m.preview_references(data, format='csl-json')


@pytest.mark.parametrize('author', [None, {}, ['text'], [None], [{}], [{'literal': ''}],
    [{'literal': 'Literal', 'family': 'Other'}], [{'family': 1}], [{'given': ''}],
    [{'family': 'Smith', 'given': 'first\nsecond'}], [{'suffix': 'Jr.'}],
    [{'family': 'Smith', 'comma-suffix': []}]])
def test_invalid_author_values(author):
    with pytest.raises(ValueError):
        m.preview_references(csl(reference(author=author)), format='csl-json')


def test_invalid_second_record_fails_whole_batch():
    with pytest.raises(ValueError):
        m.preview_references(csl(reference(title='Good'), reference(author='bad')), format='csl-json')


def test_depth_and_total_value_bounds(monkeypatch):
    nested = '"value"'
    for _ in range(m.MAX_DEPTH + 1):
        nested = '[' + nested + ']'
    data = ('[{"id":1,"type":"book","custom":' + nested + '}]').encode()
    with pytest.raises(ValueError, match='nesting'):
        m.preview_references(data, format='csl-json')
    monkeypatch.setattr(m, 'MAX_NODES', 5)
    with pytest.raises(ValueError, match='too many'):
        m.preview_references(csl(reference(custom=[1, 2, 3, 4, 5])), format='csl-json')


@pytest.mark.parametrize('values', [dict(title='a' * 513), dict(language='x' * 4001),
    dict(edition='x' * 4001), dict(volume='x' * 4001),
    dict(author=[{'literal': 'x' * 513}]), dict(author=[{'family': 'x' * 300, 'given': 'y' * 300}]),
    dict(author=[{'literal': 'x'}] * 101), dict(id='x' * 513), dict(custom='x' * 16385),
    {'x' * 129: 'value'}])
def test_field_bounds(values):
    with pytest.raises(ValueError):
        m.preview_references(csl(reference(**values)), format='csl-json')


def test_batch_bounds(monkeypatch):
    monkeypatch.setattr(m, 'MAX_RECORDS', 1)
    with pytest.raises(ValueError):
        m.preview_references(csl(reference(), reference()), format='csl-json')
    monkeypatch.setattr(m, 'MAX_BYTES', 8)
    with pytest.raises(ValueError):
        m.preview_references(csl(reference()), format='csl-json')


def test_real_ris_adapter_retains_alternatives_and_ignored_tag_inventory():
    data = (b'TY  - BOOK\r\nTI  - First\r\nT1  - Alternative\r\nAU  - First Author\r\n'
            b'A1  - Second Author\r\nLA  - en\r\nET  - 2\r\nVL  - IV\r\n'
            b'PY  - 2026\r\nDO  - 10.0/untrusted\r\nL1  - ../../never-read.pdf\r\n'
            b'UR  - https://invalid.example/\r\nER  -\r\n\r\nTY  - JOUR\r\nTI  - Second\r\nER  -\r\n')
    a, b = m.preview_references(data, format='ris')
    assert a.titles == ('First', 'Alternative') and b.titles == ('Second',)
    assert a.authors == ('First Author', 'Second Author')
    assert a.languages == ('en',) and a.editions == ('2',) and a.volumes == ('IV',)
    assert a.unsupported_fields == ('DO', 'L1', 'PY', 'TY', 'UR')
    assert b.unsupported_fields == ('TY',)
    assert a.batch_sha256 == b.batch_sha256 == hashlib.sha256(data).hexdigest()
    assert a.origin == 'RIS record 1' and b.origin == 'RIS record 2'
    assert not a.metadata_verified and not b.metadata_verified
    assert not any(value in repr(a) for value in ('10.0/untrusted', 'never-read.pdf', 'invalid.example'))


def test_ris_bounds_and_parser_rejection_remain_in_force():
    for data in (b'TY  - BOOK\nTI  - missing end\n',
                 b'TY  - BOOK\nTI  - ' + b'a' * 513 + b'\nER  -\n',
                 b'TY  - BOOK\n' + b'AU  - Name\n' * 101 + b'ER  -\n'):
        with pytest.raises(ValueError):
            m.preview_references(data, format='ris')


@pytest.mark.parametrize('data, format', [('text', 'csl-json'), (bytearray(b'[]'), 'csl-json'),
                                       (b'[]', 'bibtex'), (b'[]', 'CSL-JSON'), (b'[]', None)])
def test_invalid_api_arguments(data, format):
    with pytest.raises(ValueError):
        m.preview_references(data, format=format)
