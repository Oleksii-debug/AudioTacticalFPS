"""Report the existing document registry, without adding parser capabilities."""
from importlib.metadata import PackageNotFoundError, version

from ..extract.document_formats import FORMAT_BY_SUFFIX, SUPPORTED_SUFFIXES

LEGACY = ('.azw', '.azw3', '.chm', '.djvu', '.doc', '.lit', '.mobi', '.pdb',
          '.prc', '.ppt', '.rtf', '.tcr', '.wpd', '.xls')
IMAGES = ('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp')


def _dependencies(suffix):
    if suffix == '.pdf':
        return ('pypdf', 'pypdfium2', 'Pillow')
    if suffix == '.docx':
        return ('python-docx', 'defusedxml')
    if suffix == '.epub' or suffix in FORMAT_BY_SUFFIX and FORMAT_BY_SUFFIX[suffix].family == 'xml':
        return ('defusedxml',)
    return ()


def capabilities():
    """Installed dependencies are not per-file fidelity or language qualification."""
    result = []
    reviewed_versions = {'python-docx': '1.2.0', 'defusedxml': '0.7.1'}
    base = {
        '.txt': 'Strict UTF-8 reading text; a UTF-8 BOM is omitted. Form-feed boundaries and other characters remain.',
        '.pdf': 'Physical page order through the existing PDF/OCR reader. OCR is unverified; missing or failed OCR and mixed native/image pages need the existing reviewed import workflow.',
        '.docx': 'Existing ordered body paragraphs and simple tables. Unsupported structures fail; no printed page numbers are invented.',
        '.epub': 'Existing static XHTML/spine reader. Logical locations remain separate from printed pagination.',
    }
    for suffix in sorted(SUPPORTED_SUFFIXES):
        installed, missing = {}, []
        for package in _dependencies(suffix):
            try:
                installed[package] = version(package)
                expected = reviewed_versions.get(package)
                if expected is not None and installed[package] != expected:
                    missing.append(package + '==' + expected + ' (installed ' + installed[package] + ')')
            except PackageNotFoundError:
                missing.append(package)
        limitation = base.get(suffix) or FORMAT_BY_SUFFIX[suffix].limitations
        result.append(dict(extension=suffix, available=not missing, adapter='existing isolated document reader',
                           installed=installed, missing=missing, limitation=limitation,
                           qualification='dependencies_present' if not missing else 'dependencies_missing'))
    for suffix in (*LEGACY, *IMAGES):
        result.append(dict(extension=suffix, available=False, adapter='not enabled by this reader registry',
                           installed={}, missing=[], qualification='not_integrated',
                           limitation='Use a separately reviewed conversion tool and import the resulting supported copy. No converter, model, decryption or DRM bypass is supplied.'))
    return tuple(result)
