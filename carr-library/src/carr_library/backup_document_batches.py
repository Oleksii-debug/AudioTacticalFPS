"""Passive document-batch data in the existing common backup inventory.

Only the producer's known files are admitted. Historical destinations and
imported reading-copy anchors are inert declarations, never file permissions or
quotation authority. No converter, voice, split expression or saved action runs.
"""
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re

from .backup_producers import digest, fields, number, path, text
from .durable_state import encode, local_path, regular, state_lock
from .ingest.restore import RestoreError


PREFIX = 'state/conversion/batches/'
MAX_JOBS = 1000
MAX_JOB_BYTES = 32 * 1024**2
MAX_TEXT_BYTES = 32 * 1024**2
MAX_SOURCE_BYTES = 32 * 1024**3
_ID = r'[0-9a-f]{32}'
_ORDINAL = r'(?:[0-9]{4}|10000)'
_JOB = re.compile(PREFIX + '(' + _ID + r')(\.control)?\.json\Z')
_MEMBER = re.compile(PREFIX + '(' + _ID + ')/(.+)')
_INPUT = re.compile(r'inputs/(' + _ORDINAL + r')(\.[a-z0-9.]+)\Z')
_DOCUMENT = re.compile(r'documents/(' + _ORDINAL + r')\.json\Z')
_DERIVED = re.compile(r'derived/(' + _ORDINAL + ')/(' + _ID + r')\.txt(\.provenance\.json)?\Z')
_STATES = frozenset(('preparing', 'prepared', 'reviewed', 'running', 'paused',
                     'cancelled', 'failed', 'complete'))


def _suffixes():
    from .conversion.service import BUILTIN, IMAGES
    return frozenset((*BUILTIN, '.rtf', *IMAGES))


def limit(name):
    if match := _JOB.fullmatch(name):
        return 1024 if match[2] else MAX_JOB_BYTES
    if match := _MEMBER.fullmatch(name):
        tail = match[2]
        if source := _INPUT.fullmatch(tail):
            if source[2] == '.reading.json':
                return MAX_TEXT_BYTES
            if source[2] in _suffixes():
                return MAX_SOURCE_BYTES
        if _DOCUMENT.fullmatch(tail) or _DERIVED.fullmatch(tail):
            return MAX_TEXT_BYTES
    return None


def is_media(name):
    match = _MEMBER.fullmatch(name)
    return bool(match and _INPUT.fullmatch(match[2]))


def empty_allowed(name):
    match = _MEMBER.fullmatch(name)
    derived = _DERIVED.fullmatch(match[2]) if match else None
    return bool(derived and derived[3] is None)


@contextmanager
def capture_lock(state_root):
    """Share the exact retained owner lock without creating an absent producer."""
    root = Path(state_root) / 'conversion'
    try:
        root.lstat()
    except FileNotFoundError:
        yield
        return
    root = local_path(root, exists=True)
    regular(root, directory=True)
    with state_lock(root, 'document-batches'):
        yield


def _identity(value):
    if type(value) is not str or re.fullmatch(_ID, value) is None:
        raise ValueError('Invalid document-batch identity.')


def _timestamp(value):
    text(value, 80)
    if datetime.fromisoformat(value).tzinfo is None:
        raise ValueError('A document checkpoint needs an explicit timestamp timezone.')


def _artifact(value):
    fields(value, {'path', 'sha256', 'bytes'})
    text(value['path'], 512)
    digest(value['sha256'])
    number(value['bytes'], 1, MAX_TEXT_BYTES, integer=True)


def _preview(value, identity):
    from .conversion.batches import BatchPreview
    preview = BatchPreview.from_dict(value)
    if preview.job_id != identity or preview.conflicts:
        raise ValueError('A saved batch review must bind this exact job and an approved output plan.')
    path(preview.destination)
    text(preview.voice_id, 512, empty=preview.action == 'text')
    text(preview.language, 80, empty=True)
    return preview


def _outputs(values, preview):
    from .conversion.batches import _studio_job
    from .conversion.planning import SplitPlan
    if type(values) is not list or len(values) > len(preview.outputs):
        raise ValueError('Invalid document output receipt count.')
    for index, value in enumerate(values):
        if preview.action == 'text':
            fields(value, {'name', 'sha256', 'bytes', 'provenance'})
            digest(value['sha256'])
            number(value['bytes'], 0, MAX_TEXT_BYTES, integer=True)
            receipt = value['provenance']
            fields(receipt, {'name', 'sha256', 'bytes'})
            if receipt['name'] != value['name'] + '.provenance.json':
                raise ValueError('A document output receipt has a different exact name.')
            digest(receipt['sha256'])
            number(receipt['bytes'], 1, MAX_TEXT_BYTES, integer=True)
        else:
            fields(value, {'name', 'studio_job', 'project_fingerprint'})
            _identity(value['studio_job'])
            digest(value['project_fingerprint'])
            if index >= len(preview.plans) or value['studio_job'] != _studio_job(
                    preview.job_id, index + 1, SplitPlan.from_dict(preview.plans[index]),
                    preview.voice_id, preview.language, preview.format, preview.destination):
                raise ValueError('The Studio admission identity differs from its reviewed batch.')
        if value['name'] != preview.outputs[index]:
            raise ValueError('Document output receipts must be the ordered reviewed prefix.')


