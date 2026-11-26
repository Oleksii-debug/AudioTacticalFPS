# -*- coding: utf-8 -*-
# Selected unmodified classes from h2non/filetype.py (MIT).
from .base import Type


class Zip(Type):
    """
    Implements the Zip archive type matcher.
    """
    MIME = 'application/zip'
    EXTENSION = 'zip'

    def __init__(self):
        super(Zip, self).__init__(
            mime=Zip.MIME,
            extension=Zip.EXTENSION
        )

    def match(self, buf):
        return (len(buf) > 3 and
                buf[0] == 0x50 and buf[1] == 0x4B and
                (buf[2] == 0x3 or buf[2] == 0x5 or
                    buf[2] == 0x7) and
                (buf[3] == 0x4 or buf[3] == 0x6 or
                    buf[3] == 0x8))


class Pdf(Type):
    """
    Implements the PDF archive type matcher.
    """
    MIME = 'application/pdf'
    EXTENSION = 'pdf'

    def __init__(self):
        super(Pdf, self).__init__(
            mime=Pdf.MIME,
            extension=Pdf.EXTENSION
        )

    def match(self, buf):
        # Detect BOM and skip first 3 bytes
        if (len(buf) > 3 and
            buf[0] == 0xEF and
            buf[1] == 0xBB and
            buf[2] == 0xBF):  # noqa E129
            buf = buf[3:]

        return (len(buf) > 3 and
                buf[0] == 0x25 and
                buf[1] == 0x50 and
                buf[2] == 0x44 and
                buf[3] == 0x46)


class Exe(Type):
    """
    Implements the EXE archive type matcher.
    """
    MIME = 'application/x-msdownload'
    EXTENSION = 'exe'

    def __init__(self):
        super(Exe, self).__init__(
            mime=Exe.MIME,
            extension=Exe.EXTENSION
        )

    def match(self, buf):
        return (len(buf) > 1 and
                buf[0] == 0x4D and
                buf[1] == 0x5A)


class Rtf(Type):
    """
    Implements the RTF archive type matcher.
    """
    MIME = 'application/rtf'
    EXTENSION = 'rtf'

    def __init__(self):
        super(Rtf, self).__init__(
            mime=Rtf.MIME,
            extension=Rtf.EXTENSION
        )

    def match(self, buf):
        return (len(buf) > 4 and
                buf[0] == 0x7B and
                buf[1] == 0x5C and
                buf[2] == 0x72 and
                buf[3] == 0x74 and
                buf[4] == 0x66)
