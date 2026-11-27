"""Local standards-based citation formatting and explicit DOI metadata previews."""
from .formatting import (
    BibliographyEntry, CitationCancelled, CitationCluster, CitationError,
    CitationRequest, FormattedCitations, RenderedCitation, StyleInfo,
    available_styles, citation_attribution, render_citations,
)
from .documents import CitationParagraph, export_citation_docx, read_citation_docx
from .regeneration import regenerate_citation_docx
from .doi import (
    AccessLocation, DoiMetadataClient, DoiMetadataError, DoiMetadataPreview,
    LicenseNotice, MetadataProvenance, normalize_doi,
)

__all__ = [
    'BibliographyEntry', 'CitationCancelled', 'CitationCluster', 'CitationError',
    'CitationRequest', 'FormattedCitations', 'RenderedCitation', 'StyleInfo',
    'available_styles', 'citation_attribution', 'render_citations', 'CitationParagraph',
    'export_citation_docx', 'read_citation_docx', 'regenerate_citation_docx',
    'AccessLocation', 'DoiMetadataClient', 'DoiMetadataError', 'DoiMetadataPreview',
    'LicenseNotice', 'MetadataProvenance', 'normalize_doi',
]
