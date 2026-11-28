"""Public synthetic PDF with a real page and an unreferenced large stream.

The byte count measures container streaming, not a real corpus or a complex
multi-gigabyte page. No sparse file, truncated fake PDF or external asset is used.
"""
import os

MIB = 1024 * 1024


def write_pdf(path, payload_bytes=72 * MIB):
    offsets = [0]
    with path.open('xb') as stream:
        stream.write(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')

        def object_(number, content):
            offsets.append(stream.tell())
            stream.write(f'{number} 0 obj\n'.encode('ascii'))
            stream.write(content)
            stream.write(b'\nendobj\n')

        object_(1, b'<< /Type /Catalog /Pages 2 0 R >>')
        object_(2, b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>')
        object_(3, b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                   b'/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>')
        object_(4, b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
        content = b'BT /F1 12 Tf 72 700 Td (Large railway original) Tj ET\n'
        object_(5, f'<< /Length {len(content)} >>\nstream\n'.encode('ascii') +
                content + b'endstream')
        offsets.append(stream.tell())
        stream.write(f'6 0 obj\n<< /Length {payload_bytes} >>\nstream\n'.encode('ascii'))
        block = b'x' * min(MIB, max(1, payload_bytes))
        remaining = payload_bytes
        while remaining:
            part = block[:min(len(block), remaining)]
            stream.write(part)
            remaining -= len(part)
        stream.write(b'\nendstream\nendobj\n')
        xref = stream.tell()
        stream.write(b'xref\n0 7\n0000000000 65535 f \n')
        for offset in offsets[1:]:
            stream.write(f'{offset:010d} 00000 n \n'.encode('ascii'))
        stream.write(f'trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode('ascii'))
        stream.flush()
        os.fsync(stream.fileno())
    return path
