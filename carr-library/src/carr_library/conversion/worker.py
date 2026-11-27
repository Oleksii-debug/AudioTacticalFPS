"""Isolated, bounded built-in conversion worker. Invoked by the host only."""
import json
import os
from pathlib import Path
import sys
import time


def main():
    if len(sys.argv) != 2:
        raise ValueError('A private conversion request is required.')
    request_path = Path(sys.argv[1]).resolve(strict=True)
    if request_path.stat().st_size > 65536:
        raise ValueError('Conversion request is too large.')
    request = json.loads(request_path.read_text(encoding='utf-8'))
    from .models import ConversionBudget
    budget = ConversionBudget(**request['budget'])
    operation = request.get('operation', 'extract')
    deadline = time.monotonic() + budget.timeout
    if os.name == 'posix':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (budget.memory_bytes, budget.memory_bytes))
        file_limit = max(budget.max_text_bytes + 16 * 1024 * 1024,
                         budget.max_source_bytes if operation == 'office' else 0)
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
        resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(budget.timeout)), max(2, int(budget.timeout) + 1)))
    stage = request_path.parent
    source = Path(request['source']).resolve(strict=True)
    output = Path(request['output']).resolve()
    report = Path(request['report']).resolve()
    if any(not path.is_relative_to(stage) for path in (source, output, report)):
        raise ValueError('Conversion worker paths escaped private staging.')
    office_executable = None
    if operation == 'office':
        from .office import _admit, OFFICE_SUFFIXES
        if source.suffix.casefold() not in OFFICE_SUFFIXES:
            raise ValueError('Unsupported selected office input family.')
        office_executable = str(_admit(request['office'], source.suffix.casefold(), deadline))
    def audit(event, args):
        if event == 'subprocess.Popen':
            if office_executable is None or not args or str(args[0]) != office_executable:
                raise PermissionError('The conversion worker permits only its selected fixed office converter.')
        elif event.startswith('socket.') or event in ('os.system', 'os.posix_spawn'):
            raise PermissionError('The document conversion worker does not permit network or unselected child commands.')
        if event == 'open' and args and isinstance(args[0], (str, bytes)):
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if write and not Path(os.fsdecode(args[0])).resolve().is_relative_to(stage):
                raise PermissionError('Conversion worker may only write private staging files.')
    sys.addaudithook(audit)
    if request.get('operation', 'extract') == 'inspect_image':
        # Image metadata is untrusted too. Keep Pillow plugins and every frame
        # header inside the same bounded worker used for document parsing.
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
            result = {'image_format': image.format, 'image_frames': frames, 'image_pixels': total}
    elif operation == 'office':
        from hashlib import sha256
        from .office import office_records, PROFILE
        from .formats import TextOutput
        from ..audio.jobs import file_identity, regular_file, verified_reader
        info = regular_file(source)
        if info.st_nlink != 1 or not 0 < info.st_size <= budget.max_source_bytes:
            raise ValueError('The selected office source exceeds its retained snapshot budget.')
        with verified_reader(source, expected_identity=file_identity(info)) as stream:
            data = stream.read(budget.max_source_bytes + 1)
        engine, _separator, records, warnings = office_records(source, data,
            source.suffix.casefold(), budget, deadline, request['office'])
        text = TextOutput(budget)
        for record in records:
            text.add(record['raw_text'], str(record.get('logical_locator') or {}))
        text.warnings.extend(warnings)
        selection_digest = sha256(json.dumps(request['office'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        result = text.result(engine, {'selected_components_sha256': selection_digest,
            'source_format': source.suffix.casefold(), 'private_profile_sha256': sha256(PROFILE).hexdigest(),
            'source_locators': [record.get('logical_locator') for record in records],
            'source_review_required': True})
        value = result.pop('text')
        output.write_bytes(value.encode('utf-8'))
    elif operation == 'extract':
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
