"""Real parser/fidelity and hostile-input checks for extra document previews."""
import codecs
from email.message import EmailMessage
from email import policy
from hashlib import sha256
import socket
import time

import pytest

from carr_library.extract import document_formats_text as formats


def extract(data, suffix='.html', **kwargs):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return formats.extract_text_document(data, suffix, **kwargs)


def texts(result):
    return [row['raw_text'] for row in result['records']]


def test_html_order_unicode_inline_entities_title_and_source_identity():
    data = ('<!DOCTYPE html><html><head><title>Київ &amp; Žilina</title>'
            '<style>private css</style></head><body><h1>Heading</h1>'
            '<p>  Україна <b>and</b> Žilina &amp; 東京. </p>'
            '<p>Second<br>line.</p></body></html>').encode()
    result = extract(data)
    assert texts(result) == ['Heading', '  Україна and Žilina & 東京. ', 'Second\nline.']
    assert result['metadata'] == {'title': 'Київ & Žilina'}
    assert result['provenance']['asset_sha256'] == sha256(data).hexdigest()
    assert result['provenance']['encoding'] == 'utf-8'
    assert result['status'] == 'review_required'
    for row in result['records']:
        assert row['physical_index'] is None
        assert row['printed_label'] is None
        assert row['printed_label_verified'] is False
        assert row['text_sha256'] == sha256(row['raw_text'].encode()).hexdigest()


def test_html_omissions_are_disclosed_and_do_not_join_source_words():
    result = extract('<p>Before<script>steal()</script>after.</p>'
                     '<div hidden>Hidden</div><p>Visible</p>'
                     '<div style="color:red; display: none !important">Secret</div>'
                     '<iframe src="https://example.invalid/private">Hidden frame</iframe>')
    assert texts(result) == ['Before', 'after.', 'Visible']
    assert 'html_active_or_nontext_content_omitted' in result['warnings']
    assert 'html_hidden_content_omitted' in result['warnings']


def test_html_external_resources_never_open_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected socket')
    monkeypatch.setattr(socket, 'socket', forbidden)
    result = extract('<html><head><link rel="stylesheet" href="https://invalid/style">'
                     '</head><body><a href="file:///private" onclick="run()">Body</a>'
                     '<img src="https://invalid/image" alt="not source text"></body></html>')
    assert texts(result) == ['Body']
    assert 'html_external_resources_and_actions_ignored' in result['warnings']
    assert 'html_images_and_media_not_transcribed' in result['warnings']


def test_html_static_tables_have_distinct_logical_cells():
    result = extract('<table><tr><th>Name</th><th>Year</th></tr>'
                     '<tr><td>Ada</td><td>1843</td></tr></table>')
    assert texts(result) == ['Name', 'Year', 'Ada', '1843']
    assert [x['logical_locator'] for x in result['records']] == [[1], [2], [3], [4]]


def test_html_common_optional_end_tags():
    result = extract('<!DOCTYPE html><html><body><p>One<p>Two')
    assert texts(result) == ['One', 'Two']


@pytest.mark.parametrize('block', ['div', 'h1'])
def test_html_implicit_paragraph_end_does_not_hide_following_visible_block(block):
    result = extract('<html><body><p hidden>private<' + block + '>Visible document</'
                     + block + '><p>Tail survives</p></body></html>')
    assert texts(result) == ['Visible document', 'Tail survives']
    assert 'html_hidden_content_omitted' in result['warnings']


@pytest.mark.parametrize('body', ['<body><p>Visible</p></body>', '<p>Visible</p>', 'Visible'])
def test_html_implicit_head_end_preserves_visible_body(body):
    result = extract('<html><head><title>Title</title>' + body + '</html>')
    assert texts(result) == ['Visible']
    assert result['metadata']['title'] == 'Title'


def test_html_ambiguous_implicit_inline_reparenting_requires_review():
    with pytest.raises(ValueError, match='implicit element closure'):
        extract('<p hidden><b>private<div>Visible</div></b>')


@pytest.mark.parametrize('reference', ['&#0;', '&#xD800;', '&#x110000;', '&#1;', '&#xFFFF;'])
def test_html_invalid_numeric_reference_is_not_silently_replaced(reference):
    with pytest.raises(ValueError, match='character reference'):
        extract('<p>Before' + reference + 'After</p>')


