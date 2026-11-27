"""Actual bounded worker reads for declared MIME roots and inert ZIP members."""
from email.message import EmailMessage
from email import policy
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.conversion.formats import extract_builtin


def archive(members):
    target = BytesIO()
    with ZipFile(target, 'w', compression=ZIP_DEFLATED) as writer:
        for name, data in members:
            writer.writestr(name, data)
    return target.getvalue()


def mime_archive(*, start='<root@example.invalid>', duplicate=False):
    message = EmailMessage(policy=policy.SMTP)
    message.set_type('multipart/related')
    if start is not None:
        message.set_param('start', start)
    resource = EmailMessage()
    resource.set_content(b'not-an-opened-image', maintype='image', subtype='png')
    resource['Content-ID'] = '<resource@example.invalid>'
    message.attach(resource)
    body = EmailMessage()
    body.set_content('<html><body><h1>Київ — читання</h1><script>discard_this_code()</script>'
                     '<p>Retained body.</p><img src="https://example.invalid/private.png"></body></html>',
                     subtype='html', charset='utf-8')
    body['Content-ID'] = '<root@example.invalid>'
    message.attach(body)
    if duplicate:
        other = EmailMessage()
        other.set_content('Ambiguous root')
        other['Content-ID'] = '<root@example.invalid>'
        message.attach(other)
    return message.as_bytes()


@pytest.mark.parametrize('suffix', ['.mht', '.mhtml'])
def test_actual_mhtml_uses_declared_root_and_preserves_original_without_fetching_resources(tmp_path, suffix):
    source = tmp_path / ('Оригінал' + suffix)
    original = mime_archive()
    source.write_bytes(original)
    destination = tmp_path / 'reading'
    result = ConversionService(destination, input_roots=(source,)).extract(source, 'reading.txt')
    text = result.path.read_text(encoding='utf-8')
    assert 'Київ — читання' in text and 'Retained body.' in text
    assert 'discard_this_code' not in text and 'not-an-opened-image' not in text
    assert result.report['engine'] == 'stdlib-mhtml-root/1'
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['metadata']['root_content_type'] == 'text/html'
    assert result.report['verified_for_quotation'] is False
    assert source.read_bytes() == original
    assert len(list(destination.iterdir())) == 2


@pytest.mark.parametrize('damage', ['missing-root', 'duplicate-root', 'image-root', 'renamed-html'])
def test_mhtml_ambiguity_or_wrong_root_never_publishes_a_reading_copy(tmp_path, damage):
    data = (b'<html>renamed MIME</html>' if damage == 'renamed-html' else
            mime_archive(start='<absent>' if damage == 'missing-root' else
                         '<resource@example.invalid>' if damage == 'image-root' else '<root@example.invalid>',
                         duplicate=damage == 'duplicate-root'))
    source, destination = tmp_path / 'original.mht', tmp_path / 'output'
    source.write_bytes(data)
    with pytest.raises(ConversionError):
        ConversionService(destination, input_roots=(source,)).extract(source, 'reading.txt')
    assert source.read_bytes() == data and list(destination.iterdir()) == []


def test_mhtml_without_start_uses_first_direct_mime_part(tmp_path):
    message = EmailMessage()
    message.set_type('multipart/related')
    body = EmailMessage()
    body.set_content('First declared reading body.', charset='utf-8')
    message.attach(body)
    source = tmp_path / 'reading.mht'
    source.write_bytes(message.as_bytes())
    result = extract_builtin(source, ConversionBudget())
    assert 'First declared reading body.' in result['text']
    assert result['metadata']['root_content_type'] == 'text/plain'


@pytest.mark.parametrize('suffix,members,expected', [
    ('.txtz', [('book.txt', 'Одна книга.\r\nExact text.'), ('metadata.opf', '<package/>')], 'Одна книга.\r\nExact text.'),
    ('.zip', [('b/second.html', '<p>Second reading body.</p>'), ('a/first.txt', 'First reading body.')], 'First reading body.'),
])
def test_actual_text_archive_is_bounded_ordered_and_source_preserving(tmp_path, suffix, members, expected):
    source = tmp_path / ('original' + suffix)
    original = archive(members)
    source.write_bytes(original)
    destination = tmp_path / 'output'
    result = ConversionService(destination, input_roots=(source,)).extract(source, 'reading.txt')
    actual = result.path.read_bytes().decode('utf-8')
    assert expected in actual and source.read_bytes() == original
    assert result.report['metadata']['members'] == sorted(name for name, _ in members if not name.endswith('.opf'))
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert result.report['verified_for_quotation'] is False
    if suffix == '.zip':
        assert actual.index('First reading body.') < actual.index('Second reading body.')
    assert len(list(destination.iterdir())) == 2


@pytest.mark.parametrize('suffix,members', [
    ('.txtz', [('one.txt', 'one'), ('two.txt', 'two')]),
    ('.txtz', [('only.opf', '<package/>')]),
    ('.zip', [('../outside.txt', 'escape')]),
    ('.zip', [('A.txt', 'one'), ('a.txt', 'two')]),
    ('.zip', [('body.txt', 'safe'), ('run.js', 'code')]),
    ('.zip', [('nested.zip', b'not traversed')]),
])
def test_archive_unsafe_or_ambiguous_members_do_not_publish_or_extract(tmp_path, suffix, members):
    source = tmp_path / ('source' + suffix)
    original = archive(members)
    source.write_bytes(original)
    output = tmp_path / 'output'
    with pytest.raises(ConversionError):
        ConversionService(output, input_roots=(source,)).extract(source, 'reading.txt')
    assert source.read_bytes() == original and list(output.iterdir()) == []
    assert not (tmp_path / 'outside.txt').exists()


def test_archive_expansion_budget_is_applied_before_member_decode(tmp_path):
    source = tmp_path / 'source.zip'
    source.write_bytes(archive([('reading.txt', 'x' * 10000)]))
    with pytest.raises(ConversionError, match='expansion'):
        extract_builtin(source, ConversionBudget(max_archive_bytes=100))


def test_mime_root_decode_budget_is_separate_from_container_size(tmp_path):
    source = tmp_path / 'source.mht'
    source.write_bytes(mime_archive())
    with pytest.raises(ConversionError, match='budget'):
        extract_builtin(source, ConversionBudget(max_member_bytes=10))
