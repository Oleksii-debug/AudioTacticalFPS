"""Real, offline CSL formatting in a bounded JavaScript interpreter.

The unmodified citeproc-js processor, styles and locales are pinned resources.
No host functions, filesystem or network APIs are installed in its interpreter.
Formatting bibliographic metadata does not authenticate a quotation or page.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit


ATTRIBUTION = ('(c) Frank Bennett\n'
               'citeproc-js implements the Citation Style Language\n'
               'https://citationstyles.org/')
PROCESSOR_PACKAGE_VERSION = '2.4.63'
PROCESSOR_REPORTED_VERSION = '1.4.61'
LOCALES = ('en-US', 'en-GB', 'uk-UA', 'sk-SK')
MAX_CUSTOM_STYLE_BYTES = 512 * 1024
MAX_CUSTOM_STYLE_DEPTH = 64
MAX_CUSTOM_STYLE_NODES = 10000
MAX_CUSTOM_STYLE_ATTRIBUTES = 32768
_BIDI = frozenset('\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')


@dataclass(frozen=True)
class StyleInfo:
    id: str
    title: str
    kind: str
    filename: str


@dataclass(frozen=True)
class CustomStyle:
    """An explicitly supplied style snapshot; never a path or remote resource.

    Use :func:`load_custom_style` to construct it. Rendering checks the source,
    digest and metadata again, including when this value was built directly.
    """
    info: StyleInfo
    source: bytes
    sha256: str


_STYLES = (
    StyleInfo('apa', 'APA Style 7th edition', 'in-text', 'apa.csl'),
    StyleInfo('chicago-author-date', 'Chicago Manual of Style 18th edition (author-date)',
              'in-text', 'chicago-author-date.csl'),
    StyleInfo('chicago-note', 'Chicago Manual of Style 18th edition (notes and bibliography)',
              'note', 'chicago-notes-bibliography.csl'),
    StyleInfo('mla', 'MLA Handbook 9th edition (in-text citations)',
              'in-text', 'modern-language-association.csl'),
    StyleInfo('harvard', 'Cite Them Right 12th edition (author-date/Harvard)',
              'in-text', 'harvard-cite-them-right.csl'),
    StyleInfo('vancouver', 'NLM/Vancouver: Citing Medicine 2nd edition (citation-sequence)',
              'in-text', 'nlm-citation-sequence.csl'),
    StyleInfo('ieee', 'IEEE Reference Guide version 11.29.2023', 'in-text', 'ieee.csl'),
)
_STYLE_BY_ID = {style.id: style for style in _STYLES}
_ROOT = Path(__file__).parent
_HASHES = {
    '_vendor_citeproc/citeproc.js': '55abba1a8b8b48c14323fd053f9646013b4b7a0c977938064ecf5195a502b9f1',
    'styles/apa.csl': '3917def5fc951b104d64216c3373096042368b9704ba269ca83c14ebaba889fc',
    'styles/chicago-author-date.csl': '002fade78d7e4fe9d42936a16b43a8066b097013f6255df40b1bfba6631eff9b',
    'styles/chicago-notes-bibliography.csl': '4b6be4bceaf8f3c31b49331c9f9e38c977f666d6ab80e19a2ac8482c7511abeb',
    'styles/harvard-cite-them-right.csl': '0ca3d2c7c881cd98cdd81098b5349f5921edf430498aa9a5a41918621cf7cc45',
    'styles/ieee.csl': 'b4c7619fc16c45a31e4cc3271eab94ffe83192d3b4c7fc729470a3b459448de3',
    'styles/locales-en-GB.xml': 'e4a1fed9cec04b5fed5e9027b4055925cd4d8f7c9fe233eb0ceca6d4be3999f6',
    'styles/locales-en-US.xml': 'ac864c7c21166b4390d82c31792cdc509400727fa0060b43d8aa17e07f9cb079',
    'styles/locales-sk-SK.xml': '51600e20de1f57ab128af5f6a8b1a79b3a65bb1c03d39f075a73a484bdf573e0',
    'styles/locales-uk-UA.xml': 'e967c8d1a13606d918c1074e0fc4d10add62f5eff56eadb42c25b3dad78e580d',
    'styles/modern-language-association.csl': 'e1432698ef28938d044b06bd45ddcc57d6b974cec25f71007075646a11f99484',
    'styles/nlm-citation-sequence.csl': '492f7935df42054b33d515f7275122f419780df990ea35925dcbbff0fecf1137',
}


@dataclass(frozen=True)
class RenderBudget:
    """Explicit resource budgets, not a limit on the stored library's size."""
    max_records: int = 2000
    max_citations: int = 4000
    max_input_bytes: int = 8 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: float = 30.0

    def validate(self):
        for field in ('max_records', 'max_citations', 'max_input_bytes',
                      'max_output_bytes', 'memory_bytes'):
            if type(getattr(self, field)) is not int or getattr(self, field) <= 0:
                raise ValueError('Citation resource budgets must be positive integers.')
        if (type(self.cpu_seconds) not in (int, float)
                or not 0 < self.cpu_seconds <= 300
                or not 4 * 1024 * 1024 <= self.memory_bytes <= 2 * 1024**3):
            raise ValueError('Choose a CPU budget up to 300 seconds and 4 MiB to 2 GiB of memory.')


@dataclass(frozen=True)
class RenderedBibliography:
    style: StyleInfo
    locale: str
    output: str
    entries: tuple[str, ...]
    entry_ids: tuple[tuple[str, ...], ...]
    citations: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    attribution: str = ATTRIBUTION
    processor_package_version: str = PROCESSOR_PACKAGE_VERSION
    processor_reported_version: str = PROCESSOR_REPORTED_VERSION
    style_sha256: str = ''
    custom_style: bool = False

    @property
    def bibliography(self):
        return '\n\n'.join(self.entries)


