"""Citation-document compatibility over the canonical local CSL service."""
from .formatting import (
    BibliographyEntry, CitationCancelled, CitationCluster, CitationError,
    CitationRequest, FormattedCitations, RenderedCitation, StyleInfo,
    available_styles, render_citations,
)
from .documents import CitationParagraph, citation_docx_bytes, export_citation_docx, read_citation_docx
from .regeneration import regenerate_citation_docx


__all__ = [
    'BibliographyEntry', 'CitationCancelled', 'CitationCluster', 'CitationError',
    'CitationRequest', 'FormattedCitations', 'RenderedCitation', 'StyleInfo',
    'available_styles', 'render_citations', 'CitationParagraph',
    'citation_docx_bytes', 'export_citation_docx', 'read_citation_docx', 'regenerate_citation_docx',
]
