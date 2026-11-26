"""Word and self-contained HTML dossiers from authenticated evidence snapshots.

The caller authenticates historical source/metadata in one read transaction,
as anchored_export.export_saved does. This renderer repeats the existing
Evidence integrity checks; it cannot turn a caller's verification flag into
source authorization. It performs no database or network I/O and never opens
a caller-provided source or destination path.
"""
from collections import OrderedDict
from html import escape
from io import BytesIO
import unicodedata
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .evidence_export import Evidence, _encoded, _locator, _records


_TEXT_FIELDS = ('workspace_id', 'topic_id', 'evidence_id', 'edition_id',
                'revision_id', 'asset_sha256', 'raw_text_sha256', 'raw_text',
                'quote', 'title', 'author', 'edition', 'volume', 'note')
_JSON_EXPLANATION = ('JSON string encoding preserves every source character. '
                     'Escapes distinguish carriage returns, line feeds, tabs, '
                     'literal backslashes and nonprinting characters.')


def _needs_json(value, *, multiline=True):
    return any((unicodedata.category(c).startswith('C')
                and not (multiline and c in '\n\t'))
               or c in '\u2028\u2029' for c in value)


def _metadata(value):
    """Keep user text out of layout, XML, hyperlinks and document instructions."""
    return _encoded(value) + ' (JSON string)' if _needs_json(value, multiline=False) else value


def _prepare(workspace, topic, rows):
    def typed():
        for row in rows:
            if (not isinstance(row, Evidence)
                    or any(not isinstance(getattr(row, field), str) for field in _TEXT_FIELDS)):
                raise ValueError('invalid evidence document record')
            yield row

    # Exhaust the validator, including its final complete-span check, before
    # either renderer is invoked. A late corrupt row cannot produce a dossier.
    items = tuple(_records(workspace, topic, typed()))
    groups = OrderedDict()
    for row in items:
        groups.setdefault(row.evidence_id, []).append(row)
    bibliography, keys, references = [], {}, []
    for slices in groups.values():
        first = slices[0]
        # A different immutable description is a different bibliography entry,
        # even when edition/revision IDs happen to match. Never merge by title.
        key = (first.edition_id, first.revision_id, first.asset_sha256,
               first.title, first.author, first.edition, first.volume)
        if key not in keys:
            keys[key] = len(bibliography) + 1
            bibliography.append(first)
        references.append(keys[key])
    return tuple(tuple(slices) for slices in groups.values()), tuple(bibliography), tuple(references)


def _source_text(value, label, kind):
    if _needs_json(value):
        yield 'label', label + ' as a JSON string'
        yield 'explanation', _JSON_EXPLANATION
        yield kind + '_json', _encoded(value)
    else:
        yield 'label', label
        yield kind, value


def _bibliography_text(row):
    author = _metadata(row.author) if row.author.strip() else 'Author unknown'
    title = _metadata(row.title) if row.title.strip() else 'Title unknown'
    edition = _metadata(row.edition) if row.edition.strip() else 'unknown'
    volume = _metadata(row.volume) if row.volume.strip() else 'unknown'
    return f'{author}. {title}. Edition {edition}; volume {volume}.'