def available_styles():
    """Return installed, pinned styles. No discovery request leaves the device."""
    return _STYLES


def _resource(name):
    if name not in _HASHES:
        raise ValueError('This CSL resource is not in the approved local manifest.')
    try:
        # Read no more than the largest approved-asset budget even if a
        # replaced installation file is huge. The digest still verifies the
        # exact bytes, including an unexpected short or extended resource.
        with (_ROOT / name).open('rb') as handle:
            data = handle.read(2 * 1024 * 1024 + 1)
    except OSError as exc:
        raise RuntimeError('The installed citation resources are incomplete; repair the application.') from exc
    if sha256(data).hexdigest() != _HASHES[name]:
        raise RuntimeError('The installed citation resource failed its integrity check.')
    return data.decode('utf-8')


def _xml_json(text):
    """Translate pinned XML into citeproc-js's documented JSON XML interface."""
    from defusedxml import ElementTree

    def node(element):
        name = element.tag.split('}')[-1]
        attrs = {key.replace('{http://www.w3.org/XML/1998/namespace}', 'xml:').split('}')[-1]: value
                 for key, value in element.attrib.items()}
        children = [node(child) for child in element]
        if not children and (element.text and element.text.strip() or name in ('term', 'single', 'multiple')):
            children = [element.text or '']
        return {'name': name, 'attrs': attrs, 'children': children}

    return node(ElementTree.fromstring(text))


_PROTECTED = frozenset(('__proto__', 'prototype', 'constructor', 'toString',
    'toLocaleString', 'valueOf', 'hasOwnProperty', 'isPrototypeOf',
    'propertyIsEnumerable', '__defineGetter__', '__defineSetter__',
    '__lookupGetter__', '__lookupSetter__'))

_CSL_NAMESPACE = 'http://purl.org/net/xbiblio/csl'
_XML_NAMESPACE = 'http://www.w3.org/XML/1998/namespace'
_STYLE_FORMATTING = frozenset(('font-style', 'font-variant', 'font-weight',
                              'text-decoration', 'vertical-align'))
_STYLE_AFFIXES = frozenset(('prefix', 'suffix'))
_STYLE_RENDERING = frozenset(('text', 'date', 'number', 'names', 'label', 'group', 'choose'))
_STYLE_NAME_OPTIONS = frozenset(('and', 'delimiter-precedes-et-al', 'delimiter-precedes-last',
    'et-al-min', 'et-al-use-first', 'et-al-use-last', 'et-al-subsequent-min',
    'et-al-subsequent-use-first', 'initialize', 'initialize-with', 'name-as-sort-order',
    'sort-separator', 'name-delimiter', 'names-delimiter', 'name-form'))
_STYLE_TEXT_OPTIONS = _STYLE_FORMATTING | _STYLE_AFFIXES | {
    'text-case', 'strip-periods', 'quotes', 'display'}
_STYLE_CONDITIONS = frozenset(('disambiguate', 'is-numeric', 'is-uncertain-date',
                              'locator', 'position', 'type', 'variable', 'locale'))
_STYLE_ATTRIBUTES = {
    'style': _STYLE_NAME_OPTIONS | {'version', 'class', 'default-locale',
        'initialize-with-hyphen', 'page-range-format', 'demote-non-dropping-particle'},
    'info': set(), 'author': set(), 'contributor': set(), 'id': set(),
    'issn': set(), 'eissn': set(), 'issnl': set(), 'email': set(), 'uri': set(),
    'updated': set(), 'published': set(),
    'title': {'xml:lang'}, 'title-short': {'xml:lang'}, 'summary': {'xml:lang'},
    'rights': {'license', 'xml:lang'}, 'link': {'href', 'rel', 'xml:lang'},
    'category': {'citation-format', 'field'},
    'macro': {'name'}, 'locale': {'xml:lang'}, 'terms': set(),
    'term': {'name', 'form', 'gender', 'gender-form', 'match'},
    'single': set(), 'multiple': set(),
    'style-options': {'punctuation-in-quote', 'limit-day-ordinals-to-day-1'},
    'citation': _STYLE_NAME_OPTIONS | {'disambiguate-add-names', 'disambiguate-add-givenname',
        'disambiguate-add-year-suffix', 'givenname-disambiguation-rule', 'cite-group-delimiter',
        'collapse', 'year-suffix-delimiter', 'after-collapse-delimiter', 'near-note-distance'},
    'bibliography': _STYLE_NAME_OPTIONS | {'hanging-indent', 'second-field-align', 'line-spacing',
        'entry-spacing', 'subsequent-author-substitute', 'subsequent-author-substitute-rule'},
    'layout': _STYLE_FORMATTING | _STYLE_AFFIXES | {'delimiter', 'locale'},
    'sort': set(), 'key': {'variable', 'macro', 'sort', 'names-min', 'names-use-first', 'names-use-last'},
    'text': _STYLE_TEXT_OPTIONS | {'variable', 'macro', 'term', 'value', 'form', 'plural'},
    'date': _STYLE_TEXT_OPTIONS | {'variable', 'form', 'date-parts', 'delimiter'},
    'date-part': _STYLE_TEXT_OPTIONS | {'name', 'form', 'range-delimiter'},
    'number': _STYLE_TEXT_OPTIONS | {'variable', 'form'},
    'names': _STYLE_FORMATTING | _STYLE_AFFIXES | _STYLE_NAME_OPTIONS | {'variable', 'delimiter', 'display'},
    'name': _STYLE_FORMATTING | _STYLE_AFFIXES | _STYLE_NAME_OPTIONS | {'form', 'delimiter'},
    'name-part': _STYLE_TEXT_OPTIONS | {'name'},
    'et-al': _STYLE_FORMATTING | {'term'}, 'substitute': set(),
    'label': _STYLE_TEXT_OPTIONS | {'variable', 'form', 'plural'},
    'group': _STYLE_FORMATTING | _STYLE_AFFIXES | {'delimiter', 'display'},
    'choose': set(), 'if': _STYLE_CONDITIONS | {'match'},
    'else-if': _STYLE_CONDITIONS | {'match'}, 'else': set(),
}
_STYLE_METADATA = frozenset(('id', 'title', 'title-short', 'summary', 'rights', 'link',
    'published', 'updated', 'issn', 'eissn', 'issnl', 'email', 'uri'))
