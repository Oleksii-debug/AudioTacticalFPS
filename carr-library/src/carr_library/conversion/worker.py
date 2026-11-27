"""Isolated, bounded built-in conversion worker. Invoked by the host only."""
import json
import os
from pathlib import Path
import sys
from time import monotonic


def image_details(source, budget):
    from PIL import Image
    from .models import ConversionError
    with Image.open(source) as image:
        if image.format not in ('PNG', 'JPEG', 'BMP', 'TIFF', 'WEBP'):
            raise ConversionError('Unsupported raster format for document OCR.')
        frames = getattr(image, 'n_frames', 1)
        if not 1 <= frames <= budget.max_pages:
            raise ConversionError('Raster frame count exceeds the OCR budget.')
        total = 0
        for index in range(frames):
            image.seek(index)
            pixels = image.width * image.height
            total += pixels
            if not 0 < pixels <= budget.max_image_pixels or total > budget.max_total_image_pixels:
                raise ConversionError('Raster dimensions exceed the OCR pixel budget.')
        return {'image_format': image.format, 'image_frames': frames, 'image_pixels': total}


def main():
    if len(sys.argv) != 2:
        raise ValueError('A private conversion request is required.')
    request_path = Path(sys.argv[1]).resolve(strict=True)
    if request_path.stat().st_size > 65536:
        raise ValueError('Conversion request is too large.')
    request = json.loads(request_path.read_text(encoding='utf-8'))
    from .models import ConversionBudget
    budget = ConversionBudget(**request['budget'])
    if os.name == 'posix':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (budget.memory_bytes, budget.memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (budget.max_text_bytes + 16 * 1024 * 1024,
                                                  budget.max_text_bytes + 16 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(budget.timeout)), max(2, int(budget.timeout) + 1)))
    stage = request_path.parent
    source = Path(request['source']).resolve(strict=True)
    output = Path(request['output']).resolve()
    report = Path(request['report']).resolve()
    if any(not path.is_relative_to(stage) for path in (source, output, report)):
        raise ValueError('Conversion worker paths escaped private staging.')
    native_ocr = [None]
    def audit(event, args):
        if event.startswith('socket.') or event in ('os.system', 'os.posix_spawn'):
            raise PermissionError('The document conversion worker does not permit network or child commands.')
        if event == 'subprocess.Popen' and (native_ocr[0] is None or args[0] != native_ocr[0]):
            raise PermissionError('Only the verified OCR executable may be started by the OCR adapter.')
        if event == 'open' and args and isinstance(args[0], (str, bytes)):
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if write and not Path(os.fsdecode(args[0])).resolve().is_relative_to(stage):
                raise PermissionError('Conversion worker may only write private staging files.')
    sys.addaudithook(audit)
    if request.get('operation', 'extract') in ('inspect_image', 'ocr_image'):
        # Image metadata is untrusted too. Keep Pillow plugins and every frame
        # header inside the same bounded worker used for document parsing.
        result = image_details(source, budget)
        if request.get('operation') == 'ocr_image':
            from .models import ConversionError
            from ..extract.ocr_runtime import discover_ocr_runtime
            from ..extract.ocr_process import run_ocr
            from ..extract.runtime_identity import runtime_identity
            from ..extract.page_ocr import Config
            languages = '+'.join(request['languages'])
            deadline = monotonic() + budget.timeout

            def remaining():
                seconds = deadline - monotonic()
                if seconds <= 0:
                    raise TimeoutError('Image OCR exceeded its selected time budget.')
                return seconds

            runtime = discover_ocr_runtime(languages, model_profile=request['profile'], remaining=remaining)
            if runtime is None or runtime.fingerprint() != request['runtime']:
                raise ConversionError('The selected OCR runtime changed before execution.')
            native_ocr[0] = runtime.executable
            config = Config(languages=languages, model_profile=request['profile'],
                            timeout=max(1, min(120, int(budget.timeout))))
            before = runtime_identity(config)
            run_ocr(runtime, [runtime.executable, str(source), str(stage / 'ocr'),
                              '-l', languages, '--psm', '3'], remaining=remaining)
            runtime.assert_unchanged(remaining)
            if runtime_identity(config) != before:
                raise ConversionError('The selected OCR engine or models changed during execution.')
            native_ocr[0] = None
            os.replace(stage / 'ocr.txt', output)
            result = {'engine': 'local-tesseract-ocr', 'sections': [],
                'metadata': {'languages': request['languages'], 'model_profile': request['profile'],
                             **result, 'runtime_identity': before},
                'warnings': ['OCR text is an unverified reading derivative. Compare it with the source before quoting.',
                             'Coordinates, page labels, orientation and document layout are not reconstructed by this text-only export.']}
    elif request.get('operation', 'extract') == 'extract':
        from .formats import extract_builtin
        result = extract_builtin(source, budget, encoding=request.get('encoding'))
        value = result.pop('text')
        output.write_bytes(value.encode('utf-8'))
    else:
        raise ValueError('Unsupported conversion worker operation.')
    report.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Reader diagnostics are fixed explanations; no source bodies or secrets.
        print(type(exc).__name__ + ': ' + str(exc)[:2000], file=sys.stderr)
        raise SystemExit(1)