def _blocks(workspace, topic, title, groups, bibliography, references):
    yield 'title', _metadata(title)
    yield 'paragraph', ('Source quotations and research notes from the selected topic. '
                        'Quotations retain their saved source locations; notes remain separate.')
    count = sum(len(slices) for slices in groups)
    yield 'paragraph', (f'Evidence records: {len(groups)}. Source slices: {count}. '
                        f'Bibliography entries: {len(bibliography)}.')
    yield 'heading1', 'Evidence'
    if not groups:
        yield 'paragraph', 'No saved evidence in this topic.'
    for number, (slices, reference) in enumerate(zip(groups, references), 1):
        yield 'heading2', f'Evidence {number}'
        yield 'paragraph', f'Bibliography reference {reference}.'
        for row in slices:
            if len(slices) > 1:
                yield 'heading3', f'Source slice {row.span_index} of {row.span_count}'
            yield 'paragraph', _locator(row)
            yield from _source_text(row.quote, 'Exact quotation', 'quotation')
        yield 'heading3', 'Research note'
        if slices[0].note:
            yield from _source_text(slices[0].note, 'Note text', 'note')
        else:
            yield 'paragraph', 'No research note recorded.'

    yield 'heading1', 'Bibliography'
    if not bibliography:
        yield 'paragraph', 'No source references to list.'
    else:
        yield 'paragraph', ('These entries retain the saved citation metadata. Publication year '
                            'and publisher are not recorded in these evidence snapshots. '
                            'Different saved metadata snapshots are listed separately.')
    for number, row in enumerate(bibliography, 1):
        yield 'heading2', f'Reference {number}'
        yield 'paragraph', _bibliography_text(row)

    yield 'heading1', 'Source provenance'
    yield 'paragraph', 'Workspace identifier: ' + _metadata(workspace)
    yield 'paragraph', 'Topic identifier: ' + _metadata(topic)
    if not groups:
        yield 'paragraph', 'No source slices to describe.'
    for number, (slices, reference) in enumerate(zip(groups, references), 1):
        first = slices[0]
        yield 'heading2', f'Evidence {number}'
        yield 'provenance', '\n'.join((
            'Evidence identifier: ' + _metadata(first.evidence_id),
            f'Bibliography reference: {reference}',
            'Edition identifier: ' + _metadata(first.edition_id),
            'Revision identifier: ' + _metadata(first.revision_id),
            'Source file SHA-256: ' + first.asset_sha256))
        for row in slices:
            yield 'provenance', '\n'.join((
                f'Source slice {row.span_index} of {row.span_count}: ' + _locator(row),
                'Source text SHA-256: ' + row.raw_text_sha256,
                f'Offsets: [{row.start}, {row.end}) Unicode code points; end excluded.'))