def validate_job(value, identity):
    fields(value, {'schema', 'kind', 'id', 'library_root', 'status', 'created',
                   'updated', 'sources', 'ocr_languages', 'skipped', 'review',
                   'review_sha256', 'outputs', 'history', 'message'})
    if (value['schema'] != 'carr-assistant-job/1' or value['kind'] != 'document-batch/1'
            or value['id'] != identity or value['status'] not in _STATES):
        raise ValueError('Invalid typed document-batch checkpoint.')
    _identity(identity)
    path(value['library_root'])
    _timestamp(value['created'])
    _timestamp(value['updated'])
    text(value['message'], 8192, empty=True)
    if (type(value['sources']) is not list or not 1 <= len(value['sources']) <= 10000
            or type(value['ocr_languages']) is not list or len(value['ocr_languages']) > 16
            or type(value['skipped']) is not list or len(value['skipped']) > 64
            or type(value['history']) is not list or len(value['history']) > 8):
        raise ValueError('Document-batch metadata exceeds its producer budgets.')
    for language in value['ocr_languages']:
        if type(language) is not str or re.fullmatch(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,79}', language) is None:
            raise ValueError('Invalid retained OCR language choice.')
    for name in value['skipped']:
        text(name, 512)
    total = 0
    for index, row in enumerate(value['sources'], 1):
        fields(row, {'id', 'name', 'kind', 'path', 'sha256', 'bytes', 'prepared', 'attempt'})
        ordinal = f'{index:04d}'
        if row['id'] != ordinal or row['kind'] not in ('document', 'reading-copy'):
            raise ValueError('Document sources must retain their exact ordered identities.')
        text(row['name'], 512)
        digest(row['sha256'])
        number(row['bytes'], 1, MAX_SOURCE_BYTES, integer=True)
        total += row['bytes']
        if total > MAX_SOURCE_BYTES:
            raise ValueError('Retained document source bytes exceed the producer budget.')
        text(row['path'], 512)
        prefix = identity + '/inputs/' + ordinal
        suffix = row['path'].removeprefix(prefix)
        if (not row['path'].startswith(prefix)
                or suffix not in ({'.reading.json'} if row['kind'] == 'reading-copy' else _suffixes())):
            raise ValueError('A document source escaped its exact retained input identity.')
        if row['attempt'] is not None:
            _identity(row['attempt'])
            if row['kind'] != 'document':
                raise ValueError('An imported reading copy cannot acquire a converter attempt.')
        prepared = row['prepared']
        if prepared is None:
            continue
        fields(prepared, {'reading', 'reading_sha256', 'artifacts'})
        digest(prepared['reading_sha256'])
        reading = identity + '/documents/' + ordinal + '.json'
        expected = {reading}
        if row['kind'] == 'document':
            if row['attempt'] is None:
                raise ValueError('A prepared conversion is missing its exact attempt.')
            output = identity + '/derived/' + ordinal + '/' + row['attempt'] + '.txt'
            expected.update((output, output + '.provenance.json'))
        if (prepared['reading'] != reading or type(prepared['artifacts']) is not list
                or len(prepared['artifacts']) != len(expected)):
            raise ValueError('Invalid prepared document receipt graph.')
        for artifact in prepared['artifacts']:
            _artifact(artifact)
        if {item['path'] for item in prepared['artifacts']} != expected:
            raise ValueError('Prepared artifacts do not match the exact owned source attempt.')
    if value['review'] is None:
        if value['review_sha256'] is not None or value['outputs'] != [] or value['history'] != []:
            raise ValueError('Unreviewed document jobs cannot carry output authority.')
        if value['status'] in ('reviewed', 'running', 'complete'):
            raise ValueError('This document status requires an explicit saved output review.')
    else:
        if any(row['prepared'] is None for row in value['sources']):
            raise ValueError('A reviewed document batch must retain every prepared source.')
        preview = _preview(value['review'], identity)
        if preview.fingerprint != value['review_sha256']:
            raise ValueError('The saved document review digest changed.')
        _outputs(value['outputs'], preview)
        if value['status'] == 'complete' and len(value['outputs']) != len(preview.outputs):
            raise ValueError('A completed document batch has an incomplete output receipt graph.')
    for history in value['history']:
        fields(history, {'review', 'outputs'})
        _outputs(history['outputs'], _preview(history['review'], identity))
    return value


