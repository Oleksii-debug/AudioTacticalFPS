"""Scoped CSL previews and exports for the existing academic library.

Formatting changes presentation, never the source, saved evidence or approval.
Names remain literal unless the user explicitly selects the documented comma
convention. Publication page extents are never reused as quotation locators.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
from io import BytesIO
import json

from ..research.export_file import write_binary_export, write_export


NAME_MODES = (('literal', 'Keep author names exactly as entered'),
              ('family-given', 'Personal names use Family, Given, optional Suffix'))
LOCALES = (('en-US', 'English (United States)'), ('en-GB', 'English (United Kingdom)'),
           ('uk-UA', 'Ukrainian'), ('sk-SK', 'Slovak'))
EXPORT_FORMATS = (('text', 'Plain text', 'txt'), ('html', 'Formatted HTML', 'html'),
                  ('docx', 'Word document', 'docx'))
METADATA_NOTICE = ('Bibliographic formatting uses the entered metadata. It does not verify '
                   'source text or printed page numbers. Publication page ranges are not '
                   'quotation locators. Personal research notes are excluded.')


@dataclass(frozen=True)
class StylePreview:
    workspace: str
    identity: str | None
    snapshot_sha256: str
    style: str
    locale: str
    name_mode: str
    titles: tuple[str, ...]
    result: object = field(repr=False)
    warnings: tuple[str, ...] = ()

    @property
    def bibliography(self):
        return '\n\n'.join(self.result.entries).strip() + '\n'

    @property
    def citations(self):
        return '\n'.join(self.result.citations).strip() + '\n'

    @property
    def explanation(self):
        parts = [self.result.style.title, METADATA_NOTICE]
        if self.warnings:
            parts.append('Review notes:\n' + '\n'.join(self.warnings))
        parts.append('Bibliography:\n' + self.bibliography)
        if self.result.citations:
            label = 'Note examples' if self.result.style.kind == 'note' else 'Citation examples'
            examples = [f'{index}. {title}\n{citation}' for index, (title, citation)
                        in enumerate(zip(self.titles, self.result.citations), 1)]
            parts.append(label + ':\n' + '\n\n'.join(examples))
        parts.append(self.result.attribution)
        return '\n\n'.join(parts)


def records_for_style(snapshot, *, name_mode='literal'):
    """Prepare CSL data from the existing exporter, without guessing names."""
    if name_mode not in dict(NAME_MODES):
        raise ValueError('Choose a supported author-name convention.')
    if not isinstance(snapshot, str) or len(snapshot.encode('utf-8')) > 8 * 1024 * 1024:
        raise ValueError('Select fewer references for one formatting preview (8 MiB budget).')
    try:
        records = json.loads(snapshot)
    except (ValueError, RecursionError) as exc:
        raise ValueError('The bibliography snapshot is not valid CSL JSON.') from exc
    if not isinstance(records, list) or not records:
        raise ValueError('There are no active references in this selection.')
    notes = []
    if name_mode == 'literal':
        for record in records:
            for author in record.get('author', []):
                value = author.get('literal', '')
                if value.startswith('{') and value.endswith('}'):
                    author['literal'] = value[1:-1]
        notes.append('Author names are kept literally. Personal-name initials and inversion '
                     'require an explicit Family, Given convention or structured CSL input.')
        return records, tuple(notes)
    converted, literal = 0, 0
    for record in records:
        for author in record.get('author', []):
            if 'literal' not in author:
                continue
            value = author['literal'].strip()
            # Braces explicitly mark an organisation or other literal author.
            if value.startswith('{') and value.endswith('}'):
                author['literal'] = value[1:-1]
                literal += 1
                continue
            parts = [part.strip() for part in value.split(',')]
            if len(parts) == 1:
                literal += 1
                continue
            if len(parts) not in (2, 3) or not all(parts):
                raise ValueError('A personal author name must use Family, Given, optional Suffix. '
                                 'Use literal-name mode for organisations or other spellings.')
            author.clear()
            author.update(family=parts[0], given=parts[1])
            if len(parts) == 3:
                author['suffix'] = parts[2]
            converted += 1
    notes.append(f'Applied the explicitly selected Family, Given convention to {converted} author names; '
                 f'{literal} names remain literal. No stored name was changed.')
    return records, tuple(notes)


class BibliographyWorkbench:
    """Reauthorize one library and the exact metadata before copy or export."""

    def __init__(self, controller):
        self.controller = controller

    def _snapshot(self, identity):
        return self.controller.export(identity=identity, format='csl-json', include_notes=False)

    def preview(self, *, identity=None, style='apa', locale='en-US', name_mode='literal'):
        from .csl import render
        snapshot = self._snapshot(identity)
        records, warnings = records_for_style(snapshot, name_mode=name_mode)
        result = render(records, style=style, locale=locale, output='text')
        preview = StylePreview(self.controller.workspace, identity,
            sha256(snapshot.encode('utf-8')).hexdigest(), style, locale, name_mode,
            tuple(record.get('title', 'Untitled') for record in records), result,
            tuple(dict.fromkeys((*warnings, *result.warnings))))
        self.check(preview)
        return preview

    def check(self, preview):
        if not isinstance(preview, StylePreview) or preview.workspace != self.controller.workspace:
            raise ValueError('This bibliography preview belongs to another library.')
        snapshot = self._snapshot(preview.identity)
        if sha256(snapshot.encode('utf-8')).hexdigest() != preview.snapshot_sha256:
            raise ValueError('The references changed. Refresh the formatting preview before copying or exporting.')
        return snapshot

    def copy_text(self, preview, *, part='bibliography'):
        self.check(preview)
        if part == 'bibliography':
            return preview.bibliography
        if part == 'citations':
            return preview.citations
        raise ValueError('Choose bibliography or citation examples to copy.')

    def export(self, preview, destination, *, format='text', cancelled=lambda: False,
               require_absent=False, approval=None):
        snapshot = self.check(preview)
        if cancelled():
            raise TimeoutError('Bibliography export cancelled; no output was changed.')
        if format == 'text':
            content = preview.bibliography
            writer = write_export
        elif format in ('html', 'docx'):
            from .csl import render
            records, _ = records_for_style(snapshot, name_mode=preview.name_mode)
            rendered = render(records, style=preview.style, locale=preview.locale, output='html')
            if format == 'html':
                content = html_document(rendered)
                writer = write_export
            else:
                content = docx_document(rendered)
                writer = write_binary_export
        else:
            raise ValueError('Choose plain text, HTML or Word document output.')
        def cancelled_or_changed():
            # Recheck after the caller's callback, including the writer's final
            # staged-file checkpoint. A changed record or revoked workspace
            # must not publish an old preview after a long render or write.
            if cancelled():
                return True
            self.check(preview)
            return False

        writer(destination, content, private_root=self.controller.library.root,
               cancelled=cancelled_or_changed, require_absent=require_absent, approval=approval)
        return content


class _InlineMarkup(HTMLParser):
    """Keep only citation typography; never import links or active content."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'br':
            self.parts.append(('\n', frozenset(self.stack)))
            return
        if tag == 'div' and attributes.get('class') == 'csl-right-inline' and self.parts:
            self.parts.append(('\t', frozenset()))
        if tag in ('i', 'em', 'b', 'strong', 'sup', 'sub'):
            self.stack.append(tag)
        elif tag == 'span':
            # Only the processor's typography is meaningful in Word.
            style = attributes.get('style', '').replace(' ', '').lower()
            if 'font-variant:small-caps' in style:
                self.stack.append('small-caps')
            else:
                self.stack.append('span')

    def handle_endtag(self, tag):
        expected = 'small-caps' if tag == 'span' and self.stack and self.stack[-1] == 'small-caps' else tag
        if self.stack and self.stack[-1] == expected:
            self.stack.pop()

    def handle_data(self, data):
        self.parts.append((data, frozenset(self.stack)))