_STYLE_CHILDREN = {
    'style': {'info', 'locale', 'macro', 'citation', 'bibliography'},
    'info': _STYLE_METADATA | {'author', 'contributor', 'category'},
    'author': {'name', 'email', 'uri'}, 'contributor': {'name', 'email', 'uri'},
    'locale': {'info', 'terms', 'date', 'style-options'}, 'terms': {'term'},
    'term': {'single', 'multiple'}, 'citation': {'sort', 'layout'},
    'bibliography': {'sort', 'layout'}, 'sort': {'key'},
    'macro': _STYLE_RENDERING, 'layout': _STYLE_RENDERING, 'group': _STYLE_RENDERING,
    'substitute': _STYLE_RENDERING, 'if': _STYLE_RENDERING,
    'else-if': _STYLE_RENDERING, 'else': _STYLE_RENDERING,
    'choose': {'if', 'else-if', 'else'}, 'date': {'date-part'},
    'names': {'name', 'et-al', 'label', 'substitute'}, 'name': {'name-part'},
}
_STYLE_KEY_VALUES = frozenset(('name', 'macro', 'term', 'variable', 'type', 'form',
    'gender', 'gender-form', 'match', 'xml:lang', 'locale', 'default-locale',
    'is-numeric', 'is-uncertain-date', 'locator', 'position'))
_STYLE_LOCAL_CODES = frozenset((*LOCALES, 'en', 'uk', 'sk'))


def _style_element(tag):
    if not isinstance(tag, str) or not tag.startswith('{' + _CSL_NAMESPACE + '}'):
        raise ValueError('Custom style elements must use the CSL XML namespace.')
    name = tag[len(_CSL_NAMESPACE) + 2:]
    if name not in _STYLE_ATTRIBUTES:
        raise ValueError('The custom style contains an unsupported CSL element.')
    return name


def _style_attribute(key):
    if key == '{' + _XML_NAMESPACE + '}lang':
        return 'xml:lang'
    if '{' in key or ':' in key:
        raise ValueError('The custom style contains an unsupported XML attribute namespace.')
    return key


def _custom_style_tree(source):
    """Enforce parser limits as nodes arrive, before constructing a large tree."""
    from defusedxml import ElementTree
    from defusedxml.common import DefusedXmlException
    from xml.etree.ElementTree import ParseError, TreeBuilder

    class BoundedTree(TreeBuilder):
        def __init__(self):
            super().__init__()
            self.depth = self.nodes = self.attributes = self.characters = self.namespaces = 0

        def start(self, tag, attrs):
            self.depth += 1
            self.nodes += 1
            self.attributes += len(attrs)
            if self.depth > MAX_CUSTOM_STYLE_DEPTH:
                raise ValueError('The custom CSL style exceeds the XML depth limit of 64.')
            if self.nodes > MAX_CUSTOM_STYLE_NODES:
                raise ValueError('The custom CSL style exceeds the XML node budget.')
            if len(attrs) > 64 or self.attributes > MAX_CUSTOM_STYLE_ATTRIBUTES:
                raise ValueError('The custom CSL style exceeds the XML attribute budget.')
            name = _style_element(tag)
            for key, value in attrs.items():
                key = _style_attribute(key)
                if key not in _STYLE_ATTRIBUTES[name]:
                    raise ValueError('The custom style contains an unsupported CSL attribute.')
                _text(value, 'custom CSL attribute', 8192)
                if key in _STYLE_KEY_VALUES and any(part in _PROTECTED or part.lower() in _PROTECTED
                                                    for part in value.split()):
                    raise ValueError('The custom style contains a reserved processor identifier.')
                if key in ('xml:lang', 'locale', 'default-locale') and (not value.split()
                        or any(not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', part)
                               or any(segment in _PROTECTED or segment.lower() in _PROTECTED
                                      for segment in part.split('-')) for part in value.split())):
                    raise ValueError('The custom style contains an invalid locale identifier.')
                if key == 'locale' and any(part not in _STYLE_LOCAL_CODES for part in value.split()):
                    raise ValueError('The custom style requests a locale unavailable offline. '
                                     'Use en-US, en-GB, uk-UA or sk-SK.')
            # Upstream derives another object key by dropping "ordinal-".
            # Validate that numeric suffix, not just the complete term name.
            if name == 'term':
                term = attrs.get('name', '')
                if not term.strip():
                    raise ValueError('Custom CSL locale terms need a name.')
                if term.startswith('ordinal') and term != 'ordinal' and not re.fullmatch(r'ordinal-[0-9]{2}', term):
                    raise ValueError('Custom CSL ordinal terms must be ordinal or ordinal-00 through ordinal-99.')
            return super().start(tag, attrs)

        def end(self, tag):
            result = super().end(tag)
            self.depth -= 1
            return result

        def data(self, value):
            self.characters += len(value)
            if self.characters > 256 * 1024:
                raise ValueError('The custom CSL style exceeds the XML text budget.')
            _text(value, 'custom CSL text', 256 * 1024)
            super().data(value)

        def comment(self, value):
            self.characters += len(value)
            if self.characters > 256 * 1024:
                raise ValueError('The custom CSL style exceeds the XML text budget.')
            _text(value, 'custom CSL comment', 256 * 1024)

        def pi(self, target, value):
            raise ValueError('XML processing instructions are not supported in custom CSL styles.')

        def start_ns(self, prefix, uri):
            self.namespaces += 1
            if self.namespaces > 64 or uri not in (_CSL_NAMESPACE, _XML_NAMESPACE):
                raise ValueError('The custom style contains unsupported XML namespace declarations.')

    parser = ElementTree.DefusedXMLParser(target=BoundedTree(), forbid_dtd=True,
                                          forbid_entities=True, forbid_external=True)
    try:
        parser.feed(source)
        return parser.close()
    except (ParseError, DefusedXmlException) as exc:
        raise ValueError('Custom CSL must be well-formed XML without DTDs, entities or external resources.') from exc


