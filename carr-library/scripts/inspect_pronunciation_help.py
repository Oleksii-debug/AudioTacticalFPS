"""Inert, bounded inspection of the author's public pronunciation help.

This research script never starts Balabolka, a help viewer, an extracted script,
or a speech engine. Proprietary help is temporary and is not uploaded as an
artifact. Only source identities, bounded topic inventory and short relevant
grammar excerpts are printed for human review.
"""
from hashlib import sha256
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
import time
import urllib.parse
import urllib.request
import zipfile

SOURCE = 'https://www.cross-plus-a.com/balabolka_portable.zip'
MAX_DOWNLOAD = 64 * 1024**2
MAX_CHM = 2 * 1024**2
MAX_TOPIC = 512 * 1024
DEADLINE = time.monotonic() + 180


def remaining():
    left = DEADLINE - time.monotonic()
    if left <= 0:
        raise TimeoutError('Pronunciation documentation inspection exceeded 180 seconds.')
    return left


def valid_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in ('www.cross-plus-a.com', 'cross-plus-a.com')
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('Documentation redirects must stay on the exact HTTPS publisher host.')
    return value


class PublisherRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return super().redirect_request(request, fp, code, message, headers, valid_url(newurl))


def capture():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublisherRedirect())
    with opener.open(valid_url(SOURCE), timeout=min(15, remaining())) as response:
        valid_url(response.geturl())
        if response.status != 200:
            raise ValueError('The publisher did not return its public help archive.')
        if response.headers.get('Content-Length') is not None:
            if not 0 < int(response.headers['Content-Length']) <= MAX_DOWNLOAD:
                raise ValueError('Public documentation archive exceeds its download allowance.')
        blocks, count = [], 0
        while True:
            remaining()
            block = response.read(min(64 * 1024, MAX_DOWNLOAD - count + 1))
            if not block:
                break
            count += len(block)
            if count > MAX_DOWNLOAD:
                raise ValueError('Public documentation archive exceeds its download allowance.')
            blocks.append(block)
        raw = b''.join(blocks)
        print(json.dumps(dict(source_url=response.geturl(), bytes=len(raw),
                              sha256=sha256(raw).hexdigest(), purpose='inert documentation inspection')), flush=True)
        return raw


def limit_child():
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_TOPIC, MAX_TOPIC))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def seven(binary, *args):
    with TemporaryFile() as output, TemporaryFile() as error:
        subprocess.run([binary, *args], stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                       check=True, timeout=min(25, remaining()), preexec_fn=limit_child)
        output.seek(0)
        result = output.read(MAX_TOPIC + 1)
        if len(result) > MAX_TOPIC:
            raise ValueError('The inert help parser exceeded its captured output allowance.')
        return result


