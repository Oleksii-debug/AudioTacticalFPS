"""Bounded, offline BibTeX metadata import using bibtexparser and pylatexenc.

Only strings are parsed. No TeX is executed, no attachment is opened, and no
external cross-reference is fetched. The caller uses the existing academic
preview/selection transaction; this module never writes to a database.

``parse_bibtex`` returns the same ``[(fields, warnings)]`` shape as the RIS/CSL
adapters. Original keys, entry text, and used string definitions are retained
as inert, explicitly labelled research notes. Stable application reference IDs
are never replaced by the imported citation keys.
"""
from contextvars import ContextVar
from functools import lru_cache
import logging
import re
import unicodedata


MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 2000
MAX_BLOCKS = 6000
MAX_STRINGS = 1000
MAX_FIELDS = 256
MAX_DEPTH = 64
MAX_MACRO_DEPTH = 32
MAX_CROSSREF_DEPTH = 16
MAX_VALUE = 100000
MAX_AUTHORS = 100
MAX_NOTES = 100000
_MACRO = re.compile(r'[A-Za-z_][A-Za-z0-9_:.+\-]*\Z')
_FIELD = re.compile(r'[A-Za-z][A-Za-z0-9_\-]*\Z')
_COMMAND = re.compile(r'\\([A-Za-z]+|[^A-Za-z])')
_BIDI = frozenset('\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
_MONTHS = dict(zip('jan feb mar apr may jun jul aug sep oct nov dec'.split(),
                  'January February March April May June July August September October November December'.split()))
_TYPES = dict(book='book', booklet='book', proceedings='book', mvbook='book',
              article='article', inproceedings='article', conference='article',
              incollection='article', inbook='article', phdthesis='thesis',
              mastersthesis='thesis', thesis='thesis', techreport='report',
              report='report', online='webpage', www='webpage', electronic='webpage',
              misc='other', unpublished='other')
_HANDLED = frozenset(('title', 'author', 'year', 'date', 'journal', 'journaltitle',
                      'booktitle', 'publisher', 'volume', 'number', 'issue', 'pages',
                      'doi', 'isbn', 'url', 'abstract', 'note', 'annote', 'keywords',
                      'crossref'))
_INHERIT = frozenset(('year', 'date', 'publisher', 'volume', 'number', 'issue',
                      'journal', 'journaltitle', 'booktitle'))
# Unknown or active commands remain literal instead of being silently discarded.
_TEXT_COMMANDS = frozenset(("'", '`', '"', '^', '~', '=', '.', 'u', 'v', 'H',
    'c', 'd', 'b', 'r', 'k', 't', 'i', 'j', 'l', 'L', 'o', 'O', 'ae', 'AE',
    'oe', 'OE', 'aa', 'AA', 'ss', 'SS', '%', '&', '_', '#', '{', '}', '$',
    ' ', ',', ';', ':', '!', '/', 'textit', 'textbf', 'emph', 'textsc',
    'textrm', 'textnormal', 'texttt', 'textsf', 'textsl', 'textup',
    'textbackslash', 'textasciitilde', 'textasciicircum', 'textendash',
    'textemdash', 'textquotedbl', 'textquotedblleft', 'textquotedblright',
    'textquoteleft', 'textquoteright', 'textdegree', 'textregistered',
    'textcopyright', 'copyright', 'LaTeX', 'TeX', 'BibTeX', 'url'))
_quiet_logs = ContextVar('carr_bibtex_import_quiet_logs', default=False)


class _InputLogFilter(logging.Filter):
    def filter(self, record):
        # Upstream diagnostics can contain user field contents. Suppress only
        # this import's context, without changing other threads' log levels.
        return not _quiet_logs.get()


@lru_cache(maxsize=1)
def _upstream():
    try:
        import bibtexparser
        from bibtexparser import model
        from bibtexparser.middlewares.names import (parse_single_name_into_parts,
                                                    split_multiple_persons_names)
        from pylatexenc.latex2text import (LatexNodes2Text, MacroTextSpec,
                                          get_default_latex_context_db)
    except ImportError as exc:
        raise ValueError('BibTeX import requires the installed bibliography components '
                         '(bibtexparser 2.1.0 and pylatexenc 2.11).') from exc
    if getattr(bibtexparser, '__version__', None) != '2.1.0':
        raise ValueError('This BibTeX importer requires bibtexparser 2.1.0.')
    for name in ('bibtexparser.splitter', 'bibtexparser.library',
                 'pylatexenc.latexwalker', 'pylatexenc.latex2text'):
        logging.getLogger(name).addFilter(_InputLogFilter())

    class TextOnlyDecoder(LatexNodes2Text):
        def read_input_file(self, filename):
            raise ValueError('File inclusion is unavailable in bibliography import.')

        def set_tex_input_directory(self, *args, **kwargs):
            raise ValueError('File inclusion is unavailable in bibliography import.')

    context = get_default_latex_context_db()
    context.add_context_category('carr-bibliography-text', prepend=True, macros=[
        MacroTextSpec('LaTeX', simplify_repl='LaTeX'),
        MacroTextSpec('TeX', simplify_repl='TeX'),
        MacroTextSpec('BibTeX', simplify_repl='BibTeX'),
        MacroTextSpec('url', simplify_repl='%s'),
    ])
    return (bibtexparser, model, split_multiple_persons_names,
            parse_single_name_into_parts, TextOnlyDecoder, context)


def _preflight(text):
    if type(text) is not str or not text.strip():
        raise ValueError('Choose a nonempty UTF-8 BibTeX bibliography.')
    if len(text) > MAX_BYTES or len(text.encode('utf-8', errors='strict')) > MAX_BYTES:
        raise ValueError('BibTeX input exceeds the 8 MiB import budget.')
    if any((unicodedata.category(c) in ('Cs', 'Cc') and c not in '\t\r\n')
           or c in _BIDI for c in text):
        raise ValueError('BibTeX contains unsupported control characters.')
    depth = 0
    escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '{':
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError('BibTeX brace nesting exceeds the import budget.')
        elif char == '}':
            depth = max(0, depth - 1)
    if sum(1 for _ in re.finditer(r'@[\w]*[ \t]*(?=[{(])', text)) > MAX_BLOCKS:
        raise ValueError('Too many BibTeX blocks in one import.')


def _key(value, label):
    if (type(value) is not str or not 1 <= len(value) <= 512
            or any(c.isspace() or c in '{}(),"#%=' for c in value)):
        raise ValueError(f'Invalid BibTeX {label}.')
    return value


def _literal(expression, start):
    """Return one braced/quoted component, without its outer delimiter."""
    opener = expression[start]
    depth, index = (1 if opener == '{' else 0), start + 1
    while index < len(expression):
        char = expression[index]
        if char == '\\':
            index += 2
            continue
        if char == '{':
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError('BibTeX value nesting exceeds the import budget.')
        elif char == '}':
            depth -= 1
            if opener == '{' and depth == 0:
                return expression[start + 1:index], index + 1
            if depth < 0:
                raise ValueError('Unbalanced braces in a BibTeX value.')
        elif char == '"' and opener == '"' and depth == 0:
            return expression[start + 1:index], index + 1
        index += 1
    raise ValueError('Unclosed BibTeX value.')


class _Expressions:
    def __init__(self, definitions):
        self.definitions = definitions
        self.cache = {}
        self.depths = {}
        self.total = 0

    def symbol(self, key, stack):
        key = key.lower()
        if key not in self.definitions:
            if key in _MONTHS:
                # Built-in months are constant values, like braced literals;
                # only explicit @string definitions add a dependency level.
                return _MONTHS[key], frozenset()
            raise ValueError(f'Undefined BibTeX string: {key}. Quote literal text in braces.')
        if key in stack or len(stack) >= MAX_MACRO_DEPTH:
            raise ValueError('Cyclic or excessively nested BibTeX string definition.')
        if key in self.cache:
            if len(stack) + self.depths[key] > MAX_MACRO_DEPTH:
                raise ValueError('Cyclic or excessively nested BibTeX string definition.')
            return self.cache[key]
        expression = self.definitions[key].value.rstrip()
        # BibTeX permits one optional trailing comma in @string blocks; the
        # upstream structural parser retains that comma in String.value.
        if expression.endswith(','):
            expression = expression[:-1]
        value, used = self.resolve(expression, (*stack, key))
        # A cached expansion still carries its full dependency depth. Otherwise
        # defining parents first lets arbitrarily long chains appear shallow.
        depth = 1 + max((self.depths[name] for name in used), default=0)
        if len(stack) + depth > MAX_MACRO_DEPTH:
            raise ValueError('Cyclic or excessively nested BibTeX string definition.')
        result = value, used | {key}
        self.cache[key] = result
        self.depths[key] = depth
        return result

    def resolve(self, expression, stack=()):
        if type(expression) is not str or not expression.strip() or len(expression) > MAX_VALUE:
            raise ValueError('Empty or oversized BibTeX value expression.')
        chunks, used, index, size = [], set(), 0, 0
        while index < len(expression):
            while index < len(expression) and expression[index].isspace():
                index += 1
            if index == len(expression):
                raise ValueError('Missing value after BibTeX concatenation operator.')
            if expression[index] in '{"':
                value, index = _literal(expression, index)
            else:
                start = index
                while index < len(expression) and not expression[index].isspace() and expression[index] != '#':
                    index += 1
                token = expression[start:index]
                if token.isascii() and token.isdecimal():
                    value = token
                elif _MACRO.fullmatch(token):
                    value, dependencies = self.symbol(token, stack)
                    used.update(dependencies)
                else:
                    raise ValueError('Invalid BibTeX string, number, or concatenation expression.')
            size += len(value)
            if size > MAX_VALUE:
                raise ValueError('Expanded BibTeX value exceeds the import budget.')
            chunks.append(value)
            while index < len(expression) and expression[index].isspace():
                index += 1
            if index == len(expression):
                break
            if expression[index] != '#':
                raise ValueError('Expected # between BibTeX value components.')
            index += 1
            if index == len(expression):
                raise ValueError('Missing value after BibTeX concatenation operator.')
        result = ''.join(chunks)
        self.total += len(result.encode('utf-8'))
        if self.total > MAX_BYTES:
            raise ValueError('Total expanded BibTeX metadata exceeds the import budget.')
        return result, frozenset(used)


def _decode(value, field, warnings, decoder, *, multiline=False):
    commands = set(_COMMAND.findall(value))
    unsupported = commands - _TEXT_COMMANDS
    if unsupported:
        warnings.append(f'Unexpanded TeX in {field} remains literal: '
                        + ', '.join('\\' + name for name in sorted(unsupported)) + '.')
        result = value
    else:
        try:
            result = decoder.latex_to_text(value, tolerant_parsing=False, strict_braces=True)
        except (ValueError, TypeError, RecursionError):
            warnings.append(f'Unrecognized TeX syntax in {field} is retained literally.')
            result = value
    if type(result) is not str or len(result) > MAX_VALUE:
        raise ValueError(f'Expanded BibTeX {field} exceeds the import budget.')
    return result.strip() if multiline else ' '.join(result.split())


def _authors(value, warnings, decoder, split_names, split_parts, *, literal_names=False):
    if not value.strip():
        return ()
    names = split_names(value)
    if len(names) > MAX_AUTHORS or any(not name.strip() for name in names):
        raise ValueError('Invalid or excessive BibTeX author list.')
    result = []
    for name in names:
        name = name.strip()
        if re.search(r'(?i)(?:^and(?:\s|$)|(?:^|\s)and$)', name):
            raise ValueError('An imported BibTeX author list has a missing name around and.')
        corporate = False
        if name.startswith('{'):
            content, end = _literal(name, 0)
            corporate = end == len(name)
        if corporate:
            display = _decode(content, 'corporate author', warnings, decoder)
            if not literal_names:
                display = '{' + display + '}'
        else:
            parts = split_parts(name, strict=True)
            if ',' in name:
                # BibTeX comma order is Family, Suffix, Given; the application's
                # explicit display convention is Family, Given, Suffix.
                # Decode all positions before deciding whether one is empty.
                # Dropping a missing family/given component would promote a
                # suffix or given name into a different identity field.
                family, given, suffix = (_decode(part, 'author', warnings, decoder) for part in (
                    ' '.join(parts.von + parts.last), ' '.join(parts.first), ' '.join(parts.jr)))
                if not family or not given:
                    display = _decode(name, 'author', warnings, decoder)
                    if not literal_names:
                        display = '{' + display + '}'
                    warnings.append('Incomplete comma-separated person name is retained literally; '
                                    'family, given name and suffix were not reassigned. Review the original entry.')
                elif any(',' in part for part in (family, given, suffix)):
                    display = _decode(name, 'author', warnings, decoder)
                    if not literal_names:
                        display = '{' + display + '}'
                    warnings.append('Protected punctuation in a person name is kept as a literal name '
                                    'to avoid guessing given names or suffixes.')
                else:
                    # An empty optional suffix is harmless after the two
                    # required personal-name components have been confirmed.
                    display = ', '.join((family, given, suffix) if suffix else (family, given))
            else:
                display = _decode(name, 'author', warnings, decoder)
        if not display or (not literal_names and display == '{}'):
            raise ValueError('An imported BibTeX author is empty.')
        result.append(display)
    warnings.append('Author names remain display text. Explicit comma names use Family, Given, '
                    + ('Suffix; corporate and protected names remain literal text. Review name formatting.'
                       if literal_names else
                       'Suffix; outer braces mark a literal corporate or protected name. Review name formatting.'))
    if any(name.lower() == 'others' for name in names):
        warnings.append('The BibTeX author marker others is retained literally; review abbreviated authorship.')
    return tuple(result)


def _provenance(key, records, definitions):
    entry, fields, used, _ = records[key]
    used = set(used)
    content = ['BibTeX import provenance', 'Citation key: ' + key,
               'Original entry (inert text):', entry.raw or '']
    while fields.get('crossref', '').strip():
        parent_key = fields['crossref'].strip()
        parent, fields, parent_used, _ = records[parent_key]
        content.extend(('Referenced entry (inert text):', parent.raw or ''))
        used.update(parent_used)
    if used:
        content.append('Used string definitions (inert text):')
        content.extend(definitions[name].raw or '' for name in definitions if name in used)
    return '\n'.join(content)


def _keywords(value, warnings, decoder):
    parts, depth, start, index = [], 0, 0, 0
    while index < len(value):
        char = value[index]
        if char == '\\':
            index += 2
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
        elif char in ',;' and depth == 0:
            parts.append(value[start:index])
            start = index + 1
        index += 1
    parts.append(value[start:])
    tags = tuple(dict.fromkeys(item for item in
        (_decode(part, 'keywords', warnings, decoder) for part in parts) if item))
    if len(tags) > 100:
        raise ValueError('A BibTeX reference contains more than 100 keywords.')
    return tags


def _inherit_records(records):
    completed = {}
    depths = {}

    def inherited(key, stack=()):
        if key in stack or len(stack) >= MAX_CROSSREF_DEPTH:
            raise ValueError('Cyclic or excessively nested BibTeX cross-reference.')
        if key in completed:
            if len(stack) + depths[key] > MAX_CROSSREF_DEPTH:
                raise ValueError('Cyclic or excessively nested BibTeX cross-reference.')
            return completed[key]
        entry, own, used, warnings = records[key]
        fields = dict(own)
        parent_key = fields.get('crossref', '').strip()
        depth = 1
        if parent_key:
            if parent_key not in records:
                raise ValueError(f'BibTeX cross-reference {parent_key} is missing from this file.')
            parent, _, _, _ = records[parent_key]
            parent_fields = inherited(parent_key, (*stack, key))
            depth += depths[parent_key]
            if len(stack) + depth > MAX_CROSSREF_DEPTH:
                raise ValueError('Cyclic or excessively nested BibTeX cross-reference.')
            copied = []
            for field in sorted(_INHERIT):
                if field not in fields and field in parent_fields:
                    fields[field] = parent_fields[field]
                    copied.append(field)
            if (entry.entry_type.lower() in ('inbook', 'incollection', 'inproceedings', 'conference')
                    and 'booktitle' not in fields and parent_fields.get('title')):
                fields['booktitle'] = parent_fields['title']
                copied.append('booktitle from parent title')
            warnings.append('Local cross-reference ' + parent.key + ': inherited '
                            + (', '.join(copied) if copied else 'no supported missing fields')
                            + '. Child title, authors, identifiers and page extent are not inferred.')
        completed[key] = fields
        depths[key] = depth
        return fields

    for key in records:
        inherited(key)
    return completed


def parse_bibtex(text, *, literal_names=False):
    """Parse one complete bounded metadata batch, or fail without partial rows.

    Braces/quotes, numeric fields, case-insensitive strings, concatenation and
    local crossrefs are supported. Crossrefs inherit only documented container
    fields, never child titles/authors/page extents. Preambles are inert and
    ignored with a warning. TeX text accents use pylatexenc; unknown commands
    remain literal. Original entry/macro text is preserved in research notes.
    ``literal_names=True`` omits only parser-generated name markers for a caller
    that stores every author literally; decoded literal braces remain intact.
    """
    if type(literal_names) is not bool:
        raise ValueError('The BibTeX literal-name policy must be boolean.')
    _preflight(text)
    parser, model, split_names, split_parts, decoder_type, context = _upstream()
    token = _quiet_logs.set(True)
    try:
        try:
            library = parser.parse_string(text, parse_stack=[])
        except Exception as exc:
            raise ValueError('Malformed BibTeX bibliography; no references imported.') from exc
        if library.failed_blocks:
            block = library.failed_blocks[0]
            line = (block.start_line or 0) + 1
            raise ValueError(f'Malformed or duplicate BibTeX block near line {line}; no references imported.')
        if len(library.blocks) > MAX_BLOCKS or not 1 <= len(library.entries) <= MAX_RECORDS:
            raise ValueError('Import between 1 and 2,000 BibTeX references at a time.')
        if len(library.strings) > MAX_STRINGS:
            raise ValueError('Too many BibTeX string definitions.')
        for block in library.blocks:
            expected = ('comment' if isinstance(block, model.ExplicitComment) else
                        'preamble' if isinstance(block, model.Preamble) else
                        'string' if isinstance(block, model.String) else None)
            if expected:
                header = re.match(r'\s*@([\w]+)', block.raw or '')
                if not header or header[1].lower() != expected:
                    raise ValueError('Unsupported BibTeX block type resembling a reserved directive.')
        definitions = {}
        for string in library.strings:
            key = _key(string.key, 'string key').lower()
            if not _MACRO.fullmatch(key) or key in definitions:
                raise ValueError('Duplicate or invalid BibTeX string definition.')
            definitions[key] = string
        expressions = _Expressions(definitions)
        for key in definitions:
            expressions.symbol(key, ())
        general = []
        if library.preambles:
            general.append('BibTeX preambles are ignored and never executed; no TeX definitions are installed.')
        if library.comments:
            general.append('BibTeX comments are not copied into reference fields.')
        for comment in library.comments:
            if (isinstance(comment, model.ImplicitComment)
                    and re.search(r'(?m)^\s*@\w+', comment.comment)):
                raise ValueError('Malformed BibTeX block was found outside a complete entry.')
        records, folded = {}, set()
        for entry in library.entries:
            key = _key(entry.key, 'citation key')
            if not _FIELD.fullmatch(entry.entry_type) or len(entry.entry_type) > 128:
                raise ValueError('Invalid BibTeX entry type.')
            if key.casefold() in folded:
                raise ValueError('Duplicate BibTeX citation key (including letter-case variants).')
            folded.add(key.casefold())
            if not 1 <= len(entry.fields) <= MAX_FIELDS:
                raise ValueError(f'BibTeX reference {key} has no fields or too many fields.')
            fields, used = {}, set()
            for field in entry.fields:
                name = field.key.lower()
                if not _FIELD.fullmatch(name) or len(name) > 128 or name in fields:
                    raise ValueError(f'Duplicate or invalid BibTeX field in reference {key}.')
                fields[name], dependencies = expressions.resolve(field.value)
                used.update(dependencies)
            records[key] = (entry, fields, used, list(general))
        inherited = _inherit_records(records)
        decoder = decoder_type(latex_context=context, math_mode='verbatim', keep_comments=True)
        results, output_bytes = [], 0
        for key, (entry, own, used, warnings) in records.items():
            fields = inherited[key]
            kind = entry.entry_type.lower()
            if kind not in _TYPES or kind in ('inbook', 'incollection', 'inproceedings', 'conference'):
                warnings.append(f'BibTeX type {kind} is imported as {_TYPES.get(kind, "other")}; '
                                'the original type is retained in provenance notes.')
            unknown = set(fields) - _HANDLED
            if unknown:
                warnings.append('BibTeX fields retained only in provenance notes: '
                                + ', '.join(sorted(unknown)) + '.')
            if unknown & {'file', 'files', 'attachment', 'attachments', 'pdf', 'path', 'localfile'}:
                warnings.append('Attachment paths remain inert provenance text; no file is opened.')

            def value(name, *, multiline=False):
                return _decode(fields.get(name, ''), name, warnings, decoder, multiline=multiline)

            year = value('year')
            date = value('date')
            if date:
                match = re.fullmatch(r'([0-9]{4})(?:-[0-9]{2}(?:-[0-9]{2})?)?', date)
                if match:
                    if year and year != match[1]:
                        raise ValueError(f'Conflicting BibTeX year and date in reference {key}.')
                    year = year or match[1]
                    if date != year:
                        warnings.append('Publication date is imported as its year; full date remains in provenance notes.')
                elif not year:
                    warnings.append('Unrecognized date remains in provenance notes; publication year is left empty.')
            containers = [value(name) for name in ('journal', 'journaltitle', 'booktitle') if fields.get(name)]
            containers = list(dict.fromkeys(containers))
            if len(containers) > 1:
                warnings.append('First supplied container title is used; alternatives remain in provenance notes.')
            issue = value('number') or value('issue')
            if fields.get('number') and fields.get('issue') and value('number') != value('issue'):
                raise ValueError(f'Conflicting BibTeX number and issue in reference {key}.')
            tags = _keywords(fields.get('keywords', ''), warnings, decoder)
            note_text = '\n\n'.join(part for part in (value('note', multiline=True), value('annote', multiline=True)) if part)
            provenance = _provenance(key, records, definitions)
            notes = '\n\n'.join(part for part in (note_text, provenance) if part)
            if len(notes) > MAX_NOTES:
                raise ValueError(f'BibTeX provenance for reference {key} exceeds the note budget; no text was truncated.')
            warnings.append('Citation key and original BibTeX are retained in research notes, not used as '
                            'the application reference ID. Exports omitting notes omit this provenance.')
            values = dict(title=value('title'), authors=_authors(fields.get('author', ''),
                warnings, decoder, split_names, split_parts, literal_names=literal_names),
                item_type=_TYPES.get(kind, 'other'),
                year=year, container_title=containers[0] if containers else '',
                publisher=value('publisher'), volume=value('volume'), issue=issue,
                pages=value('pages'), doi=value('doi'), isbn=value('isbn'), url=value('url'),
                abstract=value('abstract', multiline=True), notes=notes, tags=tags)
            warnings = tuple(dict.fromkeys(warnings))
            for item in (*values.values(), *warnings):
                for value_text in ((item,) if isinstance(item, str) else item):
                    output_bytes += len(value_text.encode('utf-8'))
            if output_bytes > MAX_BYTES:
                raise ValueError('Expanded BibTeX references and provenance exceed the import budget.')
            results.append((values, warnings))
        return results
    except (RecursionError, UnicodeError) as exc:
        raise ValueError('Invalid encoding or excessive nesting in BibTeX metadata.') from exc
    finally:
        _quiet_logs.reset(token)