def test_html_valid_character_references_follow_documented_html_text_rules():
    result = extract('<p>&#x423;&#1082;&#1088; &amp; &#128;</p>')
    assert texts(result) == ['Укр & €']
    assert 'html_legacy_character_reference_mapped' in result['warnings']


@pytest.mark.parametrize('source', [
    '<div><b>Text</div>', '<p>Text</span>', '<script>unclosed',
    '<!DOCTYPE html SYSTEM "https://invalid/private"><p>Text</p>',
    '<!DOCTYPE html [<!ENTITY private SYSTEM "file:///private">]><p>&private;</p>',
    '<?php dangerous?><p>Text</p>', '<p id="a" id="b">Ambiguous</p>',
])
def test_html_malformed_or_unsupported_structures_fail_closed(source):
    with pytest.raises(ValueError):
        extract(source)


def test_html_hidden_flag_with_no_value_and_empty_aria_flag():
    result = extract('<p aria-hidden>Readable</p><p hidden>Hidden</p>')
    assert texts(result) == ['Readable']


def test_html_declared_cp1251_is_decoded_without_replacement():
    data = '<html><head><meta charset="windows-1251"></head><body><p>Київ</p></body></html>'.encode('cp1251')
    result = extract(data)
    assert texts(result) == ['Київ']
    assert result['provenance']['encoding'] == 'cp1251'


def test_html_declared_legacy_content_type_charset():
    data = ('<head><meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
            '</head><p>Crème €</p>').encode('cp1252')
    assert texts(extract(data)) == ['Crème €']


@pytest.mark.parametrize('data', [
    b'<meta charset="utf-8"><p>\xff</p>',
    b'<meta charset="cp1251"><meta charset="utf-8"><p>body</p>',
    b'<meta charset="base64_codec"><p>body</p>',
    codecs.BOM_UTF8 + b'<meta charset="cp1252"><p>body</p>',
])
def test_html_invalid_or_conflicting_encoding_is_not_silently_replaced(data):
    with pytest.raises(ValueError):
        extract(data)


def test_html_utf16_bom_and_unicode_are_retained():
    result = extract('<p>Українська 日本語</p>'.encode('utf-16'))
    assert texts(result) == ['Українська 日本語']
    assert result['provenance']['encoding'] in ('utf-16-le', 'utf-16-be')


def test_html_bom_and_conflicting_explicit_endian_meta_are_rejected():
    source = '<meta charset="utf-16-be"><p>Visible</p>'
    with pytest.raises(ValueError, match='declarations conflict'):
        extract(codecs.BOM_UTF16_LE + source.encode('utf-16-le'))


def test_html_large_text_is_readable_in_bounded_segments():
    source = 'Україна Žilina ' * 20_000
    result = extract('<p>' + source + '</p>')
    assert ''.join(texts(result)) == source
    assert all(len(text) <= 65_536 for text in texts(result))


def test_eml_declared_endian_must_match_bom():
    import base64
    payload = codecs.BOM_UTF16_LE + 'Body'.encode('utf-16-le')
    source = (b'Content-Type: text/plain; charset=utf-16-be\r\n'
              b'Content-Transfer-Encoding: base64\r\n\r\n' + base64.b64encode(payload))
    with pytest.raises(ValueError, match='declarations conflict'):
        extract(source, '.eml')


def test_markdown_is_literal_and_never_normalizes_unicode_or_line_endings():
    source = '# Title\r\n\r\n[link](https://invalid)\r\n<script>not executed</script>\r\nе\u0301\t'
    result = extract(source, '.md')
    assert ''.join(texts(result)) == source
    assert 'markdown_markup_preserved' in result['warnings']
    assert result['provenance']['format'] == 'md'


def test_markdown_long_whitespace_regions_are_not_discarded():
    source = 'A' + ' ' * (3 * formats._SEGMENT) + 'B'
    result = extract(source, '.markdown')
    assert ''.join(texts(result)) == source
    assert len(texts(result)) > 1


@pytest.mark.parametrize('source', [b'\xff damaged', b'valid\x00invalid', b'   \r\n'])
def test_markdown_rejects_corruption_controls_and_empty_text(source):
    with pytest.raises(ValueError):
        extract(source, '.md')


def test_markdown_utf8_and_utf16_bom():
    assert texts(extract(codecs.BOM_UTF8 + 'Žilina'.encode(), '.md')) == ['Žilina']
    assert texts(extract('Київ'.encode('utf-16'), '.md')) == ['Київ']


def mail_bytes(message):
    return message.as_bytes(policy=policy.SMTP)