def _validate_style_structure(root):
    tag = lambda name: '{' + _CSL_NAMESPACE + '}' + name
    if root.tag != tag('style'):
        raise ValueError('Choose an independent CSL style, not a locale or another XML document.')
    if any(node.attrib.get('rel') == 'independent-parent' for node in root.iter(tag('link'))):
        raise ValueError('Dependent CSL styles require an external parent. Choose the independent '
                         'parent style file; external styles are never downloaded.')
    if root.attrib.get('version') != '1.0' or root.attrib.get('class') not in ('in-text', 'note'):
        raise ValueError('A custom CSL style needs version="1.0" and class="in-text" or "note".')
    for node in root.iter():
        name = _style_element(node.tag)
        children = [_style_element(child.tag) for child in node]
        if set(children) - _STYLE_CHILDREN.get(name, set()):
            raise ValueError('The custom CSL style contains misplaced elements.')
        if node.text and node.text.strip() and name not in _STYLE_METADATA | {'name', 'term', 'single', 'multiple'}:
            raise ValueError('The custom CSL style contains text outside a supported text element.')
        if any(child.tail and child.tail.strip() for child in node):
            raise ValueError('The custom CSL style contains unsupported mixed XML content.')
        if name == 'text' and len(set(node.attrib) & {'variable', 'macro', 'term', 'value'}) != 1:
            raise ValueError('Each CSL text element must select exactly one variable, macro, term or value.')
        if name in ('macro', 'layout') and not children:
            raise ValueError('Custom CSL macros and layouts must contain rendering instructions.')
        if name == 'choose' and (not children or children[0] != 'if' or children.count('if') != 1
                or children.count('else') > 1 or 'else' in children[:-1]):
            raise ValueError('The custom style contains an invalid CSL conditional.')
        if name in ('if', 'else-if') and not set(node.attrib) & _STYLE_CONDITIONS:
            raise ValueError('Each custom CSL condition needs an explicit test.')
        if name == 'term' and children and sorted(children) != ['multiple', 'single']:
            raise ValueError('Custom CSL plural terms need one single and one multiple form.')
    infos = root.findall(tag('info'))
    if len(infos) != 1 or len(root) == 0 or root[0] is not infos[0]:
        raise ValueError('A custom CSL style must begin with exactly one info element.')
    info = infos[0]
    metadata = {}
    for name, maximum in (('title', 1024), ('id', 4096)):
        nodes = info.findall(tag(name))
        if len(nodes) != 1 or len(nodes[0]):
            raise ValueError('A custom CSL style needs exactly one plain-text title and id.')
        value = _text(nodes[0].text or '', 'custom CSL ' + name, maximum, required=True).strip()
        if name == 'id' and (value in _PROTECTED or any(char.isspace() for char in value)):
            raise ValueError('The custom style identifier must be nonreserved and contain no whitespace.')
        metadata[name] = ' '.join(value.split()) if name == 'title' else value
    for name in ('citation', 'bibliography'):
        sections = root.findall(tag(name))
        if len(sections) != 1:
            raise ValueError('This bibliography workbench needs one citation and one bibliography '
                             'section in each independent CSL style.')
        children = [_style_element(child.tag) for child in sections[0]]
        if children not in (['layout'], ['sort', 'layout']):
            raise ValueError('Each CSL citation and bibliography section needs one layout after any sort.')
    macros = {}
    for node in root.findall(tag('macro')):
        name = _text(node.attrib.get('name', ''), 'custom CSL macro name', 256, required=True)
        if name in macros:
            raise ValueError('Custom CSL macro names must be unique.')
        macros[name] = {child.attrib['macro'] for child in node.iter() if 'macro' in child.attrib}
    for node in root.iter():
        if 'macro' in node.attrib and node.attrib['macro'] not in macros:
            raise ValueError('The custom CSL style refers to an undefined macro.')
    visiting, heights = set(), {}

    def height(name):
        if name in visiting:
            raise ValueError('The custom CSL style contains recursive macros.')
        if len(visiting) >= MAX_CUSTOM_STYLE_DEPTH:
            raise ValueError('The custom CSL style exceeds the macro depth limit of 64.')
        if name not in heights:
            visiting.add(name)
            heights[name] = 1 + max((height(child) for child in macros[name]), default=0)
            visiting.remove(name)
            if heights[name] > MAX_CUSTOM_STYLE_DEPTH:
                raise ValueError('The custom CSL style exceeds the macro depth limit of 64.')
        return heights[name]

    for name in macros:
        height(name)
    return StyleInfo(metadata['id'], metadata['title'], root.attrib['class'], '')