def _parts(entry):
    parser = _InlineMarkup()
    parser.feed(entry)
    parser.close()
    return tuple(parser.parts)


def _safe_entry(entry):
    pieces = []
    for text, styles in _parts(entry):
        text = escape(text).replace('\n', '<br>').replace('\t', ' ')
        for tag in ('i', 'em', 'b', 'strong', 'sup', 'sub'):
            if tag in styles:
                text = f'<{tag}>' + text + f'</{tag}>'
        if 'small-caps' in styles:
            text = '<span class="small-caps">' + text + '</span>'
        pieces.append(text)
    return ''.join(pieces)


def html_document(rendered):
    """Self-contained formatted bibliography, with no external resources."""
    title = escape(rendered.style.title + ' bibliography')
    entries = '\n'.join('<p class="reference">' + _safe_entry(entry) + '</p>'
                        for entry in rendered.entries)
    return ('<!doctype html>\n<html lang="' + escape(rendered.locale, quote=True) + '">\n'
            '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            '<title>' + title + '</title><style>body{max-width:52rem;margin:2rem auto;padding:0 1rem;'
            'font:12pt/1.6 Georgia,serif}.reference{padding-left:2em;text-indent:-2em}'
            '.small-caps{font-variant:small-caps}</style></head><body>\n'
            '<h1>' + title + '</h1>\n' + entries + '\n</body></html>\n')


def docx_document(rendered):
    """Create an editable Word bibliography with safe CSL typography."""
    from docx import Document
    from docx.shared import Inches, Pt
    document = Document()
    document.core_properties.title = rendered.style.title + ' bibliography'
    document.core_properties.subject = 'Bibliography from local entered metadata'
    document.core_properties.author = ''
    normal = document.styles['Normal']
    normal.font.name, normal.font.size = 'Times New Roman', Pt(12)
    normal.paragraph_format.line_spacing = 2
    document.add_heading('Bibliography', level=1)
    for entry in rendered.entries:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Inches(0.5)
        paragraph.paragraph_format.first_line_indent = Inches(-0.5)
        for text, styles in _parts(entry):
            run = paragraph.add_run(text)
            run.italic = bool(styles & {'i', 'em'})
            run.bold = bool(styles & {'b', 'strong'})
            run.font.superscript = 'sup' in styles
            run.font.subscript = 'sub' in styles
            run.font.small_caps = 'small-caps' in styles
    output = BytesIO()
    document.save(output)
    return output.getvalue()
