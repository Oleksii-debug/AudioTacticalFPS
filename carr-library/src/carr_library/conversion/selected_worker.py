"""Fixed selected-adapter dispatch used only by conversion.worker."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time

from ._selected_files import checked_directory, file_identity, regular_file, verified_reader
from .models import ConversionError
from .selected_adapters import selected_records

MAX_SOURCE_BYTES = 64 * 1024**2
MAX_TEXT_BYTES = 32 * 1024**2
MAX_REPORT_BYTES = 16 * 1024**2
MAX_RECORDS = 10000


def selected_request(request, budget):
    fields = {'operation', 'source', 'output', 'report', 'budget', 'adapter',
              'source_sha256', 'source_size'}
    if (type(request) is not dict or set(request) != fields
            or request['operation'] != 'selected'
            or type(request['source_sha256']) is not str
            or re.fullmatch('[0-9a-f]{64}', request['source_sha256']) is None
            or type(request['source_size']) is not int
            or not 0 < request['source_size'] <= min(MAX_SOURCE_BYTES, budget.max_source_bytes)):
        raise ConversionError('Invalid bounded selected conversion request.')
    source = Path(request['source'])
    stage = checked_directory(source.parent)
    suffix = source.suffix.casefold()
    if (not source.is_absolute() or source != stage / ('source' + suffix)
            or Path(request['output']) != stage / 'reading.txt'
            or Path(request['report']) != stage / 'details.json'
            or os.path.lexists(stage / 'reading.txt') or os.path.lexists(stage / 'details.json')):
        raise ConversionError('The selected converter request escaped its private staging.')
    info = regular_file(source)
    if info.st_size != request['source_size']:
        raise ConversionError('The private conversion source size changed.')
    with verified_reader(source, expected_identity=file_identity(info)) as stream:
        data = stream.read(request['source_size'] + 1)
    if len(data) != request['source_size'] or sha256(data).hexdigest() != request['source_sha256']:
        raise ConversionError('The private conversion source checksum changed.')
    deadline = time.monotonic() + min(600, budget.timeout)
    engine, separator, records, warnings = selected_records(
        source, data, suffix, budget, deadline, request['adapter'])
    if (type(engine) is not str or not 0 < len(engine) <= 200
            or separator not in ('\n\n', '\n\f\n')
            or type(warnings) is not list or len(warnings) > 100
            or any(type(item) is not str or len(item) > 256 for item in warnings)):
        raise ConversionError('The selected adapter returned invalid reading metadata.')
    sections, size, characters, visible = [], 0, 0, False
    with (stage / 'reading.txt').open('xb') as stream:
        for index, record in enumerate(records, 1):
            if index > min(MAX_RECORDS, budget.max_sections) or time.monotonic() >= deadline:
                raise ConversionError('Selected conversion record or time budget exceeded.')
            if type(record) is not dict:
                raise ConversionError('The selected adapter returned an invalid record.')
            text = record.get('raw_text')
            if type(text) is not str or '\x00' in text:
                raise ConversionError('The selected adapter returned invalid reading text.')
            prefix = separator if index > 1 else ''
            raw = (prefix + text).encode('utf-8', errors='strict')
            size += len(raw)
            if size > min(MAX_TEXT_BYTES, budget.max_text_bytes):
                raise ConversionError('Selected conversion exceeds its reading text budget.')
            stream.write(raw)
            start = characters + len(prefix)
            characters = start + len(text)
            visible |= bool(text.strip())
            notices = record.get('warnings', [])
            if (type(notices) is not list or len(notices) > 100
                    or any(type(item) is not str or len(item) > 256 for item in notices)):
                raise ConversionError('The selected adapter returned invalid warnings.')
            sections.append({'label': 'Logical section ' + str(index),
                'start': start, 'end': characters, 'physical_index': None,
                'printed_label': None, 'printed_label_verified': False,
                'status': 'review_required', 'warnings': notices,
                'logical_locator': record.get('logical_locator'),
                'text_sha256': sha256(text.encode('utf-8')).hexdigest()})
        stream.flush()
        os.fsync(stream.fileno())
    if not sections or not visible or not size:
        raise ConversionError('The selected document has no supported readable text.')
    return {'engine': engine, 'sections': sections,
            'metadata': {'separator': separator, 'selected_conversion': True},
            'warnings': warnings}
