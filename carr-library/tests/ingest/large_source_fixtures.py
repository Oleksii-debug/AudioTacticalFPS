"""Real, bounded-on-write source fixtures; no sparse files or fake size metadata."""
from hashlib import sha256
from pathlib import Path

MIB = 1024 * 1024
SEGMENT_BYTES = 4 * MIB


def file_digest(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(MIB):
            digest.update(block)
    return digest.hexdigest()


def write_large_text(path, *, segments=20):
    """80 MiB of prose, with only original form-feed logical boundaries."""
    path = Path(path)
    digest, rows, offset, meaningful = sha256(), [], 0, 0
    filler = (b'Documented railway infrastructure preserves original historical evidence; '
              b'bibliographic provenance records verified chronology without replacing the source.\r\n') * 512
    with path.open('xb') as stream:
        for index in range(segments):
            if index:
                stream.write(b'\f')
                digest.update(b'\f')
                offset += 1
            prefix = (f'Largeanchor{index:03d} original line. Київ.\r\n').encode('utf-8')
            meaningful += len(prefix.translate(None, b' \t\r\n\f'))
            payload_digest = sha256(prefix)
            stream.write(prefix)
            digest.update(prefix)
            remaining = SEGMENT_BYTES - len(prefix)
            while remaining:
                chunk = filler[:min(remaining, len(filler))]
                stream.write(chunk)
                digest.update(chunk)
                payload_digest.update(chunk)
                meaningful += len(chunk.translate(None, b' \t\r\n\f'))
                remaining -= len(chunk)
            rows.append((offset, SEGMENT_BYTES, payload_digest.hexdigest()))
            offset += SEGMENT_BYTES
    assert path.stat().st_size == segments * SEGMENT_BYTES + segments - 1
    return dict(path=path, size=offset, sha256=digest.hexdigest(), segments=tuple(rows),
                meaningful_bytes=meaningful)


def write_large_pdf(path, *, size=128 * MIB):
    """A valid one-page PDF with a real uncompressed embedded evidence file.

    The large bytes belong to an EmbeddedFile stream referenced by the PDF
    catalog. There is no executable attachment, sparse allocation, trailing
    junk or mocked stat size. Both actual pinned parsers inspect the page.
    """
    path = Path(path)
    stream_header = b'7 0 obj\n<< /Type /EmbeddedFile /Length '
    page_text = b'BT /F1 12 Tf 72 700 Td (Large PDF railway evidence.) Tj ET\n'
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(evidence.txt) 6 0 R] >> >> >>',
        b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(page_text)).encode() + b' >>\nstream\n' + page_text + b'endstream',
        b'<< /Type /Filespec /F (evidence.txt) /EF << /F 7 0 R >> >>',
    ]
    header = b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n'
    prefix, offsets = bytearray(header), [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(prefix))
        prefix.extend(str(index).encode() + b' 0 obj\n' + value + b'\nendobj\n')
    offsets.append(len(prefix))

    def parts(payload_size):
        opening = stream_header + str(payload_size).encode() + b' >>\nstream\n'
        closing = b'\nendstream\nendobj\n'
        xref = len(prefix) + len(opening) + payload_size + len(closing)
        tail = (b'xref\n0 8\n0000000000 65535 f \n'
                + b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
                + b'trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n'
                + str(xref).encode() + b'\n%%EOF\n')
        return opening, closing, tail

    payload = size - len(prefix) - 256
    for _ in range(8):
        opening, closing, tail = parts(payload)
        corrected = size - len(prefix) - len(opening) - len(closing) - len(tail)
        if corrected == payload:
            break
        payload = corrected
    opening, closing, tail = parts(payload)
    assert payload > 0 and len(prefix) + len(opening) + payload + len(closing) + len(tail) == size
    hashed = sha256()
    with path.open('xb') as stream:
        def write(data):
            stream.write(data)
            hashed.update(data)
        write(prefix)
        write(opening)
        block = (b'Original embedded evidence, preserved byte for byte.\r\n' * 2048)
        remaining = payload
        while remaining:
            part = block[:min(remaining, len(block))]
            write(part)
            remaining -= len(part)
        write(closing)
        write(tail)
    assert path.stat().st_size == size
    return dict(path=path, size=size, sha256=hashed.hexdigest(), embedded_size=payload)


def instrument_import_worker(monkeypatch, *, code=''):
    """Instrument only this supervisor's real bootstrap; keep stdlib Popen intact."""
    from carr_library.ingest import worker
    delegate = worker.subprocess
    processes = []
    anchor = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"

    def launch(command, *args, **options):
        selected = False
        if isinstance(command, (list, tuple)) and '-c' in command:
            index = command.index('-c') + 1
            selected = index < len(command) and anchor in command[index]
            if selected and code:
                command = list(command)
                command[index] = command[index].replace(
                    anchor, 'exec(' + repr(code) + '); ' + anchor)
        process = delegate.Popen(command, *args, **options)
        if selected:
            processes.append(process)
        return process

    class WorkerSubprocess:
        Popen = staticmethod(launch)

        def __getattr__(self, name):
            return getattr(delegate, name)

    monkeypatch.setattr(worker, 'subprocess', WorkerSubprocess())
    return processes


def observe_import_peak(monkeypatch, peak_path):
    """Measure the actual existing worker, preserving its bootstrap and source."""
    code = r'''
def _write_large_import_peak():
    import json, os, sys
    from pathlib import Path
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        kernel.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        peak = counters.PeakWorkingSetSize
        metric = 'PeakWorkingSetSize'
    else:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak *= 1 if sys.platform == 'darwin' else 1024
        metric = 'ru_maxrss'
    Path(PEAK_FILE).write_text(json.dumps({'peak_bytes': peak, 'metric': metric}), encoding='utf-8')
'''
    code = code.replace('PEAK_FILE', repr(str(peak_path)))

    return instrument_import_worker(monkeypatch, code=(
        code + '\nimport atexit\natexit.register(_write_large_import_peak)\n'))