def validate_file(name, raw):
    from .backup_sidecars import _json
    if match := _JOB.fullmatch(name):
        value = _json(raw)
        if match[2]:
            fields(value, {'command'})
            if value['command'] not in ('run', 'pause', 'cancel'):
                raise ValueError('Invalid document-batch control.')
        else:
            validate_job(value, match[1])
        return
    match = _MEMBER.fullmatch(name)
    if match is None or limit(name) is None:
        raise ValueError('Unknown document-batch state member.')
    if _DOCUMENT.fullmatch(match[2]):
        from .speech.reading import ReadingDocument
        ReadingDocument.from_dict(_json(raw))
    elif (derived := _DERIVED.fullmatch(match[2])) and derived[3]:
        _conversion_receipt(_json(raw))
    # Inputs and an unreceipted active attempt are retained data, not a complete
    # conversion. Their job and any completed byte graph are checked together.


def _conversion_receipt(value):
    fields(value, {'schema', 'source_name', 'source_format', 'source_sha256',
                   'source_size', 'text_sha256', 'text_size', 'verified_for_quotation',
                   'original_modified', 'printed_pages_reconstructed', 'elapsed_seconds',
                   'engine', 'sections', 'metadata', 'warnings'})
    if (value['schema'] != 'carr-reading-derivative/1'
            or any(value[key] is not False for key in
                   ('verified_for_quotation', 'original_modified', 'printed_pages_reconstructed'))
            or value['source_format'] not in _suffixes()):
        raise ValueError('Invalid passive document conversion provenance.')
    text(value['source_name'], 512)
    text(value['engine'], 1024)
    digest(value['source_sha256'])
    digest(value['text_sha256'])
    number(value['source_size'], 1, MAX_SOURCE_BYTES, integer=True)
    number(value['text_size'], 1, MAX_TEXT_BYTES, integer=True)
    number(value['elapsed_seconds'], 0, 86400 * 2)
    if (type(value['sections']) is not list or len(value['sections']) > 100000
            or type(value['metadata']) is not dict
            or type(value['warnings']) is not list or len(value['warnings']) > 10000):
        raise ValueError('Invalid bounded document conversion details.')
    for warning in value['warnings']:
        text(warning, 8192)
    for section in value['sections']:
        fields(section, {'label', 'physical_index', 'printed_label', 'text_sha256'}, {'start', 'end'})
        text(section['label'], 512, empty=True)
        digest(section['text_sha256'])
        for key in ('physical_index',):
            if section[key] is not None:
                number(section[key], 1, 100000, integer=True)
        if section['printed_label'] is not None:
            text(section['printed_label'], 512)
        if ('start' in section) != ('end' in section):
            raise ValueError('Incomplete converted section offsets.')
        if 'start' in section:
            number(section['start'], 0, MAX_TEXT_BYTES, integer=True)
            number(section['end'], section['start'], MAX_TEXT_BYTES, integer=True)
    return value