def load_custom_style(data: bytes | str) -> CustomStyle:
    """Validate explicit UTF-8 XML content and freeze its exact bytes and SHA-256.

    Only independent styles with citation and bibliography layouts are accepted.
    This function never reads a path, fetches a URL, or persists a style file.
    Inline locale terms are permitted; all external locale data stays pinned.
    """
    if type(data) not in (bytes, str) or not 0 < len(data) <= MAX_CUSTOM_STYLE_BYTES:
        raise ValueError('Choose a nonempty custom CSL file no larger than 512 KiB.')
    try:
        source = data.encode('utf-8', errors='strict') if type(data) is str else data
        if len(source) > MAX_CUSTOM_STYLE_BYTES:
            raise ValueError('Choose a nonempty custom CSL file no larger than 512 KiB.')
        text = source.decode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise ValueError('Custom CSL files must contain valid UTF-8 text.') from exc
    _text(text, 'custom CSL source', MAX_CUSTOM_STYLE_BYTES, required=True)
    declaration = re.match(r'\ufeff?<\?xml\s[^?]*\?>', text)
    if declaration:
        encoding = re.search(r'\bencoding\s*=\s*([\'"])([^\'"]+)\1', declaration.group())
        if encoding and encoding.group(2).casefold() != 'utf-8':
            raise ValueError('Custom CSL XML must declare UTF-8 encoding.')
    root = _custom_style_tree(source)
    info = _validate_style_structure(root)
    return CustomStyle(info, source, sha256(source).hexdigest())


def _checked_custom_style(style):
    if type(style) is not CustomStyle or type(style.source) is not bytes or type(style.info) is not StyleInfo:
        raise ValueError('Invalid custom CSL snapshot. Load the style again.')
    checked = load_custom_style(style.source)
    if type(style.sha256) is not str or style.sha256 != checked.sha256 or style.info != checked.info:
        raise ValueError('The custom CSL style snapshot changed. Load the style again.')
    return checked
_NAMES = frozenset(('author', 'editor', 'translator', 'recipient', 'interviewer',
    'composer', 'director', 'illustrator', 'original-author', 'collection-editor',
    'container-author', 'editorial-director', 'reviewed-author', 'chair', 'compiler',
    'contributor', 'curator', 'executive-producer', 'guest', 'host', 'narrator',
    'organizer', 'performer', 'producer', 'script-writer', 'series-creator'))
_DATES = frozenset(('issued', 'accessed', 'event-date', 'original-date', 'submitted',
                    'available-date'))
_FIELDS = frozenset(('id', 'type', 'title', 'title-short', 'container-title',
    'container-title-short', 'collection-title', 'collection-number', 'publisher',
    'publisher-place', 'original-title', 'original-publisher', 'original-publisher-place',
    'event', 'event-title', 'event-place', 'volume', 'issue', 'page', 'page-first',
    'edition', 'number', 'number-of-pages', 'number-of-volumes', 'version', 'chapter-number',
    'DOI', 'ISBN', 'ISSN', 'URL', 'PMID', 'PMCID', 'archive', 'archive-place',
    'archive_location', 'archive-collection', 'call-number', 'abstract', 'note',
    'language', 'genre', 'medium', 'status', 'source', 'annote', 'keyword', 'jurisdiction',
    'authority', 'section', 'dimensions', 'scale', 'references', 'reviewed-title',
    'citation-label', 'year-suffix', 'citation-number', 'journalAbbreviation',
    'shortTitle')) | _NAMES | _DATES
_TYPES = frozenset(('article', 'article-journal', 'article-magazine', 'article-newspaper',
    'bill', 'book', 'broadcast', 'chapter', 'classic', 'collection', 'dataset',
    'document', 'entry', 'entry-dictionary', 'entry-encyclopedia', 'event', 'figure',
    'graphic', 'hearing', 'interview', 'legal_case', 'legislation', 'manuscript',
    'map', 'motion_picture', 'musical_score', 'pamphlet', 'paper-conference',
    'patent', 'performance', 'periodical', 'personal_communication', 'post',
    'post-weblog', 'regulation', 'report', 'review', 'review-book', 'software',
    'song', 'speech', 'standard', 'thesis', 'treaty', 'webpage'))


def _text(value, label, maximum=100000, *, required=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'Invalid or oversized {label}.')
    if required and not value.strip():
        raise ValueError(f'{label} must not be empty.')
    # Match the existing academic metadata boundary: preserve natural RTL
    # letters while rejecting directional overrides/isolates and C1 controls.
    if any(char in _BIDI or unicodedata.category(char) in ('Cc', 'Cs')
           and char not in '\n\t\r' for char in value):
        raise ValueError(f'{label} contains unsupported control characters.')
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise ValueError(f'{label} contains invalid Unicode.') from exc
    return value


def _safe_url(value):
    _text(value, 'URL', 8192)
    try:
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or not url.hostname
                or url.username is not None or url.password is not None
                or any(char.isspace() for char in value)):
            raise ValueError
        _ = url.port
    except ValueError as exc:
        raise ValueError('Bibliographic links must be HTTP or HTTPS URLs without credentials.') from exc
    return value


