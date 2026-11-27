"""Synthetic legacy containers; no copyrighted book or active fixture is copied."""
import struct
from uuid import UUID


def palm(text=b'Synthetic public reading text.', *, encrypted=False):
    header = bytearray(78)
    header[:16] = b'Synthetic source'
    header[60:68] = b'TEXtREAd'
    header[76:78] = struct.pack('>H', 2)
    records = struct.pack('>II', 96, 0) + struct.pack('>II', 112, 1) + b'\0\0'
    first = struct.pack('>HHIHHHH', 1, 0, len(text), 1, 4096, int(encrypted), 0)
    return bytes(header) + records + first + text


def iff(kind, *chunks):
    payload = kind + b''.join(name + struct.pack('>I', len(data)) + data + b'\0' * (len(data) & 1)
                              for name, data in chunks)
    return b'AT&TFORM' + struct.pack('>I', len(payload)) + payload


def _encint(value):
    parts = [value & 127]
    value >>= 7
    while value:
        parts.append(128 | (value & 127))
        value >>= 7
    return bytes(reversed(parts))


def chm(files):
    """Minimal uncompressed ITSF3/ITSP/PMGL container built from synthetic bytes.

    Field layout follows the publisher's CHM reader, without copying its code:
    https://github.com/ip7z/7zip/blob/26.06/CPP/7zip/Archive/Chm/ChmIn.cpp
    A real 7-Zip child, not this generator, must accept every native fixture.
    """
    contents = {'::DataSpace/NameList': struct.pack('<HHH', 16, 1, 12) +
                'Uncompressed'.encode('utf-16-le') + b'\0\0'}
    contents.update({'/' + name: data for name, data in files.items()})
    body, rows = bytearray(), bytearray()
    for name, data in sorted(contents.items()):
        name = name.encode('utf-8')
        rows += _encint(len(name)) + name + _encint(0) + _encint(len(body)) + _encint(len(data))
        body += data
    chunk_size = 4096
    free = chunk_size - 20 - len(rows)
    if free < 2:
        raise ValueError('Synthetic CHM directory needs an additional chunk.')
    pmgl = struct.pack('<4sIIII', b'PMGL', free, 0, 0xffffffff, 0xffffffff)
    pmgl += rows + b'\0' * (free - 2) + struct.pack('<H', len(contents))
    itsp = struct.pack('<4s12I16s4I', b'ITSP', 1, 84, 10, chunk_size, 2, 1,
        0xffffffff, 0, 0, 0xffffffff, 1, 1033,
        UUID('5d02926a-212e-11d0-9df9-00a0c922e6ec').bytes_le, 84,
        0xffffffff, 0xffffffff, 0xffffffff)
    directory = itsp + pmgl
    content_offset = 96 + 24 + len(directory)
    size = content_offset + len(body)
    itsf = struct.pack('<4s5I16s16s5Q', b'ITSF', 3, 96, 1, 0, 1033,
        UUID('7c01fd10-7baa-11d0-9e0c-00a0c922e6ec').bytes_le,
        UUID('7c01fd11-7baa-11d0-9e0c-00a0c922e6ec').bytes_le,
        96, 24, 120, len(directory), content_offset)
    section = struct.pack('<IIQII', 0x1fe, 0, size, 0, 0)
    return itsf + section + directory + bytes(body)


def sample_chm():
    return chm({
        'a.htm': b'<html><head><meta charset="utf-8"></head><body><p>SECOND CHM READING PAGE</p>'
                 b'<script>ACTIVE_CONTENT_MUST_NOT_APPEAR()</script></body></html>',
        'z.htm': b'<html><body><p>FIRST CHM READING PAGE</p></body></html>',
        'contents.hhc': b'<html><body><object type="text/sitemap"><param name="Local" value="z.htm"></object>'
                        b'<object type="text/sitemap"><param name="Local" value="a.htm"></object></body></html>',
    })


def tcr(text):
    """Synthetic literal dictionary in the publisher's documented TCR framing.

    The native reader still performs real TCR decoding. A one-byte mapping
    avoids invoking the unrelated publisher compressor for a cancellation input.
    Format reference: calibre v9.19.0 src/calibre/ebooks/compression/tcr.py.
    """
    if type(text) is not bytes or not text:
        raise ValueError('A nonempty synthetic byte body is required.')
    return b'!!8-Bit!!' + b''.join(bytes((1, code)) for code in range(256)) + text
