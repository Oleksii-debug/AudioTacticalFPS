"""Selected local documents to new reading text, using the current worker."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from time import monotonic

from ..audio.jobs import (
    checked_directory, regular_file, file_identity, verified_reader, publish_noreplace, read_json,
)
from ..research.export_file import _filename as filename_component
from ..data_paths import guard_data_directory
from ..extract.document_formats import SUPPORTED_SUFFIXES
from ..extract.page_ocr import Config
from ..ingest.worker import _invoke
from .capabilities import capabilities
from .models import ConversionBudget, ConversionError, ConversionResult, ConversionUnavailable, MAX_REPORT_BYTES


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Document conversion cancelled; no new completed derivative was published.')


def _hash(path, cancelled, expected=None):
    digest = sha256()
    with verified_reader(path, expected_identity=expected) as stream:
        while block := stream.read(1024 * 1024):
            _check(cancelled)
            digest.update(block)
    _check(cancelled)
    return digest.hexdigest()


class ConversionService:
    def __init__(self, root, *, input_roots=(), budget=None, ocr_config=None):
        self.root = checked_directory(guard_data_directory(root, purpose='reading derivative output'))
        info = self.root.stat()
        self._root_identity = (info.st_dev, info.st_ino)
        self.budget = ConversionBudget() if budget is None else budget
        if not isinstance(self.budget, ConversionBudget):
            raise TypeError('A typed conversion budget is required.')
        self.ocr_config = Config() if ocr_config is None else ocr_config
        if not isinstance(self.ocr_config, Config):
            raise TypeError('A typed existing OCR configuration is required.')
        if not isinstance(input_roots, (tuple, list)) or len(input_roots) > self.budget.max_files:
            raise ValueError('Choose a bounded set of local input files or directories.')
        selected = []
        for value in input_roots:
            path = Path(value).absolute()
            checked_directory(path.parent)
            if path.is_dir():
                selected.append(checked_directory(path))
            else:
                info = regular_file(path)
                if info.st_nlink != 1:
                    raise ValueError('Selected source files cannot have external hard-link aliases.')
                selected.append(path.resolve(strict=True))
        self.input_roots = tuple(selected)

    def capabilities(self):
        return capabilities()

    def _input(self, value):
        path = Path(value).absolute()
        checked_directory(path.parent)
        info = regular_file(path)
        path = path.resolve(strict=True)
        if not any(path == root or root.is_dir() and path.is_relative_to(root) for root in self.input_roots):
            raise PermissionError('The document is outside the explicitly selected conversion inputs.')
        if info.st_nlink != 1 or not 0 < info.st_size <= self.budget.max_source_bytes:
            raise ConversionError('The selected document is linked, empty or exceeds its source-byte budget.')
        return path, info

    def _output(self, value):
        root = checked_directory(self.root)
        root_info = root.stat()
        if (root_info.st_dev, root_info.st_ino) != self._root_identity:
            raise ConversionError('The selected output directory was replaced.')
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        filename_component(path.name)
        parent = checked_directory(path.parent)
        path = parent / path.name
        if not path.is_relative_to(root) or path.suffix.casefold() != '.txt':
            raise PermissionError('Choose a new .txt reading file inside the selected output directory.')
        receipt = path.with_name(path.name + '.provenance.json')
        filename_component(receipt.name)
        if os.path.lexists(path) or os.path.lexists(receipt):
            raise FileExistsError('The reading text or its provenance report already exists. Choose another name.')
        return path, receipt

    def extract(self, source, output, *, cancelled=lambda: False):
        if not callable(cancelled):
            raise ValueError('A conversion cancellation callback is required.')
        _check(cancelled)
        source, before = self._input(source)
        target, report_path = self._output(output)
        suffix = source.suffix.casefold()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ConversionUnavailable('This format is not enabled by the current document reader registry.')
        if source == target or source == report_path:
            raise ValueError('The original document must remain unchanged.')
        capability = next(row for row in capabilities() if row['extension'] == suffix)
        if not capability['available']:
            raise ConversionUnavailable('The existing reader dependencies are unavailable: ' +
                                        ', '.join(capability['missing']))
        if shutil.disk_usage(target.parent).free < (
                before.st_size + self.budget.max_text_bytes + MAX_REPORT_BYTES + 16 * 1024 * 1024):
            raise ConversionError('Not enough free disk for the bounded source, reading text and provenance.')
        source_identity = file_identity(before)
        started = monotonic()
        with TemporaryDirectory(prefix='.carr-conversion-', dir=target.parent) as directory:
            stage = Path(directory)
            snapshot = stage / ('source' + suffix)
            converted, details_path = stage / 'reading.txt', stage / 'details.json'
            digest, count = sha256(), 0
            with verified_reader(source, expected_identity=source_identity) as read, snapshot.open('xb') as write:
                while block := read.read(1024 * 1024):
                    _check(cancelled)
                    count += len(block)
                    if count > before.st_size:
                        raise ConversionError('The selected source grew during its private snapshot.')
                    digest.update(block)
                    write.write(block)
                write.flush()
                os.fsync(write.fileno())
            if count != before.st_size:
                raise ConversionError('The selected source was truncated.')
            source_hash = digest.hexdigest()
            result = _invoke(dict(action='convert', source=str(snapshot), output=str(converted),
                report=str(details_path), sha256=source_hash, source_size=count,
                budget=asdict(self.budget), ocr_config=asdict(self.ocr_config), timeout=self.budget.timeout),
                timeout=self.budget.timeout, cancelled=cancelled)
            _check(cancelled)
            if not isinstance(result, dict) or result.get('state') != 'complete':
                state = result.get('state') if isinstance(result, dict) else None
                if state == 'cancelled':
                    raise InterruptedError('Document conversion stopped before publication.')
                if state == 'timed_out':
                    raise TimeoutError('Document conversion exceeded the selected time limit.')
                raise ConversionError('The existing document reader could not produce a complete derivative. ' +
                                      str(result.get('message', '') if isinstance(result, dict) else '')[:2000])
            details = read_json(details_path, limit=MAX_REPORT_BYTES)
            expected = {'engine', 'separator', 'segments', 'warnings', 'source_sha256', 'source_size'}
            if (not isinstance(details, dict) or set(details) != expected
                    or details['source_sha256'] != source_hash or details['source_size'] != count
                    or not isinstance(details['engine'], str) or not 1 <= len(details['engine']) <= 200
                    or details['separator'] not in ('\f', '\n\n', '\n\f\n')
                    or not isinstance(details['segments'], list)
                    or not 1 <= len(details['segments']) <= self.budget.max_records
                    or not isinstance(details['warnings'], list)
                    or len(details['warnings']) > 100
                    or any(not isinstance(item, str) or len(item) > 256 for item in details['warnings'])):
                raise ConversionError('The conversion worker returned invalid provenance.')
            text_info = regular_file(converted)
            if (text_info.st_nlink != 1 or not 0 < text_info.st_size <= self.budget.max_text_bytes
                    or result.get('text_bytes') != text_info.st_size
                    or result.get('records') != len(details['segments'])):
                raise ConversionError('The conversion worker returned invalid output sizes.')
            # The worker emitted strict UTF-8. Stream again to reject corrupt
            # output before publication without retaining a second full string.
            import codecs
            decoder, visible = codecs.getincrementaldecoder('utf-8')('strict'), False
            with verified_reader(converted) as stream:
                while block := stream.read(1024 * 1024):
                    _check(cancelled)
                    text = decoder.decode(block)
                    if '\x00' in text:
                        raise ConversionError('The reading derivative contains invalid NUL text.')
                    visible |= bool(text.strip())
                visible |= bool(decoder.decode(b'', final=True).strip())
            if not visible:
                raise ConversionError('The converted document has no readable text.')
            if _hash(source, cancelled, source_identity) != source_hash:
                raise ConversionError('The original changed during conversion; no output was published.')
            report = dict(schema='carr-reading-derivative/1', source_name=source.name,
                source_format=suffix, source_sha256=source_hash, source_size=count,
                text_sha256=_hash(converted, cancelled), text_size=text_info.st_size,
                verified_for_quotation=False, original_modified=False,
                printed_pages_reconstructed=False, elapsed_seconds=round(monotonic() - started, 3),
                **{key: details[key] for key in ('engine', 'separator', 'segments', 'warnings')})
            encoded = (json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
            if len(encoded) > MAX_REPORT_BYTES:
                raise ConversionError('The final provenance report exceeds its byte budget.')
            receipt = stage / 'provenance.json'
            with receipt.open('xb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._output(target)
            _check(cancelled)
            owned = None
            text_identity = file_identity(regular_file(converted))[:4]
            receipt_identity = file_identity(regular_file(receipt))[:4]
            try:
                publish_noreplace(converted, target)
                owned = file_identity(regular_file(target))
                _check(cancelled)
                publish_noreplace(receipt, report_path)  # Completion marker.
            except BaseException:
                # Delete only our exact just-published text, never a file that
                # another writer replaced or edited after the first rename.
                # The publisher may move the first file and then raise while
                # syncing its directory, before it returns an ownership stamp.
                if owned is None and not os.path.lexists(converted) and os.path.lexists(target):
                    try:
                        current = file_identity(regular_file(target))
                        if (current[:4] == text_identity
                                and _hash(target, lambda: False, current) == report['text_sha256']):
                            owned = current
                    except (OSError, ValueError, RuntimeError):
                        pass
                receipt_moved = False
                if not os.path.lexists(receipt) and os.path.lexists(report_path):
                    try:
                        receipt_moved = file_identity(regular_file(report_path))[:4] == receipt_identity
                    except (OSError, ValueError):
                        pass
                # A directory fsync can fail after the receipt move succeeded.
                # Keep that matching pair for inspection, and propagate the
                # durability error instead of claiming successful publication.
                if not receipt_moved and owned is not None and os.path.lexists(target):
                    try:
                        if file_identity(regular_file(target)) == owned:
                            target.unlink()
                    except (OSError, ValueError):
                        pass
                raise
        return ConversionResult(target, report, report_path)

    def extract_many(self, sources, *, cancelled=lambda: False, progress=None):
        results = []
        for index, source in enumerate(sources, 1):
            if index > self.budget.max_files:
                raise ConversionError('Conversion batch exceeds its selected file budget.')
            _check(cancelled)
            source, _ = self._input(source)
            name = re.sub(r'[^\w .-]', '_', source.stem, flags=re.UNICODE).strip(' .')[:48] or 'document'
            result = self.extract(source, f'{index:04d}-{name}-reading.txt', cancelled=cancelled)
            results.append(result)
            if progress is not None:
                progress(index, result)
        return tuple(results)
