"""Bounded, non-executing text previews for HTML, Markdown and RFC MIME mail.

The immutable bytes belong to the caller's retained-source store.  This module
only returns unverified logical text segments: it never writes files, resolves
URLs, runs a renderer, opens an attachment, or approves a quotation.  Markdown
is deliberately preserved as markup, not interpreted as a browser document.

Parser reuse: Python's PSF-licensed ``html.parser``, ``email.feedparser``,
``email.message`` and codecs.  Their documented callback/message interfaces are
used rather than a second HTML or MIME grammar:
https://docs.python.org/3.11/library/html.parser.html
https://docs.python.org/3.11/library/email.parser.html
https://docs.python.org/3.11/library/email.message.html

Cancellation raises InterruptedError; an elapsed absolute monotonic deadline
raises TimeoutError.  Other input errors have fixed, non-source-bearing text.
Call from the existing isolated import worker for its process-level limits.
"""
from __future__ import annotations

import base64
import binascii
import codecs
from email import errors as mail_errors, policy
from email.feedparser import BytesFeedParser
from email.message import EmailMessage
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import math
import platform
import quopri
import re
import time


TEXT_SUFFIXES = frozenset(('.html', '.htm', '.xhtml', '.md', '.markdown', '.eml'))
_CHUNK = 16 * 1024
_SEGMENT = 64 * 1024
_MAX_RECORDS = 10_000
_MAX_NODES = 200_000
_MAX_DEPTH = 128
_MAX_TOKEN = 64 * 1024
_MAX_MAIL_PARTS = 128
_MAX_MAIL_DEPTH = 24
_MAX_MAIL_HEADERS = 128
_MAX_HEADER_BYTES = 64 * 1024
_VOID = frozenset(('area base br col embed hr img input link meta param source '
                   'track wbr').split())
_BLOCKS = frozenset(('address article aside blockquote caption dd details div '
                     'dl dt fieldset figcaption figure footer form h1 h2 h3 h4 '
                     'h5 h6 header hgroup hr li main nav ol p pre section '
                     'summary table tbody td tfoot th thead tr ul').split())
_OMITTED = frozenset(('script style head template noscript iframe object embed '
                      'applet svg math canvas audio video').split())
_OPTIONAL_END = frozenset(('html body head p li dt dd thead tbody tfoot tr td th '
                          'option optgroup colgroup').split())
_HEAD_CONTENT = frozenset(('html head base basefont bgsound link meta noframes '
                          'script style template title').split())
_UNSAFE_CONTROL = re.compile('[\x00-\x08\x0b\x0e-\x1f\x7f]')
_HIDDEN_STYLE = re.compile(r'(?:^|;)\s*(?:display\s*:\s*none|'
                           r'visibility\s*:\s*(?:hidden|collapse))'
                           r'\s*(?:!important\s*)?(?:;|$)', re.I)
_CHARSET = re.compile(r'charset\s*=\s*[\"\']?([A-Za-z0-9_.:+-]{1,64})', re.I)
_CODECS = frozenset((
    'ascii', 'utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be',
    'utf-32', 'utf-32-le', 'utf-32-be', 'cp1250', 'cp1251', 'cp1252',
    'cp1253', 'cp1254', 'cp1255', 'cp1256', 'cp1257', 'cp1258', 'cp874',
    'iso8859-1', 'iso8859-2', 'iso8859-3', 'iso8859-4', 'iso8859-5',
    'iso8859-6', 'iso8859-7', 'iso8859-8', 'iso8859-9', 'iso8859-10',
    'iso8859-11', 'iso8859-13', 'iso8859-14', 'iso8859-15', 'iso8859-16',
    'koi8-r', 'koi8-u', 'shift_jis', 'euc_jp', 'euc_kr', 'gb2312', 'gbk',
    'gb18030', 'big5', 'big5hkscs', 'cp932', 'cp949', 'cp950',
))


