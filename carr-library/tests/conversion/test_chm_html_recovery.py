"""Observed legacy CHM recovery through the existing inert preview boundary."""
from hashlib import sha256
import json
import time

import pytest
from lxml import etree

from carr_library.conversion import ConversionError, legacy, legacy_runtime
from carr_library.conversion.worker import convert_request
from carr_library.extract.document_formats import extract_document
from carr_library.extract import document_formats_text as html
from tests.conversion.test_legacy_adapters import FIXTURES, request, runtime

DECLARATION = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">'


def document(body, encoding='utf-8'):
    charset = 'utf-8' if encoding == 'utf-8-sig' else encoding
    return (DECLARATION + '<html><head><meta charset="' + charset + '"></head><body>'
            + body + '</body></html>').encode(encoding)


def text_of(raw):
    return '\n'.join(row['raw_text'] for row in extract_document(raw, '.html')['records'])


@pytest.mark.parametrize('body', [
    '<table><tr><td>FIRST</a></td><td>SECOND</td></tr></table>',
    '<table><tr><td>FIRST</th><td>SECOND</td></tr></table>',
    '<p>FIRST</li><p>SECOND</p>',
    '<dl><dd><p><a href="local.htm">FIRST</a> SECOND</a></p></dd></dl>',
    '<p>FIRST SECOND</b>',
    '<dl><dd><ul><li>FIRST<li>SECOND</li></li></ul></dd></dl>',
    '<pre>FIRST SECOND</dt></pre>',
    '<dl><dt>FIRST</dt><dd>SECOND</dt></dl>',
    '<p>FIRST SECOND</p><UL>\r\n\r\n',
    '<p>FIRST SECOND</p><ul>\n',
    '<p>FIRST SECOND</IL>',
], ids=['orphan-anchor-cell', 'wrong-table-header-end', 'orphan-list-end',
        'repeated-anchor-end', 'orphan-bold-end', 'repeated-list-end',
        'definition-end-in-pre', 'definition-end-in-description',
        'empty-trailing-upper-list', 'empty-trailing-lower-list', 'il-typo'])
def test_complete_observed_publisher_shape_population_preserves_order(body):
    original = document(body, 'cp1252')
    before = sha256(original).hexdigest()
    notices = []
    recovered = legacy._chm_html(original, notices=notices)
    assert sha256(original).hexdigest() == before and recovered != original
    visible = text_of(recovered)
    assert visible.index('FIRST') < visible.index('SECOND')
    assert visible.count('FIRST') == visible.count('SECOND') == 1
    assert 'legacy_html_recovered_with_bounded_lxml' in notices
    rows = extract_document(recovered, '.html')['records']
    assert all(row['physical_index'] is None and row['printed_label'] is None for row in rows)


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'cp1251'])
def test_recovery_retains_declared_charset_and_numeric_unicode_without_replacement(encoding):
    original = document('<p>До &#128512;</a> Після</p>', encoding)
    recovered = legacy._chm_html(original)
    visible = text_of(recovered)
    assert 'До 😀 Після' in visible and '\ufffd' not in visible
    assert original == document('<p>До &#128512;</a> Після</p>', encoding)
    assert ('charset="' + ('utf-8' if encoding == 'utf-8-sig' else encoding) + '"') in recovered.decode(encoding)


def test_recovery_preserves_active_hidden_and_comment_suppression():
    original = document(
        '<p>FIRST</a></p><script>const x = "</a>"; ACTIVE_SCRIPT</script>'
        '<style>.x { content: "</a>"; } ACTIVE_STYLE</style>'
        '<object><p>ACTIVE_OBJECT</p></object>'
        '<div hidden><a>HIDDEN_ATTRIBUTE</a></div>'
        '<p aria-hidden="true">HIDDEN_ARIA</p><p style="display:none">HIDDEN_STYLE</p>'
        '<!-- HIDDEN_COMMENT --><p>SECOND</p>')
    notices = []
    recovered = legacy._chm_html(original, notices=notices)
    visible = text_of(recovered)
    assert visible.index('FIRST') < visible.index('SECOND')
    assert not any(value in visible for value in (
        'ACTIVE_SCRIPT', 'ACTIVE_STYLE', 'ACTIVE_OBJECT', 'HIDDEN_ATTRIBUTE',
        'HIDDEN_ARIA', 'HIDDEN_STYLE', 'HIDDEN_COMMENT'))
    assert 'html_active_or_nontext_content_omitted' in notices
    assert 'html_hidden_content_omitted' in notices


@pytest.mark.parametrize('declaration', [
    b'', b'<!DOCTYPE html>', b'<!DOCTYPE HTML PUBLIC "-//FOREIGN//DTD HTML 4.01//EN">',
])
def test_general_or_undeclared_html_never_enters_legacy_recovery(declaration, monkeypatch):
    raw = declaration + b'<html><body><p>FIRST</a> SECOND</p></body></html>'
    monkeypatch.setattr(etree, 'HTMLParser', lambda **options: pytest.fail('legacy parser must not run'))
    assert legacy._chm_html(raw) == raw
    with pytest.raises(ValueError):
        extract_document(raw, '.html')


