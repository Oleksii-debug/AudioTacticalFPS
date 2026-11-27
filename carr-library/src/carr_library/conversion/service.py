"""Create source-preserving text derivatives with atomic provenance receipts."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import mkdtemp
from time import monotonic

from ..file_publication import publish_directory_no_replace as _publish_file
from .safety import (local_path, regular, sync_directory, ProcessBudget,
                     run_process, worker_command, read_bytes, _open_regular, _identity)
from .models import ConversionBudget, ConversionError, ConversionResult, ConversionUnavailable


BUILTIN = ('.txt', '.md', '.markdown', '.html', '.htm', '.xhtml', '.eml', '.docx', '.epub',
           '.pdf', '.fb2', '.fb3', '.odt', '.ods', '.odp', '.pptx', '.xlsx')
LEGACY = ('.azw', '.azw3', '.chm', '.djvu', '.doc', '.lit', '.mobi', '.pdb', '.prc', '.ppt', '.tcr', '.wpd', '.xls')
IMAGES = ('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp')
PANDOC_READERS = {'.rtf': 'rtf', '.md': 'commonmark', '.markdown': 'commonmark', '.html': 'html+raw_html',
                  '.htm': 'html+raw_html', '.fb2': 'fb2', '.odt': 'odt', '.docx': 'docx', '.epub': 'epub'}


def _fingerprint(path):
    info = regular(path)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(path, cancelled=lambda: False):
    result = sha256()
    stream, before = _open_regular(path)
    count = 0
    with stream:
        while data := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError('Document conversion cancelled.')
            result.update(data)
            count += len(data)
            if count > before.st_size:
                raise ConversionError('Conversion file grew while hashing.')
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ConversionError('Conversion file changed while hashing.')
    if count != before.st_size or _identity(regular(path)) != _identity(before):
        raise ConversionError('Conversion file changed while hashing.')
    return result.hexdigest()


class ConversionService:
    def __init__(self, root, *, input_roots=(), budget=None, pandoc=None):
        self.root = local_path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_path(self.root, exists=True)
        self._root_identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self.input_roots = tuple(local_path(path, exists=True) for path in input_roots)
        self.budget = ConversionBudget() if budget is None else budget
        if not isinstance(self.budget, ConversionBudget):
            raise TypeError('A typed conversion budget is required.')
        self.pandoc = local_path(pandoc, exists=True) if pandoc else None
        if self.pandoc and not self.pandoc.is_file():
            raise ValueError('Select an installed Pandoc executable.')

    def capabilities(self, *, cancelled=lambda: False):
        deadline = monotonic() + self.budget.timeout
        def remaining():
            if cancelled():
                raise InterruptedError('Converter inventory cancelled.')
            seconds = deadline - monotonic()
            if seconds <= 0:
                raise TimeoutError('Converter inventory exceeded its time budget.')
            return seconds
        remaining()
        pandoc = str(self.pandoc) if self.pandoc else shutil.which('pandoc')
        from ..extract.ocr_runtime import discover_ocr_runtime, OcrRuntimeError
        try:
            ocr = discover_ocr_runtime(remaining=remaining) is not None
        except OcrRuntimeError:
            ocr = False
        values = [{'extension': suffix, 'available': True, 'adapter': 'built-in isolated reader',
                   'limitation': 'Reading derivative only; structural and source-review limitations are reported.'}
                  for suffix in BUILTIN]
        values.append({'extension': '.rtf', 'available': bool(pandoc), 'adapter': 'installed Pandoc --sandbox',
                       'limitation': 'Requires an installed compatible Pandoc; no images, active content or exact layout.'})
        for suffix in IMAGES:
            values.append({'extension': suffix, 'available': ocr,
                           'adapter': 'installed Tesseract with explicit languages',
                           'limitation': 'OCR must be explicitly selected; unverified text only, no automatic quotation approval.'})
        for suffix in LEGACY:
            values.append({'extension': suffix, 'available': False, 'adapter': 'separate reviewed converter needed',
                           'limitation': 'Legacy format is not enabled without a tested offline/active-content isolation adapter; no DRM bypass.'})
        remaining()
        return tuple(values)

    def _input(self, value):
        path = local_path(value, exists=True)
        if not any(path == root or (root.is_dir() and path.is_relative_to(root)) for root in self.input_roots):
            raise PermissionError('The document is outside the selected conversion inputs.')
        info = regular(path)
        if not 0 < info.st_size <= self.budget.max_source_bytes:
            raise ConversionError('Document is empty or exceeds its selected source byte budget.')
        return path

    def _output(self, value):
        local_path(self.root, exists=True)
        if (self.root.stat().st_dev, self.root.stat().st_ino) != self._root_identity:
            raise ValueError('The selected conversion output folder changed.')
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        path = local_path(path, root=self.root)
        if path.suffix.casefold() != '.txt':
            raise ValueError('A reading derivative must use a .txt output filename.')
        if not path.parent.is_dir():
            raise ValueError('Choose an existing output subdirectory.')
        report = path.with_name(path.name + '.provenance.json')
        if os.path.lexists(path) or os.path.lexists(report):
            raise FileExistsError('The text or provenance output already exists. Choose another name.')
        return path, report

    def _process_budget(self, timeout=None, *, source_size=0, ocr=False):
        return ProcessBudget(timeout=timeout or self.budget.timeout,
            max_output_bytes=source_size + self.budget.max_text_bytes + 16 * 1024 * 1024
                             + (512 * 1024 * 1024 if ocr else 0),
            reserve_disk_bytes=16 * 1024 * 1024)

    def _env(self, stage):
        values = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LD_LIBRARY_PATH',
            'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'CARR_OCR_DIR',
            'CARR_OCR_MODELS_DIR', 'TESSDATA_PREFIX') if key in os.environ}
        values.update({'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(stage), 'TEMP': str(stage), 'TMP': str(stage),
                       'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'DO_NOT_TRACK': '1'})
        return values

    def _pandoc(self, source, output, stage, suffix, cancelled, remaining):
        executable = self.pandoc or shutil.which('pandoc')
        if not executable or suffix not in PANDOC_READERS:
            raise ConversionUnavailable('Install a compatible Pandoc explicitly to read this format.')
        executable = local_path(executable, exists=True)
        regular(executable)
        args = [str(executable), '--sandbox', '--from=' + PANDOC_READERS[suffix], '--to=plain',
                '--wrap=none', '--output=' + str(output), str(source),
                '+RTS', '-M' + str(self.budget.memory_bytes // (1024 * 1024)) + 'M', '-RTS']
        result = run_process(args, budget=self._process_budget(remaining(), source_size=regular(source).st_size),
                             cancelled=cancelled, watch=(stage,), env=self._env(stage), cwd=stage)
        return {'engine': 'installed-pandoc-sandbox', 'sections': [], 'metadata': {},
                'warnings': ['Pandoc plain-text derivative; layout, pictures and printed pages are not retained.',
                             'No custom filters, PDF engine, template, resource embedding or network input is enabled.']
                            + (['Pandoc reported conversion warnings; review source fidelity.'] if result.stderr else [])}

    def _image(self, source, output, stage, languages, profile, cancelled, remaining):
        if not languages or not isinstance(languages, (tuple, list)) or len(languages) > 16:
            raise ConversionUnavailable('Choose one or more installed OCR languages before converting an image.')
        if any(not isinstance(language, str) or not re.fullmatch(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,79}', language) for language in languages):
            raise ValueError('Invalid OCR language identifier.')
        from ..extract.ocr_runtime import discover_ocr_runtime
        if profile not in ('installed', 'fast', 'best'):
            raise ValueError('Choose an installed OCR model profile.')
        runtime = discover_ocr_runtime('+'.join(languages), model_profile=profile, remaining=remaining)
        if runtime is None:
            raise ConversionUnavailable('No local Tesseract OCR engine is installed.')
        request, image_report = stage / 'image-request.json', stage / 'image-details.json'
        request.write_text(json.dumps({'operation': 'ocr_image', 'source': str(source),
            'output': str(output), 'report': str(image_report), 'budget': asdict(self.budget),
            'languages': list(languages), 'profile': profile,
            'runtime': runtime.fingerprint()}), encoding='utf-8')
        run_process(worker_command(request),
                    budget=self._process_budget(remaining(), source_size=regular(source).st_size, ocr=True),
                    cancelled=cancelled, watch=(stage,), env=self._env(stage), cwd=stage)
        runtime.assert_unchanged(remaining)
        return json.loads(read_bytes(image_report, 1024 * 1024, remaining))

    def extract(self, source, output, *, cancelled=lambda: False, adapter='auto', encoding=None,
                ocr_languages=(), ocr_profile='installed'):
        if not callable(cancelled):
            raise ValueError('A conversion cancellation callback is required.')
        requested_cancel = cancelled
        deadline = monotonic() + self.budget.timeout
        stopped = False

        def remaining():
            nonlocal stopped
            stopped = stopped or bool(requested_cancel())
            if stopped:
                raise InterruptedError('Document conversion cancelled; no incomplete result was approved.')
            value = deadline - monotonic()
            if value <= 0:
                raise TimeoutError('Document conversion exceeded its total time budget.')
            return value

        def cancelled():
            remaining()
            return False

        remaining()
        source = self._input(source)
        target, report_path = self._output(output)
        parent_identity = (target.parent.stat().st_dev, target.parent.stat().st_ino)
        if source == target:
            raise ValueError('The source must remain unchanged; choose a new output file.')
        if adapter not in ('auto', 'builtin', 'pandoc'):
            raise ValueError('Choose a supported conversion adapter.')
        suffix = source.suffix.casefold()
        if suffix in LEGACY or suffix not in (*BUILTIN, '.rtf', *IMAGES):
            raise ConversionUnavailable('This legacy/unknown format is not safely enabled. See the supported-format matrix.')
        if cancelled():
            raise InterruptedError('Document conversion cancelled before reading source bytes.')
        started = monotonic()
        before = _fingerprint(source)
        original_digest = sha256()
        if shutil.disk_usage(target.parent).free < before[2] + self.budget.max_text_bytes + 16 * 1024 * 1024:
            raise ConversionError('Not enough free disk for the source snapshot and bounded text output.')
        stage = Path(mkdtemp(prefix='.pending-conversion-', dir=target.parent))
        stage_identity = (stage.stat().st_dev, stage.stat().st_ino)
        committed, warnings = False, []
        try:
            snapshot = stage / ('source' + suffix)
            converted, details_path = stage / 'reading.txt', stage / 'details.json'
            read, opened = _open_regular(source)
            if _identity(opened) != before:
                read.close()
                raise ConversionError('Source changed before its snapshot was opened.')
            with read, snapshot.open('xb') as write:
                count = 0
                while data := read.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('Document conversion cancelled while taking its source snapshot.')
                    count += len(data)
                    if count > self.budget.max_source_bytes or count > before[2]:
                        raise ConversionError('Source changed or exceeded the snapshot budget.')
                    original_digest.update(data)
                    write.write(data)
                write.flush()
                os.fsync(write.fileno())
                if _identity(os.fstat(read.fileno())) != before:
                    raise ConversionError('Source changed during conversion planning.')
            if count != before[2] or _fingerprint(source) != before:
                raise ConversionError('Source changed during conversion planning.')
            if _digest(snapshot, cancelled) != original_digest.hexdigest():
                raise ConversionError('The private source snapshot could not be verified.')
            if suffix in IMAGES:
                details = self._image(snapshot, converted, stage, ocr_languages, ocr_profile, cancelled, remaining)
            elif adapter == 'pandoc' or (adapter == 'auto' and suffix == '.rtf'):
                details = self._pandoc(snapshot, converted, stage, suffix, cancelled, remaining)
            else:
                request = stage / 'request.json'
                request.write_text(json.dumps({'source': str(snapshot), 'output': str(converted),
                    'report': str(details_path), 'budget': asdict(self.budget), 'encoding': encoding}), encoding='utf-8')
                run_process(worker_command(request),
                    budget=self._process_budget(remaining(), source_size=regular(snapshot).st_size),
                    cancelled=cancelled, watch=(stage,), env=self._env(stage), cwd=stage)
                details = json.loads(read_bytes(details_path, 16 * 1024 * 1024, remaining))
            if (not isinstance(details, dict) or set(details) != {'engine', 'sections', 'metadata', 'warnings'}
                    or not isinstance(details['engine'], str) or not isinstance(details['sections'], list)
                    or not isinstance(details['metadata'], dict) or not isinstance(details['warnings'], list)):
                raise ConversionError('The converter returned an invalid provenance report.')
            if cancelled():
                raise InterruptedError('Document conversion cancelled before publication.')
            size = regular(converted).st_size
            if not 0 < size <= self.budget.max_text_bytes:
                raise ConversionError('Converted text is empty or exceeds the selected text budget.')
            # Strict UTF-8, no replacement characters introduced by this host.
            value = read_bytes(converted, self.budget.max_text_bytes, remaining).decode('utf-8', errors='strict')
            if not value.strip() or '\x00' in value:
                raise ConversionError('Converter returned empty or invalid text.')
            if _fingerprint(source) != before or _digest(source, cancelled) != original_digest.hexdigest():
                raise ConversionError('The original source changed; derivative was not published.')
            digest = _digest(converted, cancelled)
            report = {'schema': 'carr-reading-derivative/1', 'source_name': source.name,
                'source_format': suffix, 'source_sha256': original_digest.hexdigest(), 'source_size': before[2],
                'text_sha256': digest, 'text_size': size, 'verified_for_quotation': False,
                'original_modified': False, 'printed_pages_reconstructed': False,
                'elapsed_seconds': round(monotonic() - started, 3), **details}
            receipt = stage / 'provenance.json'
            receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
            for path in (converted, receipt):
                with path.open('r+b') as stream:
                    os.fsync(stream.fileno())
            self._output(target)  # Repeat local/path/no-overwrite authorization before publication.
            if (target.parent.stat().st_dev, target.parent.stat().st_ino) != parent_identity:
                raise ValueError('The chosen conversion output folder changed.')
            remaining()
            if _fingerprint(source) != before:
                raise ConversionError('The original source changed before publication.')
            published = False
            published_identity = _identity(regular(converted))[:2]
            try:
                _publish_file(converted, target)
                published = True
                if cancelled():
                    raise InterruptedError('Document conversion cancelled before its provenance receipt.')
                local_path(target, root=self.root, exists=True)
                if ((self.root.stat().st_dev, self.root.stat().st_ino) != self._root_identity
                        or (target.parent.stat().st_dev, target.parent.stat().st_ino) != parent_identity
                        or _identity(regular(target))[:2] != published_identity):
                    raise ValueError('The conversion destination changed before its provenance receipt.')
                if _fingerprint(source) != before:
                    raise ConversionError('The original source changed before its provenance receipt.')
                _publish_file(receipt, report_path)  # Receipt is the completion marker.
                committed = True
            except BaseException:
                if published and os.path.lexists(target):
                    if _identity(target.lstat())[:2] == published_identity:
                        target.unlink()
                raise
            try:
                sync_directory(target.parent)
            except OSError:
                warnings.append('The text and provenance were saved, but folder durability could not be confirmed.')
        finally:
            try:
                info = stage.lstat()
                if (info.st_dev, info.st_ino) != stage_identity:
                    raise ValueError('Private conversion staging was replaced; it was not removed.')
                shutil.rmtree(stage)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                if not committed:
                    raise
                warnings.append('The text and provenance were saved; private temporary cleanup needs attention.')
        return ConversionResult(target, report, report_path, tuple(warnings))

    def extract_many(self, sources, *, cancelled=lambda: False, adapter='auto', ocr_languages=(),
                     ocr_profile='installed'):
        """Finite batch; stable checksum names avoid silently overwriting another source."""
        results = []
        for index, source in enumerate(sources):
            if index >= self.budget.max_sections:
                raise ConversionError('Conversion batch exceeds the selected item budget.')
            if cancelled():
                raise InterruptedError('Conversion batch stopped; previous completed files remain available.')
            source = self._input(source)
            identity = _digest(source, cancelled)[:12]
            name = re.sub(r'[^\w-]', '-', source.stem)[:80] or 'document'
            results.append(self.extract(source, name + '-' + identity + '.txt', cancelled=cancelled,
                                        adapter=adapter, ocr_languages=ocr_languages, ocr_profile=ocr_profile))
        return tuple(results)
