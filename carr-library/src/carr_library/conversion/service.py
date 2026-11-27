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
from uuid import uuid4

from ..audio.jobs import (
    checked_directory, regular_file, file_identity, verified_reader, publish_noreplace, read_json,
)
from ..audio.paths import filename_component
from ..data_paths import guard_data_directory
from ..extract.page_ocr import Config
from ..ingest.worker import _invoke
from .capabilities import CONVERSION_SUFFIXES, IMAGES, capabilities
from .legacy import LEGACY_SUFFIXES, QUALIFIED_SUFFIXES, kind_for
from .legacy_runtime import ToolSelection
from .models import (
    ConversionBudget, ConversionError, ConversionResult, ConversionUnavailable, MAX_REPORT_BYTES,
    selected_text_encoding,
)


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
    def __init__(self, root, *, input_roots=(), budget=None, ocr_config=None,
                 pandoc=None, image_ocr=False, office=None, calibre=None, djvu=None, chm=None,
                 legacy_preview=False):
        self.root = checked_directory(guard_data_directory(root, purpose='reading derivative output'))
        info = self.root.stat()
        self._root_identity = (info.st_dev, info.st_ino)
        self.budget = ConversionBudget() if budget is None else budget
        if not isinstance(self.budget, ConversionBudget):
            raise TypeError('A typed conversion budget is required.')
        self.ocr_config = Config() if ocr_config is None else ocr_config
        if not isinstance(self.ocr_config, Config):
            raise TypeError('A typed existing OCR configuration is required.')
        if type(image_ocr) is not bool or image_ocr and ocr_config is None:
            raise ValueError('Raster OCR requires an explicit OCR configuration and selection.')
        self.image_ocr = image_ocr
        if type(legacy_preview) is not bool:
            raise TypeError('The explicit legacy fixture-preview option must be boolean.')
        self.legacy_preview = legacy_preview
        self.legacy_tools = {kind: None if value is None else ToolSelection.capture(value, kind)
                             for kind, value in (('calibre', calibre), ('djvu', djvu), ('chm', chm))}

        from .office import OfficeSelection
        self.office = None if office is None else OfficeSelection.capture(office)
        self.pandoc = None
        self._pandoc_identity = None
        self._pandoc_parent_identity = None
        if pandoc is not None:
            path = Path(pandoc)
            if not path.is_absolute():
                raise ValueError('Choose an absolute local Pandoc executable path.')
            checked_directory(path.parent)
            info = regular_file(path)
            if info.st_nlink != 1 or not 0 < info.st_size <= 256 * 1024 * 1024:
                raise ValueError('Choose a regular, unlinked, bounded Pandoc executable.')
            self.pandoc = path.resolve(strict=True)
            self._pandoc_identity = file_identity(info)
            parent_info = checked_directory(self.pandoc.parent).stat()
            self._pandoc_parent_identity = (parent_info.st_dev, parent_info.st_ino)
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
        return capabilities(pandoc_selected=self.pandoc is not None, image_ocr_selected=self.image_ocr,
                            office_selected=self.office is not None,
                            **{kind + '_selected': value is not None for kind, value in self.legacy_tools.items()})

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

    def extract(self, source, output, *, encoding=None, cancelled=lambda: False, broker=None):
        """Use the caller's current application broker when supplied.

        The desktop passes its one ResourceBroker; standalone callers keep the
        existing explicit output-root API. A lease outlives native cleanup and
        both publications, including failure paths.
        """
        encoding = selected_text_encoding(encoding)
        if broker is None:
            return self._extract(source, output, encoding=encoding, cancelled=cancelled)
        from ..operations import ResourceBroker, ResourceRequest, ResourceCancelled
        if not isinstance(broker, ResourceBroker):
            raise TypeError('Use the shared typed local resource coordinator.')
        request = ResourceRequest(cpu_slots=broker.limits.cpu_slots,
            memory_bytes=3 * 1024**3, disk_path=self.root,
            disk_bytes=2 * self.budget.max_source_bytes + self.budget.max_text_bytes
                       + MAX_REPORT_BYTES + 64 * 1024**2)
        try:
            with broker.reserve(request, operation_id='conversion:' + uuid4().hex,
                                cancelled=cancelled, timeout=60) as lease:
                return self._extract(source, output, encoding=encoding, cancelled=lease.cancelled)
        except ResourceCancelled as error:
            raise InterruptedError('Document conversion cancelled before resource admission.') from error

    def _extract(self, source, output, *, encoding, cancelled):
        if not callable(cancelled):
            raise ValueError('A conversion cancellation callback is required.')
        _check(cancelled)
        source, before = self._input(source)
        target, report_path = self._output(output)
        suffix = source.suffix.casefold()
        if encoding is not None and suffix != '.txt':
            raise ConversionError('An explicit text encoding applies only to plain .txt sources.')
        if suffix not in CONVERSION_SUFFIXES:
            raise ConversionUnavailable('This format is not enabled by the current document reader registry.')
        if suffix in LEGACY_SUFFIXES:
            if self.legacy_tools[kind_for(suffix)] is None:
                raise ConversionUnavailable('Select the actual installed legacy-tool executable explicitly.')
            if suffix not in QUALIFIED_SUFFIXES and not self.legacy_preview:
                raise ConversionUnavailable('This legacy format has not passed the required native fixture qualification.')
        if suffix == '.rtf' and self.pandoc is None:
            raise ConversionUnavailable('Select an installed compatible Pandoc executable for RTF explicitly.')
        from .office import OFFICE_SUFFIXES
        if suffix in OFFICE_SUFFIXES and self.office is None:
            raise ConversionUnavailable('Select the installed LibreOffice program executable for this format explicitly.')
        if suffix in IMAGES and not self.image_ocr:
            raise ConversionUnavailable('Select OCR and its language/model configuration for raster images explicitly.')
        if source == target or source == report_path:
            raise ValueError('The original document must remain unchanged.')
        capability = next(row for row in self.capabilities() if row['extension'] == suffix)
        if not capability['available'] and not (suffix in LEGACY_SUFFIXES and self.legacy_preview):
            raise ConversionUnavailable('The existing reader dependencies are unavailable: ' +
                                        ', '.join(capability['missing']))
        native_bytes = self.budget.max_source_bytes + 64 * 1024**2 if suffix in OFFICE_SUFFIXES or suffix in LEGACY_SUFFIXES else 16 * 1024**2
        if shutil.disk_usage(target.parent).free < (
                before.st_size + self.budget.max_text_bytes + MAX_REPORT_BYTES + native_bytes):
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
            message = dict(action='convert', source=str(snapshot), output=str(converted),
                report=str(details_path), sha256=source_hash, source_size=count,
                budget=asdict(self.budget), ocr_config=asdict(self.ocr_config), timeout=self.budget.timeout)
            if encoding is not None:
                message['text_encoding'] = encoding
            if suffix in LEGACY_SUFFIXES:
                kind = kind_for(suffix)
                message['adapter'] = dict(kind=kind, runtime=self.legacy_tools[kind].snapshot(stage, cancelled=cancelled))
            elif suffix == '.rtf':
                executable_info = regular_file(self.pandoc)
                parent_info = checked_directory(self.pandoc.parent).stat()
                if (executable_info.st_nlink != 1
                        or file_identity(executable_info) != self._pandoc_identity
                        or (parent_info.st_dev, parent_info.st_ino) != self._pandoc_parent_identity
                        or not 0 < executable_info.st_size <= 256 * 1024 * 1024):
                    raise ConversionError('The selected Pandoc executable changed or exceeds its byte budget.')
                message['adapter'] = dict(kind='pandoc-rtf', executable=dict(path=str(self.pandoc),
                    bytes=executable_info.st_size,
                    sha256=_hash(self.pandoc, cancelled, self._pandoc_identity)))
            elif suffix in IMAGES:
                message['adapter'] = dict(kind='raster-ocr')
            elif suffix in OFFICE_SUFFIXES:
                message['adapter'] = dict(kind='libreoffice', runtime=self.office.snapshot(cancelled=cancelled))
            try:
                result = _invoke(message,
                    timeout=self.budget.timeout, cancelled=cancelled)
            except Exception as error:
                if suffix in OFFICE_SUFFIXES:
                    from .office import progress_detail
                    error.add_note(progress_detail(stage))
                raise
            _check(cancelled)
            if not isinstance(result, dict) or result.get('state') != 'complete':
                state = result.get('state') if isinstance(result, dict) else None
                detail = ''
                if suffix in OFFICE_SUFFIXES:
                    from .office import progress_detail
                    detail = ' ' + progress_detail(stage)
                if state == 'cancelled':
                    raise InterruptedError('Document conversion stopped before publication.')
                if state == 'timed_out':
                    raise TimeoutError('Document conversion exceeded the selected time limit.' + detail)
                raise ConversionError('The existing document reader could not produce a complete derivative.' + detail + ' ' +
                                      str(result.get('message', '') if isinstance(result, dict) else '')[:2000])
            details = read_json(details_path, limit=MAX_REPORT_BYTES)
            expected = {'engine', 'separator', 'segments', 'warnings', 'source_sha256', 'source_size'}
            if encoding is not None:
                expected.add('source_encoding')
            if (not isinstance(details, dict) or set(details) != expected
                    or encoding is not None and details.get('source_encoding') != encoding
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
            if encoding is not None:
                report['source_encoding'] = encoding
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

    def extract_many(self, sources, *, encoding=None, cancelled=lambda: False, progress=None, broker=None):
        encoding = selected_text_encoding(encoding)
        results = []
        for index, source in enumerate(sources, 1):
            if index > self.budget.max_files:
                raise ConversionError('Conversion batch exceeds its selected file budget.')
            _check(cancelled)
            source, _ = self._input(source)
            name = re.sub(r'[^\w .-]', '_', source.stem, flags=re.UNICODE).strip(' .')[:48] or 'document'
            result = self.extract(source, f'{index:04d}-{name}-reading.txt', encoding=encoding,
                                  cancelled=cancelled, broker=broker)
            results.append(result)
            if progress is not None:
                progress(index, result)
        return tuple(results)
