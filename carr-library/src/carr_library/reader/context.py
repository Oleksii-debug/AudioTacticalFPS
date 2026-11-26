"""More/clipboard candidate; all source resolution belongs to citations.anchors."""
from dataclasses import dataclass
from ..citations.anchors import Anchor, format_citation, resolve_span


@dataclass(frozen=True)
class ContextPage:
    anchor: Anchor
    raw_text: str
    citation: str

    @property
    def quote(self):
        return self.anchor.quote


@dataclass(frozen=True)
class Context:
    pages: tuple[ContextPage, ...]

    def page(self, position):
        """Zero-based navigation within the verified span; no wraparound."""
        if type(position) is not int or not 0 <= position < len(self.pages):
            raise IndexError('context page outside span')
        return self.pages[position]

    def clipboard(self, workspace, position, mode, load_page):
        """One page per exact quote; never concatenate fragments into a false quote."""
        current = open_context(workspace, (p.anchor for p in self.pages), load_page)
        page = current.page(position)
        if mode == 'quote':
            return page.quote
        if mode == 'citation':
            return page.citation
        if mode == 'both':
            return page.quote + '\n\nCitation: ' + page.citation
        raise ValueError('unknown clipboard mode')


def open_context(workspace, anchors, load_page):
    """Resolve all slices before returning anything; caller supplies authorized loader.

    No current-revision fallback, source file open, network, or clipboard writes.
    The host must discard the context on workspace switch; copy re-resolves.
    """
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError('workspace required')
    anchors = tuple(anchors)
    if not anchors or any(a.identity.workspace != workspace for a in anchors):
        raise ValueError('empty or cross-workspace context')
    pairs = resolve_span(anchors, load_page)
    return Context(tuple(ContextPage(a, raw, format_citation(a)) for a, raw in pairs))
