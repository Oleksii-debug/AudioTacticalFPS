"""Bounded optional legacy reading adapters; native qualification is separate."""
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
from tempfile import TemporaryFile
import time
from urllib.parse import unquote, urlsplit

from ..audio.jobs import checked_directory, regular_file, verified_reader, file_identity
from ..extract.document_formats import extract_document
from .legacy_runtime import admitted_runtime
from .models import ConversionError, ConversionUnavailable
from .office import _stage_usage

CALIBRE_SUFFIXES = frozenset(('.azw', '.azw3', '.lit', '.mobi', '.prc', '.tcr'))
DJVU_SUFFIXES = frozenset(('.djvu',))
CHM_SUFFIXES = frozenset(('.chm',))
LEGACY_SUFFIXES = CALIBRE_SUFFIXES | DJVU_SUFFIXES | CHM_SUFFIXES
# Qualified only by the recorded source-bound Windows fixture population.
# DjVu ordinary path: run 46024229261 at 684cc8ddac569fdcf5d685b33302f570481ea6f8.
# Calibre 9.19.0: six unprotected synthetic formats and three refusal/budget/cancel
# cases passed both populations in run 46026674640 at 779967. This candidate
# additionally requires the same native fixtures through ordinary admission.
QUALIFIED_SUFFIXES = DJVU_SUFFIXES | CALIBRE_SUFFIXES
MAX_MEMBERS = 2048
MAX_MEMBER_BYTES = 8 * 1024**2


def kind_for(suffix):
    return 'calibre' if suffix in CALIBRE_SUFFIXES else 'djvu' if suffix in DJVU_SUFFIXES else 'chm' if suffix in CHM_SUFFIXES else None


