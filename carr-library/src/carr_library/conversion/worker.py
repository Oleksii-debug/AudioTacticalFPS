"""Isolated, bounded built-in conversion worker. Invoked by the host only."""
from hashlib import sha256
import json
import re
import os
from pathlib import Path
import sys


def main():
    if len(sys.argv) not in (2, 3):
        raise ValueError('A private conversion request is required.')
    from ._selected_files import regular_file, verified_reader
    request_path = Path(sys.argv[1]).absolute()
    info = regular_file(request_path)
    if not 0 < info.st_size <= 65536:
        raise ValueError('Conversion request is too large.')
    with verified_reader(request_path) as stream:
        raw = stream.read(65537)
    if len(raw) != info.st_size:
        raise ValueError('The conversion request changed.')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate conversion request key.')
            value[key] = item
        return value
    request = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                         parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('Invalid conversion number.')))
    if type(request) is not dict:
        raise ValueError('Invalid conversion request.')
    selected = request.get('operation') == 'selected'
    if selected and (len(sys.argv) != 3 or re.fullmatch('[0-9a-f]{64}', sys.argv[2]) is None
                     or sha256(raw).hexdigest() != sys.argv[2]):
        raise ValueError('The selected conversion request checksum changed.')
    if (selected and os.name != 'nt' and type(request.get('adapter')) is dict
            and request['adapter'].get('kind') in ('calibre-book', 'libreoffice')):
        raise ValueError('Selected native book and Office conversion requires an owned Windows Job.')
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
    def audit(event, args):
        if (event.startswith('socket.') or event in ('os.system', 'os.posix_spawn')
                or event == 'subprocess.Popen' and not selected):
            raise PermissionError('The document conversion worker does not permit network or unselected child commands.')
        if event == 'open' and args and isinstance(args[0], (str, bytes)):
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if write and not Path(os.fsdecode(args[0])).resolve().is_relative_to(stage):
                raise PermissionError('Conversion worker may only write private staging files.')
    sys.addaudithook(audit)
    if selected:
        from .selected_worker import selected_request
        result = selected_request(request, budget)
    elif request.get('operation', 'extract') == 'inspect_image':
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
    elif request.get('operation', 'extract') == 'extract':
        from .formats import extract_builtin
        result = extract_builtin(source, budget, encoding=request.get('encoding'))
        value = result.pop('text')
        output.write_bytes(value.encode('utf-8'))
    else:
        raise ValueError('Unsupported conversion worker operation.')
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(raw) > 16 * 1024**2:
        raise ValueError('Conversion provenance exceeds its byte budget.')
    with report.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Reader diagnostics are fixed explanations; no source bodies or secrets.
        print(type(exc).__name__ + ': ' + str(exc)[:2000], file=sys.stderr)
        raise SystemExit(1)