def safe_member(name):
    # No extraction to pathname is performed, even for an accepted member.
    if (not isinstance(name, str) or not name or '\x00' in name or '\\' in name
            or name.startswith(('/', '-')) or any(ch in name for ch in ':*?[]')
            or '..' in PurePosixPath(name).parts):
        raise ValueError('Unsafe documentation member name.')
    return name


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden, self.parts = 0, []
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'li', 'tr', 'pre', 'h1', 'h2', 'h3', 'div'):
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'li', 'tr', 'pre', 'h1', 'h2', 'h3', 'div'):
            self.parts.append('\n')
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def inspect(chm, binary):
    listing = seven(binary, 'l', '-slt', '-ba', str(chm)).decode('utf-8', 'strict')
    rows, current = [], {}
    for line in listing.splitlines() + ['']:
        if not line.strip():
            if current:
                rows.append(current); current = {}
        elif ' = ' in line:
            key, value = line.split(' = ', 1)
            current[key] = value
    if not rows or len(rows) > 512:
        raise ValueError('Compiled help exceeds its bounded member count.')
    topics, total = [], 0
    for row in rows:
        if 'Path' not in row:
            continue
        name = row['Path']
        size = int(row.get('Size', '0'))
        if size < 0 or size > MAX_TOPIC:
            raise ValueError('Compiled help member exceeds its byte allowance.')
        total += size
        if total > 16 * 1024**2:
            raise ValueError('Compiled help exceeds its total uncompressed allowance.')
        if PurePosixPath(name).suffix.lower() in ('.htm', '.html'):
            topics.append(safe_member(name))
    if not topics or len(topics) > 96:
        raise ValueError('Compiled help exceeds its bounded HTML topic count.')
    print(json.dumps(dict(chm_bytes=chm.stat().st_size, chm_sha256=sha256(chm.read_bytes()).hexdigest(),
                          topic_paths=topics)), flush=True)
    matched, printed, consumed, candidates = 0, 0, 0, []
    topic_words = re.compile(r'\b(?:bxd|rex|dic)\b|pronunciation correction|dictionary format', re.I)
    grammar_words = re.compile(r'\b(?:bxd|rex|dic|syntax|wildcard|asterisk|delimiter|case.sensitive|'
                               r'precedence|whole word|regular expression|replacement|doubled|escape)\b', re.I)
    for name in sorted(topics, key=lambda item: (not bool(re.search(r'dict|pron|replace', item, re.I)), item)):
        remaining()
        raw = seven(binary, 'x', '-so', str(chm), name)
        consumed += len(raw)
        if consumed > 8 * 1024**2:
            raise ValueError('Captured HTML topics exceed their total read allowance.')
        match = re.search(br'charset\s*=\s*["\x27]?\s*([A-Za-z0-9_-]+)', raw[:4096], re.I)
        encoding = match.group(1).decode('ascii') if match else 'utf-8'
        if encoding.lower() not in ('utf-8', 'utf8', 'windows-1252', 'windows-1251', 'iso-8859-1'):
            raise ValueError('Unknown author-declared help encoding.')
        parser = Paragraphs()
        parser.feed(raw.decode(encoding, 'strict'))
        paragraphs = [' '.join(part.split()) for part in ''.join(parser.parts).splitlines()]
        whole = '\n'.join(paragraphs)
        if not topic_words.search(whole):
            continue
        excerpts = []
        for index, paragraph in enumerate(paragraphs):
            if not grammar_words.search(paragraph):
                continue
            for part in paragraphs[max(0, index-1):index+2]:
                if part and part not in excerpts:
                    excerpts.append(part[:800])
        if not excerpts:
            continue
        score = len(re.findall(r'\b(?:bxd|rex|dic)\b', whole, re.I)) * 10
        score += len(grammar_words.findall(whole))
        candidates.append((score, name, len(raw), sha256(raw).hexdigest(), excerpts))
    for _score, name, size, checksum, excerpts in sorted(candidates, reverse=True):
        budget = min(5000, 16000 - printed)
        if budget <= 0:
            break
        excerpt = '\n'.join(excerpts)[:budget]
        printed += len(excerpt); matched += 1
        print(json.dumps(dict(topic=name, bytes=size, sha256=checksum,
                              relevant_excerpt=excerpt), ensure_ascii=False), flush=True)
    if not matched:
        raise ValueError('No pronunciation format documentation was found; do not infer compatibility.')
    print(json.dumps(dict(status='INSPECTED', grammar_topics=matched, excerpt_characters=printed,
                          execution='NO_HELP_OR_APPLICATION_EXECUTION', artifact_upload='NONE')), flush=True)


def main():
    if os.name != 'posix':
        raise RuntimeError('This inert research step requires the bounded Linux parser.')
    binary = shutil.which('7zz') or shutil.which('7z')
    if not binary:
        raise RuntimeError('Install the reviewed system 7zip package for inert CHM extraction.')
    raw = capture()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 4096:
            raise ValueError('Public archive exceeds its entry allowance.')
        selected = [row for row in entries if PurePosixPath(row.filename).name.lower() == 'english.chm']
        if len(selected) != 1:
            raise ValueError('Select the single English help from the publisher archive.')
        member = selected[0]
        safe_member(member.filename)
        mode = member.external_attr >> 16
        if (member.is_dir() or stat.S_ISLNK(mode) or member.flag_bits & 1
                or not 0 < member.file_size <= MAX_CHM):
            raise ValueError('The publisher help member is not a bounded plain document.')
        with archive.open(member) as stream:
            help_bytes = stream.read(MAX_CHM + 1)
        if len(help_bytes) != member.file_size or not help_bytes.startswith(b'ITSF'):
            raise ValueError('The help capture is incomplete or not Compiled HTML Help.')
        with TemporaryDirectory(prefix='carr-pronunciation-docs-') as root:
            target = Path(root) / 'English.chm'
            target.write_bytes(help_bytes)
            inspect(target, binary)


if __name__ == '__main__':
    main()