def _name(value):
    if type(value) is not dict or set(value) - {
            'literal', 'family', 'given', 'suffix', 'dropping-particle',
            'non-dropping-particle', 'comma-suffix', 'static-ordering'}:
        raise ValueError('A CSL creator must contain supported name fields only.')
    if 'literal' in value and any(value.get(key) for key in value if key != 'literal'):
        raise ValueError('A creator cannot have both literal and personal-name fields.')
    result = {}
    for key, item in value.items():
        if key in ('comma-suffix', 'static-ordering'):
            if type(item) is not bool:
                raise ValueError('CSL name options must be boolean.')
            result[key] = item
        else:
            result[key] = _text(item, 'creator name', 4096)
    if not any(result.get(key) for key in ('literal', 'family', 'given')):
        raise ValueError('A CSL creator must have a literal or personal name.')
    return result


def _date(value):
    if type(value) is not dict or set(value) - {'date-parts', 'literal', 'raw', 'circa', 'season'}:
        raise ValueError('Unsupported CSL date representation.')
    result = {}
    if 'date-parts' in value:
        parts = value['date-parts']
        if (type(parts) is not list or not 1 <= len(parts) <= 2
                or any(type(part) is not list or not 1 <= len(part) <= 3 for part in parts)):
            raise ValueError('CSL dates need one or two date-parts arrays.')
        for part in parts:
            if any(type(number) is not int for number in part):
                raise ValueError('CSL date parts must be integers.')
            if (not -9999 <= part[0] <= 9999
                    or len(part) > 1 and not 1 <= part[1] <= 12
                    or len(part) > 2 and not 1 <= part[2] <= 31):
                raise ValueError('CSL date parts are outside supported ranges.')
            # calendar uses a proleptic Gregorian calendar and supports the
            # same negative/zero year range; no guessed correction is made.
            if len(part) == 3 and part[2] > monthrange(part[0], part[1])[1]:
                raise ValueError('CSL date parts do not form a valid calendar date.')
        result['date-parts'] = [list(part) for part in parts]
    for key in ('literal', 'raw'):
        if key in value:
            result[key] = _text(value[key], 'publication date', 256)
    if 'circa' in value:
        if type(value['circa']) is not bool:
            raise ValueError('The approximate-date flag must be boolean.')
        result['circa'] = value['circa']
    if 'season' in value:
        if type(value['season']) is not int or not 1 <= value['season'] <= 4:
            raise ValueError('A CSL season must be an integer from 1 to 4.')
        result['season'] = value['season']
    return result


def _records(records, budget):
    if type(records) not in (list, tuple) or not 1 <= len(records) <= budget.max_records:
        raise ValueError(f'Format 1 to {budget.max_records} references in this configured batch.')
    result, identities, warnings, input_bytes = [], set(), [], 0
    for position, record in enumerate(records, 1):
        if type(record) is not dict or set(record) - _FIELDS:
            raise ValueError(f'Reference {position} contains unsupported CSL fields.')
        identity = record.get('id')
        if type(identity) is int:
            identity = str(identity)
        _text(identity, 'reference identifier', 256, required=True)
        if identity in identities or identity in _PROTECTED:
            raise ValueError('Reference identifiers must be unique and nonreserved.')
        identities.add(identity)
        if not isinstance(record.get('type'), str) or record['type'] not in _TYPES:
            raise ValueError(f'Reference {position} has an unsupported CSL type.')
        _text(record.get('title', ''), 'reference title', required=True)
        row = {}
        for key, value in record.items():
            if key in _NAMES:
                if type(value) is not list or len(value) > 200:
                    raise ValueError('Each CSL creator list may contain at most 200 names.')
                row[key] = [_name(item) for item in value]
            elif key in _DATES:
                row[key] = _date(value)
            elif key == 'URL' and value:
                row[key] = _safe_url(value)
            elif type(value) is int:
                if not -10**12 <= value <= 10**12:
                    raise ValueError('A bibliographic numeric field is outside supported ranges.')
                row[key] = value
            else:
                row[key] = _text(value, 'bibliographic field')
        row['id'] = identity
        input_bytes += len(json.dumps(row, ensure_ascii=False).encode('utf-8'))
        if input_bytes > budget.max_input_bytes:
            raise ValueError('The selected references exceed the configured formatting byte budget.')
        if not row.get('author'):
            warnings.append(f'Reference {position} has no author; the selected style supplies its own fallback.')
        if not row.get('issued'):
            warnings.append(f'Reference {position} has no publication date; the selected style supplies its own fallback.')
        result.append(row)
    return result, identities, warnings


_LABELS = frozenset(('book', 'chapter', 'column', 'figure', 'folio', 'issue', 'line',
    'note', 'opus', 'page', 'paragraph', 'part', 'section', 'sub-verbo', 'verse', 'volume',
    'act', 'appendix', 'article-locator', 'canon', 'elocation', 'equation', 'rule',
    'scene', 'table', 'timestamp', 'title', 'title-locator'))