class _Budget:
    def __init__(self, max_chars, cancelled, deadline):
        self.max_chars = max_chars
        self.cancelled = cancelled
        self.deadline = deadline
        self.characters = 0
        self.nodes = 0
        self.records = []
        self.warnings = {'logical_locator_not_page', 'layout_not_verified'}
        self.encodings = set()

    def check(self):
        if self.cancelled is not None and self.cancelled():
            raise InterruptedError('Document extraction was cancelled.')
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError('Document extraction deadline elapsed.')

    def consume(self, text):
        self.check()
        self.characters += len(text)
        if self.characters > self.max_chars:
            raise ValueError('Document text budget exceeded.')
        if _UNSAFE_CONTROL.search(text):
            raise ValueError('Document text contains unsupported control characters.')

    def node(self):
        self.check()
        self.nodes += 1
        if self.nodes > _MAX_NODES:
            raise ValueError('Document structure budget exceeded.')

    def add(self, text, locator, *, counted=False, keep_whitespace=False):
        if not counted:
            self.consume(text)
        if not keep_whitespace and not text.strip():
            return
        if len(self.records) >= _MAX_RECORDS:
            raise ValueError('Document segment budget exceeded.')
        self.records.append(dict(
            logical_locator=list(locator), raw_text=text,
            text_sha256=sha256(text.encode('utf-8')).hexdigest(),
            physical_index=None, printed_label=None,
            printed_label_verified=False))


def _codec(name):
    if not isinstance(name, str) or not 0 < len(name) <= 64:
        raise ValueError('Document character encoding is unsupported.')
    try:
        result = codecs.lookup(name).name
    except LookupError:
        raise ValueError('Document character encoding is unsupported.') from None
    # Never accept Python transformation codecs (zlib/base64/rot13/etc.).
    if result not in _CODECS:
        raise ValueError('Document character encoding is unsupported.')
    return result


def _decode(data, budget, declared=None):
    budget.check()
    bom = None
    bom_endian = None
    for prefix, encoding in ((codecs.BOM_UTF32_LE, 'utf-32'),
                             (codecs.BOM_UTF32_BE, 'utf-32'),
                             (codecs.BOM_UTF8, 'utf-8-sig'),
                             (codecs.BOM_UTF16_LE, 'utf-16'),
                             (codecs.BOM_UTF16_BE, 'utf-16')):
        if data.startswith(prefix):
            bom = encoding
            if encoding in ('utf-16', 'utf-32'):
                bom_endian = encoding + ('-le' if prefix in
                    (codecs.BOM_UTF16_LE, codecs.BOM_UTF32_LE) else '-be')
            break
    declared = _codec(declared) if declared else None
    if bom and declared:
        family = bom.replace('-sig', '')
        if declared != family and not declared.startswith(family + '-'):
            raise ValueError('Document character encoding declarations conflict.')
        if declared.endswith(('-le', '-be')) and declared != bom_endian:
            raise ValueError('Document character encoding declarations conflict.')
    encoding = bom or declared or 'utf-8'
    if not bom and encoding in ('utf-16', 'utf-32'):
        raise ValueError('Document character encoding requires a byte order mark.')
    try:
        # No replacement/ignore error policy: corrupted characters must not
        # quietly become an apparently complete extracted source.
        value = data.decode(encoding, errors='strict')
    except UnicodeError:
        raise ValueError('Document bytes do not match the character encoding.') from None
    budget.encodings.add(bom_endian or encoding)
    budget.check()
    return value


def _literal_blocks(text, budget, prefix=()):
    """Retain every character, including original line endings, in order."""
    for number, start in enumerate(range(0, len(text), _SEGMENT), 1):
        budget.add(text[start:start + _SEGMENT], (*prefix, number), keep_whitespace=True)