def _docx(blocks):
    # The pinned python-docx dependency is loaded only after every record passed
    # validation. No template or filename is ever taken from source metadata.
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(0.8)
    section.left_margin = section.right_margin = Inches(0.85)
    normal = document.styles['Normal']
    normal.font.name, normal.font.size = 'Arial', Pt(11)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.12
    language = OxmlElement('w:lang')
    language.set(qn('w:val'), 'en-US')
    normal.element.get_or_add_rPr().append(language)
    for name, size in (('Title', 22), ('Heading 1', 16), ('Heading 2', 13), ('Heading 3', 11)):
        style = document.styles[name]
        style.font.name, style.font.size = 'Arial', Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.bold = name != 'Title'
        style.paragraph_format.space_before = Pt(14 if name == 'Heading 1' else 8)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.keep_with_next = True
        # Remove template decorative borders rather than applying a title rule.
        ppr = style.element.find(qn('w:pPr'))
        if ppr is not None:
            border = ppr.find(qn('w:pBdr'))
            if border is not None:
                ppr.remove(border)
    for name, monospace in (('Dossier Quotation', False), ('Dossier Quotation JSON', True),
                            ('Dossier Note', False), ('Dossier Note JSON', True),
                            ('Dossier Provenance', True)):
        style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = normal
        style.font.name = 'Consolas' if monospace else 'Arial'
        style.font.size = Pt(9 if monospace else 11)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.keep_together = False
        if 'Quotation' in name:
            style.paragraph_format.left_indent = Inches(0.2)
            style.paragraph_format.right_indent = Inches(0.2)
        # The English framing does not assert a language for multilingual data.
        lang = OxmlElement('w:lang')
        lang.set(qn('w:val'), 'und')
        style.element.get_or_add_rPr().append(lang)
    styles = {'title': 'Title', 'heading1': 'Heading 1', 'heading2': 'Heading 2',
              'heading3': 'Heading 3', 'quotation': 'Dossier Quotation',
              'quotation_json': 'Dossier Quotation JSON', 'note': 'Dossier Note',
              'note_json': 'Dossier Note JSON', 'provenance': 'Dossier Provenance'}
    for kind, text in blocks:
        paragraph = document.add_paragraph(text, style=styles.get(kind, 'Normal'))
        if kind == 'label':
            paragraph.runs[0].bold = True
            paragraph.paragraph_format.keep_with_next = True
        elif kind == 'explanation':
            paragraph.paragraph_format.keep_with_next = True
            for run in paragraph.runs:
                run.font.size = Pt(10)
        elif kind == 'heading1' and text == 'Source provenance':
            paragraph.paragraph_format.page_break_before = True
    properties = document.core_properties
    properties.title = blocks[0][1] if len(blocks[0][1]) <= 255 else blocks[0][1][:252] + '...'
    properties.subject = 'Research quotations and notes'
    properties.language = 'en-US'
    properties.comments = ''
    properties.keywords = ''
    # python-docx's template includes its own creator and old dates. They are
    # neither source metadata nor facts about this export and must not leak.
    for name in ('dc:creator', 'cp:lastModifiedBy', 'dcterms:created', 'dcterms:modified',
                 'cp:lastPrinted', 'cp:revision'):
        child = properties._element.find(qn(name))
        if child is not None:
            properties._element.remove(child)
    raw = BytesIO()
    document.save(raw)
    # Stable package timestamps avoid recording the user's export clock in ZIP
    # members. The document itself contains no generated dates or machine paths.
    output = BytesIO()
    with ZipFile(raw) as source, ZipFile(output, 'w', compression=ZIP_DEFLATED) as target:
        for member in source.infolist():
            info = ZipInfo(member.filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            target.writestr(info, source.read(member))
    return output.getvalue()


def _html(blocks):
    output = ['<!doctype html>', '<html lang="en"><head><meta charset="utf-8">',
              '<meta name="viewport" content="width=device-width, initial-scale=1">',
              '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
              'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
              '<title>' + escape(blocks[0][1]) + '</title>',
              '<style>body{font:1rem/1.55 Arial,sans-serif;color:#111;background:#fff;'
              'max-width:54rem;margin:2rem auto;padding:0 1.25rem}'
              'h1,h2,h3,h4{line-height:1.25;color:#000}h2{margin-top:2rem}'
              'h3{margin-top:1.5rem}h4{margin-bottom:.5rem}'
              'p{white-space:pre-wrap;overflow-wrap:anywhere}'
              'pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;tab-size:4}'
              'blockquote{margin:1rem 1.25rem}'
              '.quotation_json,.note_json,.provenance{font:.9rem/1.5 monospace}'
              '.explanation{font-size:.92rem}.label{font-weight:bold;margin-bottom:.4rem}'
              '@media print{body{max-width:none;margin:0}h1,h2,h3,h4,.label,.explanation'
              '{break-after:avoid}section:last-child{break-before:page}}</style></head><body><main>']
    section = 0
    for kind, text in blocks:
        safe = escape(text)
        if kind == 'title':
            output.append('<h1>' + safe + '</h1>')
        elif kind == 'heading1':
            if section:
                output.append('</section>')
            section += 1
            output.append(f'<section aria-labelledby="section-{section}">'
                          f'<h2 id="section-{section}">' + safe + '</h2>')
        elif kind in ('heading2', 'heading3'):
            level = 'h3' if kind == 'heading2' else 'h4'
            output.append(f'<{level}>' + safe + f'</{level}>')
        elif kind.startswith('quotation'):
            # The inner span also preserves an initial LF, which HTML parsers
            # otherwise strip immediately after a <pre> start tag.
            encoding = 'json-string' if kind.endswith('_json') else 'unicode'
            output.append(f'<blockquote class="{kind}" data-encoding="{encoding}">'
                          '<pre><span lang="">' + safe + '</span></pre></blockquote>')
        elif kind.startswith('note'):
            encoding = 'json-string' if kind.endswith('_json') else 'unicode'
            output.append(f'<pre class="{kind}" data-encoding="{encoding}"><span lang="">'
                          + safe + '</span></pre>')
        else:
            output.append(f'<p class="{kind}">' + safe + '</p>')
    if section:
        output.append('</section>')
    output.append('</main></body></html>')
    return '\n'.join(output) + '\n'


def export_document(workspace_id, topic_id, rows, *, format='docx', title='Research dossier'):
    """Return DOCX bytes or standalone HTML text; never write or launch a file.

    Clean Unicode, LF and tab source characters are retained natively. CR and
    other nonprinting characters use explicitly labelled reversible JSON, not
    a silently normalized quotation. Metadata is plain text, never active
    fields, relationships, source-derived hyperlinks or external resources.
    The visible title is complete; the optional DOCX core title is limited to
    255 characters by python-docx, with an explicit ellipsis when necessary.
    """
    if format not in ('docx', 'html'):
        raise ValueError('unsupported academic document format')
    if not isinstance(title, str) or not title.strip() or len(title) > 512:
        raise ValueError('document title must contain 1 to 512 characters')
    groups, bibliography, references = _prepare(workspace_id, topic_id, rows)
    blocks = tuple(_blocks(workspace_id, topic_id, title, groups, bibliography, references))
    return _docx(blocks) if format == 'docx' else _html(blocks)