def test_eml_plain_body_and_subject_metadata_separate():
    msg = EmailMessage()
    msg['Subject'] = 'Research title'
    msg['From'] = 'author@example.invalid'
    msg.set_content('Перша сторінка.\nŽilina and 東京.', charset='utf-8', cte='8bit')
    result = extract(mail_bytes(msg), '.eml')
    assert texts(result) == ['Перша сторінка.\r\nŽilina and 東京.\r\n']
    assert result['metadata'] == {'title': 'Research title'}
    assert 'email_headers_not_authenticated' in result['warnings']
    assert all('author@example' not in text for text in texts(result))


def test_eml_alternatives_choose_plain_once_and_skip_attachment():
    msg = EmailMessage()
    msg['Subject'] = 'Only body'
    msg.set_content('Exact body.', cte='quoted-printable')
    msg.add_alternative('<p>Duplicate HTML body.</p>', subtype='html')
    msg.add_attachment(b'Private attachment should never be read', maintype='text',
                       subtype='plain', filename='secret.txt')
    result = extract(mail_bytes(msg), '.eml')
    assert texts(result) == ['Exact body.\r\n']
    assert 'email_alternative_body_selected' in result['warnings']
    assert 'email_attachments_omitted' in result['warnings']


def test_eml_html_only_body_is_nonexecuting(monkeypatch):
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network opened'))
    msg = EmailMessage()
    msg.set_content('<p>Before</p><script>private</script><p>After</p>'
                    '<img src="https://invalid/pixel">', subtype='html', cte='base64')
    result = extract(mail_bytes(msg), '.eml')
    assert texts(result) == ['Before', 'After']
    assert 'html_active_or_nontext_content_omitted' in result['warnings']


def test_eml_mixed_inline_text_parts_preserve_declared_order():
    msg = EmailMessage()
    msg.make_mixed()
    for content in ('First', 'Second'):
        child = EmailMessage()
        child.set_content(content)
        msg.attach(child)
    result = extract(mail_bytes(msg), '.eml')
    assert texts(result) == ['First\r\n', 'Second\r\n']
    assert [x['logical_locator'] for x in result['records']] == [[1, 1], [2, 1]]


def test_eml_attached_message_body_is_not_leaked():
    msg = EmailMessage()
    msg.set_content('Public body')
    attached = EmailMessage()
    attached.set_content('PRIVATE FORWARDED EMAIL')
    msg.add_attachment(attached)
    result = extract(mail_bytes(msg), '.eml')
    assert texts(result) == ['Public body\r\n']
    assert 'email_attachments_omitted' in result['warnings']


def test_eml_related_body_uses_explicit_start_and_omits_resources():
    msg = EmailMessage()
    msg.make_related()
    image = EmailMessage()
    image.set_content(b'not actually an image', maintype='image', subtype='png')
    image['Content-ID'] = '<image>'
    msg.attach(image)
    body = EmailMessage()
    body.set_content('<p>Chosen body</p>', subtype='html')
    body['Content-ID'] = '<body>'
    msg.attach(body)
    msg.set_param('start', '<body>')
    result = extract(mail_bytes(msg), '.eml')
    assert texts(result) == ['Chosen body']
    assert 'email_related_resources_omitted' in result['warnings']


def test_eml_cp1251_8bit_bytes_preserved_and_bad_bytes_rejected():
    header = b'Content-Type: text/plain; charset=windows-1251\r\nContent-Transfer-Encoding: 8bit\r\n\r\n'
    result = extract(header + 'Українська'.encode('cp1251'), '.eml')
    assert texts(result) == ['Українська']
    invalid = b'Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n\xff'
    with pytest.raises(ValueError, match='character encoding'):
        extract(invalid, '.eml')


@pytest.mark.parametrize('source', [
    b'Content-Type: multipart/mixed\r\n\r\nmissing boundary',
    b'Content-Type: multipart/mixed; boundary=abc\r\n\r\n--abc\r\n\r\nbody',
    b'Content-Type: text/plain\r\nContent-Type: text/html\r\n\r\nambiguous',
    b'Content-Transfer-Encoding: base64\r\n\r\nYWJj@',
    b'Content-Transfer-Encoding: base64\r\n\r\nYWJj=',
    b'Content-Transfer-Encoding: base64\r\n\r\nYWJj==',
    b'Content-Transfer-Encoding: base64\r\n\r\nYWJj====',
    b'Content-Transfer-Encoding: base64\r\n\r\nYQ',
    b'Content-Transfer-Encoding: base64\r\n\r\nY=Q=',
    b'Content-Transfer-Encoding: quoted-printable\r\n\r\nabc=zz',
    b'Content-Transfer-Encoding: x-uuencode\r\n\r\nunsupported',
    b'Content-Type: text/plain; charset=base64_codec\r\n\r\nunsupported',
    b'Content-Type: text/plain\r\n\r\n\xff',
])
def test_eml_malformed_structures_and_decodings_fail_closed(source):
    with pytest.raises(ValueError):
        extract(source, '.eml')