class _EncodingHint(HTMLParser):
    """Only inspect a bounded ASCII-compatible preamble for declared charset."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = set()

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        values = dict(attrs)
        for key, value in attrs:
            if key == 'charset' and value:
                self.values.add(_codec(value))
            elif (key == 'content' and value
                  and (values.get('http-equiv') or '').lower() == 'content-type'):
                match = _CHARSET.search(value)
                if match:
                    self.values.add(_codec(match.group(1)))

    handle_startendtag = handle_starttag


def _html_encoding(data):
    # BOM-aware decoding below takes priority. UTF-16/32 has no ASCII-compatible
    # preamble, and the actual parsed meta declaration is still checked later.
    if data.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE,
                        codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE,
                        codecs.BOM_UTF32_BE)):
        return None
    hints = _EncodingHint()
    hints.feed(data[:_MAX_TOKEN].decode('latin-1'))
    if len(hints.values) > 1:
        raise ValueError('Document character encoding declarations conflict.')
    return next(iter(hints.values), None)


class _HTMLPreview(HTMLParser):
    def __init__(self, budget, prefix=(), *, encoding=None):
        # Inspect numeric references before the stdlib's HTML replacement rule
        # could silently erase invalid characters or turn them into U+FFFD.
        super().__init__(convert_charrefs=False)
        self.budget, self.prefix, self.encoding = budget, prefix, encoding
        self.stack = []
        self.current = []
        self.current_chars = 0
        self.ordinal = 0
        self.title = []
        self.title_chars = 0
        self.has_markup = False
        budget.warnings.update(('html_text_preview', 'html_styles_not_applied',
                                'html_images_and_media_not_transcribed'))

    def _flush(self):
        if self.current:
            text = ''.join(self.current)
            self.current.clear()
            self.current_chars = 0
            if text.strip():
                self.ordinal += 1
                self.budget.add(text, (*self.prefix, self.ordinal), counted=True)

    def _append(self, data):
        self.budget.consume(data)
        for start in range(0, len(data), _SEGMENT):
            piece = data[start:start + _SEGMENT]
            if self.current_chars + len(piece) > _SEGMENT:
                self._flush()
            self.current.append(piece)
            self.current_chars += len(piece)

    def handle_starttag(self, tag, attrs):
        self.budget.node()
        self.has_markup = True
        raw = self.get_starttag_text()
        if len(raw) > _MAX_TOKEN or len(attrs) > 256 or len(tag) > 128:
            raise ValueError('HTML token budget exceeded.')
        keys = [key for key, _ in attrs]
        if len(keys) != len(set(keys)):
            raise ValueError('HTML attributes are ambiguous.')
        values = dict(attrs)
        if tag not in _HEAD_CONTENT:
            heads = [i for i, item in enumerate(self.stack) if item[0] == 'head']
            if heads:
                position = heads[-1]
                if position != len(self.stack) - 1:
                    raise ValueError('HTML implicit element closure requires review.')
                self._flush()
                del self.stack[position:]
        if tag in _BLOCKS:
            paragraphs = [i for i, item in enumerate(self.stack) if item[0] == 'p']
            if paragraphs:
                position = paragraphs[-1]
                if any(item[0] not in _OPTIONAL_END for item in self.stack[position + 1:]):
                    raise ValueError('HTML implicit element closure requires review.')
                self._flush()
                del self.stack[position:]
        if tag == 'meta':
            declared = values.get('charset')
            if (not declared and values.get('content')
                    and (values.get('http-equiv') or '').lower() == 'content-type'):
                match = _CHARSET.search(values['content'])
                declared = match.group(1) if match else None
            if declared and self.encoding:
                actual = self.encoding.replace('-sig', '')
                expected = _codec(declared)
                if (actual != expected and not actual.startswith(expected + '-')
                        and not expected.startswith(actual + '-')):
                    raise ValueError('Document character encoding declarations conflict.')
        if tag in _BLOCKS or tag in _OMITTED:
            self._flush()
        if tag == 'br':
            if not self.stack or not self.stack[-1][1]:
                self._append('\n')
            return
        if tag in ('script', 'style', 'iframe', 'object', 'embed', 'applet',
                   'template', 'noscript', 'svg', 'math', 'canvas', 'audio', 'video'):
            self.budget.warnings.add('html_active_or_nontext_content_omitted')
        if tag in ('link', 'base') or any(key.startswith('on') for key in values):
            self.budget.warnings.add('html_external_resources_and_actions_ignored')
        hidden = ('hidden' in values or (values.get('aria-hidden') or '').lower() == 'true'
                  or bool(_HIDDEN_STYLE.search(values.get('style') or '')))
        if hidden:
            self._flush()
            self.budget.warnings.add('html_hidden_content_omitted')
        if tag in _VOID:
            return
        # Common HTML optional end tags need not be written by the author.
        if self.stack and (tag == self.stack[-1][0]
                           and tag in ('p', 'li', 'dt', 'dd', 'tr', 'td', 'th', 'option')):
            self.stack.pop()
        suppressed = bool(self.stack and self.stack[-1][1]) or tag in _OMITTED or hidden
        self.stack.append((tag, suppressed))
        if len(self.stack) > _MAX_DEPTH:
            raise ValueError('HTML nesting budget exceeded.')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self.budget.node()
        if tag in _VOID:
            return
        positions = [i for i, item in enumerate(self.stack) if item[0] == tag]
        if not positions:
            raise ValueError('HTML element nesting is malformed.')
        position = positions[-1]
        if any(item[0] not in _OPTIONAL_END for item in self.stack[position + 1:]):
            raise ValueError('HTML element nesting is malformed.')
        del self.stack[position:]
        if tag in _BLOCKS or tag in _OMITTED:
            self._flush()

    def handle_data(self, data):
        self.budget.check()
        if any(tag == 'title' for tag, _ in self.stack):
            self.title_chars += len(data)
            if self.title_chars > 4096:
                raise ValueError('HTML metadata budget exceeded.')
            self.title.append(data)
            return
        if self.stack and self.stack[-1][0] == 'head' and data.strip():
            self._flush()
            self.stack.pop()
        if not self.stack or not self.stack[-1][1]:
            self._append(data)

    def handle_entityref(self, name):
        self.budget.node()
        self.handle_data(unescape('&' + name + ';'))

    def handle_charref(self, name):
        self.budget.node()
        try:
            digits = name[1:] if name[:1].lower() == 'x' else name
            base = 16 if name[:1].lower() == 'x' else 10
            if len(digits) > 64:
                raise ValueError
            value = int(digits, base)
        except ValueError:
            raise ValueError('HTML character reference is invalid.') from None
        if (value == 0 or value > 0x10ffff or 0xd800 <= value <= 0xdfff
                or 0xfdd0 <= value <= 0xfdef or value & 0xffff in (0xfffe, 0xffff)
                or value < 32 and value not in (9, 10, 12, 13)):
            raise ValueError('HTML character reference is invalid.')
        rendered = unescape('&#' + name + ';')
        if rendered != chr(value):
            self.budget.warnings.add('html_legacy_character_reference_mapped')
        self.handle_data(rendered)

    def handle_decl(self, decl):
        if decl.strip().lower() != 'doctype html':
            raise ValueError('HTML external or complex document type is unsupported.')

    def unknown_decl(self, data):
        raise ValueError('HTML declaration is unsupported.')

    def handle_pi(self, data):
        # Do not interpret PHP, template instructions, XML stylesheet PIs, etc.
        if not data.lower().startswith('xml '):
            raise ValueError('HTML processing instruction is unsupported.')
        self.budget.warnings.add('html_xml_declaration_not_rendered')

    def finish(self):
        self.close()
        if any(tag not in _OPTIONAL_END for tag, _ in self.stack):
            raise ValueError('HTML element nesting is malformed.')
        self._flush()
        title = ''.join(self.title).strip()
        if _UNSAFE_CONTROL.search(title) or len(title) > 512:
            title = ''
        return title


def _html(text, budget, prefix=(), *, encoding=None):
    parser = _HTMLPreview(budget, prefix, encoding=encoding)
    for start in range(0, len(text), _CHUNK):
        budget.check()
        parser.feed(text[start:start + _CHUNK])
        if len(parser.rawdata) > _MAX_TOKEN:
            raise ValueError('HTML token budget exceeded.')
    title = parser.finish()
    budget.check()
    return title


def _mail(data, budget):
    count = 0

    class BoundedMessage(EmailMessage):
        def __init__(self, *args, **kwargs):
            nonlocal count
            budget.check()
            count += 1
            if count > _MAX_MAIL_PARTS:
                raise ValueError('Email part budget exceeded.')
            self._carr_header_chars = 0
            self._carr_header_count = 0
            super().__init__(*args, **kwargs)

        def set_raw(self, name, value):
            budget.check()
            self._carr_header_chars += len(name) + len(value)
            self._carr_header_count += 1
            if (self._carr_header_count > _MAX_MAIL_HEADERS
                    or self._carr_header_chars > _MAX_HEADER_BYTES):
                raise ValueError('Email header budget exceeded.')
            return super().set_raw(name, value)

    try:
        parser = BytesFeedParser(_factory=BoundedMessage,
                                 policy=policy.default.clone(raise_on_defect=True))
        for start in range(0, len(data), _CHUNK):
            budget.check()
            parser.feed(data[start:start + _CHUNK])
        root = parser.close()
        budget.check()
        pending = [(root, 1)]
        while pending:
            item, depth = pending.pop()
            budget.check()
            if depth > _MAX_MAIL_DEPTH:
                raise ValueError('Email nesting budget exceeded.')
            if item.defects:
                raise ValueError('Email structure is malformed.')
            for name in ('Content-Type', 'Content-Transfer-Encoding',
                         'Content-Disposition', 'MIME-Version'):
                headers = item.get_all(name, [])
                if len(headers) > 1 or any(getattr(v, 'defects', ()) for v in headers):
                    raise ValueError('Email structural headers are ambiguous or malformed.')
            if item.is_multipart():
                pending.extend((child, depth + 1) for child in item.iter_parts())
        budget.warnings.update(('email_body_only', 'email_headers_not_authenticated'))
        _mail_parts(root, budget, ())
        metadata = {}
        subject = root.get('Subject')
        if subject is not None:
            value = str(subject).strip()
            if (not getattr(subject, 'defects', ()) and len(value) <= 512
                    and not _UNSAFE_CONTROL.search(value)):
                metadata['title'] = value
        return metadata
    except (mail_errors.MessageError, mail_errors.MessageDefect, RecursionError,
            UnicodeError, binascii.Error):
        raise ValueError('Email structure or transfer encoding is malformed.') from None


def _attachment(part):
    return part.get_content_disposition() == 'attachment' or part.get_filename() is not None


def _mail_parts(part, budget, path):
    budget.check()
    if _attachment(part):
        budget.warnings.add('email_attachments_omitted')
        return
    content_type = part.get_content_type()
    if part.get_content_maintype() == 'message':
        budget.warnings.add('email_attached_messages_omitted')
        return
    if part.is_multipart():
        children = list(part.iter_parts())
        subtype = part.get_content_subtype()
        if subtype == 'alternative':
            # A plain/HTML pair describes alternatives, not consecutive text.
            # Choose a declared plain body first and never index it twice.
            candidates = [(i, child) for i, child in enumerate(children, 1)
                          if not _attachment(child)]
            selected = next(((i, child) for i, child in candidates
                             if child.get_content_type() == 'text/plain'), None)
            if selected is None:
                selected = next(((i, child) for i, child in candidates
                                 if child.get_content_type() in
                                 ('text/html', 'multipart/related')), None)
            if selected is None:
                raise ValueError('Email has no supported alternative body.')
            budget.warnings.add('email_alternative_body_selected')
            i, child = selected
            _mail_parts(child, budget, (*path, i))
            return
        if subtype == 'related':
            start = part.get_param('start')
            if start:
                matches = [(i, child) for i, child in enumerate(children, 1)
                           if str(child.get('Content-ID', '')) == start]
                if len(matches) != 1:
                    raise ValueError('Email related root is missing or ambiguous.')
                i, child = matches[0]
            elif children:
                i, child = 1, children[0]
            else:
                raise ValueError('Email related body is empty.')
            budget.warnings.add('email_related_resources_omitted')
            _mail_parts(child, budget, (*path, i))
            return
        if subtype != 'mixed':
            raise ValueError('Email multipart subtype requires separate review.')
        for i, child in enumerate(children, 1):
            _mail_parts(child, budget, (*path, i))
        return
    if content_type not in ('text/plain', 'text/html'):
        budget.warnings.add('email_nontext_parts_omitted')
        return
    transfer = str(part.get('Content-Transfer-Encoding', '7bit')).strip().lower()
    if transfer not in ('base64', 'quoted-printable', '7bit', '8bit', 'binary'):
        raise ValueError('Email transfer encoding is unsupported.')
    if transfer in ('base64', 'quoted-printable'):
        payload = part.get_payload(decode=False)
        if not isinstance(payload, str):
            raise ValueError('Email text payload is invalid.')
        # Encoded MIME forms must be ASCII. Reject malformed bytes instead of
        # accepting the email module's legacy charset replacement behavior.
        try:
            raw = payload.encode('ascii', 'strict')
        except UnicodeError:
            raise ValueError('Email text payload encoding is invalid.') from None
    else:
        # decode=True is essential even for an unencoded payload: the public
        # decode=False path may replace bytes using the declared charset.  For
        # these three allowlisted transfer modes decode=True returns the exact
        # source bytes, after MIME boundary line-break handling, without text
        # charset decoding or an alternative codec.
        raw = part.get_payload(decode=True)
        if not isinstance(raw, bytes):
            raise ValueError('Email text payload is invalid.')
    if transfer == 'base64':
        encoded = raw.translate(None, b' \t\r\n')
        # CPython 3.11's strict decoder accepts excess padding after complete
        # quartets. Keep the supported MIME grammar consistent across runtimes;
        # the standard decoder still validates alphabet and padding placement.
        if len(encoded) % 4 or encoded.endswith(b'==='):
            raise ValueError('Email base64 payload has invalid padding.')
        raw = base64.b64decode(encoded, validate=True)
    elif transfer == 'quoted-printable':
        if re.search(br'=(?![0-9A-Fa-f]{2}|\r?\n|\r)', raw):
            raise ValueError('Email quoted-printable payload is malformed.')
        raw = quopri.decodestring(raw)
    elif transfer == '7bit':
        if not raw.isascii():
            raise ValueError('Email 7-bit payload contains non-ASCII bytes.')
    budget.check()
    charset = part.get_content_charset()
    if not charset:
        charset = 'ascii' if transfer == '7bit' else 'utf-8'
        budget.warnings.add('email_charset_default_used')
    text = _decode(raw, budget, charset)
    if content_type == 'text/html':
        _html(text, budget, path or (1,), encoding=_codec(charset))
    else:
        if str(part.get_param('format', '')).lower() == 'flowed':
            budget.warnings.add('email_flowed_text_not_rewrapped')
        _literal_blocks(text, budget, path or (1,))


def extract_text_document(data: bytes, suffix: str, *, max_bytes=64 * 1024 * 1024,
                          max_chars=10_000_000, cancelled=None, deadline=None):
    """Extract a bounded text preview, with explicit fidelity warnings.

    ``suffix`` is a canonical extension including its dot. Limits are positive
    integer budgets supplied by the enclosing job, not promised corpus limits.
    ``deadline`` is an absolute finite ``time.monotonic()`` value.
    """
    if (type(max_bytes) is not int or max_bytes <= 0
            or type(max_chars) is not int or max_chars <= 0):
        raise ValueError('Document resource budgets are invalid.')
    if (deadline is not None and (isinstance(deadline, bool)
            or not isinstance(deadline, (int, float)) or not math.isfinite(deadline))):
        raise ValueError('Document extraction deadline is invalid.')
    if cancelled is not None and not callable(cancelled):
        raise ValueError('Document cancellation callback is invalid.')
    if type(data) is not bytes or not 0 < len(data) <= max_bytes:
        raise ValueError('Document source byte budget exceeded.')
    if not isinstance(suffix, str) or suffix not in TEXT_SUFFIXES:
        raise ValueError('Document text format is unsupported.')
    budget = _Budget(max_chars, cancelled, deadline)
    budget.check()
    metadata = {}
    if suffix == '.eml':
        metadata = _mail(data, budget)
        engine = 'python.email'
    elif suffix in ('.md', '.markdown'):
        _literal_blocks(_decode(data, budget), budget)
        budget.warnings.add('markdown_markup_preserved')
        engine = 'python.codecs'
    else:
        text = _decode(data, budget, _html_encoding(data))
        title = _html(text, budget, encoding=next(iter(budget.encodings)))
        if title:
            metadata['title'] = title
        engine = 'python.html.parser'
    if not any(record['raw_text'].strip() for record in budget.records):
        raise ValueError('Document has no supported nonempty text body.')
    budget.check()
    encodings = sorted(budget.encodings)
    return dict(status='review_required', records=budget.records,
                warnings=sorted(budget.warnings), metadata=metadata,
                provenance=dict(asset_sha256=sha256(data).hexdigest(), engine=engine,
                                version=platform.python_version(), format=suffix[1:],
                                encoding=encodings[0] if len(encodings) == 1 else 'mixed',
                                encodings=encodings))