def _clusters(citations, records, identities, budget):
    if citations is None:
        if len(records) > budget.max_citations:
            raise ValueError('The default citation sequence exceeds the configured batch budget.')
        return [[{'id': row['id']}] for row in records], False
    if type(citations) not in (list, tuple) or len(citations) > budget.max_citations:
        raise ValueError('The requested citation sequence exceeds the configured batch budget.')
    result, manual, input_bytes = [], False, 0
    for cluster in citations:
        if type(cluster) not in (list, tuple) or not 1 <= len(cluster) <= budget.max_records:
            raise ValueError('Each citation cluster must contain one or more reference items.')
        rendered, seen = [], set()
        for item in cluster:
            if type(item) is not dict or set(item) - {
                    'id', 'locator', 'label', 'prefix', 'suffix', 'suppress-author'}:
                raise ValueError('Unsupported citation-item fields.')
            identity = item.get('id')
            if type(identity) is int:
                identity = str(identity)
            if not isinstance(identity, str) or identity not in identities:
                raise ValueError('Every citation item must reference a selected bibliography record.')
            if identity in seen:
                raise ValueError('A citation cluster contains the same reference more than once.')
            seen.add(identity)
            row = {'id': identity}
            if 'locator' in item:
                row['locator'] = _text(item['locator'], 'citation locator', 512, required=True)
                manual = True
            if 'label' in item:
                if not isinstance(item['label'], str) or item['label'] not in _LABELS or 'locator' not in item:
                    raise ValueError('A locator label needs a supported label and an explicit locator.')
                row['label'] = item['label']
            for key in ('prefix', 'suffix'):
                if key in item:
                    row[key] = _text(item[key], 'citation affix', 4096)
            if 'suppress-author' in item:
                if type(item['suppress-author']) is not bool:
                    raise ValueError('The suppress-author citation option must be boolean.')
                row['suppress-author'] = item['suppress-author']
            input_bytes += len(json.dumps(row, ensure_ascii=False).encode('utf-8'))
            if input_bytes > budget.max_input_bytes:
                raise ValueError('Citation clusters exceed the configured formatting byte budget.')
            rendered.append(row)
        result.append(rendered)
    return result, manual


class _SafeMarkup(HTMLParser):
    """Keep CSL typography and layout only; export never creates active links."""
    _allowed = frozenset(('div', 'span', 'i', 'em', 'b', 'strong', 'sup', 'sub', 'u'))
    _classes = frozenset(('csl-entry', 'csl-bib-body', 'csl-left-margin',
                         'csl-right-inline', 'csl-block', 'csl-indent'))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('<br>')
        elif tag in self._allowed:
            attributes = dict(attrs)
            safe = ''
            if attributes.get('class') in self._classes:
                safe += ' class="' + attributes['class'] + '"'
            if tag == 'span' and attributes.get('style', '').replace(' ', '').rstrip(';') == 'font-variant:small-caps':
                safe += ' style="font-variant:small-caps;"'
            self.parts.append('<' + tag + safe + '>')
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack:
                opened = self.stack.pop()
                self.parts.append('</' + opened + '>')
                if opened == tag:
                    break

    def handle_data(self, data):
        self.parts.append(escape(data, quote=False))

    def finish(self):
        self.close()
        while self.stack:
            self.parts.append('</' + self.stack.pop() + '>')
        return ''.join(self.parts)


def _safe_html(value):
    parser = _SafeMarkup()
    parser.feed(value)
    return parser.finish()


_BRIDGE = r'''
(function (payload) {
    var messages = [], records = Object.create(null), ids = [];
    payload.records.forEach(function (row) { records[row.id] = row; ids.push(row.id); });
    CSL.debug = function (message) {
        if (messages.length < 100) messages.push(String(message).slice(0, 1000));
    };
    var sys = {
        retrieveItem: function (id) {
            if (!Object.prototype.hasOwnProperty.call(records, id)) throw new Error('Unknown reference');
            return records[id];
        },
        retrieveLocale: function (name) {
            if (Object.prototype.hasOwnProperty.call(payload.locales, name)) return payload.locales[name];
            var aliases = {'en':'en-US', 'uk':'uk-UA', 'sk':'sk-SK'};
            return Object.prototype.hasOwnProperty.call(aliases, name)
                ? payload.locales[aliases[name]] || false : false;
        }
    };
    var engine = new CSL.Engine(sys, payload.style, payload.locale, true);
    engine.setOutputFormat(payload.output);
    var cited = Object.create(null), citationOrder = [];
    payload.citations.forEach(function (items) {
        items.forEach(function (item) {
            if (!cited[item.id]) { cited[item.id] = true; citationOrder.push(item.id); }
        });
    });
    // Register citable items in first-use order. Treating every record as
    // uncited first would incorrectly number a numeric bibliography by the
    // import order instead of the order of citations in the document.
    engine.updateItems(citationOrder);
    engine.updateUncitedItems(ids.filter(function (id) { return !cited[id]; }));
    var previous = [], output = [];
    payload.citations.forEach(function (items, index) {
        var id = 'citation-' + (index + 1);
        var noteIndex = payload.noteStyle ? index + 1 : 0;
        var result = engine.processCitationCluster({citationID:id, citationItems:items,
            properties:{noteIndex:noteIndex}}, previous, []);
        if (result[0].citation_errors && result[0].citation_errors.length) {
            throw new Error('The processor could not format every citation.');
        }
        result[1].forEach(function (change) { output[change[0]] = change[1]; });
        previous.push([id, noteIndex]);
    });
    var bibliography = engine.makeBibliography();
    if (!bibliography || bibliography[0].bibliography_errors.length) {
        throw new Error('The processor could not format the bibliography.');
    }
    return JSON.stringify({entries:bibliography[1], entry_ids:bibliography[0].entry_ids,
        citations:output, warnings:messages, version:CSL.PROCESSOR_VERSION});
})
'''


