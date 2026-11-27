"""Report current readers and adapters that require explicit local selection."""
from importlib.metadata import PackageNotFoundError, version

from ..extract.document_formats import FORMAT_BY_SUFFIX, SUPPORTED_SUFFIXES
from .office import OFFICE_SUFFIXES
from .legacy import LEGACY_SUFFIXES, QUALIFIED_SUFFIXES, kind_for

LEGACY = ('.azw', '.azw3', '.chm', '.djvu', '.doc', '.lit', '.mobi', '.pdb',
          '.prc', '.ppt', '.tcr', '.wpd', '.xls')
IMAGES = ('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp')
CONVERSION_SUFFIXES = frozenset((*SUPPORTED_SUFFIXES, '.rtf', *IMAGES, *OFFICE_SUFFIXES, *LEGACY_SUFFIXES))


def _dependencies(suffix):
    if suffix == '.pdf':
        return ('pypdf', 'pypdfium2', 'Pillow')
    if suffix == '.docx':
        return ('python-docx', 'defusedxml')
    if suffix == '.epub' or suffix in FORMAT_BY_SUFFIX and FORMAT_BY_SUFFIX[suffix].family == 'xml':
        return ('defusedxml',)
    return ()


def capabilities(*, pandoc_selected=False, image_ocr_selected=False, office_selected=False,
                 calibre_selected=False, djvu_selected=False, chm_selected=False):
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
    result.append(dict(extension='.rtf', available=bool(pandoc_selected),
        adapter='explicitly selected local Pandoc --sandbox', installed={}, missing=[],
        qualification='selected_runtime_requires_check' if pandoc_selected else 'explicit_selection_required',
        limitation='Select an installed compatible Pandoc executable explicitly. Only fixed RTF-to-plain-text arguments are used; no custom filters, templates, external resources or exact layout.'))
    try:
        pillow = {'Pillow': version('Pillow')}
    except PackageNotFoundError:
        pillow = {}
    for suffix in IMAGES:
        result.append(dict(extension=suffix, available=bool(image_ocr_selected and pillow),
            adapter='existing local Tesseract and explicit OCR model selection',
            installed=pillow, missing=[] if pillow else ['Pillow'],
            qualification='selected_runtime_requires_check' if image_ocr_selected else 'explicit_selection_required',
            limitation='Explicit OCR language/profile or verified model-pack selection is required. Every bounded image/TIFF frame becomes unverified reading text; printed pagination and exact layout are not invented.'))
    legacy_selected = {'calibre': calibre_selected, 'djvu': djvu_selected, 'chm': chm_selected}
    legacy_limits = {
        'calibre': 'Qualification covers synthetic unprotected MOBI (including AZW/PRC extension aliases), AZW3/KF8, freely generated LIT and TCR with explicitly selected calibre 9.19.0. Its exact-version wrapper rejects other revisions, user plugins and protected-book handling. Plain reading text requires source review; arbitrary real books and other Palm database subtypes are not qualified.',
        'djvu': 'Embedded UTF-8 text from standalone or bundled self-contained pages only. Indirect/shared-dictionary references and pages requiring OCR remain explicit errors.',
        'chm': 'Bounded 7-Zip CHM extraction to stdout and existing inert HTML reader. One strict UTF-8 TOC or declared stable filename order; no help viewer, scripts, ActiveX or network loading.',
    }
    for suffix in LEGACY:
        if suffix in LEGACY_SUFFIXES:
            kind = kind_for(suffix)
            qualified = suffix in QUALIFIED_SUFFIXES
            result.append(dict(extension=suffix, available=bool(qualified and legacy_selected[kind]),
                adapter='explicitly selected ' + kind + ' reading adapter', installed={}, missing=[],
                qualification=('selected_runtime_requires_check' if legacy_selected[kind] else 'explicit_selection_required')
                              if qualified else 'native_fixture_not_qualified',
                limitation=legacy_limits[kind]))
            continue
        if suffix in OFFICE_SUFFIXES:
            result.append(dict(extension=suffix, available=bool(office_selected),
                adapter='explicitly selected LibreOffice with private inactive-content profile',
                installed={}, missing=[],
                qualification='selected_runtime_requires_check' if office_selected else 'explicit_selection_required',
                limitation=('Selected binary Word/PowerPoint/Excel, WordPerfect header family or PalmDoc TEXt/REAd PDB only. '
                    'Fixed verified installed filters create ODF for the existing guarded reader; no macros, external-link updates, '
                    'exact layout or printed pagination are admitted. Unsupported content remains an explicit error.')))
            continue
        result.append(dict(extension=suffix, available=False, adapter='not enabled by this reader registry',
                           installed={}, missing=[], qualification='not_integrated',
                           limitation='Use a separately reviewed conversion tool and import the resulting supported copy. No converter, model, decryption or DRM bypass is supplied.'))
    return tuple(result)
