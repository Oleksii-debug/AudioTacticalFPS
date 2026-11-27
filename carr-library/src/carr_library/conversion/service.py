"""Create source-preserving text derivatives with atomic provenance receipts."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from time import monotonic

from ..durable_state import _open_json_file, local_path, regular, sync_directory
from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError
from ..audio.cleanup import retained_temporary_directory
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


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


@contextmanager
def _opened_snapshot(path, *, links=(1,)):
    """Hold the same bounded regular file through pathname revalidation."""
    def checked(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink not in links
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ConversionError('Conversion requires an unchanged regular local file.')
        return _stamp(info)

    before = checked(Path(path).lstat())
    if os.name == 'nt':
        # Reuse the canonical non-following, non-inheritable Win32 reader.
        stream = _open_json_file(Path(path))
    else:
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
        descriptor = os.open(path, flags)
        try:
            stream = os.fdopen(descriptor, 'rb')
        except BaseException:
            os.close(descriptor)
            raise
    with stream:
        if checked(os.fstat(stream.fileno())) != before:
            raise ConversionError('The conversion file changed before it was read.')
        yield stream, before
        if (checked(os.fstat(stream.fileno())) != before
                or checked(Path(path).lstat()) != before):
            raise ConversionError('The conversion file changed while it was read.')


def _read_bytes(path, maximum, cancelled=lambda: False):
    with _opened_snapshot(path) as (stream, before):
        if before[2] > maximum:
            raise ConversionError('The conversion file exceeds its selected byte budget.')
        if cancelled():
            raise InterruptedError('Document conversion cancelled.')
        value = stream.read(before[2] + 1)
        if len(value) != before[2]:
            raise ConversionError('The conversion file changed while it was read.')
        if cancelled():
            raise InterruptedError('Document conversion cancelled.')
    return value


def _digest(path, cancelled=lambda: False, *, maximum=None, links=(1,), deadline=None):
    result, count = sha256(), 0
    with _opened_snapshot(path, links=links) as (stream, before):
        if maximum is not None and before[2] > maximum:
            raise ConversionError('The conversion file exceeds its selected byte budget.')
        while True:
            if cancelled():
                raise InterruptedError('Document conversion cancelled.')
            if deadline is not None and monotonic() >= deadline:
                raise TimeoutError('Verifying the saved conversion exceeded its time budget.')
            data = stream.read(min(1024 * 1024, before[2] + 1 - count))
            if not data:
                break
            count += len(data)
            if count > before[2]:
                raise ConversionError('The conversion file grew while it was read.')
            result.update(data)
        if count != before[2]:
            raise ConversionError('The conversion file changed while it was read.')
    return result.hexdigest()


def _parent_identity(path):
    info = regular(path, directory=True)
    return info.st_dev, info.st_ino


def _verify_published_pair(root, parent, parent_identity, files, *, timeout):
    """Prove this invocation's exact two outputs, without late cancellation."""
    if (local_path(parent, root=root, exists=True) != parent
            or _parent_identity(parent) != parent_identity):
        raise ConversionError('The conversion destination directory changed.')
    deadline = monotonic() + min(timeout, 30)
    observed = []
    for staged, target, identity, expected in files:
        retained = os.path.lexists(staged)
        links = (2,) if retained else (1,)
        info = target.lstat()
        stamp = _stamp(info)
        if (info.st_dev, info.st_ino, info.st_size) != identity:
            raise ConversionError('The saved conversion is not this publication.')
        stage_stamp = None
        if retained:
            stage_info = staged.lstat()
            stage_stamp = _stamp(stage_info)
            if (stat.S_ISLNK(stage_info.st_mode)
                    or getattr(stage_info, 'st_file_attributes', 0) & 0x400
                    or (stage_info.st_dev, stage_info.st_ino, stage_info.st_size) != identity):
                raise ConversionError('The conversion staging identity changed.')
        if _digest(target, maximum=identity[2], links=links, deadline=deadline) != expected:
            raise ConversionError('The saved conversion bytes changed.')
        observed.append((staged, target, identity, expected, links, stamp, stage_stamp))
    # The second digest cannot bless a replacement or edit of the first file.
    # Rehash earlier files once, under the same finite pair-verification budget,
    # then recheck every selected and staging name as one final graph.
    for _staged, target, identity, expected, links, _stamp_before, _stage_before in observed[:-1]:
        if _digest(target, maximum=identity[2], links=links, deadline=deadline) != expected:
            raise ConversionError('The saved conversion bytes changed.')
    for staged, target, _identity, _expected, _links, stamp, stage_stamp in observed:
        if _stamp(target.lstat()) != stamp:
            raise ConversionError('The saved conversion changed during pair verification.')
        if stage_stamp is None:
            if os.path.lexists(staged):
                raise ConversionError('The conversion staging identity changed.')
        elif _stamp(staged.lstat()) != stage_stamp:
            raise ConversionError('The conversion staging identity changed.')
    if (local_path(parent, root=root, exists=True) != parent
            or _parent_identity(parent) != parent_identity):
        raise ConversionError('The conversion destination directory changed.')