def validate_collection(stage, entries, *, cancelled=lambda: False):
    from .backup_sidecars import _check_cancelled, _json, _stream_file, read_regular
    by_name = {entry['filename']: entry for entry in entries
               if entry['filename'].startswith(PREFIX)}
    if not by_name:
        return
    from .conversion.planning import SplitPlan, reading_identity, verify_plan
    from .speech.reading import ReadingDocument
    jobs = {match[1]: name for name in by_name
            if (match := _JOB.fullmatch(name)) and match[2] is None}
    if len(jobs) > MAX_JOBS or sum(by_name[name]['size_bytes'] for name in jobs.values()) > MAX_JOB_BYTES:
        raise RestoreError('Document-batch inventory exceeds its job count or aggregate metadata budget.')
    stage = Path(stage)

    def checked(name, *, payload=False):
        _check_cancelled(cancelled)
        entry = by_name.get(name)
        if entry is None:
            raise RestoreError('A document batch is missing a retained source or prepared artifact.')
        maximum = limit(name)
        if maximum is None:
            raise RestoreError('Unknown file in a typed document-batch snapshot.')
        if payload:
            raw = read_regular(stage / name, maximum, empty=empty_allowed(name))
            if len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']:
                raise RestoreError('Retained document metadata changed during validation.')
            return raw
        _stream_file(stage / name, expected=entry, maximum=maximum, cancelled=cancelled,
                     empty=empty_allowed(name))
        return entry

    admitted = set()
    for identity, name in sorted(jobs.items()):
        _check_cancelled(cancelled)
        record = validate_job(_json(checked(name, payload=True)), identity)
        admitted.add(name)
        control = PREFIX + identity + '.control.json'
        if control in by_name:
            validate_file(control, checked(control, payload=True))
            admitted.add(control)
        documents, characters = [], 0
        for row in record['sources']:
            _check_cancelled(cancelled)
            source_name = PREFIX + row['path']
            source = checked(source_name)
            if (source['sha256'], source['size_bytes']) != (row['sha256'], row['bytes']):
                raise RestoreError('Retained document source differs from its exact job declaration.')
            admitted.add(source_name)
            imported = None
            if row['kind'] == 'reading-copy':
                imported = ReadingDocument.from_dict(_json(checked(source_name, payload=True)))
            reading_name = PREFIX + identity + '/documents/' + row['id'] + '.json'
            document = None
            if reading_name in by_name:
                document = ReadingDocument.from_dict(_json(checked(reading_name, payload=True)))
                admitted.add(reading_name)
            if row['attempt'] is not None:
                output = PREFIX + identity + '/derived/' + row['id'] + '/' + row['attempt'] + '.txt'
                for derived in (output, output + '.provenance.json'):
                    if derived in by_name:
                        checked(derived)
                        admitted.add(derived)
                if output + '.provenance.json' in by_name:
                    report = _conversion_receipt(_json(checked(output + '.provenance.json', payload=True)))
                    if ((report['source_sha256'], report['source_size']) != (row['sha256'], row['bytes'])
                            or report['source_name'] != row['path'].rsplit('/', 1)[1]
                            or report['source_format'] != Path(row['path']).suffix):
                        raise RestoreError('Converted provenance differs from its exact retained source.')
                    if output in by_name:
                        actual = by_name[output]
                        if (report['text_sha256'], report['text_size']) != (actual['sha256'], actual['size_bytes']):
                            raise RestoreError('Converted text differs from its retained provenance.')
            prepared = row['prepared']
            if prepared is None:
                continue
            if document is None or reading_identity(document) != prepared['reading_sha256']:
                raise RestoreError('A prepared reading copy differs from its exact saved identity.')
            characters += len(document.text)
            if characters > 16_000_000:
                raise RestoreError('Prepared document text exceeds the producer character budget.')
            documents.append(document)
            for artifact in prepared['artifacts']:
                actual = checked(PREFIX + artifact['path'])
                if (actual['sha256'], actual['size_bytes']) != (artifact['sha256'], artifact['bytes']):
                    raise RestoreError('A prepared document artifact differs from its exact receipt.')
            if row['kind'] == 'document':
                text_name = PREFIX + identity + '/derived/' + row['id'] + '/' + row['attempt'] + '.txt'
                converted = checked(text_name, payload=True).decode('utf-8', errors='strict')
                expected_document = ReadingDocument(converted, title=document.title)
            else:
                expected_document = imported
            if reading_identity(document) != reading_identity(expected_document):
                raise RestoreError('A prepared reading copy differs from its exact imported or converted source.')
        if record['review'] is not None:
            reviews = [(record['review'], record['outputs']),
                       *((item['review'], item['outputs']) for item in record['history'])]
            fingerprint = sha256(encode([{'sha256': row['sha256'], 'bytes': row['bytes'],
                'name': row['name'], 'reading_sha256': row['prepared']['reading_sha256']}
                for row in record['sources']])).hexdigest()
            for value, outputs in reviews:
                _check_cancelled(cancelled)
                preview = _preview(value, identity)
                if preview.source_fingerprint != fingerprint:
                    raise RestoreError('A document review belongs to different retained sources.')
                selected = documents
                if preview.combined:
                    combined = '\n\n'.join(document.text for document in documents)
                    if len(combined) > 2_000_000:
                        raise RestoreError('Combined reading text exceeds its producer budget.')
                    selected = [ReadingDocument(combined, title='Combined reading copy')]
                if len(preview.plans) != len(selected):
                    raise RestoreError('The reviewed plan count differs from its prepared documents.')
                names, parts = [], []
                for document, saved in zip(selected, preview.plans):
                    # This existing helper validates saved rows and source_span;
                    # it does not evaluate split expressions or run a worker.
                    plan = verify_plan(document, SplitPlan.from_dict(saved), cancelled=cancelled)
                    names.extend(part.name for part in plan.parts)
                    parts.extend((part.sha256, len(document.text[part.start:part.end].encode('utf-8')))
                                 for part in plan.parts)
                if preview.action == 'text':
                    if names != list(preview.outputs):
                        raise RestoreError('Reviewed text output names differ from their saved parts.')
                    for receipt, expected in zip(outputs, parts):
                        if (receipt['sha256'], receipt['bytes']) != expected:
                            raise RestoreError('A text output receipt differs from its reviewed part.')
                elif len(preview.outputs) != len(preview.plans):
                    raise RestoreError('Studio output count differs from its reviewed document plans.')
    if admitted != set(by_name):
        raise RestoreError('A document-batch file has no exact typed job or active attempt owner.')
