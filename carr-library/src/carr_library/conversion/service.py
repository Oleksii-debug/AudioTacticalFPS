"""Create source-preserving text derivatives with atomic provenance receipts."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from ..durable_state import local_path, regular, sync_directory
from ..audio.models import AudioBudget, AudioCancelled, AudioError
from ..audio.process import run_process
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
    with Path(path).open('rb') as stream:
        while data := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError('Document conversion cancelled.')
            result.update(data)
    return result.hexdigest()


def capabilities(*, pandoc_selected=False, office_selected=False, calibre_selected=False):
    pandoc = bool(pandoc_selected)
    from ..extract.ocr_runtime import discover_ocr_runtime, OcrRuntimeError
    try:
        ocr = discover_ocr_runtime() is not None
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
    from .office import OFFICE_SUFFIXES
    from .calibre import CALIBRE_SUFFIXES
    for suffix in LEGACY:
        selected = bool(office_selected) if suffix in OFFICE_SUFFIXES else bool(calibre_selected)
        tool = 'LibreOffice' if suffix in OFFICE_SUFFIXES else 'Calibre'
        values.append({'extension': suffix, 'available': bool(selected and os.name == 'nt'),
                       'adapter': 'explicitly selected ' + tool + '; limited reading conversion',
                       'limitation': 'Requires the owned Windows Job route and an explicitly selected compatible local runtime. '
                         'The complete external folder retains exact original bytes, reading text and fidelity report. '
                         'It is not native catalog ingestion or quotation approval; no DRM bypass or scanned-DjVu OCR.'})
    return tuple(values)


class ConversionService:
    def __init__(self, root, *, input_roots=(), budget=None, pandoc=None,
                 office=None, calibre=None, state_root=None, guard=lambda: None):
        if not callable(guard):
            raise ValueError('A current conversion owner guard is required.')
        self.guard = guard
        self.guard()
        self.root = local_path(root)
        self.input_roots = tuple(local_path(path, exists=True) for path in input_roots)
        self.budget = budget or (ConversionBudget(max_source_bytes=64 * 1024**2,
            memory_bytes=3 * 1024**3) if office is not None or calibre is not None else ConversionBudget())
        if not isinstance(self.budget, ConversionBudget):
            raise TypeError('A typed conversion budget is required.')
        self.state_root = None
        directories = set()
        if state_root is not None:
            state = Path(state_root).absolute()
            if state.name != 'state':
                raise ValueError('Choose the current library state directory for document conversion.')
            # A fresh LibraryApp has no state directory until a feature needs it.
            # Create only this child of the already captured existing library.
            library = local_path(state.parent, exists=True)
            info = regular(library, directory=True)
            library_identity = info.st_dev, info.st_ino
            self.state_root = local_path(library / 'state')
            if self.root.is_relative_to(library):
                raise PermissionError('Choose a reading-copy folder outside the research library.')
            self.guard()
            info = regular(local_path(library, exists=True), directory=True)
            if (info.st_dev, info.st_ino) != library_identity:
                raise ConversionError('The selected library directory changed before state initialization.')
            self.state_root.mkdir(mode=0o700, exist_ok=True)
            self.guard()
            info = regular(local_path(library, exists=True), directory=True)
            if (info.st_dev, info.st_ino) != library_identity:
                raise ConversionError('The selected library directory changed during state initialization.')
            regular(self.state_root, directory=True)
            directories.update((library, self.state_root))
        if any(value is not None for value in (pandoc, office, calibre)) and self.state_root is None:
            raise ConversionUnavailable('Select the current library before using an optional document converter.')
        from ._selected_files import checked_directory, file_identity, regular_file
        self._selected_input_files = []
        for path in self.input_roots:
            if path.is_dir():
                directories.add(checked_directory(path))
            else:
                self._selected_input_files.append((path, file_identity(regular_file(path))))
                directories.add(checked_directory(path.parent))
        self._selected_input_files = tuple(self._selected_input_files)
        captured = []
        for path in sorted(directories, key=str):
            info = regular(path, directory=True)
            captured.append((path, (info.st_dev, info.st_ino)))
        self._selected_directories = tuple(captured)
        from .office import OfficeSelection
        from .calibre import CalibreSelection
        self.office = None if office is None else OfficeSelection.capture(office)
        self.calibre = None if calibre is None else CalibreSelection.capture(calibre)
        self.pandoc = local_path(pandoc, exists=True) if pandoc is not None else None
        self._pandoc_identity = self._pandoc_parent_identity = None
        if self.pandoc is not None:
            self._pandoc_identity = file_identity(regular_file(self.pandoc))
            info = regular(self.pandoc.parent, directory=True)
            self._pandoc_parent_identity = info.st_dev, info.st_ino
        self.guard()
        for path, identity in self._selected_directories:
            info = regular(local_path(path, exists=True), directory=True)
            if (info.st_dev, info.st_ino) != identity:
                raise ConversionError('The selected library or input directory changed.')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = regular(self.root, directory=True)
        self._selected_directories += ((self.root, (info.st_dev, info.st_ino)),)
        self.guard()

    def capabilities(self):
        return capabilities(pandoc_selected=self.pandoc is not None,
            office_selected=self.office is not None, calibre_selected=self.calibre is not None)

    def _input(self, value):
        path = local_path(value, exists=True)
        if not any(path == root or (root.is_dir() and path.is_relative_to(root)) for root in self.input_roots):
            raise PermissionError('The document is outside the selected conversion inputs.')
        info = regular(path)
        if not 0 < info.st_size <= self.budget.max_source_bytes:
            raise ConversionError('Document is empty or exceeds its selected source byte budget.')
        return path

    def _output(self, value):
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

    def _process_budget(self, timeout=None):
        return AudioBudget(timeout=timeout or self.budget.timeout,
            max_output_bytes=self.budget.max_text_bytes + 16 * 1024 * 1024,
            max_log_bytes=256 * 1024, reserve_disk_bytes=16 * 1024 * 1024)

    def _env(self, stage):
        values = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LD_LIBRARY_PATH') if key in os.environ}
        values.update({'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(stage), 'TEMP': str(stage), 'TMP': str(stage),
                       'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'DO_NOT_TRACK': '1'})
        return values

    def _pandoc(self, source, output, stage, suffix, cancelled):
        executable = self.pandoc or shutil.which('pandoc')
        if not executable or suffix not in PANDOC_READERS:
            raise ConversionUnavailable('Install a compatible Pandoc explicitly to read this format.')
        executable = local_path(executable, exists=True)
        regular(executable)
        args = [str(executable), '--sandbox', '--from=' + PANDOC_READERS[suffix], '--to=plain',
                '--wrap=none', '--output=' + str(output), str(source),
                '+RTS', '-M' + str(self.budget.memory_bytes // (1024 * 1024)) + 'M', '-RTS']
        result = run_process(args, budget=self._process_budget(), cancelled=cancelled,
                             watch=(output,), env=self._env(stage), cwd=stage)
        return {'engine': 'installed-pandoc-sandbox', 'sections': [], 'metadata': {},
                'warnings': ['Pandoc plain-text derivative; layout, pictures and printed pages are not retained.',
                             'No custom filters, PDF engine, template, resource embedding or network input is enabled.']
                            + (['Pandoc reported conversion warnings; review source fidelity.'] if result.stderr else [])}

    def _image(self, source, output, stage, languages, cancelled):
        if not languages or not isinstance(languages, (tuple, list)) or len(languages) > 16:
            raise ConversionUnavailable('Choose one or more installed OCR languages before converting an image.')
        if any(not isinstance(language, str) or not re.fullmatch(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,79}', language) for language in languages):
            raise ValueError('Invalid OCR language identifier.')
        from ..extract.ocr_runtime import discover_ocr_runtime
        runtime = discover_ocr_runtime('+'.join(languages))
        if runtime is None:
            raise ConversionUnavailable('No local Tesseract OCR engine is installed.')
        request, image_report = stage / 'image-request.json', stage / 'image-details.json'
        request.write_text(json.dumps({'operation': 'inspect_image', 'source': str(source),
            'output': str(output), 'report': str(image_report), 'budget': asdict(self.budget)}), encoding='utf-8')
        run_process([str(Path(sys.executable).absolute()), '-I', '-B', '-m',
                     'carr_library.conversion.worker', str(request)],
                    budget=self._process_budget(), cancelled=cancelled,
                    watch=(image_report,), env=self._env(stage), cwd=stage)
        if regular(image_report).st_size > 65536:
            raise ConversionError('Raster metadata exceeded its report budget.')
        image_metadata = json.loads(image_report.read_text(encoding='utf-8'))
        executable = str(local_path(runtime.executable, exists=True))
        arguments = runtime.command([executable, str(source), str(stage / 'ocr'), '-l', '+'.join(languages), '--psm', '3'])
        generated = stage / 'ocr.txt'
        run_process(arguments, budget=self._process_budget(), cancelled=cancelled, watch=(generated,),
                    env=self._env(stage), cwd=runtime.root or stage)
        runtime.assert_unchanged()
        os.replace(generated, output)
        return {'engine': 'local-tesseract-ocr', 'sections': [],
                'metadata': {'languages': list(languages), **image_metadata},
                'warnings': ['OCR text is an unverified reading derivative. Compare it with the source before quoting.',
                             'Coordinates, page labels, orientation and document layout are not reconstructed by this text-only export.']}

    def extract(self, source, output, *, cancelled=lambda: False, adapter='auto', encoding=None, ocr_languages=()):
        suffix = Path(source).suffix.casefold()
        if suffix in LEGACY or suffix == '.rtf':
            if adapter not in ('auto', 'pandoc') or suffix != '.rtf' and adapter != 'auto':
                raise ConversionUnavailable('The selected optional adapter cannot replace another document reader.')
            from .selected import extract_selected
            return extract_selected(self, source, output, cancelled=cancelled)
        source = self._input(source)
        target, report_path = self._output(output)
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
        with TemporaryDirectory(prefix='.pending-conversion-', dir=target.parent) as folder:
            stage = Path(folder)
            snapshot = stage / ('source' + suffix)
            converted, details_path = stage / 'reading.txt', stage / 'details.json'
            with source.open('rb') as read, snapshot.open('xb') as write:
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
            if count != before[2] or _fingerprint(source) != before:
                raise ConversionError('Source changed during conversion planning.')
            try:
                if suffix in IMAGES:
                    details = self._image(snapshot, converted, stage, ocr_languages, cancelled)
                elif adapter == 'pandoc' or (adapter == 'auto' and suffix == '.rtf'):
                    details = self._pandoc(snapshot, converted, stage, suffix, cancelled)
                else:
                    request = stage / 'request.json'
                    request.write_text(json.dumps({'source': str(snapshot), 'output': str(converted),
                        'report': str(details_path), 'budget': asdict(self.budget), 'encoding': encoding}), encoding='utf-8')
                    run_process([str(Path(sys.executable).absolute()), '-I', '-B', '-m',
                        'carr_library.conversion.worker', str(request)], budget=self._process_budget(),
                        cancelled=cancelled, watch=(converted, details_path), env=self._env(stage), cwd=stage)
                    if regular(details_path).st_size > 16 * 1024 * 1024:
                        raise ConversionError('Conversion provenance exceeded its budget.')
                    details = json.loads(details_path.read_text(encoding='utf-8'))
            except AudioCancelled as exc:
                raise InterruptedError('Document conversion cancelled; no result was published.') from exc
            except AudioError as exc:
                raise ConversionError(str(exc).replace('audio', 'document').replace('Audio', 'Document')) from exc
            if cancelled():
                raise InterruptedError('Document conversion cancelled before publication.')
            size = regular(converted).st_size
            if not 0 < size <= self.budget.max_text_bytes:
                raise ConversionError('Converted text is empty or exceeds the selected text budget.')
            # Strict UTF-8, no replacement characters introduced by this host.
            value = converted.read_bytes().decode('utf-8', errors='strict')
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
            published = False
            try:
                os.link(converted, target)
                published = True
                if cancelled():
                    raise InterruptedError('Document conversion cancelled before its provenance receipt.')
                os.link(receipt, report_path)  # Receipt is the completion marker.
                sync_directory(target.parent)
            except BaseException:
                if published and target.exists() and target.stat().st_ino == converted.stat().st_ino:
                    target.unlink()
                raise
        return ConversionResult(target, report, report_path)

    def extract_many(self, sources, *, cancelled=lambda: False, adapter='auto', ocr_languages=()):
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
                                        adapter=adapter, ocr_languages=ocr_languages))
        return tuple(results)