def _rollback_owned_outputs(parent, parent_identity, files):
    """Remove only this attempt's incomplete names, preserving foreign files."""
    if _parent_identity(parent) != parent_identity:
        return
    for staged, target, identity, _expected in reversed(files):
        try:
            info, stage_info = target.lstat(), staged.lstat()
        except FileNotFoundError:
            continue
        if ((info.st_dev, info.st_ino, info.st_size) == identity
                and (stage_info.st_dev, stage_info.st_ino, stage_info.st_size) == identity
                and stat.S_ISREG(info.st_mode) and stat.S_ISREG(stage_info.st_mode)
                and not getattr(info, 'st_file_attributes', 0) & 0x400):
            target.unlink()


def _completion_warning():
    return ('The reading text and its provenance were verified as published, but final '
            'cleanup or durability confirmation was not completed. Review the saved files '
            'before starting another conversion.')


class ConversionService:
    def __init__(self, root, *, input_roots=(), budget=None, pandoc=None):
        self.root = local_path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.input_roots = tuple(local_path(path, exists=True) for path in input_roots)
        self.budget = budget or ConversionBudget()
        if not isinstance(self.budget, ConversionBudget):
            raise TypeError('A typed conversion budget is required.')
        self.pandoc = local_path(pandoc, exists=True) if pandoc else None
        if self.pandoc and not self.pandoc.is_file():
            raise ValueError('Select an installed Pandoc executable.')

    def capabilities(self):
        pandoc = str(self.pandoc) if self.pandoc else shutil.which('pandoc')
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
        for suffix in LEGACY:
            values.append({'extension': suffix, 'available': False, 'adapter': 'separate reviewed converter needed',
                           'limitation': 'Legacy format is not enabled without a tested offline/active-content isolation adapter; no DRM bypass.'})
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
                             watch=(output,), env=self._env(stage), cwd=stage, owned_tree=True)
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
                    watch=(image_report,), env=self._env(stage), cwd=stage, owned_tree=True)
        if regular(image_report).st_size > 65536:
            raise ConversionError('Raster metadata exceeded its report budget.')
        image_metadata = json.loads(_read_bytes(image_report, 65536, cancelled).decode('utf-8'))
        executable = str(local_path(runtime.executable, exists=True))
        arguments = runtime.command([executable, str(source), str(stage / 'ocr'), '-l', '+'.join(languages), '--psm', '3'])
        generated = stage / 'ocr.txt'
        run_process(arguments, budget=self._process_budget(), cancelled=cancelled, watch=(generated,),
                    env=self._env(stage), cwd=runtime.root or stage, owned_tree=True)
        runtime.assert_unchanged()
        os.replace(generated, output)
        return {'engine': 'local-tesseract-ocr', 'sections': [],
                'metadata': {'languages': list(languages), **image_metadata},
                'warnings': ['OCR text is an unverified reading derivative. Compare it with the source before quoting.',
                             'Coordinates, page labels, orientation and document layout are not reconstructed by this text-only export.']}

    def extract(self, source, output, *, cancelled=lambda: False, adapter='auto', encoding=None, ocr_languages=()):
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
        completed = None
        files = ()
        parent_identity = None
        try:
            with retained_temporary_directory(prefix='.pending-conversion-', dir=target.parent) as folder:
                stage = Path(folder)
                snapshot = stage / ('source' + suffix)
                converted, details_path = stage / 'reading.txt', stage / 'details.json'
                with _opened_snapshot(source) as (read, snapshot_before), snapshot.open('xb') as write:
                    if snapshot_before[:5] != before:
                        raise ConversionError('Source changed before its snapshot was opened.')
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
                            cancelled=cancelled, watch=(converted, details_path), env=self._env(stage), cwd=stage, owned_tree=True)
                        if regular(details_path).st_size > 16 * 1024 * 1024:
                            raise ConversionError('Conversion provenance exceeded its budget.')
                        details = json.loads(_read_bytes(details_path, 16 * 1024 * 1024, cancelled).decode('utf-8'))
                except AudioCleanupPending:
                    raise  # The retained directory and native owner must reach the host unchanged.
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
                value = _read_bytes(converted, self.budget.max_text_bytes, cancelled).decode('utf-8', errors='strict')
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
                receipt_bytes = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
                if len(receipt_bytes) > 16 * 1024 * 1024:
                    raise ConversionError('Conversion provenance exceeded its byte budget.')
                receipt.write_bytes(receipt_bytes)
                for path in (converted, receipt):
                    with path.open('r+b') as stream:
                        os.fsync(stream.fileno())
                # Recheck the fsynced staged bytes and original source before the
                # first output becomes visible. The exact wire receipt is unchanged.
                if (_digest(converted, cancelled, maximum=self.budget.max_text_bytes) != digest
                        or _digest(receipt, cancelled, maximum=16 * 1024 * 1024) != sha256(receipt_bytes).hexdigest()):
                    raise ConversionError('The staged conversion changed before publication.')
                if _fingerprint(source) != before or _digest(source, cancelled, maximum=self.budget.max_source_bytes) != original_digest.hexdigest():
                    raise ConversionError('The original source changed; derivative was not published.')
                self._output(target)  # Repeat local/path/no-overwrite authorization before publication.
                parent_identity = _parent_identity(target.parent)
                files = tuple((staged, output_path,
                               (info.st_dev, info.st_ino, info.st_size), expected)
                    for staged, output_path, info, expected in (
                        (converted, target, regular(converted), digest),
                        (receipt, report_path, regular(receipt), sha256(receipt_bytes).hexdigest())))
                if cancelled():
                    raise InterruptedError('Document conversion cancelled before publication.')
                try:
                    os.link(converted, target)
                    if cancelled():
                        raise InterruptedError('Document conversion cancelled before its provenance receipt.')
                    os.link(receipt, report_path)  # Receipt is the completion marker.
                    _verify_published_pair(self.root, target.parent, parent_identity, files,
                                           timeout=self.budget.timeout)
                    completed = ConversionResult(target, report, report_path)
                    # No cancellation after a proven completion marker. Cleanup and
                    # directory durability are reported separately from saved bytes.
                    converted.unlink()
                    receipt.unlink()
                    sync_directory(target.parent)
                except (Exception, KeyboardInterrupt) as exc:
                    try:
                        _verify_published_pair(self.root, target.parent, parent_identity, files,
                                               timeout=self.budget.timeout)
                    except (Exception, KeyboardInterrupt):
                        _rollback_owned_outputs(target.parent, parent_identity, files)
                        raise exc
                    completed = ConversionResult(target, report, report_path, _completion_warning())
        except AudioCleanupPending:
            raise  # The exact retained native owner remains responsible for staging.
        except (Exception, KeyboardInterrupt):
            if completed is None:
                raise
            _verify_published_pair(self.root, target.parent, parent_identity, files,
                                   timeout=self.budget.timeout)
            completed = replace(completed, cleanup_warning=_completion_warning())
        if completed is None:
            raise ConversionError('The conversion did not publish a complete result.')
        _verify_published_pair(self.root, target.parent, parent_identity, files,
                               timeout=self.budget.timeout)
        return completed

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
