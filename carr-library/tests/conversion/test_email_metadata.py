"""Actual isolated MIME conversion retains only bounded passive Subject data."""
from email.message import EmailMessage
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.conversion import service as service_module
from carr_library.conversion.models import ConversionError
from carr_library.conversion.service import ConversionService


def message_bytes(subject=None):
    message = EmailMessage()
    message['From'] = 'sender@example.invalid'
    message['To'] = 'reader@example.invalid'
    if subject is not None:
        message['Subject'] = subject
    message.set_content('Selected plain body.\nSecond paragraph.')
    message.add_alternative('<html><body><p>ALTERNATIVE BODY ONLY</p></body></html>',
                            subtype='html')
    message.add_attachment(b'ATTACHMENT MUST STAY OUT', maintype='application',
                           subtype='octet-stream', filename='private.txt')
    return message.as_bytes()


def selected(tmp_path, raw, suffix='.eml'):
    source = tmp_path / ('original' + suffix)
    source.write_bytes(raw)
    output = tmp_path / 'output'
    output.mkdir()
    converter = ConversionService(output, input_roots=(source,))
    return source, output, converter


@pytest.mark.parametrize('subject', [
    'Звіт про Європу — 2026',
    'Й and И\u0306 stay distinct',
    '../../outside.txt <script>data only</script>',
])
def test_actual_email_subject_is_passive_source_bound_report_data(tmp_path, subject):
    original = message_bytes(subject)
    source, output, converter = selected(tmp_path, original)
    result = converter.extract(source, 'chosen-reading.txt')
    assert result.report['metadata'] == {'subject': subject}
    assert json.loads(result.report_path.read_text(encoding='utf-8')) == result.report
    assert result.report['source_name'] == 'original.eml'
    assert result.report['source_format'] == '.eml'
    assert result.report['source_size'] == len(original)
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['original_modified'] is False
    assert result.report['verified_for_quotation'] is False
    assert 'email_headers_not_authenticated' in result.report['warnings']
    assert 'email_attachments_omitted' in result.report['warnings']
    assert 'email_alternative_body_selected' in result.report['warnings']
    text = result.path.read_text(encoding='utf-8')
    assert 'Selected plain body.' in text and 'Second paragraph.' in text
    for excluded in (subject, 'sender@example.invalid', 'reader@example.invalid',
                     'ALTERNATIVE BODY ONLY', 'ATTACHMENT MUST STAY OUT'):
        assert excluded not in text
    assert result.path == output / 'chosen-reading.txt'
    assert set(output.iterdir()) == {result.path, result.report_path}
    assert source.read_bytes() == original
    assert not (tmp_path / 'outside.txt').exists()


@pytest.mark.parametrize('length', [None, 512, 513])
def test_actual_email_subject_uses_existing_reader_bound_and_omission_policy(tmp_path, length):
    subject = None if length is None else 'Ж' * length
    original = message_bytes(subject)
    source, output, converter = selected(tmp_path, original)
    result = converter.extract(source, 'reading.txt')
    assert result.report['metadata'] == ({'subject': subject} if length == 512 else {})
    assert 'Selected plain body.' in result.path.read_text(encoding='utf-8')
    assert set(output.iterdir()) == {result.path, result.report_path}
    assert source.read_bytes() == original


def test_actual_email_batch_keeps_each_subject_bound_to_its_original(tmp_path):
    output = tmp_path / 'output'
    output.mkdir()
    sources = []
    originals = []
    subjects = ['First reviewed subject', '../second-subject-is-data.txt']
    for index, subject in enumerate(subjects, 1):
        source = tmp_path / ('mail-' + str(index) + '.eml')
        raw = message_bytes(subject)
        source.write_bytes(raw)
        sources.append(source)
        originals.append(raw)
    results = ConversionService(output, input_roots=sources).extract_many(sources)
    assert len(results) == 2
    for index, (source, original, subject, result) in enumerate(
            zip(sources, originals, subjects, results), 1):
        assert result.report['metadata'] == {'subject': subject}
        assert result.report['source_sha256'] == sha256(original).hexdigest()
        assert result.path.name == f'{index:04d}-mail-{index}-reading.txt'
        assert source.read_bytes() == original
    assert set(output.iterdir()) == {
        path for result in results for path in (result.path, result.report_path)}
    assert not (tmp_path / 'second-subject-is-data.txt').exists()


@pytest.mark.parametrize('tamper', [
    'extra-key', 'list', 'integer', 'boolean', 'too-long', 'control',
    'surrogate', 'missing-field',
])
def test_parent_refuses_forged_email_metadata_before_any_publication(tmp_path, monkeypatch, tamper):
    original = message_bytes('Reviewed subject')
    source, output, converter = selected(tmp_path, original)
    actual = service_module._invoke
    observed = []

    def changed(message, **options):
        result = actual(message, **options)
        assert result['state'] == 'complete'
        path = Path(message['report'])
        details = json.loads(path.read_text(encoding='utf-8'))
        assert details['metadata'] == {'subject': 'Reviewed subject'}
        observed.append(message['sha256'])
        replacements = {
            'extra-key': {'subject': 'Reviewed subject', 'filename': '../foreign.txt'},
            'list': ['Reviewed subject'],
            'integer': {'subject': 1},
            'boolean': {'subject': True},
            'too-long': {'subject': 'x' * 513},
            'control': {'subject': 'unsafe\x00subject'},
            'surrogate': {'subject': '\ud800'},
        }
        if tamper == 'missing-field':
            details.pop('metadata')
        else:
            details['metadata'] = replacements[tamper]
        path.write_text(json.dumps(details, ensure_ascii=True), encoding='utf-8')
        return result

    monkeypatch.setattr(service_module, '_invoke', changed)
    with pytest.raises(ConversionError, match='invalid (email metadata|provenance)'):
        converter.extract(source, 'reading.txt')
    assert observed == [sha256(original).hexdigest()]
    assert source.read_bytes() == original
    assert list(output.iterdir()) == []
    assert not (tmp_path / 'foreign.txt').exists()


def test_parent_rejects_email_metadata_added_to_non_email_worker_output(tmp_path, monkeypatch):
    original = b'Unrelated strict UTF-8 document.'
    source, output, converter = selected(tmp_path, original, '.txt')
    actual = service_module._invoke

    def changed(message, **options):
        result = actual(message, **options)
        assert result['state'] == 'complete'
        path = Path(message['report'])
        details = json.loads(path.read_text(encoding='utf-8'))
        assert 'metadata' not in details
        details['metadata'] = {'subject': 'Not an email'}
        path.write_text(json.dumps(details), encoding='utf-8')
        return result

    monkeypatch.setattr(service_module, '_invoke', changed)
    with pytest.raises(ConversionError, match='invalid provenance'):
        converter.extract(source, 'reading.txt')
    assert source.read_bytes() == original
    assert list(output.iterdir()) == []
