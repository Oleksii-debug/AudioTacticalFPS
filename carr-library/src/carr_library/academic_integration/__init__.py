"""Source-bound citation documents using the canonical offline CSL core."""
from .formatting import (
    BibliographyEntry, CitationCancelled, CitationCluster, CitationError,
    CitationRequest, FormattedCitations, RenderedCitation, StyleInfo,
    available_styles, render_citations,
)
from .documents import CitationParagraph, export_citation_docx, read_citation_docx
from .regeneration import regenerate_citation_docx
__all__ = [
    'BibliographyEntry', 'CitationCancelled', 'CitationCluster', 'CitationError',
    'CitationRequest', 'FormattedCitations', 'RenderedCitation', 'StyleInfo',
    'available_styles', 'render_citations', 'CitationParagraph',
    'export_citation_docx', 'read_citation_docx', 'regenerate_citation_docx',
]