def _remaining(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('Selected legacy conversion exceeded its time budget.')


def _run(args, stage, deadline, *, maximum, stage_limit, output=None, environment=None):
    env = {key: value for key, value in os.environ.items() if key in ('SYSTEMROOT', 'WINDIR', 'LANG')}
    env['PATH'] = os.pathsep.join((str(Path(args[0]).parent),
        str(Path(env.get('SYSTEMROOT', 'C:/Windows')) / 'System32'))) if os.name == 'nt' else os.defpath
    env.update(TMPDIR=str(stage), TEMP=str(stage), TMP=str(stage), PYTHONDONTWRITEBYTECODE='1')
    if environment:
        env.update(environment)
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        _remaining(deadline)
        _stage_usage(stage, stage_limit, deadline)
        process = subprocess.Popen([str(value) for value in args], cwd=stage, env=env,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
            start_new_session=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while True:
                _remaining(deadline)
                if os.fstat(stdout.fileno()).st_size > maximum or os.fstat(stderr.fileno()).st_size > 256 * 1024:
                    raise ConversionError('Selected legacy tool exceeded its output or diagnostic budget.')
                if output is not None and os.path.lexists(output) and regular_file(output).st_size > maximum:
                    raise ConversionError('Selected legacy reading output exceeds its byte budget.')
                _stage_usage(stage, stage_limit, deadline)
                if process.poll() is not None:
                    break
                time.sleep(.02)
            if process.returncode:
                raise ConversionUnavailable('Selected legacy tool failed its fixed operation (exit ' + str(process.returncode) + ').')
            stdout.seek(0)
            return stdout.read(maximum + 1)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _text(raw):
    try:
        text = raw.decode('utf-8', 'strict')
    except UnicodeError:
        raise ConversionError('Selected legacy tool did not produce strict UTF-8 text.') from None
    if '\0' in text:
        raise ConversionError('Selected legacy tool produced unsupported NUL text.')
    return text


def _calibre_header(data, suffix, maximum_text=32 * 1024**2):
    if suffix == '.tcr':
        if not data.startswith(b'!!8-Bit!!'):
            raise ConversionError('TCR input lacks its real compression header.')
        position, lengths = 9, []
        for _ in range(256):
            if position >= len(data):
                raise ConversionError('TCR compression dictionary is truncated.')
            length = data[position]
            position += 1 + length
            if position > len(data):
                raise ConversionError('TCR compression dictionary exceeds the source.')
            lengths.append(length)
        expanded = 0
        for code in data[position:]:
            expanded += lengths[code]
            if expanded > maximum_text:
                raise ConversionError('TCR expanded text exceeds the selected byte budget.')
        if expanded == 0:
            raise ConversionError('TCR contains no readable expanded text.')
        return
    if suffix == '.lit':
        if len(data) < 40 or data[:8] != b'ITOLITLS' or struct.unpack_from('<I', data, 8)[0] != 1:
            raise ConversionError('LIT input lacks its supported container header.')
        if b'/DRMStorage/Licenses/EUL' in data or b'/DRMStorage/DRMBookplate' in data:
            raise ConversionUnavailable('Protected or personalized LIT inputs are not supported.')
        return
    if len(data) < 86 or data[60:68] not in (b'BOOKMOBI', b'TEXtREAd'):
        raise ConversionError('Only actual MOBI/KF8 or TextRead Palm containers are admitted.')
    if data[60:68] == b'TEXtREAd' and suffix not in ('.mobi', '.prc'):
        raise ConversionError('This extension requires an actual MOBI/KF8 container.')
    count = struct.unpack_from('>H', data, 76)[0]
    if not 1 <= count <= 8192 or 78 + 8 * count > len(data):
        raise ConversionError('Invalid bounded Palm record inventory.')
    offsets = [struct.unpack_from('>I', data, 78 + 8 * index)[0] for index in range(count)]
    if (offsets != sorted(set(offsets)) or offsets[0] < 78 + 8 * count
            or offsets[-1] >= len(data) or offsets[0] + 16 > (offsets[1] if count > 1 else len(data))):
        raise ConversionError('Palm record offsets are invalid.')
    if struct.unpack_from('>I', data, offsets[0] + 4)[0] > maximum_text:
        raise ConversionError('MOBI/Palm declared text exceeds the selected byte budget.')
    if struct.unpack_from('>H', data, offsets[0] + 12)[0] != 0:
        raise ConversionUnavailable('Encrypted MOBI/KF8/Palm content is not supported.')


def _calibre(source, data, suffix, budget, deadline, selection):
    _calibre_header(data, suffix, budget.max_text_bytes)
    stage = source.parent
    output, script = stage / 'calibre.txt', stage / 'calibre_guard.py'
    for name in ('calibre-config', 'calibre-cache', 'calibre-temp'):
        (stage / name).mkdir(mode=0o700)
    helper = Path(__file__).with_name('calibre_guard.py')
    with verified_reader(helper) as read:
        raw_script = read.read(64 * 1024 + 1)
    if len(raw_script) > 64 * 1024:
        raise ConversionError('Fixed calibre helper exceeds its byte budget.')
    with script.open('xb') as stream:
        stream.write(raw_script)
    with output.open('xb'):
        pass
    with admitted_runtime(selection, stage, 'calibre', deadline) as (executable, runtime, identity):
        debug = executable.with_name('calibre-debug.exe' if executable.suffix == '.exe' else 'calibre-debug')
        _run([debug, '--run-without-debug', '-e', script, '--', source, output, executable.parent,
              str(budget.max_text_bytes)], stage, deadline, maximum=budget.max_text_bytes,
             stage_limit=2 * budget.max_source_bytes + 64 * 1024**2, output=output,
             environment={
                 'CALIBRE_CONFIG_DIRECTORY': str(stage / 'calibre-config'),
                 'CALIBRE_CACHE_DIRECTORY': str(stage / 'calibre-cache'),
                 'CALIBRE_TEMP_DIR': str(stage / 'calibre-temp'),
                 'CALIBRE_ALLOW_PYTHON_TEMPLATES': '0',
                 'CALIBRE_NO_DEFAULT_PROGRAMS': '1',
                 'CALIBRE_NO_NATIVE_FILEDIALOGS': '1',
                 'QT_QPA_PLATFORM': 'offscreen',
             })
    info = regular_file(output)
    if info.st_nlink != 1 or not 0 < info.st_size <= budget.max_text_bytes:
        raise ConversionError('Calibre reading output is linked, empty or too large.')
    with verified_reader(output, expected_identity=file_identity(info)) as stream:
        raw = stream.read(budget.max_text_bytes + 1)
    text = _text(raw)
    records = [dict(raw_text=text, physical_index=None, status='review_required',
        logical_locator={'source_format': suffix, 'converter': 'calibre-9.19.0',
                         'selected_components_sha256': identity,
                         'fixed_helper_sha256': sha256(raw_script).hexdigest(),
                         'location_kind': 'converted_plain_text'})]
    return 'selected-calibre-' + suffix[1:], '\n\n', records, [
        'converted_layout_and_printed_pages_not_retained',
        'original_characters_and_reading_order_require_review',
        'unprotected_book_only_no_user_plugins',
        'network_and_external_programs_disabled_in_fixed_wrapper',
        'selected_native_tool_is_not_an_os_sandbox']


def _djvu_pages(data, maximum):
    if len(data) < 16 or data[:8] != b'AT&TFORM':
        raise ConversionError('DjVu input lacks its real IFF header.')
    declared = struct.unpack_from('>I', data, 8)[0]
    if declared < 4 or 12 + declared != len(data):
        raise ConversionError('DjVu top-level byte size is invalid.')
    form = data[12:16]
    if form not in (b'DJVU', b'DJVM'):
        raise ConversionError('Only standalone or bundled DjVu documents are admitted.')
    pages, entries, dirm = 0, 0, False
    pending = [(16, len(data), form, 0)]
    if form == b'DJVU':
        pages = 1
    while pending:
        position, end, parent, depth = pending.pop()
        while position < end:
            entries += 1
            if entries > 8192 or depth > 8 or position + 8 > end:
                raise ConversionError('DjVu chunk budget or framing is invalid.')
            tag = data[position:position + 4]
            size = struct.unpack_from('>I', data, position + 4)[0]
            start, stop = position + 8, position + 8 + size
            if stop > end:
                raise ConversionError('DjVu chunk exceeds its containing form.')
            if tag == b'INCL':
                # The selected subset never follows an indirect dictionary/file.
                # Shared-dictionary bundles require a separately reviewed resolver.
                raise ConversionUnavailable('DjVu indirect or shared-dictionary references are not enabled.')
            if tag == b'DIRM':
                if parent != b'DJVM' or dirm or not size or not data[start] & 0x80:
                    raise ConversionUnavailable('DjVu indirect documents are not enabled.')
                dirm = True
            if tag == b'FORM':
                if size < 4 or data[start:start + 4] not in (b'DJVU', b'DJVI'):
                    raise ConversionError('DjVu contains an unsupported nested form.')
                nested = data[start:start + 4]
                if nested == b'DJVU':
                    pages += 1
                pending.append((start + 4, stop, nested, depth + 1))
            # IFF aligns the next sibling, not the end of its containing FORM.
            # DjVuLibre closes an odd final chunk without an in-form pad byte.
            position = stop if stop == end else stop + (size & 1)
    if not 1 <= pages <= maximum or form == b'DJVM' and not dirm:
        raise ConversionError('DjVu page inventory is absent or exceeds its budget.')
    return pages


def _djvu(source, data, budget, deadline, selection):
    pages = _djvu_pages(data, min(budget.max_pages, budget.max_records))
    records, total = [], 0
    with admitted_runtime(selection, source.parent, 'djvu', deadline) as (executable, runtime, identity):
        companion = executable.with_name('djvused.exe' if executable.suffix == '.exe' else 'djvused')
        count = _run([companion, source, '-e', 'n'], source.parent, deadline,
                     maximum=256, stage_limit=2 * budget.max_source_bytes + 64 * 1024**2).strip()
        if not re.fullmatch(b'[1-9][0-9]{0,4}', count) or int(count) != pages:
            raise ConversionError('DjVu native page inventory disagrees with its bounded container.')
        for index in range(1, pages + 1):
            raw = _run([executable, '--page=' + str(index), source], source.parent, deadline,
                       maximum=budget.max_text_bytes - total,
                       stage_limit=2 * budget.max_source_bytes + 64 * 1024**2)
            total += len(raw)
            text = _text(raw)
            if not text.strip():
                raise ConversionUnavailable('A DjVu page lacks readable embedded text; this adapter does not invent OCR.')
            records.append(dict(raw_text=text, physical_index=None, status='review_required',
                logical_locator={'djvu_page': index, 'selected_components_sha256': identity,
                                 'text_source': 'embedded_text_layer'}))
    return 'selected-djvulibre-text', '\n\f\n', records, [
        'embedded_text_layer_not_verified', 'no_djvu_ocr_performed',
        'only_self_contained_pages_without_indirect_dictionary_references',
        'printed_pagination_not_reconstructed']


def _member(value):
    name = value.replace('\\', '/')
    if (not name or len(name) > 1024 or name.startswith('/') or ':' in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or any(part in ('', '.', '..') for part in name.split('/'))
            or any(part.rstrip(' .') != part for part in name.split('/'))):
        raise ConversionError('CHM contains an unsafe or ambiguous member path.')
    return name


def _chm_listing(raw, maximum):
    rows, aliases, total = [], set(), 0
    text = _text(raw).replace('\r\n', '\n').strip('\n')
    if not text:
        raise ConversionError('CHM member inventory is empty.')
    for block in text.split('\n\n'):
        fields = {}
        for line in block.splitlines():
            key, separator, value = line.partition(' = ')
            if (separator != ' = ' or key in fields or key not in {
                    'Path', 'Size', 'Packed Size', 'Method', 'Block', 'Folder',
                    'Attributes', 'Encrypted', 'CRC', 'Modified', 'Created', 'Accessed', 'Offset'}):
                raise ConversionError('CHM listing is ambiguous or uses unsupported fields: ' + repr(key[:80]))
            fields[key] = value
        if 'Path' not in fields or not re.fullmatch('[0-9]{1,12}', fields.get('Size', '')):
            raise ConversionError('CHM listing lacks a bounded member identity.')
        name, size = _member(fields['Path']), int(fields['Size'])
        if name.casefold() in aliases or len(rows) >= MAX_MEMBERS or fields.get('Encrypted', '-') != '-':
            raise ConversionError('CHM has ambiguous, encrypted or excessive members.')
        aliases.add(name.casefold())
        total += size
        if total > maximum or size > maximum:
            raise ConversionError('CHM expanded member inventory exceeds its byte budget.')
        rows.append(dict(name=name, bytes=size, directory=fields.get('Folder') == '+'))
    return rows


class _Toc(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 20000:
            raise ConversionError('CHM table of contents exceeds its node budget.')
        attrs = dict(attrs)
        if tag.casefold() == 'param' and attrs.get('name', '').casefold() == 'local':
            if len(self.links) >= MAX_MEMBERS:
                raise ConversionError('CHM table of contents exceeds its entry budget.')
            self.links.append(attrs.get('value', ''))



_LEGACY_HTML_DOCTYPES = {
    '-//W3C//DTD HTML 3.2 Final//EN': frozenset(('', 'http://www.w3.org/TR/REC-html32.dtd')),
    '-//W3C//DTD HTML 4.0 Transitional//EN': frozenset(('', 'http://www.w3.org/TR/REC-html40/loose.dtd')),
    '-//W3C//DTD HTML 4.01 Transitional//EN': frozenset(('', 'http://www.w3.org/TR/html4/loose.dtd')),
    '-//W3C//DTD HTML 4.01//EN': frozenset(('', 'http://www.w3.org/TR/html4/strict.dtd')),
}


def _chm_recovered_html(text, *, encoding, before, max_chars, deadline, notices):
    """Recover the admitted legacy end-token classes, then check inert text."""
    from lxml import etree
    from ..extract.document_formats_text import (
        _Budget, _HTMLPreview, _decode, _html_encoding, _CHUNK, _MAX_TOKEN, _MAX_DEPTH)

    class NoExternal(etree.Resolver):
        def resolve(self, url, public_id, context):
            raise ConversionError('CHM HTML recovery cannot resolve an external resource.')

    budget = _Budget(max_chars, None, deadline)
    budget.check()
    encoded = text.encode('utf-8', 'strict')
    if len(encoded) > MAX_MEMBER_BYTES:
        raise ConversionError('CHM HTML recovery exceeds its input byte budget.')
    # This is a bounded in-memory HTML parse, never a filename/URL, XML/DTD
    # parser, browser or XInclude operation. The original token pass has
    # already rejected unsupported declarations, attributes and controls.
    parser = etree.HTMLParser(encoding='utf-8', recover=True, no_network=True,
        huge_tree=False, remove_blank_text=False, remove_comments=False,
        remove_pis=False, collect_ids=False)
    parser.resolvers.add(NoExternal())
    for offset in range(0, len(encoded), _CHUNK):
        budget.check()
        parser.feed(encoded[offset:offset + _CHUNK])
    root = parser.close()
    budget.check()
    if root is None:
        raise ConversionError('CHM HTML recovery returned no complete document.')
    depth = 0
    for event, element in etree.iterwalk(root, events=('start', 'end', 'comment', 'pi')):
        budget.node()
        if event == 'start':
            depth += 1
            if depth > _MAX_DEPTH:
                raise ValueError('HTML nesting budget exceeded.')
        elif event == 'end':
            depth -= 1
    # Numeric references preserve non-ASCII characters without lying about a
    # previously admitted legacy charset. The declared PUBLIC HTML prefix is
    # ASCII-compatible, so UTF-16/32 documents never enter this recovery path.
    serialized = etree.tostring(root, method='html', encoding='ascii', with_tail=False)
    result = serialized.decode('ascii', 'strict').encode(encoding, 'strict')
    if len(result) > MAX_MEMBER_BYTES:
        raise ConversionError('CHM HTML recovery exceeds its output byte budget.')
    budget.check()

    checked = _Budget(max_chars, None, deadline)
    reading = _decode(result, checked, _html_encoding(result))
    strict = _HTMLPreview(checked, encoding=next(iter(checked.encodings)))
    for offset in range(0, len(reading), _CHUNK):
        checked.check()
        strict.feed(reading[offset:offset + _CHUNK])
        if len(strict.rawdata) > _MAX_TOKEN:
            raise ValueError('HTML token budget exceeded.')
    strict.finish()

    def visible_order(records):
        digest = sha256()
        count = 0
        for record in records:
            checked.check()
            for token in re.finditer(r'\S+', record['raw_text']):
                checked.check()
                value = token.group().encode('utf-8', 'strict')
                digest.update(len(value).to_bytes(8, 'big'))
                digest.update(value)
                count += 1
        return count, digest.digest()

    # libxml2 recovery may discard broken content. It may not silently drop,
    # add, reorder or expose visible words here. Only inert layout/whitespace
    # boundaries can differ; original bytes and both member hashes remain.
    if visible_order(before.records) != visible_order(checked.records):
        raise ConversionError('CHM HTML recovery changed the ordered visible text.')
    checked.check()
    if notices is not None:
        for warning in sorted(before.warnings | {'legacy_html_recovered_with_bounded_lxml'}):
            if warning not in notices:
                notices.append(warning)
    return result


def _chm_paragraph_ends(raw, *, max_chars, deadline, notices):
    """Admit known legacy end-token repairs through the current inert reader.

    Empty orphan paragraphs retain the existing exact normalization. Other
    observed harmless end-token classes use lxml HTML recovery only after a
    complete strict-budget/suppression pass, followed by strict inert reading
    and equality of the ordered visible words. General HTML stays unchanged.
    """
    from ..extract.document_formats_text import (
        _Budget, _HTMLPreview, _decode, _html_encoding, _CHUNK, _MAX_TOKEN, _MAX_DEPTH)
    if not 0 < len(raw) <= MAX_MEMBER_BYTES:
        raise ConversionError('CHM HTML normalization exceeds its source byte budget.')
    budget = _Budget(max_chars, None, deadline)
    text = _decode(raw, budget, _html_encoding(raw))
    encoding = next(iter(budget.encodings))
    end_token = re.compile(r'</[ \t\r\n]*p[ \t\r\n]*>', re.IGNORECASE)
    # Complete publisher census: unmatched anchors/bold/list-definition/table
    # headers, an IL typo and two otherwise empty trailing lists. Other
    # malformed structures and any malformed end-token syntax stay refused.
    orphan_ends = frozenset(('a', 'b', 'li', 'dt', 'th', 'il'))

    class ParagraphEnds(_HTMLPreview):
        def __init__(self):
            super().__init__(budget, encoding=encoding)
            self.insertions = []
            self.description_ends = []
            self.entity_replacements = []
            self.recovery = False
            self.line_number = 1
            self.line_start = 0

        def _offset(self):
            line, column = self.getpos()
            while self.line_number < line:
                budget.check()
                self.line_start = text.index('\n', self.line_start) + 1
                self.line_number += 1
            return self.line_start + column


        def handle_starttag(self, tag, attrs):
            if (tag == 'dd' and self.stack and self.stack[-1][0] == 'dd'
                    and any(item[0] == 'body' for item in self.stack)):
                # Materialize only the current reader's already-supported
                # same-tag optional closure. libxml otherwise nests these DDs
                # and emits an extra closing tag rejected by the strict pass.
                self.description_ends.append(self._offset())
                self.recovery = True
            return super().handle_starttag(tag, attrs)

        def handle_entityref(self, name):
            from html import unescape
            budget.node()
            offset = self._offset()
            end = offset + len(name) + 1
            if len(name) > _MAX_TOKEN or text[offset:end] != '&' + name:
                raise ValueError('Unsupported legacy named character reference.')
            terminated = text[end:end + 1] == ';'
            if terminated:
                end += 1
            # HTMLParser's callback omits whether the source had a semicolon.
            # Preserve that exact lexeme: appending one to "&ltswitch" invents
            # a literal semicolon after the valid HTML "&lt" prefix.
            value = unescape(text[offset:end])
            self.handle_data(value)
            if not terminated:
                # Numeric references cannot become markup when the normalized
                # reading copy later enters libxml and the unchanged reader.
                canonical = ''.join('&#' + str(ord(char)) + ';' for char in value)
                self.entity_replacements.append((offset, end, canonical))
                self.recovery = True

        def handle_endtag(self, tag):
            in_body = any(item[0] == 'body' for item in self.stack)
            if (tag == 'p' and in_body
                    and not any(item[0] == 'p' for item in self.stack)):
                offset = self._offset()
                if end_token.match(text, offset) is None:
                    raise ValueError('Unsupported legacy paragraph end token.')
                self.insertions.append(offset)
                # The synthetic empty p inherits the current suppression.
                budget.node()
                self._flush()
                self.stack.append(('p', bool(self.stack and self.stack[-1][1])))
                if len(self.stack) > _MAX_DEPTH:
                    raise ValueError('HTML nesting budget exceeded.')
            try:
                return super().handle_endtag(tag)
            except ValueError as error:
                if str(error) != 'HTML element nesting is malformed.' or not in_body:
                    raise
                offset = self._offset()
                token = re.compile(r'</[ \t\r\n]*' + re.escape(tag) + r'[ \t\r\n]*>',
                                   re.IGNORECASE)
                if token.match(text, offset) is None:
                    raise
                if tag in orphan_ends and not any(item[0] == tag for item in self.stack):
                    self.recovery = True
                    return
                if (tag == 'body' and self.stack[-1][0] == 'ul'
                        and re.search(r'<ul[ \t\r\n]*>[ \t\r\n]*\Z',
                            text[max(0, offset - _MAX_TOKEN):offset], re.IGNORECASE)):
                    self.stack.pop()
                    self.recovery = True
                    return super().handle_endtag(tag)
                raise

    parser = ParagraphEnds()
    for start in range(0, len(text), _CHUNK):
        budget.check()
        parser.feed(text[start:start + _CHUNK])
        if len(parser.rawdata) > _MAX_TOKEN:
            raise ValueError('HTML token budget exceeded.')
    parser.finish()
    budget.check()
    if not parser.insertions and not parser.recovery:
        return raw
    pieces, previous = [], 0
    edits = ([(offset, offset, '<p>') for offset in parser.insertions]
             + [(offset, offset, '</dd>') for offset in parser.description_ends]
             + parser.entity_replacements)
    for start, end, replacement in sorted(edits):
        budget.check()
        if start < previous:
            raise ConversionError('CHM HTML normalization has overlapping source edits.')
        pieces.extend((text[previous:start], replacement))
        previous = end
    pieces.append(text[previous:])
    normalized = ''.join(pieces)
    result = normalized.encode(encoding, 'strict')
    if len(result) > MAX_MEMBER_BYTES:
        raise ConversionError('CHM HTML normalization exceeds its output byte budget.')
    budget.check()
    warning = 'legacy_html_orphan_paragraph_end_normalized'
    if parser.insertions and notices is not None and warning not in notices:
        notices.append(warning)
    if parser.description_ends:
        budget.warnings.add('legacy_html_implicit_description_end_materialized')
    if parser.entity_replacements:
        budget.warnings.add('legacy_html_unterminated_named_reference_normalized')
    if parser.recovery:
        return _chm_recovered_html(normalized, encoding=encoding, before=budget,
            max_chars=max_chars, deadline=deadline, notices=notices)
    return result


def _chm_html(raw, *, max_chars=10_000_000, deadline=None, notices=None):
    """Normalize declared legacy HTML only; never open or resolve its DTD."""
    pattern = (rb"""\A(?P<prefix>(?:\xef\xbb\xbf)?[ \t\r\n]*)<!DOCTYPE[ \t\r\n]+HTML"""
        rb"""[ \t\r\n]+PUBLIC[ \t\r\n]+(?P<q>['"])(?P<public>[^'"<>\[\]\r\n]{1,100})(?P=q)"""
        rb"""(?:[ \t\r\n]+(?P<sq>['"])(?P<system>[^'"<>\[\]\r\n]{1,120})(?P=sq))?[ \t\r\n]*>""")
    match = re.match(pattern, raw, flags=re.IGNORECASE)
    if match is None:
        return raw
    public = match.group('public').decode('ascii', 'strict')
    system = (match.group('system') or b'').decode('ascii', 'strict')
    if system not in _LEGACY_HTML_DOCTYPES.get(public, ()):
        return raw
    reading = match.group('prefix') + b'<!DOCTYPE html>' + raw[match.end():]
    return _chm_paragraph_ends(reading, max_chars=max_chars, deadline=deadline, notices=notices)


def _chm(source, data, budget, deadline, selection):
    if len(data) < 96 or data[:8] != b'ITSF\x03\0\0\0':
        raise ConversionError('Only actual CHM version-3 containers are admitted.')
    maximum = min(128 * 1024**2, 2 * budget.max_source_bytes)
    with admitted_runtime(selection, source.parent, 'chm', deadline) as (executable, runtime, identity):
        listing = _run([executable, 'l', '-slt', '-ba', '-sccUTF-8', '-tchm', '--', source],
            source.parent, deadline, maximum=1024**2, stage_limit=2 * budget.max_source_bytes + 64 * 1024**2)
        rows = _chm_listing(listing, maximum)
        html = {row['name']: row for row in rows if not row['directory']
                and PurePosixPath(row['name']).suffix.casefold() in ('.html', '.htm', '.xhtml')}
        tocs = [row for row in rows if not row['directory'] and row['name'].casefold().endswith('.hhc')]
        if not html or len(html) > min(budget.max_pages, MAX_MEMBERS) or len(tocs) > 1:
            raise ConversionUnavailable('CHM requires a bounded unambiguous static HTML reading inventory.')

        def read_member(row):
            if not 0 < row['bytes'] <= MAX_MEMBER_BYTES:
                raise ConversionError('CHM selected HTML member exceeds its byte budget.')
            raw = _run([executable, 'x', '-so', '-y', '-spd', '-sccUTF-8', '-tchm', '--',
                        source, row['name']], source.parent, deadline,
                       maximum=row['bytes'], stage_limit=2 * budget.max_source_bytes + 64 * 1024**2)
            if len(raw) != row['bytes']:
                raise ConversionError('CHM extracted bytes differ from the selected member inventory.')
            return raw

        order = []
        notices = ['static_html_only_active_content_not_executed', 'printed_pagination_not_reconstructed',
                   'chm_member_order_requires_review']
        if tocs:
            raw_toc = read_member(tocs[0])
            toc = _Toc()
            # TOC navigation is deliberately a strict UTF-8 subset; no guessed
            # decoder or script/browser behavior is used to discover content.
            toc.feed(raw_toc.decode('utf-8-sig', 'strict'))
            toc.close()
            canonical = {name.casefold(): name for name in html}
            for local in toc.links:
                parsed = urlsplit(local)
                if parsed.scheme or parsed.netloc or parsed.query:
                    raise ConversionUnavailable('CHM table of contents references an external or active target.')
                name = _member(unquote(parsed.path))
                if name.casefold() not in canonical:
                    raise ConversionUnavailable('CHM table of contents names an undeclared non-HTML target.')
                name = canonical[name.casefold()]
                if name not in order:
                    order.append(name)
        remaining = sorted(set(html) - set(order), key=lambda name: (name.casefold(), name))
        if remaining:
            notices.append('unlisted_html_members_appended_in_stable_name_order')
            order.extend(remaining)
        records, characters = [], 0
        for name in order:
            _remaining(deadline)
            raw = read_member(html[name])
            reading_bytes = _chm_html(raw,
                max_chars=min(budget.max_text_bytes - characters, 10_000_000),
                deadline=deadline, notices=notices)
            if reading_bytes != raw and 'known_legacy_html_doctype_ignored_without_dtd_resolution' not in notices:
                notices.append('known_legacy_html_doctype_ignored_without_dtd_resolution')
            extracted = extract_document(reading_bytes, '.html', max_bytes=MAX_MEMBER_BYTES,
                max_chars=min(budget.max_text_bytes - characters, 10_000_000), deadline=deadline)
            for record in extracted['records']:
                characters += len(record['raw_text'])
                if len(records) >= budget.max_records or characters > budget.max_text_bytes:
                    raise ConversionError('CHM reading text exceeds its record or character budget.')
                record = dict(record)
                record.update(physical_index=None, status='review_required',
                    logical_locator={'chm_member': name, 'member_sha256': sha256(raw).hexdigest(),
                                     'parsed_member_sha256': sha256(reading_bytes).hexdigest(),
                                     'html_location': record.get('logical_locator'),
                                     'selected_components_sha256': identity})
                records.append(record)
            notices.extend(item for item in extracted['warnings'] if item not in notices)
    return 'selected-7zip-chm-static-html', '\n\n', records, notices


def legacy_records(source, data, suffix, budget, deadline, selection):
    kind = kind_for(suffix)
    if type(selection) is not dict or set(selection) != {'kind', 'runtime'} or selection['kind'] != kind:
        raise ConversionUnavailable('Legacy reading requires its explicitly selected component identity.')
    if kind == 'calibre':
        return _calibre(source, data, suffix, budget, deadline, selection['runtime'])
    if kind == 'djvu':
        return _djvu(source, data, budget, deadline, selection['runtime'])
    if kind == 'chm':
        return _chm(source, data, budget, deadline, selection['runtime'])
    raise ConversionUnavailable('Unsupported selected legacy reader.')