@pytest.mark.parametrize(('encoded', 'expected'), [
    (b'YQ==', 'a'),
    (b'YWI=', 'ab'),
    (b'YWJj', 'abc'),
    (b' \tYQ\r\n==\t \r\n', 'a'),
    (b'\tYW\r\nI=\t\r\n', 'ab'),
    (b'\tYW\r\nJj \r\n', 'abc'),
])
def test_eml_valid_base64_quartets_preserve_body_with_mime_whitespace(encoded, expected):
    source = b'Content-Transfer-Encoding: base64\r\n\r\n' + encoded
    result = extract(source, '.eml')
    assert texts(result) == [expected]
    assert result['status'] == 'review_required'


def test_eml_mail_part_budget_before_unbounded_recursion():
    body = b'Content-Type: text/plain\r\n\r\nbody'
    # Recursive message/rfc822 parsing is bounded by the message factory even
    # though attached messages will never be selected as source text.
    nested = b'Content-Type: message/rfc822\r\n\r\n' * 200 + body
    with pytest.raises(ValueError, match='budget'):
        extract(nested, '.eml')


def test_eml_depth_and_header_budgets():
    nested = b'Content-Type: message/rfc822\r\n\r\n' * 30 + b'Subject: body\r\n\r\ntext'
    with pytest.raises(ValueError, match='nesting budget'):
        extract(nested, '.eml')
    headers = b'X-Field: value\r\n' * 129 + b'\r\nbody'
    with pytest.raises(ValueError, match='header budget'):
        extract(headers, '.eml')


def test_html_depth_node_token_and_character_budgets(monkeypatch):
    with pytest.raises(ValueError, match='nesting budget'):
        extract('<div>' * 129 + 'text' + '</div>' * 129)
    with pytest.raises(ValueError, match='token budget'):
        extract('<p title="' + 'a' * 100_000 + '">text</p>')
    with pytest.raises(ValueError, match='text budget'):
        extract('<p>12345</p>', max_chars=4)
    monkeypatch.setattr(formats, '_MAX_NODES', 4)
    with pytest.raises(ValueError, match='structure budget'):
        extract('<p>one</p><p>two</p><p>three</p>')


@pytest.mark.parametrize('suffix,source', [('.html', b'<p>body</p>'),
    ('.md', b'# Heading\nbody'), ('.eml', b'Subject: subject\r\n\r\nbody')])
def test_all_formats_enforce_cancel_deadline_and_source_budgets(suffix, source):
    with pytest.raises(InterruptedError):
        extract(source, suffix, cancelled=lambda: True)
    with pytest.raises(TimeoutError):
        extract(source, suffix, deadline=time.monotonic() - 1)
    with pytest.raises(ValueError, match='byte budget'):
        extract(source, suffix, max_bytes=2)
    with pytest.raises(ValueError, match='text budget'):
        extract(source, suffix, max_chars=1)


def test_cancellation_after_parser_starts_still_aborts():
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls > 30
    with pytest.raises(InterruptedError):
        extract('<p>Long document.</p>' * 1000, cancelled=cancel)


@pytest.mark.parametrize('kwargs', [dict(max_bytes=0), dict(max_chars=True),
    dict(deadline=float('nan')), dict(deadline=float('inf')), dict(deadline=True),
    dict(cancelled=False)])
def test_invalid_budget_arguments_are_rejected(kwargs):
    with pytest.raises(ValueError):
        extract('body', '.md', **kwargs)


def test_unsupported_rtf_is_not_advertised_or_silently_flattened():
    with pytest.raises(ValueError, match='unsupported'):
        extract(b'{\\rtf1 body}', '.rtf')


def test_error_messages_never_include_private_source_values():
    with pytest.raises(ValueError) as caught:
        extract(b'<meta charset="PRIVATE-SECRET"><p>private</p>')
    assert 'PRIVATE' not in str(caught.value)