@pytest.mark.parametrize('unsafe', [
    '<p>SECOND</div>',
    '<p><b>SECOND<table></table></b></p>',
    '<p>SECOND</a unsupported="yes"></p>',
    '<p hidden hidden>SECOND</p>',
    '<p>SECOND &#0;</p>',
    '<!DOCTYPE html SYSTEM "file:///private.dtd"><p>SECOND</p>',
    '<?php private_action ?><p>SECOND</p>',
])
def test_later_unsafe_tokens_still_fail_after_an_admitted_orphan_end(unsafe, monkeypatch):
    original = document('<p>FIRST</a></p>' + unsafe)
    monkeypatch.setattr(etree, 'HTMLParser', lambda **options: pytest.fail('unsafe source must fail before recovery'))
    with pytest.raises(ValueError):
        legacy._chm_html(original)


@pytest.mark.parametrize('limit', ['characters', 'deadline', 'nodes', 'depth'])
def test_original_reader_budgets_apply_before_native_html_recovery(limit, monkeypatch):
    original = document('<p><span>FIRST</span></a> SECOND</p>')
    options = {}
    if limit == 'characters':
        options['max_chars'] = 2
    elif limit == 'deadline':
        options['deadline'] = time.monotonic() - 1
    elif limit == 'nodes':
        monkeypatch.setattr(html, '_MAX_NODES', 4)
    else:
        monkeypatch.setattr(html, '_MAX_DEPTH', 3)
    monkeypatch.setattr(etree, 'HTMLParser', lambda **options: pytest.fail('budget rejection must precede recovery'))
    expected = TimeoutError if limit == 'deadline' else ValueError
    with pytest.raises(expected):
        legacy._chm_html(original, **options)


def test_serialized_recovery_expansion_is_bounded_before_a_reading_is_returned(monkeypatch):
    original = document('<p>' + 'Я ' * 400 + '</a></p>', 'cp1251')
    assert len(original) < 2000
    monkeypatch.setattr(legacy, 'MAX_MEMBER_BYTES', 2000)
    with pytest.raises(ConversionError, match='output byte budget'):
        legacy._chm_html(original)


@pytest.mark.parametrize('change', ['drop', 'reorder', 'expose-hidden'])
def test_recovery_cannot_silently_drop_reorder_or_expose_text(change, monkeypatch):
    original = document('<p>FIRST</a> SECOND</p><p aria-hidden="true">SECRET</p>')
    serialize = etree.tostring
    def changed(root, **options):
        value = serialize(root, **options)
        if change == 'drop':
            return value.replace(b'FIRST', b'')
        if change == 'reorder':
            return value.replace(b'FIRST', b'TEMPORARY').replace(b'SECOND', b'FIRST').replace(b'TEMPORARY', b'SECOND')
        assert b'aria-hidden="true"' in value
        return value.replace(b'aria-hidden="true"', b'aria-hidden="false"')
    monkeypatch.setattr(etree, 'tostring', changed)
    with pytest.raises(ConversionError, match='ordered visible text'):
        legacy._chm_html(original)


def test_existing_conversion_worker_keeps_raw_and_recovered_member_identities(tmp_path, monkeypatch):
    executable = runtime(tmp_path)
    stage = tmp_path / 'stage'
    stage.mkdir()
    selected = legacy_runtime.ToolSelection.capture(executable, 'chm').snapshot(stage)
    members = {
        'a.htm': document('<p>SECOND</b></p>'),
        'z.htm': document('<p>FIRST</a></p>'),
        'contents.hhc': b'<param name="Local" value="z.htm"><param name="Local" value="a.htm">',
    }
    def native(args, *other, **options):
        if args[1] == 'l':
            return '\n\n'.join('Path = ' + name + '\nSize = ' + str(len(raw)) + '\nMethod = Copy'
                              for name, raw in members.items()).encode()
        assert args[1:7] == ['x', '-so', '-y', '-spd', '-sccUTF-8', '-tchm']
        return members[args[-1]]
    monkeypatch.setattr(legacy, '_run', native)
    original = FIXTURES['sample_chm']()
    result = convert_request(request(stage, '.chm', original, selected))
    assert result['state'] == 'complete'
    assert (stage / 'reading.txt').read_text(encoding='utf-8') == 'FIRST\n\nSECOND'
    details = json.loads((stage / 'details.json').read_text(encoding='utf-8'))
    assert details['source_sha256'] == sha256(original).hexdigest()
    assert details['text_sha256'] == sha256((stage / 'reading.txt').read_bytes()).hexdigest()
    assert details['original_modified'] is False and details['verified_for_quotation'] is False
    assert [row['logical_locator']['chm_member'] for row in details['segments']] == ['z.htm', 'a.htm']
    for row in details['segments']:
        locator = row['logical_locator']
        raw = members[locator['chm_member']]
        assert locator['member_sha256'] == sha256(raw).hexdigest()
        assert locator['parsed_member_sha256'] == sha256(legacy._chm_html(raw)).hexdigest()
        assert locator['parsed_member_sha256'] != locator['member_sha256']
        assert row['physical_index'] is None and row['printed_label'] is None
        assert row['status'] == 'review_required'
