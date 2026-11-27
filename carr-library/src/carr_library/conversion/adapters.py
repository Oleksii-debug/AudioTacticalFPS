"""Selected RTF and raster adapters inside the existing owned ingest worker.

This module is not a new process broker or document reader framework. The
outer ingest._invoke owner retains every native descendant until it is stopped,
including cancellation while Pandoc or the existing Unicode OCR bridge runs.
"""
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
import time
import warnings

from ..audio.jobs import checked_directory, file_identity, regular_file, verified_reader
from .capabilities import IMAGES
from .models import ConversionError, ConversionUnavailable


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('Selected document adapter exceeded its time allowance.')
    return value


def _executable(selection, deadline):
    if (not isinstance(selection, dict) or set(selection) != {'path', 'bytes', 'sha256'}
            or not isinstance(selection['path'], str) or '\x00' in selection['path']
            or type(selection['bytes']) is not int or not 0 < selection['bytes'] <= 256 * 1024 * 1024
            or not isinstance(selection['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', selection['sha256'])):
        raise ConversionError('Invalid selected converter identity.')
    path = Path(selection['path'])
    if not path.is_absolute():
        raise ConversionError('Selected converter path must be absolute.')
    checked_directory(path.parent)
    info = regular_file(path)
    if info.st_nlink != 1 or info.st_size != selection['bytes']:
        raise ConversionError('The selected converter identity changed.')
    digest = sha256()
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        while block := stream.read(1024 * 1024):
            _remaining(deadline)
            digest.update(block)
    if digest.hexdigest() != selection['sha256']:
        raise ConversionError('The selected converter bytes changed.')
    return path


def _pandoc(source, data, budget, deadline, selection):
    if not re.match(rb'^\s*\{\\rtf[0-9]+(?:\s|\\)', data[:256]):
        raise ConversionError('The selected RTF source has no valid RTF header.')
    executable = _executable(selection, deadline)
    output = source.parent / 'pandoc.txt'
    if os.path.lexists(output):
        raise ConversionError('Private converter output already exists.')
    args = [str(executable), '--sandbox', '--from=rtf', '--to=plain', '--wrap=none',
            '--output=' + output.name, source.name, '+RTS',
            '-M' + str(budget.pandoc_memory_bytes // (1024 * 1024)) + 'M', '-RTS']
    # Deliberately inherit the existing worker's POSIX session / Windows Job.
    # A nested new session would escape the outer Cancel/timeout owner.
    environment = {key: value for key, value in os.environ.items()
                   if key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LD_LIBRARY_PATH')}
    environment.update(TMPDIR=str(source.parent), TEMP=str(source.parent), TMP=str(source.parent))
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        process = subprocess.Popen(args, cwd=source.parent, env=environment,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
            start_new_session=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while True:
                _remaining(deadline)
                if any(os.fstat(stream.fileno()).st_size > 256 * 1024 for stream in (stdout, stderr)):
                    raise ConversionError('Selected converter diagnostics exceeded their byte budget.')
                if os.path.lexists(output) and regular_file(output).st_size > budget.max_text_bytes:
                    raise ConversionError('Selected converter output exceeded its text budget.')
                if process.poll() is not None:
                    break
                time.sleep(.02)
            if process.returncode:
                raise ConversionUnavailable('Selected Pandoc failed its fixed sandboxed RTF conversion.')
            had_warnings = os.fstat(stderr.fileno()).st_size > 0
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    _executable(selection, deadline)
    info = regular_file(output)
    if info.st_nlink != 1 or not 0 < info.st_size <= budget.max_text_bytes:
        raise ConversionError('Selected converter returned an invalid text size.')
    with verified_reader(output, expected_identity=file_identity(info)) as stream:
        raw = stream.read(budget.max_text_bytes + 1)
    if len(raw) != info.st_size:
        raise ConversionError('Selected converter output changed.')
    text = raw.decode('utf-8', errors='strict')
    if not text.strip() or '\x00' in text:
        raise ConversionError('Selected converter returned invalid reading text.')
    notices = ['sandboxed_plain_text_layout_and_printed_pages_not_retained']
    if had_warnings:
        notices.append('converter_reported_warnings_review_source_fidelity')
    record = dict(raw_text=text, physical_index=None, status='review_required',
        logical_locator={'section': 1, 'converter_sha256': selection['sha256'], 'sandbox': True})
    return 'selected-pandoc-rtf-sandbox', '\n\n', [record], notices


def _image_records(source, data, suffix, config, budget, deadline):
    from PIL import Image
    from ..extract.ocr_runtime import bind_model_pack, discover_ocr_runtime
    from ..extract.ocr_process import run_ocr
    from ..extract.page_ocr import _read_ocr_output
    from ..extract.pinned_tessdata import POLICY, stage_pinned_models

    expected = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.bmp': 'BMP',
                '.tif': 'TIFF', '.tiff': 'TIFF', '.webp': 'WEBP'}[suffix]
    # Walk at most max_pages+1 headers rather than asking n_frames to enumerate
    # an unbounded TIFF chain before the first admission check.
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            if image.format != expected:
                raise ConversionError('Image content does not match the selected raster format.')
            frames, total = [], 0
            for index in range(budget.max_pages + 1):
                _remaining(deadline)
                try:
                    image.seek(index)
                except EOFError:
                    break
                if index >= budget.max_pages:
                    raise ConversionError('Raster frame count exceeds the selected page budget.')
                width, height = image.size
                pixels = width * height
                total += pixels
                if (not 0 < pixels <= min(budget.max_image_pixels, config.max_pixels)
                        or total > budget.max_total_image_pixels):
                    raise ConversionError('Raster dimensions exceed the selected pixel budget.')
                frames.append((width, height))
            if not frames:
                raise ConversionError('No raster frames were found.')
            active_deadline = [deadline]
            def remaining():
                return _remaining(min(deadline, active_deadline[0]))
            options = {'model_profile': config.model_profile} if config.model_profile != 'installed' else {}
            runtime = discover_ocr_runtime('eng' if config.model_pack_path else config.languages,
                orientation=False if config.model_pack_path else config.orientation,
                remaining=remaining, **options)
            runtime = bind_model_pack(runtime, config.model_pack_path, config.model_pack_sha256,
                languages=config.languages, orientation=config.orientation, remaining=remaining)
            if runtime is None:
                raise ConversionUnavailable('No reviewed local OCR runtime is available for the selected languages.')
            with TemporaryDirectory(prefix='raster-ocr-', dir=source.parent) as temporary:
                stage = Path(temporary)
                def run(args):
                    return run_ocr(runtime, args, remaining=remaining)
                model_args = []
                if config.model_policy == POLICY:
                    inventory = run([runtime.executable, '--list-langs']).stdout.decode('utf-8')
                    model_args, _ = stage_pinned_models(inventory, config.languages,
                                                       stage / 'verified-models', remaining)
                version = run([runtime.executable, '--version']).stdout.decode('utf-8', 'strict').splitlines()[0]
                if not 1 <= len(version) <= 256:
                    raise ConversionError('The OCR runtime returned invalid version metadata.')
                settings = {key: value for key, value in asdict(config).items() if key != 'model_pack_path'}
                for index, size in enumerate(frames):
                    active_deadline[0] = min(deadline, time.monotonic() + config.timeout)
                    remaining()
                    image.seek(index)
                    if image.size != size:
                        raise ConversionError('Raster dimensions changed during decoding.')
                    # One decoded frame is retained at a time. White compositing
                    # gives transparent document margins explicit white pixels.
                    with image.convert('RGBA') as rgba, Image.new('RGB', size, 'white') as raster:
                        raster.paste(rgba, mask=rgba.getchannel('A'))
                        def recognize(selected, name):
                            inp, out = stage / (name + '.png'), stage / name
                            for product in (inp, out.with_suffix('.txt'), out.with_suffix('.tsv')):
                                if os.path.lexists(product):
                                    if regular_file(product).st_nlink != 1:
                                        raise ConversionError('A private OCR product acquired an unexpected alias.')
                                    product.unlink()
                            selected.save(inp, dpi=(config.dpi, config.dpi))
                            run([runtime.executable, str(inp), str(out), '-l', config.languages,
                                 '--dpi', str(config.dpi), '--psm', '3', *model_args,
                                 '-c', 'tessedit_create_txt=1', '-c', 'tessedit_create_tsv=1'])
                            return _read_ocr_output(out.with_suffix('.txt')), _read_ocr_output(out.with_suffix('.tsv'))
                        raw, _ = recognize(raster, 'frame')
                        orientation = None
                        if config.orientation:
                            from ..extract.ocr_orientation_bridge import orientation_candidate
                            # Preserve the established baseline policy. The
                            # orientation suggestion never silently changes text.
                            orientation = orientation_candidate(raster, config, runtime.executable, remaining, run, recognize,
                                native_cli=runtime.portable or bool(config.model_pack_path) or getattr(runtime, 'managed', False))
                    runtime.assert_unchanged(remaining)
                    yield dict(raw_text=raw, physical_index=None, status='review_required',
                        logical_locator={'image_frame': index + 1, 'image_format': expected,
                            'image_size': list(size), 'ocr_engine': version, 'ocr_config': settings,
                            'runtime_manifest_sha256': runtime.manifest_sha256,
                            'orientation_review': orientation},
                        warnings=['ocr_not_verified', 'image_frame_is_not_a_printed_page'])


def selected_records(source, data, suffix, config, budget, deadline, selection):
    from .office import OFFICE_SUFFIXES, office_records
    from .calibre import CALIBRE_SUFFIXES, calibre_records
    if suffix in CALIBRE_SUFFIXES:
        if (type(selection) is not dict or set(selection) != {'kind', 'runtime'}
                or selection['kind'] != 'calibre-book'):
            raise ConversionUnavailable('This book requires the explicitly selected Calibre core identity.')
        return calibre_records(source, data, suffix, budget, deadline, selection['runtime'])
    if suffix in OFFICE_SUFFIXES:
        if (type(selection) is not dict or set(selection) != {'kind', 'runtime'}
                or selection['kind'] != 'libreoffice'):
            raise ConversionUnavailable('Legacy conversion requires the explicitly selected LibreOffice identity.')
        return office_records(source, data, suffix, budget, deadline, selection['runtime'])
    if suffix == '.rtf':
        if (not isinstance(selection, dict) or set(selection) != {'kind', 'executable'}
                or selection['kind'] != 'pandoc-rtf'):
            raise ConversionUnavailable('RTF requires the explicitly selected converter identity.')
        return _pandoc(source, data, budget, deadline, selection['executable'])
    if suffix in IMAGES:
        if selection != {'kind': 'raster-ocr'}:
            raise ConversionUnavailable('Raster OCR must be explicitly selected.')
        return ('existing-tesseract-raster-ocr', '\n\f\n',
                _image_records(source, data, suffix, config, budget, deadline),
                ['reading_ocr_not_verified_for_quotation', 'frame_boundaries_are_not_printed_pagination'])
    raise ConversionUnavailable('Unsupported selected conversion adapter.')
