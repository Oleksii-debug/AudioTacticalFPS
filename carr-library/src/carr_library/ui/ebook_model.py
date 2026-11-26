"""Small, headless presentation state for an authorized EPUB reader.

The host owns source authorization and extraction. This module validates the
bound descriptors and keeps navigation/search offsets in Python code points;
none of those offsets are printed-page references or quotation approval.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import re


NOTICE = 'Extracted EPUB text — not verified for quotation'

_WARNING_TEXT = {
    'logical_locator_not_page': 'Chapter locations are logical reading positions, not printed pages.',
    'layout_not_verified': 'The original page layout has not been verified.',
    'extracted_text_unverified': 'The extracted text has not been independently verified.',
    'source_quotations_unverified': 'Quotations from this extraction have not been independently verified.',
    'xhtml_text_preview': 'This is text extracted from the EPUB XHTML; formatting and other media are not reproduced.',
    'remote_resources_not_loaded': 'Remote resources were not loaded; their content may be absent.',
    'supplementary_spine_items_included': 'Supplementary sections marked by the EPUB are included in chapter order.',
    'scripts_not_executed': 'Scripts were not executed; their generated content is unavailable.',
    'script_content_omitted': 'Script content was omitted and scripts were not executed.',
    'style_content_omitted': 'Stylesheet content was omitted from the extracted text.',
    'template_content_omitted': 'Template content was omitted from the extracted text.',
    'hidden_content_omitted': 'Explicitly hidden source content was omitted.',
    'styles_not_rendered': 'Styles were not rendered; visual layout and reading order may differ.',
    'table_layout_not_rendered': 'Table text is retained without its original layout; row and column relationships may be unclear.',
    'revision_markup_not_rendered': 'Inserted and deleted text is retained without visual revision markings.',
    'noscript_fallback_included': 'Fallback text intended for readers without scripting is included.',
    'images_not_transcribed': 'Images were not transcribed. Supplied alternative descriptions are listed separately.',
    'images_omitted_from_text': 'Images were omitted from the text. Author-supplied alternative descriptions are listed separately and remain unverified.',
    'images_without_text_alternatives': 'Some images have no text alternative, so their content is unavailable in this text reader.',
    'media_not_transcribed': 'Audio and video were not transcribed.',
    'chapter_has_no_readable_text': 'This chapter contains no extractable text.',
    'encrypted_or_obfuscated_nontext_resources_not_rendered': 'Encrypted or obfuscated nontext resources were not rendered.',
    'obfuscated_fonts_not_rendered': 'Embedded obfuscated fonts were not rendered. Text appearance may differ.',
    'page_list_labels_unverified': 'EPUB page-list labels are unverified and are not used as printed-page references.',
    'embedded_contents_unavailable': 'Embedded contents are unavailable for this stored extraction. Use the chapter selector.',
    'embedded_contents_ambiguous_spine': 'Some contents links point to a resource repeated in the reading order. Their destination is ambiguous, so the embedded contents and page hints are unavailable. Use the chapter selector.',
    'embedded_navigation_budget_exceeded': 'Contents and heading details exceed the navigation limit for this repeated reading order. All chapters remain available through the chapter selector.',
}


def _warning_text(values):
    return '\n'.join(_WARNING_TEXT.get(value, value) for value in values)


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or '\x00' in value or (not empty and not value.strip()):
        raise ValueError(f'Invalid {label}. Open the electronic book again.')
    return value


def _warnings(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError('Invalid electronic-book extraction warnings.')
    return tuple(_text(item, 'extraction warning') for item in value)


@dataclass(frozen=True)
class Chapter:
    ordinal: int
    title: str
    member: str
    linear: bool
    characters: int
    warnings: tuple[str, ...]
    image_descriptions: tuple[str, ...] = ()

    @property
    def label(self):
        supplement = '; supplementary reading' if not self.linear else ''
        return f'Chapter {self.ordinal} (unpaginated): {self.title}{supplement}'


@dataclass(frozen=True)
class ContentsEntry:
    title: str
    ordinal: int | None
    offset: int | None
    depth: int
    heading_only: bool = False

    @property
    def label(self):
        target = ('section heading; no destination' if self.heading_only else
                  f'Chapter {self.ordinal} (unpaginated)')
        return f'Level {self.depth + 1}. {self.title}; {target}'


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    wrapped: bool


class EbookSession:
    """Bind every chapter visit to one contents revision and caller scope."""

    def __init__(self, load_contents, load_chapter, still_current=lambda: True):
        self.load_contents = load_contents
        self.load_chapter = load_chapter
        self.still_current = still_current
        self.title = self.revision = ''
        self.chapters = ()
        self.toc = ()
        self.warnings = ()
        self.metadata_hints = {}
        self.ordinal = 0
        self.raw_text = None
        self.chapter_warnings = ()
        self.query = None
        self.match = None

    def check_current(self):
        if not self.still_current():
            raise ValueError('The selected book, library or topic changed. Close this reader and open it again.')

    @staticmethod
    def _chapter(value, ordinal):
        if (not isinstance(value, Mapping) or type(value.get('ordinal')) is not int
                or value['ordinal'] != ordinal or type(value.get('linear')) is not bool
                or type(value.get('characters')) is not int or value['characters'] < 0):
            raise ValueError('Invalid electronic-book chapter descriptor.')
        descriptions = value.get('image_descriptions', [])
        if not isinstance(descriptions, (list, tuple)):
            raise ValueError('Invalid electronic-book image alternative descriptions.')
        descriptions = tuple(_text(item, 'image alternative description') for item in descriptions)
        return Chapter(ordinal, _text(value.get('title'), 'chapter title'),
                       _text(value.get('member'), 'chapter identity'), value['linear'],
                       value['characters'], _warnings(value.get('warnings', [])), descriptions)

    def open(self):
        self.check_current()
        data = self.load_contents()
        self.check_current()
        if not isinstance(data, Mapping):
            raise ValueError('Electronic-book contents are unavailable.')
        title = _text(data.get('title'), 'electronic-book title')
        revision = _text(data.get('revision'), 'electronic-book revision')
        records = data.get('chapters')
        if not isinstance(records, (list, tuple)) or not records:
            raise ValueError('This electronic book has no readable chapters.')
        chapters = tuple(self._chapter(row, number) for number, row in enumerate(records, 1))
        entries = data.get('toc', [])
        if not isinstance(entries, (list, tuple)):
            raise ValueError('Invalid electronic-book contents list.')
        toc = []
        for row in entries:
            if (not isinstance(row, Mapping) or type(row.get('depth', 0)) is not int
                    or not 0 <= row.get('depth', 0) <= 64):
                raise ValueError('Invalid electronic-book contents depth.')
            label = _text(row.get('title'), 'contents title')
            ordinal, offset = row.get('ordinal'), row.get('offset')
            heading_only = row.get('heading_only', False)
            if type(heading_only) is not bool:
                raise ValueError('Invalid electronic-book contents heading.')
            if heading_only:
                if ordinal is not None or offset is not None:
                    raise ValueError('A contents heading cannot have a chapter destination.')
            elif (type(ordinal) is not int or not 1 <= ordinal <= len(chapters)
                    or type(offset) is not int or not 0 <= offset <= chapters[ordinal - 1].characters):
                raise ValueError('Electronic-book contents point outside their chapter.')
            toc.append(ContentsEntry(label, ordinal, offset, row.get('depth', 0), heading_only))
        warnings = _warnings(data.get('warnings', []))
        hints = data.get('metadata_hints', {})
        if not isinstance(hints, Mapping):
            raise ValueError('Invalid electronic-book metadata hints.')
        metadata = {}
        for key in ('titles', 'creators', 'languages', 'identifiers', 'publishers'):
            values = hints.get(key, [])
            if not isinstance(values, (list, tuple)):
                raise ValueError('Invalid electronic-book metadata hints.')
            metadata[key] = tuple(_text(value, 'metadata hint') for value in values)
        self.check_current()
        self.title, self.revision = title, revision
        self.chapters, self.toc = chapters, tuple(toc)
        self.warnings, self.metadata_hints = warnings, metadata
        self.ordinal, self.raw_text = 0, None
        self.query = self.match = None
        return self

    def visit(self, ordinal, *, offset=0):
        self.check_current()
        if type(ordinal) is not int or not 1 <= ordinal <= len(self.chapters):
            raise ValueError('Choose a chapter from the chapter list.')
        expected = self.chapters[ordinal - 1]
        if type(offset) is not int or not 0 <= offset <= expected.characters:
            raise ValueError('The contents destination is outside this chapter.')
        data = self.load_chapter(ordinal)
        self.check_current()
        chapter = self._chapter(data, ordinal)
        if (chapter != expected or data.get('revision', self.revision) != self.revision):
            raise ValueError('The electronic-book chapter or revision changed. Open its contents again.')
        raw = _text(data.get('raw_text'), 'extracted chapter text', empty=True)
        if len(raw) != chapter.characters:
            raise ValueError('The electronic-book chapter length changed.')
        self.check_current()
        self.ordinal, self.raw_text = ordinal, raw
        self.chapter_warnings = chapter.warnings
        self.query = self.match = None
        return chapter

    def entry(self, index):
        self.check_current()
        if type(index) is not int or not 0 <= index < len(self.toc):
            raise ValueError('Choose an item from the contents list.')
        return self.toc[index]

    def find(self, query, *, next_match=False):
        self.check_current()
        if self.raw_text is None:
            raise ValueError('Open a chapter before finding text.')
        if not isinstance(query, str) or not query:
            raise ValueError('Enter text to find in the current chapter.')
        start = self.match.end if next_match and self.query == query and self.match else 0
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        found = pattern.search(self.raw_text, start)
        wrapped = False
        if found is None and start > 0:
            found = pattern.search(self.raw_text, 0, start)
            wrapped = found is not None
        self.check_current()
        self.query = query
        self.match = None if found is None else Match(found.start(), found.end(), wrapped)
        return self.match

    def report(self):
        self.check_current()
        parts = [self.title, NOTICE,
                 f'{len(self.chapters)} chapters in EPUB reading order; {len(self.toc)} contents entries.',
                 'Chapter locations are unpaginated. The extracted text may omit or rearrange source content.']
        parts.append('Extraction warnings:\n' + _warning_text(self.warnings) if self.warnings else
                     'No additional extraction warnings were reported. This does not verify the text.')
        descriptions = sum(len(chapter.image_descriptions) for chapter in self.chapters)
        if descriptions:
            parts.append(f'{descriptions} author-supplied image alternative descriptions are available '
                         'in chapter information. They are unverified and are separate from the extracted text.')
        hint_labels = {'titles': 'Titles', 'creators': 'Creators', 'languages': 'Languages',
                       'identifiers': 'Identifiers', 'publishers': 'Publishers'}
        hints = [f'{label}: {"; ".join(self.metadata_hints[key])}' for key, label in hint_labels.items()
                 if self.metadata_hints.get(key)]
        if hints:
            parts.extend(('Metadata hints from the EPUB; not verified citation metadata:', *hints))
        return '\n'.join(parts)

    def chapter_status(self):
        self.check_current()
        if self.raw_text is None:
            raise ValueError('No electronic-book chapter is open.')
        chapter = self.chapters[self.ordinal - 1]
        warnings = ('\nChapter warnings:\n' + _warning_text(self.chapter_warnings)
                    if self.chapter_warnings else '\nNo additional chapter warnings were reported.')
        if chapter.image_descriptions:
            warnings += ('\nImage alternative descriptions — author-supplied and unverified; '
                         'not transcribed source text:\n' +
                         '\n'.join(f'Image {number}: {description}' for number, description in
                                   enumerate(chapter.image_descriptions, 1)))
        return (f'{chapter.label}\nChapter {self.ordinal} of {len(self.chapters)}; '
                f'{chapter.characters} extracted characters.\n{NOTICE}{warnings}')


def native_position(control, raw_text, offset):
    """Map a source offset only if the native control proves the exact prefix.

    wx.TextCtrl positions differ from Python indices on Windows (CRLF and
    UTF-16). XYToPosition supplies platform-native candidates; GetRange verifies
    them against the unmodified source. Unsupported mappings fail explicitly.
    """
    if type(offset) is not int or not 0 <= offset <= len(raw_text):
        raise ValueError('Invalid extracted-text position.')
    if control.GetValue() != raw_text:
        raise ValueError('The text control changed the extracted text; positioning is unavailable.')
    prefix = raw_text[:offset]
    row = prefix.count('\n')
    line_prefix = prefix.rsplit('\n', 1)[-1]
    columns = (len(line_prefix), len(line_prefix.encode('utf-16-le')) // 2)
    for column in dict.fromkeys(columns):
        position = control.XYToPosition(column, row)
        if position >= 0:
            try:
                if control.GetRange(0, position) == prefix:
                    return position
            except UnicodeError:
                # A Windows candidate may end between a surrogate pair.
                # Try the UTF-16 column only; never accept a partial character.
                continue
    raise ValueError('The text control could not locate that exact extracted-text position.')
