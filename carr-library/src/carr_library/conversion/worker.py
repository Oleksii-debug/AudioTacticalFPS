"""Conversion dispatch inside the existing ingest worker and process supervisor."""
from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import time

from ..audio.jobs import checked_directory, regular_file, verified_reader
from ..catalog.intake import classify
from ..extract.document_formats import FORMAT_BY_SUFFIX, extract_document
from ..extract.page_ocr import Config
from .models import (ConversionBudget, ConversionError, MAX_REPORT_BYTES,
                     email_subject_metadata, selected_text_encoding)
from .capabilities import CONVERSION_SUFFIXES, IMAGES, OFFICE_SUFFIXES
from .legacy import LEGACY_SUFFIXES


def _extract_records(source, data, suffix, config, budget, deadline, *, decoded_text=None, metadata=None):
    if suffix == '.txt':
        if decoded_text is None and data.count(b'\f') >= budget.max_records:
            raise ConversionError('Text form-feed count exceeds the record budget.')
        text = data.decode('utf-8-sig', errors='strict') if decoded_text is None else decoded_text
        if text.count('\f') >= budget.max_records:
            raise ConversionError('Text form-feed count exceeds the record budget.')
        if '\x00' in text or not text.strip():
            raise ConversionError('The original has no supported strict UTF-8 reading text.')
        return 'strict-utf8-v1', '\f', [
            dict(raw_text=part, logical_locator={'segment': index}, physical_index=None, status='candidate')
            for index, part in enumerate(text.split('\f'), 1)], []
    if suffix == '.docx':
        from ..extract.docx_ordered import extract_docx
        result = extract_docx(data)
        return 'existing-docx-ordered', '\n\n', result['records'], result['warnings']
    if suffix == '.epub':
        from ..extract.epub_ordered import extract_epub
        result = extract_epub(data)
        return 'existing-epub-spine', '\n\n', result['records'], result['warnings']
    if suffix in FORMAT_BY_SUFFIX:
        result = extract_document(data, suffix, max_bytes=budget.max_source_bytes,
                                  max_chars=min(budget.max_text_bytes, 10_000_000), deadline=deadline)
        if suffix == '.eml' and metadata is not None:
            # The existing bounded MIME reader has decoded this header from
            # the same verified source bytes as the selected body.
            original = result['metadata']
            metadata.update(email_subject_metadata(
                {'subject': original['title']} if 'title' in original else {}))
        return 'existing-' + FORMAT_BY_SUFFIX[suffix].name, '\n\n', result['records'], result['warnings']
    if suffix == '.pdf':
        from pypdf import PdfReader
        from ..extract.page_ocr import extract_page
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= budget.max_pages:
            raise ConversionError('The PDF is encrypted or exceeds the selected page budget.')
        count = len(reader.pages)
        failures = {
            'render_limit', 'ocr_engine_missing', 'pinned_ocr_model_unavailable_or_invalid',
            'ocr_output_limit', 'portable_ocr_runtime_unavailable_or_invalid',
            'ocr_failed_or_timed_out', 'ocr_empty_nonblank', 'mixed_native_and_image',
        }
        def pages():
            for index in range(1, count + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Document conversion time limit reached.')
                selected = replace(config, timeout=max(1, min(config.timeout, math.ceil(remaining))))
                record = extract_page(source, index, selected)
                unsupported = failures.intersection(record.get('warnings', ()))
                if unsupported:
                    raise ConversionError('PDF page ' + str(index) +
                        ' needs the existing reviewed import workflow: ' + ', '.join(sorted(unsupported)))
                yield dict(record, logical_locator={'physical_page': index})
        return 'existing-page-ocr-v1', '\n\f\n', pages(), ['reading_preview_not_verified_for_quotation']
    raise ConversionError('This suffix is not present in the existing reader registry.')


def _decoded_plain_text(data, encoding, budget):
    """Decode only an explicitly selected codec while retaining type screening."""
    from ..catalog._filetype.archive import Exe, Pdf, Rtf, Zip
    head = data[:262]
    if (Exe().match(head) or head.startswith(b'\x7fELF')
            or Zip().match(head) or Pdf().match(head) or Rtf().match(head)):
        raise ConversionError('A binary or document signature cannot be selected as plain text.')
    try:
        text = data.decode(encoding, errors='strict')
    except UnicodeError as exc:
        raise ConversionError('The source is not valid in the explicitly selected text encoding.') from exc
    if len(text) > budget.max_text_bytes:
        raise ConversionError('Converted text exceeds its selected byte budget.')
    encoded = text.encode('utf-8', errors='strict')
    if len(encoded) > budget.max_text_bytes:
        raise ConversionError('Converted text exceeds its selected byte budget.')
    # The existing intake still rejects binary controls and disguised formats
    # after decoding. Only the explicit codec differs from its UTF-8 default.
    classify(encoded, '.txt')
    return text


def convert_request(message):
    """Consume only a caller-owned snapshot; write only its fixed private files."""
    budget = ConversionBudget(**message['budget'])
    if message['timeout'] != budget.timeout:
        raise ConversionError('Conversion worker time limits disagree.')
    if (not isinstance(message['ocr_config'], dict)
            or set(message['ocr_config']) != set(asdict(Config()))):
        raise ConversionError('Invalid conversion OCR configuration.')
    config = Config(**message['ocr_config'])
    source = Path(message['source'])
    stage = checked_directory(source.parent)
    output, report = Path(message['output']), Path(message['report'])
    suffix = source.suffix.casefold()
    encoding = None
    if 'text_encoding' in message:
        encoding = selected_text_encoding(message['text_encoding'])
        if (encoding is None or encoding != message['text_encoding']
                or suffix != '.txt' or 'adapter' in message):
            raise ConversionError('Invalid explicit plain-text encoding request.')
    if (not source.is_absolute() or source.parent != stage or source.name != 'source' + suffix
            or suffix not in CONVERSION_SUFFIXES
            or output != stage / 'reading.txt' or report != stage / 'details.json'
            or os.path.lexists(output) or os.path.lexists(report)
            or not isinstance(message['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', message['sha256'])
            or type(message['source_size']) is not int
            or not 0 < message['source_size'] <= budget.max_source_bytes):
        raise ConversionError('Invalid private conversion request or source identity.')
    info = regular_file(source)
    if info.st_nlink != 1 or info.st_size != message['source_size']:
        raise ConversionError('Invalid private conversion source file.')
    with verified_reader(source) as stream:
        data = stream.read(budget.max_source_bytes + 1)
    if len(data) != message['source_size'] or sha256(data).hexdigest() != message['sha256']:
        raise ConversionError('The private conversion source checksum changed.')
    deadline = time.monotonic() + budget.timeout
    adapter = message.get('adapter')
    metadata = {} if suffix == '.eml' else None
    if suffix == '.rtf' or suffix in IMAGES or suffix in OFFICE_SUFFIXES or suffix in LEGACY_SUFFIXES:
        from .adapters import selected_records
        engine, separator, records, warnings = selected_records(
            source, data, suffix, config, budget, deadline, adapter)
    else:
        if adapter is not None:
            raise ConversionError('The selected adapter cannot replace an existing document reader.')
        decoded_text = None
        if encoding is None:
            classify(data, suffix)
        else:
            decoded_text = _decoded_plain_text(data, encoding, budget)
        options = {'decoded_text': decoded_text}
        if metadata is not None:
            options['metadata'] = metadata
        engine, separator, records, warnings = _extract_records(
            source, data, suffix, config, budget, deadline, **options)
        if encoding is not None:
            engine = 'explicit-text-encoding-v1'
    segments, encoded_size, characters, visible = [], 0, 0, False
    warnings = list(warnings)
    with output.open('xb') as stream:
        for index, record in enumerate(records, 1):
            if index > budget.max_records or time.monotonic() >= deadline:
                raise ConversionError('Conversion record or time budget exceeded.')
            text = record.get('raw_text')
            if not isinstance(text, str) or '\x00' in text:
                raise ConversionError('The existing reader returned invalid text.')
            prefix = separator if index > 1 else ''
            raw = (prefix + text).encode('utf-8', errors='strict')
            encoded_size += len(raw)
            if encoded_size > budget.max_text_bytes:
                raise ConversionError('Converted text exceeds its selected byte budget.')
            stream.write(raw)
            start = characters + len(prefix)
            characters = start + len(text)
            visible |= bool(text.strip())
            record_warnings = record.get('warnings', [])
            if (not isinstance(record_warnings, list) or len(record_warnings) > 100
                    or any(not isinstance(item, str) or len(item) > 256 for item in record_warnings)):
                raise ConversionError('Invalid reader warning metadata.')
            segments.append(dict(ordinal=index, logical_locator=record.get('logical_locator'),
                physical_index=record.get('physical_index'), printed_label=None,
                printed_label_verified=False, output_characters=[start, characters],
                text_sha256=sha256(text.encode('utf-8')).hexdigest(),
                status=record.get('status', 'review_required'), warnings=record_warnings))
        stream.flush()
        os.fsync(stream.fileno())
    if not segments or not visible or encoded_size == 0:
        raise ConversionError('The document has no supported readable text.')
    details = dict(engine=engine, separator=separator, segments=segments, warnings=warnings,
                   source_sha256=message['sha256'], source_size=len(data))
    if encoding is not None:
        details['source_encoding'] = encoding
    if metadata is not None:
        details['metadata'] = email_subject_metadata(metadata)
    raw = (json.dumps(details, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > MAX_REPORT_BYTES:
        raise ConversionError('Conversion provenance exceeds its report budget.')
    with report.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return dict(state='complete', text_bytes=encoded_size, records=len(segments))