def _evaluate_processor(processor, bridge, encoded, budget):
    """Run a fixed host-supplied CSL bridge in the shared bounded QuickJS context.

    The caller verifies processor resources and prepares data with JSON encoding.
    This low-level helper grants no Python callbacks, filesystem or networking.
    Process cancellation belongs to the caller's worker lifecycle.
    """
    if not isinstance(budget, RenderBudget):
        raise ValueError('Invalid citation resource budget.')
    budget.validate()
    if (not isinstance(processor, str) or len(processor.encode('utf-8')) > 2 * 1024 * 1024
            or not isinstance(bridge, str) or len(bridge.encode('utf-8')) > 65536
            or not isinstance(encoded, str)
            or len(encoded.encode('utf-8')) > budget.max_input_bytes + 4 * 1024 * 1024):
        raise ValueError('Citation interpreter input exceeds its resource budget.')
    try:
        import quickjs
    except ImportError as exc:
        raise RuntimeError('The local citation runtime is missing. Install the application academic runtime; no online fallback is used.') from exc
    context = quickjs.Context()
    context.set_memory_limit(budget.memory_bytes)
    context.set_time_limit(float(budget.cpu_seconds))
    context.set_max_stack_size(4 * 1024 * 1024)
    try:
        # JSON.parse prevents special object-literal keys from acquiring code
        # semantics. No Python callbacks, network or filesystem are installed.
        code = ('var module={exports:{}};\n' + processor + ';\n' + bridge
                + '(JSON.parse(' + json.dumps(encoded, ensure_ascii=True) + '));')
        raw = context.eval(code)
    except quickjs.JSException as exc:
        raise RuntimeError('Citation formatting could not finish within its resource budget or the selected metadata/style is incompatible. Reduce the batch or increase the configured budget.') from exc
    finally:
        del context
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > budget.max_output_bytes:
        raise RuntimeError('The formatted bibliography exceeds the configured output budget.')
    return raw


def render(records, *, style='apa', citations=None, locale='en-US', output='text', budget=None):
    """Render complete, context-aware citations and a bibliography offline.

    ``citations`` is an ordered sequence of clusters; each cluster contains CSL
    cite-item dictionaries with ``id`` and optionally ``locator``, ``label``,
    ``prefix``, ``suffix`` or ``suppress-author``. By default one citation is
    produced for each input record. Publication page extents are never promoted
    into quotation locators. Caller-provided locators are explicitly unverified.
    Input records are copied and are never mutated by the processor.
    """
    custom = None
    if type(style) is CustomStyle:
        custom = _checked_custom_style(style)
        style_info, style_hash = custom.info, custom.sha256
    elif isinstance(style, str) and style in _STYLE_BY_ID:
        style_info = _STYLE_BY_ID[style]
        style_hash = _HASHES['styles/' + style_info.filename]
    else:
        raise ValueError('Choose an installed citation style or an explicitly loaded custom CSL style.')
    if locale not in LOCALES:
        raise ValueError('Choose an installed citation locale: ' + ', '.join(LOCALES) + '.')
    if output not in ('text', 'html'):
        raise ValueError('Citation output must be text or html.')
    budget = RenderBudget() if budget is None else budget
    if not isinstance(budget, RenderBudget):
        raise ValueError('Invalid citation resource budget.')
    budget.validate()
    rows, identities, warnings = _records(records, budget)
    clusters, manual_locator = _clusters(citations, rows, identities, budget)
    if manual_locator:
        warnings.append('Citation locators were supplied manually; formatting does not verify those page or source positions.')
    if style_info.kind == 'note' and any(row.get('page') for row in rows):
        warnings.append('Page ranges in full bibliographic notes describe publication extent, not verified quotation locations.')
    payload = {'records': rows, 'citations': clusters, 'locale': locale,
        'output': output, 'noteStyle': style_info.kind == 'note',
        'style': _xml_json(custom.source.decode('utf-8') if custom
                           else _resource('styles/' + style_info.filename)),
        'locales': {name: _xml_json(_resource('styles/locales-' + name + '.xml'))
                    for name in (LOCALES if custom else dict.fromkeys(('en-US', locale)))}}
    # Bound bibliographic input and the number of cite-item repetitions before
    # allocating the complete interpreter payload.
    user_size = len(json.dumps({'records': rows, 'citations': clusters}, ensure_ascii=False).encode('utf-8'))
    if custom:
        user_size += len(custom.source)
    if user_size > budget.max_input_bytes:
        raise ValueError('References and citation clusters exceed the configured formatting byte budget.')
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    raw = _evaluate_processor(_resource('_vendor_citeproc/citeproc.js'), _BRIDGE, encoded, budget)
    result = json.loads(raw)
    if result.get('version') != PROCESSOR_REPORTED_VERSION:
        raise RuntimeError('The citation runtime identity changed unexpectedly.')
    entries = result.get('entries', [])
    rendered_citations = result.get('citations', [])
    entry_ids = result.get('entry_ids', [])
    if (len(entries) != len(entry_ids) or len(rendered_citations) != len(clusters)
            or any(not isinstance(item, str) for item in [*entries, *rendered_citations])
            or any(type(group) is not list or not group or any(identity not in identities for identity in group)
                   for group in entry_ids)):
        raise RuntimeError('The citation processor returned incomplete or invalid results.')
    returned_ids = [identity for group in entry_ids for identity in group]
    if len(returned_ids) != len(identities) or set(returned_ids) != identities:
        raise RuntimeError('The citation processor omitted or repeated a selected reference.')
    clean = _safe_html if output == 'html' else lambda value: value
    warnings.extend(_text(item, 'processor warning', 1000) for item in result.get('warnings', []))
    return RenderedBibliography(style_info, locale, output,
        tuple(clean(entry).strip() for entry in entries),
        tuple(tuple(group) for group in entry_ids),
        tuple(clean(citation).strip() for citation in rendered_citations),
        tuple(dict.fromkeys(warnings)), style_sha256=style_hash, custom_style=custom is not None)
